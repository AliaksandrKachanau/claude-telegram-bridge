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


def main() -> None:
    _setup_logging()
    log = logging.getLogger("bot")

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
        return (
            "🤖 Бот запущен.\n"
            f"📂 Проект: {proj}\n"
            f"⚙️ Режим: {settings.default_mode}\n"
            f"⏸ Пауза: {'да (до /resume)' if start_paused else 'нет'}\n"
            "\n"
            "Команды: /ask /task /new /diff /git /project /mode /cancel /pause "
            "/resume /note /confirm /draft /reply_voice /status /config\n"
            "/help — подробная справка"
        )

    _health_tasks: set = set()  # keep refs so background health tasks aren't GC'd

    async def _post_init(application) -> None:
        # First thing the owner sees after a (re)start: status + command cheatsheet.
        # Sent IMMEDIATELY. Do NOT await health.check_all() here — it probes 4 servers
        # (Telegram/Claude/Groq/Edge) in parallel but blocks on the slowest, which on a
        # slow link delays the "bot is up" notice AND the start of polling by up to ~5s.
        # Connectivity is sent as a SECOND message from a background task, and is always
        # available in /status. Pending updates are dropped below (drop_pending_updates),
        # so queued messages that piled up while the bot was down are NOT replayed.
        text = _startup_text()
        for uid in settings.allowed_user_ids:
            try:
                await application.bot.send_message(chat_id=uid, text=text)
            except Exception as e:  # noqa: BLE001
                log.warning("startup notify to %s failed: %s", uid, e)
        # Health in the background: never delays the startup notice or command readiness.
        t = asyncio.create_task(_send_health(application, settings))
        _health_tasks.add(t)
        t.add_done_callback(_health_tasks.discard)

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
