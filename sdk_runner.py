"""Live Claude Agent SDK runner (config.yaml: ``runner: sdk``).

Alternative to :mod:`claude_runner` (the classic ``claude -p`` subprocess per
message). Keeps ONE long-lived ``ClaudeSDKClient`` per project: the bundled CLI
process starts lazily and survives between turns, so the conversation context
lives in the session (``session_id`` + on-disk transcripts) instead of being
rebuilt with ``--resume`` on every message. Enables:

- ``can_use_tool`` -> ✅/❌ buttons in Telegram (the perm_ui callback lives in
  commands.py; this module only routes the verdict);
- ``interrupt()`` -> a real /cancel that stops the turn WITHOUT killing the
  process or losing the session;
- ``set_permission_mode()`` at runtime -> /mode and /ask apply to the next turn.

Why a separate module instead of extending claude_runner: the subprocess path
stays the default and must not grow SDK imports — ``claude_agent_sdk`` is a
~200 MB optional install, so every import here is LAZY (inside functions); the
bot imports and starts fine without the package (same convention as the
pluggable STT/TTS providers).

Hard constraints honoured (CLAUDE.md):
- The GLOBAL claude_lock (projects.State) still serializes all Claude work —
  turns run one at a time, never per-project parallelism.
- cwd = project.path, stable (sessions are scoped to cwd).
- The model is NEVER passed (glm-5.1 comes from user config) and auth/env are
  inherited: the SDK merges os.environ into the child process, so
  ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN reach the bundled CLI automatically.
- max_budget_usd is always set (the only reliable guardrail).

Client-recreation invariant: ``ensure()``/``drop()`` are the ONLY places a
client is opened/closed, and a close always happens BEFORE the next connect —
never two live claude.exe at once (the spirit of the claude_lock rule; the
registry deliberately holds at most one project's client). Any transport error
drops the client; the next turn lazily reconnects with ``resume=<session_id>``
(transcripts are on disk, and the bundled CLI 2.1.259 searches across projects).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from claude_runner import BRIDGE_SYSTEM_PROMPT, ClaudeResult
from config import Project, Settings

log = logging.getLogger(__name__)

# Read-only tools allowed WITHOUT a button in every mode (constructive
# allowed_tools: auto-approved before can_use_tool is ever consulted).
# If a smoke/live test shows buttons on other read-only tools (e.g. LS),
# EXTEND this list — do not remove the auto-allow mechanism.
READ_ONLY_ALLOW = ["Read", "Glob", "Grep", "TodoWrite"]

# Deny message shown to the MODEL when a read-only turn (/ask or strict /task)
# asks for a non-read-only tool. In plan mode (CLI >= 2.1.212) shell writes
# (Bash echo >, touch, rm) reach the callback like edits do, so the filter must
# be "everything outside READ_ONLY_ALLOW", not just Edit/Write.
_ASK_DENY = "/ask — режим только чтения; для правок — /task"
_STRICT_DENY = "strict — режим только чтения; для правок смените режим (/mode)"


@dataclass
class _TurnCtx:
    """Context the can_use_tool callback reads. One turn at a time — the global
    claude_lock serializes turns, so module-level state is safe."""
    read_only_reason: Optional[str]      # None | _ASK_DENY | _STRICT_DENY
    perm_ui: object                      # commands.py UI: ask()/cancel_all()


_turn_ctx: Optional[_TurnCtx] = None


class _LiveClient:
    """A connected ClaudeSDKClient bound to one project + options signature."""

    def __init__(self, project: Project, sig: tuple, client) -> None:
        self.project = project
        self.sig = sig        # constructive options that require recreation on change
        self.client = client


# project name -> live client. At most ONE entry at a time (see module docstring).
_clients: dict[str, _LiveClient] = {}


def _deny_sig(mode_cfg: dict, settings: Settings) -> tuple:
    """Constructive per-mode options: a change here (e.g. after /mode or a
    /config edit of deny_tools/budget) requires closing and reconnecting the
    client — disallowed_tools/max_budget_usd are connect-time options. The
    permission MODE itself is not here: it is applied per turn via
    set_permission_mode()."""
    return (tuple(mode_cfg.get("deny_tools") or []), settings.max_budget_usd)


async def _close_quiet(live: _LiveClient) -> None:
    """Disconnect bounded: a hung CLI must not wedge the bot (e.g. on /new)."""
    try:
        await asyncio.wait_for(live.client.disconnect(), timeout=20)
    except Exception as e:  # noqa: BLE001
        log.warning("sdk client disconnect failed: %s", e)


async def drop(project_name: str) -> None:
    """Close and forget the project's live client (context reset: /new,
    /project switch). Safe to call when nothing is connected."""
    live = _clients.pop(project_name, None)
    if live is not None:
        log.info("sdk: dropping live client for %s", project_name)
        await _close_quiet(live)


async def close_all() -> None:
    """Shutdown hook: disconnect everything so no bundled claude.exe outlives
    the bot (a hard kill would orphan it — stop_bot.bat sweeps those)."""
    for name in list(_clients):
        live = _clients.pop(name)
        await _close_quiet(live)


async def _ensure(project: Project, settings: Settings, mode_cfg: dict,
                  session_id: Optional[str]) -> _LiveClient:
    """Return a connected client for the project, (re)creating it if needed.

    Recreation cases: no client yet, a client of ANOTHER project is alive
    (registry keeps only the current project), or the constructive options
    signature changed (deny_tools / budget). Reconnect resumes the stored
    session, so context survives recreation."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    for other in [n for n in _clients if n != project.name]:
        # One live claude.exe invariant: close before any new connect.
        await _close_quiet(_clients.pop(other))

    sig = _deny_sig(mode_cfg, settings)
    live = _clients.get(project.name)
    if live is not None and live.sig == sig:
        return live
    if live is not None:
        await _close_quiet(_clients.pop(project.name))

    first_mode = mode_cfg.get("permission_mode", "acceptEdits") or "acceptEdits"
    options = ClaudeAgentOptions(
        cwd=project.path,
        resume=session_id or None,          # None -> fresh session
        # SDK equivalent of --append-system-prompt (no append field exists):
        # preset claude_code + append -> same bridge deployment context.
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": BRIDGE_SYSTEM_PROMPT},
        max_budget_usd=settings.max_budget_usd,   # always set — the real guardrail
        permission_mode=first_mode,               # per-turn via set_permission_mode
        allowed_tools=list(READ_ONLY_ALLOW),      # read-only runs free, no buttons
        disallowed_tools=list(mode_cfg.get("deny_tools") or []),
        can_use_tool=_can_use_tool,
        # Project/local settings would auto-approve tool calls BEFORE our
        # callback (e.g. this repo's ~7.5 KB of allow rules + acceptEdits
        # default in .claude/settings.local.json), silently killing the buttons
        # exactly in the bot's own project. "user" keeps the model/auth config
        # and cuts project allow rules (and project CLAUDE.md — accepted trade).
        setting_sources=["user"],
        # Determinism of the pinned package: the bundled CLI must not try to
        # self-update under us (the owner's channel is "latest").
        env={"DISABLE_AUTOUPDATER": "1"},
        # model intentionally NOT set: glm-5.1 comes from user config.
    )
    client = ClaudeSDKClient(options=options)
    # connect() without a prompt keeps the stream open — required for
    # can_use_tool over stdio and for interrupt()/set_permission_mode().
    await client.connect()
    live = _LiveClient(project=project, sig=sig, client=client)
    _clients[project.name] = live
    log.info("sdk: live client up (project=%s resume=%s)", project.name, bool(session_id))
    return live


async def _drain(client, timeout_s: float = 20.0):
    """Read the stream to the aborted turn's ResultMessage after interrupt().

    The SDK does NOT clear the buffer on interrupt: the cancelled turn's
    messages, including its ResultMessage (terminal_reason aborted_*), stay in
    the stream and MUST be consumed before the next query() — otherwise the
    next turn reads the dead turn's leftovers."""
    from claude_agent_sdk import ResultMessage
    try:
        async with asyncio.timeout(timeout_s):
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    return msg
    except TimeoutError:
        log.warning("sdk: drain after interrupt timed out")
    except Exception as e:  # noqa: BLE001
        log.warning("sdk: drain after interrupt failed: %s", e)
    return None


async def _interrupt_turn(client, perm_ui) -> None:
    """Deny hanging buttons + interrupt() — the shared part of /cancel and the
    timeout path. Deliberately does NOT drain: on /cancel the turn's own _drive
    iterator is still the LIVE reader of the stream; a second reader here would
    steal the aborted turn's ResultMessage, leaving _drive without a terminal
    message — the turn would hang until its wait_for timeout. As the sole
    reader, _drive consumes the aborted result itself, which also leaves the
    stream clean for the next query()."""
    try:
        await perm_ui.cancel_all()
    except Exception as e:  # noqa: BLE001
        log.warning("perm_ui.cancel_all failed: %s", e)
    try:
        await client.interrupt()
    except Exception as e:  # noqa: BLE001
        log.warning("sdk interrupt failed: %s", e)


async def _abort(client, perm_ui) -> None:
    """Timeout / task-cancelled path: wait_for already cancelled _drive, so
    nothing is consuming the stream — interrupt AND drain the aborted
    ResultMessage ourselves (it must not leak into the next query())."""
    await _interrupt_turn(client, perm_ui)
    msg = await _drain(client)
    if msg is not None:
        log.info("sdk: drained aborted turn (terminal_reason=%s)", msg.terminal_reason)
    return None


async def _ask_question(perm_ui, input_data: dict):
    """AskUserQuestion: show the model's clarifying question with option buttons.

    Simplified v1 (plan assumption а): >4 options or multiSelect cannot be
    rendered sanely as Telegram buttons -> deny with a request to continue
    without clarifying questions."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    questions = input_data.get("questions") if isinstance(input_data, dict) else None
    usable = isinstance(questions, list) and bool(questions)
    if usable:
        for q in questions:
            opts = (q or {}).get("options") or []
            if (q or {}).get("multiSelect") or len(opts) > 4:
                usable = False
                break
    if not usable:
        return PermissionResultDeny(
            message="уточняющий вопрос нельзя показать владельцу в Telegram "
                    "(нет вариантов, больше 4 или множественный выбор) — "
                    "продолжай, не задавая уточнений")
    verdict, answers = await perm_ui.ask("AskUserQuestion", input_data)
    if verdict != "allow":
        return PermissionResultDeny(message=str(answers or "владелец не ответил на уточнение"))
    updated = dict(input_data)
    updated["answers"] = answers or {}   # {question text: chosen label}
    return PermissionResultAllow(updated_input=updated)


async def _can_use_tool(tool_name: str, input_data: dict, context):  # noqa: ANN001
    """SDK permission callback (fires only when nothing auto-approved the call:
    hooks -> deny rules -> ask rules -> mode -> allow rules -> this callback)."""
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    ctx = _turn_ctx
    if ctx is None or ctx.perm_ui is None:
        return PermissionResultDeny(message="мост: нет активного хода — вызов инструмента отклонён")
    try:
        if ctx.read_only_reason and tool_name != "AskUserQuestion" \
                and tool_name not in READ_ONLY_ALLOW:
            # /ask and strict are hard read-only: deny BEFORE any button, so a
            # stray tap can never approve a write in a read-only turn.
            return PermissionResultDeny(message=ctx.read_only_reason)
        if tool_name == "AskUserQuestion":
            return await _ask_question(ctx.perm_ui, input_data)
        verdict, payload = await ctx.perm_ui.ask(tool_name, input_data)
        if verdict == "allow":
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message=str(payload or "владелец отклонил вызов инструмента"))
    except Exception as e:  # noqa: BLE001
        # A Telegram-side failure (send failed, 4096 limit on a huge Write, …)
        # must not kill the turn — deny with an explanation the model can act on.
        log.exception("perm_ui failed for %s", tool_name)
        return PermissionResultDeny(
            message=f"не удалось спросить владельца ({e}) — продолжай без этого инструмента")


async def _drive(client, prompt: str, project: Project) -> ClaudeResult:
    """One turn: query() then consume the stream up to the ResultMessage.

    No `break` out of the iterator (SDK docs: exiting early breaks cleanup);
    receive_response() itself terminates right after the ResultMessage. Other
    messages (Assistant/System) are ignored — live streaming output is Phase 2.
    Turn errors arrive via TWO paths and both are handled: an error ResultMessage
    is mapped below, while transport failures raise from the iterator and are
    caught by run_turn."""
    from claude_agent_sdk import ResultMessage

    await client.query(prompt)
    result = None
    async for msg in client.receive_response():
        if isinstance(msg, ResultMessage):
            result = msg
            # iterator ends on its own after the result — no break
    if result is None:
        raise RuntimeError("stream ended without a ResultMessage")

    subtype = result.subtype or ""
    if subtype == "error_max_budget_usd":
        # Budget semantics on a LIVE client is not per-query (see smoke notes):
        # a fresh client gets a fresh budget, so drop this one for the next turn.
        await drop(project.name)
    if subtype == "success" and not result.is_error:
        return ClaudeResult(
            ok=True,
            text=(result.result or "").strip(),
            session_id=result.session_id,
            cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
        )
    # error_* subtypes (error_max_turns / error_during_execution / …) and
    # is_error results carry the reason in .result / .errors
    detail = (result.result or "").strip() or "; ".join(result.errors or []) or subtype
    return ClaudeResult(ok=False, text=detail[:4000] or subtype, error="is_error")


async def run_turn(
    prompt: str,
    project: Project,
    mode_cfg: dict,
    settings: Settings,
    *,
    is_task: bool,
    mode_name: str = "",
    session_id: Optional[str] = None,
    perm_ui=None,
    register_cancel_hook: Optional[Callable[[Callable[[], Awaitable[None]]], None]] = None,
) -> ClaudeResult:
    """Run one turn on the project's live client. Same contract as
    claude_runner.run_claude (returns ClaudeResult); commands.py branches on
    settings.runner."""
    global _turn_ctx
    try:
        live = await _ensure(project, settings, mode_cfg, session_id)
    except Exception as e:  # noqa: BLE001
        log.exception("sdk: failed to connect a live client")
        return ClaudeResult(ok=False, text=f"Не удалось запустить SDK-сессию Claude: {e}",
                            error="is_error")

    client = live.client
    # /ask is always read-only regardless of mode; a strict /task is read-only
    # by definition (plan assumption з — shell writes must not slip through).
    read_only_reason = None
    if not is_task:
        read_only_reason = _ASK_DENY
    elif mode_name == "strict":
        read_only_reason = _STRICT_DENY
    _turn_ctx = _TurnCtx(read_only_reason=read_only_reason, perm_ui=perm_ui)

    async def _cancel_hook() -> None:
        # /cancel: interrupt-only (see _interrupt_turn) — _drive stays the live
        # reader, consumes the aborted ResultMessage and finishes the turn by
        # itself; commands.py's `cancelled` flag renders the user-facing reply.
        await _interrupt_turn(client, perm_ui)

    if register_cancel_hook is not None:
        try:
            register_cancel_hook(_cancel_hook)
        except Exception:  # noqa: BLE001
            log.debug("register_cancel_hook callback raised", exc_info=True)

    turn_mode = mode_cfg.get("permission_mode", "acceptEdits") if is_task else "plan"
    timeout = settings.timeout_minutes * 60
    try:
        # /ask and /mode apply to THIS turn — set on the fly before the query.
        await client.set_permission_mode(turn_mode)
        return await asyncio.wait_for(_drive(client, prompt, project), timeout=timeout)
    except asyncio.TimeoutError:
        # Auto-deny buttons, interrupt, drain; keep the client alive.
        await _abort(client, perm_ui)
        return ClaudeResult(ok=False, text="⏱ Превышено время ожидания (timeout).",
                            timed_out=True, error="timeout")
    except asyncio.CancelledError:
        # The turn task itself was cancelled (bot shutdown): abort cleanly, re-raise.
        await _abort(client, perm_ui)
        raise
    except Exception as e:  # noqa: BLE001 — ProcessError/ResultError/transport failures
        # The transport is now unreliable: drop the client; the next turn lazily
        # reconnects with resume (context lives in on-disk transcripts).
        # Buttons first: a transport failure can strike while a ✅/❌ prompt is
        # still hanging — without cancel_all they stay tappable forever, and a
        # late tap would resolve a future of a turn that no longer exists.
        if perm_ui is not None:
            try:
                await perm_ui.cancel_all()
            except Exception as ui_err:  # noqa: BLE001 — cleanup must not mask the error
                log.warning("perm_ui.cancel_all after a failed turn: %s", ui_err)
        await drop(project.name)
        log.exception("sdk: turn failed with a transport/process error")
        return ClaudeResult(ok=False, text=f"SDK-сессия Claude прервалась: {e}", error="is_error")
    finally:
        _turn_ctx = None


# ---- smoke test (no Telegram) ------------------------------------------------
if __name__ == "__main__":
    import os
    import sys
    import time

    from config import load

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    class ConsolePermUI:
        """Stand-in for the Telegram perm UI: auto allow/deny, counts prompts."""

        def __init__(self, auto: str = "allow", explode: bool = False) -> None:
            self.auto = auto
            self.explode = explode
            self.counts: dict[str, int] = {}

        async def ask(self, tool_name: str, input_data: dict):
            if self.explode:
                raise RuntimeError("simulated Telegram failure (boom)")
            self.counts[tool_name] = self.counts.get(tool_name, 0) + 1
            keys = ("command", "file_path", "path", "pattern", "url", "content")
            preview = {k: str(input_data.get(k))[:200] for k in keys if input_data.get(k)}
            print(f"  [perm] {tool_name} {preview}")
            if self.auto == "allow":
                return ("allow", None)
            return ("deny", "smoke auto-deny")

        async def cancel_all(self) -> None:
            print("  [perm] cancel_all")

    async def _main():
        s = load()
        proj = s.projects[0]
        manual = {"permission_mode": "default", "deny_tools": []}
        scenario = os.environ.get("SMOKE", "ask")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

        async def show(tag: str, r: ClaudeResult) -> None:
            print(f"{tag}: ok={r.ok} turns={r.num_turns} cost={r.cost_usd} "
                  f"session={(r.session_id or '')[:8]} err={r.error}")
            print(f"{tag} text: {(r.text or '')[:300]}")

        try:
            if scenario == "ask":
                ui = ConsolePermUI(auto="allow")
                r = await run_turn("Reply with exactly: SDK_BRIDGE_OK", proj, manual, s,
                                   is_task=False, mode_name="manual", perm_ui=ui)
                await show("ask", r)
                print("perm prompts seen (should be read-only only):", ui.counts)

            elif scenario == "task":
                # Two turns on one live client: proves session continuity and
                # shows whether max_budget_usd resets between turns (risk 2).
                ui = ConsolePermUI(auto="allow")
                r1 = await run_turn("Briefly describe what you see in the current directory. "
                                    "One short paragraph.", proj, manual, s,
                                    is_task=True, mode_name="manual", perm_ui=ui)
                await show("task#1", r1)
                r2 = await run_turn("In one sentence: how many files did you just mention?",
                                    proj, manual, s, is_task=True, mode_name="manual",
                                    session_id=r1.session_id, perm_ui=ui)
                await show("task#2", r2)
                print("perm prompts seen (manual mode; Read/Glob/Grep must NOT appear):",
                      ui.counts)
                print("budget check: cost1=", r1.cost_usd, "cost2=", r2.cost_usd,
                      "-> per-query" if (r2.cost_usd or 0) <= max(r1.cost_usd or 0, 1e-9)
                      else "-> cumulative?")

            elif scenario == "deny":
                ui = ConsolePermUI(auto="deny")
                r = await run_turn("Create a file smoke_deny_test.txt with the word hi, "
                                   "then tell me whether you managed to create it.",
                                   proj, manual, s, is_task=True, mode_name="manual", perm_ui=ui)
                await show("deny", r)
                print("perm prompts denied:", ui.counts)

            elif scenario == "interrupt":
                ui = ConsolePermUI(auto="allow")
                holder: dict = {}

                def reg(hook) -> None:
                    holder["hook"] = hook

                t = asyncio.create_task(run_turn(
                    "Count slowly from 1 to 30, one number per line, then say DONE.",
                    proj, manual, s, is_task=True, mode_name="manual", perm_ui=ui,
                    register_cancel_hook=reg))
                await asyncio.sleep(6)
                print("  [interrupt] firing cancel hook…")
                t0 = time.monotonic()
                await holder["hook"]()
                r = await t
                # Must return promptly: the hook interrupts WITHOUT draining, so
                # the turn's own stream reader consumes the aborted ResultMessage.
                # A hang here until settings.timeout_minutes means the drain race.
                print(f"  [interrupt] turn finished {time.monotonic() - t0:.1f}s after the hook")
                await show("interrupt", r)
                # The same client must survive: a follow-up turn on it.
                r2 = await run_turn("Reply with exactly: STILL_ALIVE", proj, manual, s,
                                    is_task=True, mode_name="manual", perm_ui=ui)
                await show("interrupt#2", r2)

            elif scenario == "badresume":
                r = await run_turn("say hi", proj, manual, s, is_task=False,
                                   mode_name="manual",
                                   session_id="00000000-0000-0000-0000-000000000000",
                                   perm_ui=ConsolePermUI())
                await show("badresume", r)

            elif scenario == "errpaths":
                # Path 1: error delivered as a ResultMessage (bad resume id).
                r = await run_turn("say hi", proj, manual, s, is_task=False,
                                   mode_name="manual",
                                   session_id="00000000-0000-0000-0000-000000000000",
                                   perm_ui=ConsolePermUI())
                await show("errpaths#badresume", r)
                # Path 2: perm_ui raising must yield a deny, not a crashed turn.
                ui = ConsolePermUI(explode=True)
                r2 = await run_turn("Create a file errpaths_test.txt, then report.",
                                    proj, manual, s, is_task=True, mode_name="manual",
                                    perm_ui=ui)
                await show("errpaths#permboom", r2)
        finally:
            await close_all()
            print("clients closed.")

    asyncio.run(_main())
