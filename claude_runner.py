"""Synchronous wrapper around the Claude Code CLI (`claude -p`), run off the event loop.

The single public entry point is :func:`run_claude` (async). It builds the argv,
spawns a non-interactive Claude process via :mod:`subprocess`, feeds the prompt on
stdin, parses the JSON result, and returns a :class:`ClaudeResult`. The blocking
Popen call runs in a thread executor so the asyncio bot stays responsive.

Why subprocess.Popen and not asyncio.create_subprocess_exec: on Windows the Proactor
event loop rejects ``encoding=`` for subprocesses, so we use the stdlib sync API
(which honours ``text=True, encoding="utf-8"``) wrapped in run_in_executor.

Key correctness points (verified against Claude Code 2.1.183):
- argv is built as a list (no shell), prompt goes via stdin -> avoids quoting/length
  issues with Cyrillic and long prompts.
- cwd is the project path and must stay stable: Claude scopes session lookup to cwd.
- A bad/unknown --resume id prints plain text with exit code 0, so JSON parsing is
  defensive and falls back to surfacing the raw text rather than crashing.
- `--max-turns` is NOT enforced by this CLI version; `--max-budget-usd` is the only
  reliable guardrail and is always passed.
- A fixed `--append-system-prompt` tells the headless session it runs behind the
  Telegram bridge: without it the model has no marker of its own deployment and
  confabulates ("I'm an interactive window, not in Telegram") or says "shown
  above", which never reaches the user.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Union

from config import Project, Settings

log = logging.getLogger(__name__)

# A process we may need to kill on /cancel. Both Popen (sync) expose .pid/.kill().
ProcLike = Union["subprocess.Popen"]

# Injected via --append-system-prompt so the headless session knows its own
# deployment context. The user prompt arrives on stdin with no metadata, so
# without this the model cannot tell it is talking through Telegram.
BRIDGE_SYSTEM_PROMPT = (
    "Ты работаешь в headless-режиме (claude -p) за Telegram-мостом на Windows: "
    "пользователь пишет боту в Telegram, бот передаёт текст тебе и пересылает "
    "обратно ТОЛЬКО финальный текст твоего ответа. "
    "Никакого интерактивного окна, UI, «показано выше» и preview не существует: "
    "пользователь не видит твои рассуждения, вызовы инструментов и план-режим. "
    "Всё, что пользователь должен увидеть, включай в финальный текст ответа. "
    "Созданные тобой файлы пользователь сам не увидит — если их содержимое важно, "
    "приведи его (или ключевые части) в тексте ответа. "
    "Отвечай кратко, на языке пользователя."
)


@dataclass
class ClaudeResult:
    ok: bool
    text: str
    session_id: Optional[str] = None
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    timed_out: bool = False
    error: Optional[str] = None  # "timeout" | "non-json" | "is_error" | None


def _build_argv(
    mode_cfg: dict,
    settings: Settings,
    *,
    is_task: bool,
    session_id: Optional[str],
    new_session: bool,
) -> list[str]:
    """Construct the claude.exe argv (prompt is fed via stdin, not argv)."""
    argv: list[str] = [settings.claude_exe, "-p", "--output-format", "json"]

    if new_session:
        argv += ["--session-id", str(uuid.uuid4())]
    elif session_id:
        argv += ["--resume", session_id]

    # /ask is always read-only, regardless of the current /mode.
    if is_task:
        pm = mode_cfg.get("permission_mode", "acceptEdits")
        deny = list(mode_cfg.get("deny_tools", []))
    else:
        pm = "plan"
        deny = ["Edit", "Write"]  # belt-and-suspenders on top of plan mode

    if pm == "bypassPermissions":
        argv.append("--dangerously-skip-permissions")
    else:
        argv += ["--permission-mode", pm]
        for d in deny:
            argv += ["--disallowedTools", d]

    argv += ["--max-budget-usd", str(settings.max_budget_usd)]
    if settings.max_turns:
        argv += ["--max-turns", str(settings.max_turns)]  # best-effort, not guaranteed

    # Deployment context: fixed short string, safe on argv (unlike the user prompt).
    argv += ["--append-system-prompt", BRIDGE_SYSTEM_PROMPT]
    return argv


def _kill_proc_tree(proc: ProcLike) -> None:
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("taskkill failed: %s", e)
    try:
        proc.kill()
    except Exception as e:  # noqa: BLE001
        log.warning("proc.kill() failed: %s", e)


def _run_sync(
    argv: list[str],
    cwd: str,
    prompt: str,
    settings: Settings,
    register_proc: Optional[Callable[[ProcLike], None]],
) -> ClaudeResult:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except FileNotFoundError:
        log.error("claude executable not found: %s", settings.claude_exe)
        return ClaudeResult(ok=False, text="claude.exe не найден", error="non-json")
    except Exception as e:  # noqa: BLE001
        log.exception("failed to spawn claude")
        return ClaudeResult(ok=False, text=f"Не удалось запустить claude: {e}", error="non-json")

    if register_proc is not None:
        try:
            register_proc(proc)
        except Exception:  # noqa: BLE001
            log.debug("register_proc callback raised", exc_info=True)

    timeout = settings.timeout_minutes * 60
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_proc_tree(proc)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return ClaudeResult(ok=False, text="⏱ Превышено время ожидания (timeout).", timed_out=True, error="timeout")

    text_out = (stdout or "").strip()
    err_out = (stderr or "").strip()

    try:
        data = json.loads(text_out)
    except json.JSONDecodeError:
        raw = text_out or err_out or "Нет вывода от claude."
        return ClaudeResult(ok=False, text=raw[:4000], error="non-json")

    if data.get("is_error"):
        return ClaudeResult(ok=False, text=str(data.get("result", "") or err_out)[:4000], error="is_error")

    cost = data.get("total_cost_usd")
    turns = data.get("num_turns")
    return ClaudeResult(
        ok=True,
        text=str(data.get("result", "")).strip(),
        session_id=data.get("session_id"),
        cost_usd=float(cost) if cost is not None else None,
        num_turns=int(turns) if turns is not None else None,
    )


async def run_claude(
    prompt: str,
    project: Project,
    mode_cfg: dict,
    settings: Settings,
    *,
    is_task: bool,
    session_id: Optional[str] = None,
    new_session: bool = False,
    register_proc: Optional[Callable[[ProcLike], None]] = None,
) -> ClaudeResult:
    """Run `claude -p` once (in a worker thread) and return the parsed result."""
    argv = _build_argv(
        mode_cfg, settings,
        is_task=is_task, session_id=session_id, new_session=new_session,
    )
    log.info("claude run: cwd=%s is_task=%s resume=%s new=%s", project.path, is_task, bool(session_id), new_session)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_sync, argv, project.path, prompt, settings, register_proc
    )


# ---- smoke test -------------------------------------------------------------
if __name__ == "__main__":
    import os
    from config import load

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _s = load()
    _proj = _s.projects[0]
    _mode = _s.modes[_s.default_mode]
    _scenario = os.environ.get("SMOKE", "ask")

    async def _main():
        if _scenario == "ask":
            r = await run_claude(
                "Reply with exactly: BRIDGE_OK", _proj, _mode, _s,
                is_task=False, new_session=True,
            )
        elif _scenario == "badresume":
            r = await run_claude(
                "say hi", _proj, _mode, _s, is_task=False,
                session_id="00000000-0000-0000-0000-000000000000",
            )
        else:
            r = await run_claude(
                "Briefly describe what you see in the current directory.",
                _proj, _mode, _s, is_task=True, new_session=True,
            )
        # safe print that survives the Windows cp1252 console
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print("RESULT:", r)

    asyncio.run(_main())
