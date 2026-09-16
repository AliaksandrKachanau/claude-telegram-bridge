r"""Entry point: the second Telegram bot — MT5 terminal monitor (phase 1, read-only).

Separate process, separate token (MT5_BOT_TOKEN), separate log. Shares only the
lightweight helpers from the repo root (security.py allowlist, messages.py
chunking) via a sys.path shim. Never imports claude_runner/sdk_runner/projects:
no claude.exe, no locks, no ~/.claude.json — the Claude bot is not touched.

Run:  mt5bot\run_mt5bot.bat   (or:  python -u mt5bot\mt5_bot.py from the root)
Smoke (no Telegram, token not needed):  SMOKE=attach python mt5bot\mt5_bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # security/messages live at the repo root

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_MUTEX_HANDLE: list = [None]  # keep the single-instance mutex handle open


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(LOG_DIR / "mt5bot.log", maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
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


def _acquire_single_instance() -> bool:
    """One MT5 bot per machine (named mutex; same pattern as the Claude bot)."""
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, r"Local\MT5TelegramBotMonitor")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE[0] = handle
    return True


def _token_guard_error() -> str | None:
    """Refuse to start on a missing/duplicate token (plan: token protection)."""
    mt5_tok = os.environ.get("MT5_BOT_TOKEN", "").strip()
    claude_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not mt5_tok:
        return ("MT5_BOT_TOKEN не задан в .env — создай ВТОРОГО бота у @BotFather "
                "и добавь его токен. Первый бот продолжит работать.")
    if mt5_tok == claude_tok:
        return ("MT5_BOT_TOKEN совпадает с TELEGRAM_BOT_TOKEN — нужен ОТДЕЛЬНЫЙ токен "
                "(второй бот у @BotFather), иначе два поллера будут драться за один "
                "токен (409).")
    return None


# --- Cross-machine single-instance guard ("incumbent stays") -----------------
# Ported from bot.py (proven there): 409 Conflict is symmetric, so "exit on 409"
# would kill BOTH pollers. The instance that has polled cleanly (incumbent)
# ignores 409s; one that has seen ONLY contiguous 409s since boot defers+exits.
_CONFLICT_GAP = 10.0
_QUIET = 12.0
_INCUMBENCY_POLL = 5.0
_YIELD_GRACE_MIN = 20.0
_YIELD_GRACE_MAX = 50.0


@dataclass
class _ConflictState:
    last_conflict_at: float | None = None
    last_net_error_at: float | None = None
    incumbent: bool = False
    run_start: float | None = None
    last_conflict_seen: float | None = None
    yielding: bool = False
    yield_grace: float = 0.0


_HELP = ("🤖 MT5-бот (фаза 1 — наблюдение + действия над элементами):\n"
         "Кнопки внизу чата (📊 ⏳ 📋) — всегда доступны; вернут /start-ом.\n"
         "/mt5 — пульт с обновлением на месте\n"
         "/mt5status — терминал, счёт, margin\n"
         "/mt5orders — pending-ордера\n"
         "/mt5positions — позиции с P&L\n"
         "Тап по позиции/ордеру в списке — карточка с действиями: закрыть по "
         "рынку / удалить pending (с подтверждением ✅). Торговые действия "
         "работают при mt5.allow_trading: true в config.yaml.\n"
         "/mt5 <status|orders|positions> — то же, для набора руками\n"
         "/help — эта справка\n"
         "\nУведомления: выставление pending, fill, закрытие (TP/SL/EXPERT + "
         "профит), margin level. Отмены pending не шлются (шумно).")


async def _smoke_attach() -> int:
    """SMOKE=attach: exercise the monitor core against the live terminal, no Telegram."""
    import mt5_monitor as M
    cfg = M.load_settings()

    async def _print(text: str) -> None:
        print(f"[notify] {text}")

    mon = M.Monitor(cfg, _print)
    ok = await asyncio.to_thread(mon._attach)
    print(f"attach: {ok}")
    if not ok:
        await mon.stop()
        return 1
    snap = await asyncio.to_thread(mon._snapshot)
    mon._baseline(snap)
    print(mon._summary(snap))
    print("--- status ---")
    print(await asyncio.to_thread(mon.status_text))
    print("--- orders ---")
    print(await asyncio.to_thread(mon.orders_text))
    print("--- positions ---")
    print(await asyncio.to_thread(mon.positions_text))
    # second snapshot -> diff must be quiet right after a baseline
    snap2 = await asyncio.to_thread(mon._snapshot)
    print(f"diff after baseline (expect []): {mon._diff(snap2)}")
    await mon.stop()
    return 0


def main() -> None:
    _setup_logging()
    log = logging.getLogger("mt5bot")

    if os.environ.get("SMOKE", "").lower() == "attach":
        raise SystemExit(asyncio.run(_smoke_attach()))

    import mt5_monitor as M
    from messages import chunk_text
    from security import authorized

    cfg = M.load_settings()  # also loads .env
    guard = _token_guard_error()
    if guard:
        log.error("%s", guard)
        raise SystemExit(1)
    if not cfg.allowed_user_ids:
        # Same refusal as the Claude bot: an empty allowlist would silently
        # reject every update AND every notification recipient.
        log.error("ALLOWED_USER_IDS пуст в .env — бот молча отвергал бы все апдейты. "
                  "Добавь chat_id (как у первого бота).")
        raise SystemExit(1)
    if not _acquire_single_instance():
        log.error("MT5 bot is already running on this machine — second instance "
                  "refuses to start (409/token protection).")
        raise SystemExit(1)
    if not M.MT5_AVAILABLE:
        log.error("Package MetaTrader5 not installed: .venv\\Scripts\\pip install MetaTrader5")
        raise SystemExit(1)

    from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                          KeyboardButton, ReplyKeyboardMarkup)
    from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, TimedOut
    from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                              MessageHandler, filters)

    # Two button UIs, two jobs:
    # 1) REPLY keyboard (persistent, bottom of the chat, visible by DEFAULT —
    #    no command needed first). A tap just sends the button's text.
    # 2) INLINE panel: bare /mt5 shows it; a tap re-renders the SAME message
    #    (edit_message_text) with fresh data — the "refresh in place" mode.
    # Telegram keyboards have NO color API (buttons follow the chat theme), so
    # the separation is LAYOUT: one full-width button per row — three distinct
    # strips instead of three cells squeezed side by side on a narrow screen.
    MENU_BTNS = {"📊 Статус": "status", "⏳ Ордера": "orders", "📋 Позиции": "positions"}
    m5_rk = ReplyKeyboardMarkup(
        [[KeyboardButton(t)] for t in MENU_BTNS],  # one per row = full width
        resize_keyboard=True, is_persistent=True)
    m5_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"m5:{sub}")]  # one per row too
         for t, sub in MENU_BTNS.items()])

    cs = _ConflictState(yield_grace=random.uniform(_YIELD_GRACE_MIN, _YIELD_GRACE_MAX))
    _bg_tasks: set = set()

    # Trade events must survive a failed send (weak internet is the norm
    # here): the monitor only ENQUEUES; a pump task delivers with retries and
    # backoff. A dropped send would lose the event forever — deal stamps and
    # margin latches advance at DETECTION time, not at delivery time.
    _notify_q: asyncio.Queue[str] = asyncio.Queue()

    async def _notify(text: str) -> None:
        _notify_q.put_nowait(text)  # unbounded: never blocks the monitor loop

    async def _notify_pump() -> None:
        while True:
            text = await _notify_q.get()
            for uid in cfg.allowed_user_ids:
                delay, attempts = 5.0, 0
                while True:
                    try:
                        await app.bot.send_message(chat_id=uid, text=text)
                        break
                    except Forbidden:
                        log.warning("notify: user %s blocked the bot — dropping", uid)
                        break
                    except Exception as e:  # noqa: BLE001 — keep retrying
                        attempts += 1
                        if attempts >= 60:  # ~1 h of backoff, then give up
                            log.error("notify to %s gave up after %d attempts: %s",
                                      uid, attempts, e)
                            break
                        log.warning("notify to %s failed (%s), retry %d in %.0fs",
                                    uid, e, attempts, delay)
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 60.0)
            _notify_q.task_done()

    monitor = M.Monitor(cfg, _notify)

    async def _reply(update, text: str) -> None:
        # PLAIN text only: dynamic content with _ * ` breaks Telegram Markdown.
        for chunk in chunk_text(text, 3900):
            await update.effective_message.reply_text(
                chunk, disable_web_page_preview=True)

    async def cmd_start(update, context) -> None:
        # The reply keyboard sent here PERSISTS: buttons stay at the bottom of
        # the chat by default until the user collapses them — no command needed
        # to bring them back.
        await update.effective_message.reply_text(
            "Монитор терминала MT5 (фаза 1 — наблюдение).\n"
            "Кнопки внизу — всегда под рукой; /mt5 — пульт с обновлением на месте; "
            "/help — справка",
            reply_markup=m5_rk, disable_web_page_preview=True)

    async def cmd_help(update, context) -> None:
        await _reply(update, _HELP)

    async def _mt5_answer(sub: str) -> str:
        if sub in ("status", "статус"):
            return await asyncio.to_thread(monitor.status_text)
        if sub in ("orders", "ордера", "pending"):
            return await asyncio.to_thread(monitor.orders_text)
        if sub in ("positions", "позиции", "pos"):
            return await asyncio.to_thread(monitor.positions_text)
        if sub in ("help", "помощь"):
            return _HELP
        return f"Не знаю «{sub}». Доступно: /mt5status · /mt5orders · /mt5positions · /help"

    # ---- views: lists with a button per item, item cards, confirmations ----

    nav_row = [InlineKeyboardButton(t, callback_data=f"m5:{sub}")
               for t, sub in MENU_BTNS.items()]
    LIST_CAP = 20  # blocks per message: keeps text < 4096 and buttons sane

    def _nav_kb():
        return InlineKeyboardMarkup([nav_row])

    async def _data(sub: str):
        return await asyncio.to_thread(
            monitor.orders_data if sub == "orders" else monitor.positions_data)

    async def _list_view(sub: str):
        header, items = await _data(sub)
        shown, rest = items[:LIST_CAP], len(items) - LIST_CAP
        text = header + ("\n\n" + "\n\n".join(b for _, b, _ in shown) if shown else "")
        if rest > 0:
            text += f"\n\n…и ещё {rest} — показал первые {LIST_CAP}"
        prefix = "m5ord:" if sub == "orders" else "m5pos:"
        rows = [[InlineKeyboardButton(lbl, callback_data=f"{prefix}{tk}")]
                for tk, _, lbl in shown]
        return text, InlineKeyboardMarkup(rows + [nav_row])

    async def _find_block(kind: str, ticket: int):
        """(block, offline_text): block None = item gone / terminal offline."""
        header, items = await _data("orders" if kind == "ord" else "positions")
        if not items and header[:1] in ("📴", "❌"):
            return None, header
        return next((b for t, b, _ in items if t == ticket), None), None

    async def _item_card(kind: str, ticket: int):
        blk, offline = await _find_block(kind, ticket)
        if offline:
            return offline, _nav_kb()
        if blk is None:
            return (f"{'Ордер' if kind == 'ord' else 'Позиция'} №{ticket} уже не "
                    "существует — обнови список.", _nav_kb())
        if not cfg.allow_trading:
            # Don't tease with action buttons that would refuse on the 3rd
            # screen — say it right in the card.
            return (f"{blk}\n\n⛔ Торговые действия выключены (mt5.allow_trading: "
                    "false в config.yaml) — пока можно только смотреть.", _nav_kb())
        if kind == "ord":
            action = [InlineKeyboardButton("🗑 Удалить ордер",
                                           callback_data=f"m5dl:{ticket}")]
        else:
            action = [InlineKeyboardButton("🔴 Закрыть по рынку",
                                           callback_data=f"m5cl:{ticket}")]
        return (f"{blk}\n\nЧто сделать? Тапни действие:",
                InlineKeyboardMarkup([action, nav_row]))

    async def _confirm_view(kind: str, ticket: int):
        blk, offline = await _find_block("ord" if kind == "dl" else "pos", ticket)
        if offline:
            return offline, _nav_kb()
        if blk is None:
            return "Объект уже не существует — обнови список.", _nav_kb()
        if kind == "cl":
            verb, yes, back = "закрыть ПО РЫНКУ", f"m5cly:{ticket}", f"m5pos:{ticket}"
        else:
            verb, yes, back = "удалить pending-ордер", f"m5dly:{ticket}", f"m5ord:{ticket}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтверждаю", callback_data=yes)],
            [InlineKeyboardButton("❌ Отмена", callback_data=back)],
            nav_row])
        return f"Точно {verb}?\n\n{blk}", kb

    async def _exec_view(kind: str, ticket: int):
        if kind == "cl":
            result = await asyncio.to_thread(monitor.close_position, ticket)
            back = "m5:positions"
        else:
            result = await asyncio.to_thread(monitor.delete_pending, ticket)
            back = "m5:orders"
        return result, InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 К списку (обновить)", callback_data=back)],
            nav_row])

    async def _view(sub: str):
        """(text, keyboard) for every screen; text-only subs get just the nav."""
        if sub in ("orders", "positions"):
            return await _list_view(sub)
        return await _mt5_answer(sub), _nav_kb()

    async def _send_view(update, sub: str) -> None:
        text, kb = await _view(sub)
        await update.effective_message.reply_text(
            text, reply_markup=kb, disable_web_page_preview=True)

    # Full-command aliases (/mt5status etc.) exist because Telegram auto-links
    # only the /token up to the first space — an argument after a space is NOT
    # part of the tap, so "/mt5 status" can never be sent by tapping it.
    async def cmd_mt5(update, context) -> None:
        if context.args:  # typed form: /mt5 status|orders|positions
            await _send_view(update, context.args[0].lower())
        else:  # bare /mt5 -> the inline panel (refresh-in-place mode)
            await update.effective_message.reply_text(
                "🎛 Пульт наблюдения MT5 — тапни кнопку:",
                reply_markup=m5_kb, disable_web_page_preview=True)

    async def m5_menu_text(update, context) -> None:
        # A persistent-keyboard tap arrives as a plain message whose text is
        # exactly the button label — route it to the same views.
        sub = MENU_BTNS.get((update.effective_message.text or "").strip())
        if sub:
            await _send_view(update, sub)

    async def _edit_or_send(update, q, context, text: str, kb) -> None:
        # NOTE (known trap): CallbackQuery has no .bot — context.bot is used
        # for the fallback send. "message is not modified" (the same screen
        # re-tapped) is NOT an error.
        try:
            await q.edit_message_text(text=text, reply_markup=kb,
                                      disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.warning("edit_message_text failed (%s); sending new message", e)
        except Exception:  # noqa: BLE001 — never crash on UI
            log.exception("edit_message_text failed; sending new message")
        # Very old/inline messages can lack effective_chat — private chats
        # always have from_user, and there chat id == user id.
        chat_id = (update.effective_chat.id if update.effective_chat
                   else getattr(q.from_user, "id", None))
        if chat_id is None:
            return
        try:
            await context.bot.send_message(chat_id=chat_id,
                                           text=text, reply_markup=kb,
                                           disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.warning("view fallback send failed")

    async def m5_callback(update, context) -> None:
        q = update.callback_query
        data = q.data or ""
        try:
            await q.answer()  # drop the tap spinner immediately
        except Exception:  # noqa: BLE001 — e.g. answered late by the client
            pass
        try:
            if data.startswith("m5:"):
                sub = data.split(":", 1)[1]
                if sub in ("orders", "positions"):
                    text, kb = await _list_view(sub)
                else:
                    text, kb = await _mt5_answer(sub), _nav_kb()
            elif data.startswith("m5pos:"):
                text, kb = await _item_card("pos", int(data[6:]))
            elif data.startswith("m5ord:"):
                text, kb = await _item_card("ord", int(data[6:]))
            elif data.startswith("m5cl:"):
                text, kb = await _confirm_view("cl", int(data[5:]))
            elif data.startswith("m5cly:"):
                text, kb = await _exec_view("cl", int(data[6:]))
            elif data.startswith("m5dl:"):
                text, kb = await _confirm_view("dl", int(data[5:]))
            elif data.startswith("m5dly:"):
                text, kb = await _exec_view("dl", int(data[6:]))
            else:
                text, kb = "Неизвестная кнопка.", None
        except ValueError:
            text, kb = "Не понял номер (тикет) в кнопке.", None
        await _edit_or_send(update, q, context, text, kb)

    async def cmd_mt5status(update, context) -> None:
        await _send_view(update, "status")

    async def cmd_mt5orders(update, context) -> None:
        await _send_view(update, "orders")

    async def cmd_mt5positions(update, context) -> None:
        await _send_view(update, "positions")

    async def _startup_text() -> str:
        trade = ("Торговые действия включены (allow_trading: true)."
                 if cfg.allow_trading else
                 "Торговые действия выключены (mt5.allow_trading: false) — "
                 "списки работают, закрыть/удалить нельзя.")
        return ("🤖 MT5-бот запущен (фаза 1 — наблюдение + действия над элементами).\n"
                f"Терминал: {cfg.terminal_path or 'attach к любому запущенному'}\n"
                "Кнопки внизу чата: статус · ордера · позиции (вернут /start-ом)\n"
                "Тап по позиции/ордеру в списке — действия с подтверждением.\n"
                f"{trade}\n"
                "Команды: /mt5status · /mt5orders · /mt5positions · /help")

    async def _notify_startup(application, text: str) -> None:
        # Retries: at logon the network may be down for a while (as in bot.py).
        pending = set(cfg.allowed_user_ids)
        for attempt in range(30):
            if not pending:
                return
            failed = set()
            for uid in pending:
                try:
                    # Carry the reply keyboard: buttons become the default UI
                    # from the very first message, no /start needed.
                    await application.bot.send_message(chat_id=uid, text=text,
                                                       reply_markup=m5_rk)
                except Exception as e:  # noqa: BLE001
                    failed.add(uid)
                    log.warning("startup notify to %s failed (attempt %d): %s",
                                uid, attempt + 1, e)
            pending = failed
            if pending:
                await asyncio.sleep(10)
        log.warning("startup notify gave up for %s", pending)

    async def _watch_incumbency() -> None:
        while True:
            await asyncio.sleep(_INCUMBENCY_POLL)
            if cs.incumbent:
                return
            now = time.monotonic()
            conflict_quiet = cs.last_conflict_at is None or (now - cs.last_conflict_at) > _QUIET
            net_quiet = cs.last_net_error_at is None or (now - cs.last_net_error_at) > _QUIET
            if conflict_quiet and net_quiet:
                cs.incumbent = True
                log.info("Incumbent — clean polling established; will not defer.")
                return

    async def _defer_and_exit(application) -> None:
        if cs.yielding:
            return
        cs.yielding = True
        log.error("Deferring — persistent 409; another instance owns this token "
                  "(likely another machine). This one exits.")

        async def _say() -> None:
            text = ("⏭️ Этот экземпляр MT5-бота остановился: токен уже занят действующим "
                    "poller-ом (409, вероятно на другой машине). Работает активный экземпляр.")
            for uid in cfg.allowed_user_ids:
                try:
                    await application.bot.send_message(chat_id=uid, text=text)
                except Exception:  # noqa: BLE001
                    pass
        try:
            await asyncio.wait_for(_say(), timeout=10)
        except Exception:  # noqa: BLE001
            pass
        application.stop_running()

    async def _on_error(update, context) -> None:
        err = context.error
        now = time.monotonic()
        if isinstance(err, (NetworkError, TimedOut)):
            cs.last_net_error_at = now
            log.warning("transient network error (will auto-retry): %s", err)
            return
        if isinstance(err, Conflict):
            cs.last_conflict_at = now
            if cs.incumbent:
                return
            if cs.last_conflict_seen is None or (now - cs.last_conflict_seen) > _CONFLICT_GAP:
                cs.run_start = now
                log.warning("409 Conflict — another poller holds this token; "
                            "will defer in ~%.0fs if it persists.", cs.yield_grace)
            cs.last_conflict_seen = now
            if cs.run_start is not None and (now - cs.run_start) >= cs.yield_grace:
                await _defer_and_exit(context.application)
            return
        log.exception("unhandled error (update=%s): %s", update, err)

    async def _post_init(application) -> None:
        # Register the command menu (the "/" autocomplete list). Full-command
        # names keep every menu entry tappable as a whole (see cmd_mt5 note).
        try:
            from telegram import BotCommand
            await application.bot.set_my_commands([
                BotCommand("mt5status", "терминал, счёт, margin"),
                BotCommand("mt5orders", "pending-ордера"),
                BotCommand("mt5positions", "позиции с P&L"),
                BotCommand("mt5", "кнопки: статус/ордера/позиции"),
                BotCommand("help", "справка"),
            ])
        except Exception as e:  # noqa: BLE001 — cosmetic, must not block start
            log.warning("set_my_commands failed: %s", e)
        t0 = asyncio.create_task(_notify_pump())
        _bg_tasks.add(t0)
        t0.add_done_callback(_bg_tasks.discard)
        t1 = asyncio.create_task(_notify_startup(application, await _startup_text()))
        _bg_tasks.add(t1)
        t1.add_done_callback(_bg_tasks.discard)
        t2 = asyncio.create_task(_watch_incumbency())
        _bg_tasks.add(t2)
        t2.add_done_callback(_bg_tasks.discard)
        await monitor.start()  # attach happens inside the loop; notifies by itself

    async def _post_shutdown(application) -> None:
        for t in list(_bg_tasks):  # no "Task was destroyed but it is pending"
            t.cancel()
        await monitor.stop()

    auth = authorized(cfg)  # cfg.allowed_user_ids — same allowlist as the Claude bot
    app = (Application.builder().token(cfg.token)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    app.add_error_handler(_on_error)
    app.add_handler(CommandHandler("start", auth(cmd_start)))
    app.add_handler(CommandHandler("help", auth(cmd_help)))
    app.add_handler(CommandHandler("mt5", auth(cmd_mt5)))
    app.add_handler(CommandHandler("mt5status", auth(cmd_mt5status)))
    app.add_handler(CommandHandler("mt5orders", auth(cmd_mt5orders)))
    app.add_handler(CommandHandler("mt5positions", auth(cmd_mt5positions)))
    # All inline taps of the пульт (m5:<sub>), the item lists (m5pos:/m5ord:)
    # and the trade actions with confirmation (m5cl:/m5cly:/m5dl:/m5dly:).
    # auth() works as-is: it checks update.effective_user, which CallbackQuery
    # updates carry too.
    app.add_handler(CallbackQueryHandler(auth(m5_callback), pattern=r"^m5"))
    # Persistent-keyboard taps (button text as a message). Commands above are
    # not affected: a CommandHandler only matches strings starting with "/".
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND
        & filters.Regex(r"^(📊 Статус|⏳ Ордера|📋 Позиции)$"),
        auth(m5_menu_text)))

    log.info("MT5 bot starting. Terminal: %s. Allowed users: %s. Trading: %s",
             cfg.terminal_path or "(any)", cfg.allowed_user_ids, cfg.allow_trading)
    print("MT5 bot is running. Press Ctrl+C to stop.", flush=True)

    # "callback_query" is REQUIRED for the /mt5 button panel (phase 2 will add
    # inline trade confirmations on top): without it Telegram silently never
    # delivers button taps (known trap). drop_pending_updates: no replay storm
    # after downtime (same as bot.py).
    try:
        app.run_polling(allowed_updates=["message", "callback_query"],
                        drop_pending_updates=True)
    except KeyboardInterrupt:
        log.info("stopped by Ctrl+C")


if __name__ == "__main__":
    main()
