"""Entry point: build the python-telegram-bot Application and run it.

Run manually:
    python -u bot.py
or via run_bot.bat.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, ch])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


_MUTEX_HANDLE: list = [None]  # keep the single-instance mutex handle open for the process lifetime


def _acquire_single_instance() -> bool:
    """Ensure only one bot instance runs on this machine. Returns True to proceed.

    Uses a named Windows mutex in the Local namespace (the bot runs in the owner's
    interactive session, so Local needs no extra privilege). The OS releases it when
    the process exits — even on a hard crash or `Stop-Process -Force` — so there is
    no stale lockfile to clean up. A second launch refuses to start, which prevents
    two pollers on one Telegram token (409 Conflict) and concurrent `claude -p`
    corrupting ~/.claude.json.
    """
    if sys.platform != "win32":
        return True  # guard is Windows-only; no-op elsewhere (the bot is Windows-only anyway)
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, r"Local\ClaudeTelegramBot")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS -> another instance owns it
        kernel32.CloseHandle(handle)  # we didn't acquire it; release our duplicate handle
        return False
    _MUTEX_HANDLE[0] = handle  # keep the handle open until the process exits
    return True


def main() -> None:
    _setup_logging()
    log = logging.getLogger("bot")

    if not _acquire_single_instance():
        msg = ("Бот уже запущен — второй экземпляр не стартует, чтобы избежать "
               "конфликта Telegram (409) и порчи claude.json. "
               "Сначала stop_bot.bat, затем запуск.")
        log.error(msg)  # goes to bot.log + console (StreamHandler) — one line, no separate print
        sys.exit(1)

    from telegram.error import Conflict, NetworkError, TimedOut
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    import commands as C
    from config import load
    import health as H
    from projects import State
    from security import authorized

    settings = load()
    # Autostart (Task Scheduler) launches the bot via run_autostart.vbs which sets
    # BOT_START_PAUSED=1, so an auto-started bot comes up PAUSED. A manual run_bot.bat
    # leaves this unset -> starts active (handy for testing).
    start_paused = os.environ.get("BOT_START_PAUSED", "").strip().lower() in ("1", "true", "yes", "on")
    state = State(settings, start_paused=start_paused)
    C.init(settings, state)

    async def _on_error(update, context) -> None:
        err = context.error
        # Transient network blips are expected on a weak connection -> concise log only.
        if isinstance(err, (NetworkError, TimedOut)):
            log.warning("transient network error (will auto-retry): %s", err)
            return
        if isinstance(err, Conflict):
            log.warning("Conflict — another bot instance polling? %s", err)
            return
        log.exception("unhandled error (update=%s): %s", update, err)

    def _startup_text() -> str:
        proj = settings.projects[0].name if settings.projects else "—"
        # Lead with the paused state when auto-started: the owner must know the bot
        # came up PAUSED and needs /resume, otherwise Claude silently does nothing.
        header = (
            "⏸ Бот запущен на паузе.\nОтправь /resume, чтобы включить Claude."
            if start_paused
            else "🤖 Бот запущен."
        )
        return (
            header + "\n"
            f"📂 Проект: {proj}\n"
            f"⚙️ Режим: {settings.default_mode}\n"
            "\n"
            "Команды: /ask /task /new /diff /git /project /mode /cancel /pause "
            "/resume /note /confirm /draft /reply_voice /status /config\n"
            "/help — подробная справка"
        )

    _bg_tasks: set = set()  # keep refs so background tasks aren't GC'd mid-flight

    async def _notify_startup(application, text) -> None:
        # Deliver the startup notice WITH RETRIES. At logon the network is often not up
        # yet, and a single fire-and-forget send would be lost — so the owner wouldn't
        # learn the bot came up (especially bad when it auto-starts PAUSED). Each user
        # gets the message exactly once: only still-failing users are retried, up to
        # ~5 min, then we give up. Runs as a background task so polling starts at once.
        pending = set(settings.allowed_user_ids)
        for attempt in range(30):
            if not pending:
                return
            failed = set()
            for uid in pending:
                try:
                    await application.bot.send_message(chat_id=uid, text=text)
                except Exception as e:  # noqa: BLE001
                    failed.add(uid)
                    log.warning("startup notify to %s failed (attempt %d): %s", uid, attempt + 1, e)
            pending = failed
            if pending:
                await asyncio.sleep(10)
        log.warning("startup notify gave up for %s after 30 attempts", pending)

    async def _post_init(application) -> None:
        # First thing the owner sees after a (re)start: status + command cheatsheet.
        # Sent IMMEDIATELY via a RETRYING background task (network may be down at logon):
        # do NOT block on health here — it probes 4 servers (Telegram/Claude/Groq/Edge)
        # and blocks on the slowest. Connectivity is sent as a SECOND background message,
        # and is always available in /status. Pending updates are dropped below
        # (drop_pending_updates), so queued messages that piled up while the bot was
        # down are NOT replayed.
        text = _startup_text()
        t1 = asyncio.create_task(_notify_startup(application, text))
        _bg_tasks.add(t1)
        t1.add_done_callback(_bg_tasks.discard)
        # Health in the background: never delays the startup notice or command readiness.
        t2 = asyncio.create_task(_send_health(application, settings))
        _bg_tasks.add(t2)
        t2.add_done_callback(_bg_tasks.discard)

    async def _send_health(application, settings) -> None:
        try:
            srv_lines = H.render(await H.check_all(settings))
        except Exception as e:  # noqa: BLE001
            log.warning("health check failed: %s", e)
            return
        for uid in settings.allowed_user_ids:
            try:
                await application.bot.send_message(chat_id=uid, text="\n".join(srv_lines))
            except Exception as e:  # noqa: BLE001
                log.warning("health notify to %s failed: %s", uid, e)

    auth = authorized(settings)
    app = Application.builder().token(settings.token).post_init(_post_init).build()
    app.add_error_handler(_on_error)

    app.add_handler(CommandHandler("start", auth(C.cmd_start)))
    app.add_handler(CommandHandler("help", auth(C.cmd_help)))
    app.add_handler(CommandHandler("ask", auth(C.cmd_ask)))
    app.add_handler(CommandHandler("task", auth(C.cmd_task)))
    app.add_handler(CommandHandler("new", auth(C.cmd_new)))
    app.add_handler(CommandHandler("diff", auth(C.cmd_diff)))
    app.add_handler(CommandHandler("git", auth(C.cmd_git)))
    app.add_handler(CommandHandler("project", auth(C.cmd_project)))
    app.add_handler(CommandHandler("mode", auth(C.cmd_mode)))
    app.add_handler(CommandHandler("cancel", auth(C.cmd_cancel)))
    app.add_handler(CommandHandler("status", auth(C.cmd_status)))
    app.add_handler(CommandHandler("speak", auth(C.cmd_speak)))
    app.add_handler(CommandHandler("voice", auth(C.cmd_voice_mode)))
    app.add_handler(CommandHandler("confirm", auth(C.cmd_confirm)))
    app.add_handler(CommandHandler("draft", auth(C.cmd_draft)))
    app.add_handler(CommandHandler("reply_voice", auth(C.cmd_reply_voice)))
    app.add_handler(CommandHandler("note", auth(C.cmd_note)))
    app.add_handler(CommandHandler("pause", auth(C.cmd_pause)))
    app.add_handler(CommandHandler("resume", auth(C.cmd_resume)))
    app.add_handler(CommandHandler("config", auth(C.cmd_config)))
    # Inline buttons for /note browse (callback_data starts with "nb:").
    app.add_handler(CallbackQueryHandler(auth(C.note_callback), pattern=r"^nb:"))
    # Inline buttons for paginated answers (callback_data starts with "pg:").
    app.add_handler(CallbackQueryHandler(auth(C.page_callback), pattern=r"^pg:"))
    # Inline buttons for voice-triggered /mode full confirm (callback_data "mc:full"|"mc:no").
    app.add_handler(CallbackQueryHandler(auth(C.mode_callback), pattern=r"^mc:"))
    # Inline buttons for voice-confirm gate (callback_data "vc:send|edit|cancel").
    app.add_handler(CallbackQueryHandler(auth(C.voice_confirm_callback), pattern=r"^vc:"))
    # Inline buttons for voice draft (callback_data "dr:send|clear").
    app.add_handler(CallbackQueryHandler(auth(C.draft_callback), pattern=r"^dr:"))
    # Inline buttons for /config editing (callback_data starts with "cf:").
    app.add_handler(CallbackQueryHandler(auth(C.config_callback), pattern=r"^cf:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auth(C.cmd_freetext)))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, auth(C.cmd_voice)))

    log.info("Bot starting. Projects: %s. Default mode: %s. Allowed users: %s. Start paused: %s",
             [p.name for p in settings.projects], settings.default_mode, settings.allowed_user_ids, start_paused)
    print("Bot is running. Press Ctrl+C to stop.", flush=True)

    # Allowed update types:
    # - "message":        text / voice / commands
    # - "callback_query": inline-button taps (/note browse). WITHOUT this, Telegram
    #                      never delivers button presses -> buttons silently do nothing.
    # "edited_message" is intentionally EXCLUDED — editing an old command must not
    # re-fire it (unsafe for /task) and update.message is None for edits, which used
    # to crash handlers.
    #
    # drop_pending_updates=True: anything sent while the bot was DOWN (queued on
    # Telegram's side) is discarded on boot, not replayed. Otherwise every queued
    # /task/voice would fire Claude one after another -> "typing" storm + a cascade
    # of errors (e.g. ConnectionRefused when the Claude API is unreachable).
    try:
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    except KeyboardInterrupt:
        # On Windows, Ctrl+C can land inside the Proactor loop's IOCP poll and
        # interrupt run_polling before its graceful shutdown finishes -> that
        # prints a noisy KeyboardInterrupt traceback. Catch it and exit quietly.
        log.info("stopped by Ctrl+C")


if __name__ == "__main__":
    main()
