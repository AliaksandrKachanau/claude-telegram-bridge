"""MT5 terminal monitoring core for the second Telegram bot (phase 1: read-only).

All MetaTrader5 package calls are synchronous IPC -> every call runs under a
threading lock and is invoked from the event loop via asyncio.to_thread
(never inline: a blocking call would stall the PTB poller).

The module knows nothing about Telegram: the Monitor emits plain-text event
strings through an injected async ``notify`` callback and renders command
replies as plain text (dynamic text must NOT use Telegram Markdown — the
markdown parser rejects it silently).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.yaml"

try:  # optional dependency: only the MT5 bot process needs it
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:  # pragma: no cover — repo clones without the package
    mt5 = None
    MT5_AVAILABLE = False

# MQL5 enums mirrored (the pip package returns raw ints).
ORDER_TYPE_LABEL = {
    0: "BUY", 1: "SELL",
    2: "BUY LIMIT", 3: "SELL LIMIT",
    4: "BUY STOP", 5: "SELL STOP",
    6: "BUY STOP LIMIT", 7: "SELL STOP LIMIT",
}
# Short forms for button captions (long labels get visually truncated).
ORDER_TYPE_SHORT = {
    0: "BUY", 1: "SELL", 2: "B.LIMIT", 3: "S.LIMIT",
    4: "B.STOP", 5: "S.STOP", 6: "B.STOPLIM", 7: "S.STOPLIM",
}
PENDING_TYPES = {2, 3, 4, 5, 6, 7}
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1  # distinct enum, same ints
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
# DEAL_REASON -> (emoji, label); everything user-initiated collapses to 👤
DEAL_REASON_LABEL = {
    0: ("👤", ""), 1: ("👤", ""), 2: ("👤", ""),
    3: ("⚙️", "EXPERT"), 4: ("🛑", "SL"), 5: ("🎯", "TP"),
    6: ("💥", "STOP OUT"), 7: ("🔁", "ROLLOVER"), 8: ("🔁", "VMARGIN"),
}
_MARGIN_HYSTERESIS = 50.0  # re-arm the margin warning only above warn+pct


@dataclass
class Mt5Settings:
    """Everything the MT5 bot needs; loaded from .env + config.yaml:mt5."""
    token: str = ""                       # MT5_BOT_TOKEN (guard lives in mt5_bot)
    allowed_user_ids: set[int] = field(default_factory=set)
    terminal_path: str = ""               # attach to this terminal (empty = any)
    allow_trading: bool = False           # phase 2 flag; monitor is read-only
    poll_sec: float = 2.0
    margin_warn_pct: float = 200.0
    notify_placement: bool = True
    notify_fill: bool = True
    notify_close: bool = True
    notify_cancel: bool = False           # deliberately off (noisy replacements)
    shot_timeout_sec: int = 10            # phase 3 (screenshots)


def load_settings() -> Mt5Settings:
    load_dotenv(ENV_PATH)
    raw: dict = {}
    if CONFIG_PATH.exists():
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    m = raw.get("mt5") or {}
    notify = m.get("notify") or {}
    ids: set[int] = set()
    for part in os.environ.get("ALLOWED_USER_IDS", "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return Mt5Settings(
        token=os.environ.get("MT5_BOT_TOKEN", "").strip(),
        allowed_user_ids=ids,
        terminal_path=str(m.get("terminal_path") or "").strip(),
        allow_trading=bool(m.get("allow_trading", False)),
        poll_sec=float(m.get("poll_sec", 2)),
        margin_warn_pct=float(m.get("margin_warn_pct", 200)),
        notify_placement=bool(notify.get("placement", True)),
        notify_fill=bool(notify.get("fill", True)),
        notify_close=bool(notify.get("close", True)),
        notify_cancel=bool(notify.get("cancel", False)),
        shot_timeout_sec=int(m.get("shot_timeout_sec", 10)),
    )


class Mt5Disconnected(Exception):
    """A poll found the terminal/IPC gone; the loop re-attaches."""


def _running_image_paths(exe_name: str) -> set[str]:
    """Full image paths of running processes whose exe FILE NAME matches.

    Used as the never-launch gate: mt5.initialize(path=...) would START a
    closed terminal (and its EAs) — the monitor must only ATTACH to a running
    one. Toolhelp32 + QueryFullProcessImageNameW, no extra dependencies.
    """
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (-1, 0):
        return set()
    paths: set[str] = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile and entry.szExeFile.lower() == exe_name.lower():
                # PROCESS_QUERY_LIMITED_INFORMATION — works without extra rights
                h = k32.OpenProcess(0x1000, False, entry.th32ProcessID)
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        paths.add(buf.value)
                    k32.CloseHandle(h)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return paths


def _vol(v: float) -> str:
    return f"{v:g}"


class Monitor:
    """Background poller: snapshots the terminal, diffs, emits event strings."""

    def __init__(self, cfg: Mt5Settings, notify) -> None:
        self.cfg = cfg
        self._notify = notify  # async fn(text) -> None; must not raise
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._attached = False
        self._down_notified = False
        self._baselined = False          # first snapshot is state, not events
        self._known_orders: set[int] = set()
        self._last_deal_ms: int = 0
        self._margin_warned = False
        self._digits: dict[str, int] = {}

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mt5-monitor")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        try:
            await asyncio.to_thread(self._shutdown_mt5)
        except Exception:  # noqa: BLE001 — shutdown must never raise
            log.exception("mt5 shutdown failed")

    async def _run(self) -> None:
        poll = max(1.0, self.cfg.poll_sec)
        while not self._stopped:
            try:
                if not self._attached:
                    ok = await asyncio.to_thread(self._attach)
                    if not ok:
                        if not self._down_notified:
                            self._down_notified = True
                            await self._notify(
                                "📴 Терминал MT5 недоступен (не запущен или нет связи) — "
                                "переподключаюсь каждые 5 с. Закрытый терминал сам не запускаю.")
                        await asyncio.sleep(5.0)
                        continue
                    self._attached = True
                    snap = await asyncio.to_thread(self._snapshot)
                    missed = len(snap["deals"])
                    self._baseline(snap)
                    # ONE message per reconnect: the header folds the
                    # "reconnected" notice into the fresh baseline summary.
                    was_down = self._down_notified
                    self._down_notified = False
                    extra = (f"\n💼 За время простоя произошло сделок: {missed} "
                             "(подробности — в истории терминала)."
                             if was_down and missed else "")
                    header = "📡 Подключение восстановлено." if was_down else ""
                    await self._notify(self._summary(snap, header, extra))
                    self._baselined = True
                    continue
                snap = await asyncio.to_thread(self._snapshot)
            except Mt5Disconnected:
                # IPC gone: detach; the next loop iteration re-attaches (and
                # re-baselines, so the outage gap never replays as events).
                self._attached = False
                self._baselined = False
                await asyncio.to_thread(self._shutdown_mt5)
                # Hot-loop guard: while the terminal process is up but the
                # server link is down, _attach succeeds in ~10 ms and the
                # snapshot throws again — without this sleep the loop would
                # spin shutdown/initialize dozens of times per second.
                await asyncio.sleep(5.0)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the monitor must survive anything
                log.exception("monitor loop error")
                await asyncio.sleep(5.0)
                continue
            try:
                for text in self._diff(snap):
                    await self._notify(text)
            except Exception:  # noqa: BLE001
                log.exception("diff/notify error")
            await asyncio.sleep(poll)

    # ---- MT5 plumbing (all *locked* methods run in a worker thread) --------

    def _attach(self) -> bool:
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 package not installed — pip install MetaTrader5")
        kwargs = {"timeout": 15000}
        if self.cfg.terminal_path:
            if not Path(self.cfg.terminal_path).exists():
                log.error("terminal_path does not exist: %s", self.cfg.terminal_path)
                return False
            # NEVER launch: initialize(path=...) would START the closed
            # terminal (and every EA on its charts, with AutoTrading on).
            # Attach only when that exact exe is already running.
            want = str(Path(self.cfg.terminal_path).resolve()).lower()
            running = _running_image_paths(Path(self.cfg.terminal_path).name)
            if not any(p.lower() == want for p in running):
                log.info("terminal %s is not running — waiting, NOT launching",
                         self.cfg.terminal_path)
                return False
            kwargs["path"] = self.cfg.terminal_path
        else:
            # Empty path must NOT fall through to a bare initialize(): the
            # package would LAUNCH a terminal from its default install
            # location (with live EAs). Gate on ANY running terminal64.exe.
            if not _running_image_paths("terminal64.exe"):
                log.info("no running terminal64.exe — waiting, NOT launching")
                return False
        with self._lock:
            try:
                ok = bool(mt5.initialize(**kwargs))
            except Exception as e:  # noqa: BLE001
                log.warning("mt5.initialize raised: %s", e)
                return False
            if not ok:
                log.warning("mt5.initialize failed: %s", mt5.last_error())
                return False
            if mt5.account_info() is None:
                # attached to the IPC but the account/trade connection is not
                # up yet (terminal still starting or not logged in) — treat as
                # not attached; initialize() again on the next attempt is fine.
                log.warning("mt5 attached but account_info is None: %s", mt5.last_error())
                return False
            return True

    def _shutdown_mt5(self) -> None:
        if not MT5_AVAILABLE:
            return
        with self._lock:
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def _usable_locked(self) -> bool:
        term = mt5.terminal_info()
        acc = mt5.account_info()
        return (term is not None and acc is not None
                and getattr(term, "connected", False))

    def _snapshot(self) -> dict:
        with self._lock:
            if not self._usable_locked():
                raise Mt5Disconnected("terminal_info/account_info unavailable")
            term = mt5.terminal_info()
            acc = mt5.account_info()
            orders_raw = mt5.orders_get()
            positions_raw = mt5.positions_get()
            # A transient None (IPC hiccup, busy terminal) is NOT "no orders":
            # treating it as empty would wipe the known-ticket sets and the
            # next poll would re-announce every pending as a new placement.
            if orders_raw is None or positions_raw is None:
                raise Mt5Disconnected("orders_get/positions_get returned None")
            orders = {o.ticket: o for o in orders_raw}
            positions = {p.ticket: p for p in positions_raw}
            deals = self._new_deals_locked()
            # price digits cache (symbol_info is IPC too — prefetch in-thread)
            symbols = set(orders and {o.symbol for o in orders.values()} or set())
            symbols |= {p.symbol for p in positions.values()}
            symbols |= {d.symbol for d in deals if d.symbol}
            for sym in symbols:
                if sym and sym not in self._digits:
                    si = mt5.symbol_info(sym)
                    self._digits[sym] = getattr(si, "digits", 0) or 0
            return {"orders": orders, "positions": positions, "deals": deals,
                    "acc": acc, "term": term}

    @staticmethod
    def _deal_ms(d) -> int:
        # time_msc (ms) keeps sub-second deal batches apart; fall back to the
        # second-resolution stamp when the attribute is missing.
        return getattr(d, "time_msc", 0) or (getattr(d, "time", 0) * 1000)

    def _new_deals_locked(self) -> list:
        # Deal stamps are SERVER time while the window is computed from LOCAL
        # clocks — the measured skew on this broker is ~1.7 h AHEAD, and the
        # sign can flip with broker/DST. A 1-day poll window keeps the usable
        # depth positive under any realistic skew; fresh-ness is decided by the
        # strict > filter on millisecond stamps, not by the window.
        now = datetime.now()
        window_days = 7 if not self._baselined else 1.0
        deals = mt5.history_deals_get(now - timedelta(days=window_days),
                                      now + timedelta(days=1))
        if deals is None:
            raise Mt5Disconnected("history_deals_get returned None")
        if not self._last_deal_ms:
            # First ever poll: adopt the newest SERVER stamp. No deals -> stay
            # uninitialised; never write a local-clock epoch here (domains differ).
            if deals:
                self._last_deal_ms = max(self._deal_ms(d) for d in deals)
            return []
        fresh = [d for d in deals if self._deal_ms(d) > self._last_deal_ms]
        if deals:
            self._last_deal_ms = max(self._last_deal_ms,
                                     max(self._deal_ms(d) for d in deals))
        return fresh

    # ---- event logic (pure: runs on the event loop, no IPC) ----------------

    def _px(self, symbol: str, price) -> str:
        if not price:
            return "—"
        d = self._digits.get(symbol) or (2 if price > 500 else 5)
        return f"{price:.{d}f}"

    def _baseline(self, snap: dict) -> None:
        self._known_orders = set(snap["orders"])

    def _diff(self, snap: dict) -> list[str]:
        cfg = self.cfg
        events: list[str] = []
        if cfg.notify_placement:
            for t, o in snap["orders"].items():
                if t not in self._known_orders and o.type in PENDING_TYPES:
                    events.append(
                        f"📈 {o.symbol} {ORDER_TYPE_LABEL.get(o.type, f'T{o.type}')} "
                        f"{_vol(o.volume_current)} @ {self._px(o.symbol, o.price_open)}")
        for d in snap["deals"]:
            if not d.symbol or d.type not in (DEAL_TYPE_BUY, DEAL_TYPE_SELL):
                continue  # balance/credit operations have no symbol
            side = "BUY" if d.type == DEAL_TYPE_BUY else "SELL"
            if d.entry in (DEAL_ENTRY_IN, DEAL_ENTRY_INOUT):
                if cfg.notify_fill:
                    events.append(f"✅ {d.symbol} {side} FILLED "
                                  f"{_vol(d.volume)} @ {self._px(d.symbol, d.price)}")
            elif d.entry in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY):
                if cfg.notify_close:
                    emoji, label = DEAL_REASON_LABEL.get(
                        getattr(d, "reason", -1), ("❓", "R" + str(getattr(d, "reason", -1))))
                    line = (f"{emoji} {d.symbol} "
                            f"{(label + ' ') if label else ''}closed "
                            f"{_vol(d.volume)} {d.profit:+.2f}")
                    swap = getattr(d, "swap", 0.0) or 0.0
                    if abs(swap) >= 0.005:
                        line += f" (своп {swap:+.2f})"
                    events.append(line)
        self._known_orders = set(snap["orders"])
        lvl = snap["acc"].margin_level or 0.0
        if lvl <= 0:
            # Flat (no positions): no margin at all — re-arm silently, or the
            # warning would stay latched and miss the NEXT cycle's dip.
            self._margin_warned = False
        elif not self._margin_warned and lvl < cfg.margin_warn_pct:
            self._margin_warned = True
            events.append(f"⚠️ Margin level {lvl:.0f}% "
                          f"(порог {cfg.margin_warn_pct:.0f}%)")
        elif self._margin_warned and lvl > cfg.margin_warn_pct + _MARGIN_HYSTERESIS:
            self._margin_warned = False
            events.append(f"☑️ Margin level восстановлен: {lvl:.0f}%")
        return events

    def _summary(self, snap: dict, header: str = "", extra: str = "") -> str:
        acc, term = snap["acc"], snap["term"]
        bysym: dict[str, int] = {}
        for p in snap["positions"].values():
            bysym[p.symbol] = bysym.get(p.symbol, 0) + 1
        pos_s = ", ".join(f"{s} ×{n}" for s, n in sorted(bysym.items())) or "нет"
        npend = sum(1 for o in snap["orders"].values() if o.type in PENDING_TYPES)
        lvl = (f"\nMargin {acc.margin:.2f} · Level {acc.margin_level:.0f}%"
               if (acc.margin_level or 0) > 0 else "")
        first = header or "🟢 Мониторинг MT5 запущен."
        return (f"{first}\n"
                f"Терминал: {term.name}, счёт {acc.login} ({acc.server})\n"
                f"Позиции: {len(snap['positions'])} ({pos_s}) · Pending: {npend}\n"
                f"Equity {acc.equity:.2f} {acc.currency}"
                f" · Баланс {acc.balance:.2f}{lvl}{extra}")

    # ---- command renderers (called via asyncio.to_thread) ------------------

    def _offline_text(self) -> str:
        return ("📴 Терминал MT5 недоступен (не запущен или нет связи со счётом) — "
                "переподключаюсь. Повтори команду позже.")

    def status_text(self) -> str:
        if not MT5_AVAILABLE:
            return "❌ Пакет MetaTrader5 не установлен."
        with self._lock:
            if not self._usable_locked():
                return self._offline_text()
            term = mt5.terminal_info()
            acc = mt5.account_info()
            positions = mt5.positions_get()
            orders = mt5.orders_get()
            if positions is None or orders is None:  # IPC hiccup ≠ "empty"
                return self._offline_text()
            npend = sum(1 for o in orders if o.type in PENDING_TYPES)
            conn = "✅" if term.connected else "❌"
            algo = "✅" if term.trade_allowed else "❌ (order_send будет давать 10027)"
            lvl = f"{acc.margin_level:.0f}%" if (acc.margin_level or 0) > 0 else "—"
            return (f"📊 MT5 статус\n"
                    f"Терминал: {term.name} · связь {conn} · автоторговля {algo}\n"
                    f"Счёт: {acc.login} ({acc.server}) · {acc.currency} · плечо 1:{acc.leverage}\n"
                    f"Баланс {acc.balance:.2f} · Equity {acc.equity:.2f}\n"
                    f"Margin {acc.margin:.2f} · Level {lvl} · Free {acc.margin_free:.2f}\n"
                    f"Позиций: {len(positions)} · Pending: {npend}")

    # ---- interactive list data (the bot layer turns items into buttons) ----

    def orders_data(self) -> tuple[str, list[tuple[int, str, str]]]:
        """(header, [(ticket, block_text, button_label), ...]) for /mt5orders.

        Each item is one BLOCK (blank-line separated, emoji as the direction
        "color" — Telegram text cannot be colored any other way) plus a SHORT
        label for its inline button.
        """
        if not MT5_AVAILABLE:
            return "❌ Пакет MetaTrader5 не установлен.", []
        with self._lock:
            if not self._usable_locked():
                return self._offline_text(), []
            raw = mt5.orders_get()
            if raw is None:  # transient IPC failure is NOT "no orders"
                return self._offline_text(), []
            orders = [o for o in raw if o.type in PENDING_TYPES]
            if not orders:
                return "Pending-ордеров нет.", []
            for o in orders:  # prime price digits so _px never guesses
                if o.symbol and o.symbol not in self._digits:
                    si = mt5.symbol_info(o.symbol)
                    self._digits[o.symbol] = getattr(si, "digits", 0) or 0
            header = f"⏳ Pending: {len(orders)}"
            items: list[tuple[int, str, str]] = []
            for o in sorted(orders, key=lambda x: (x.symbol, x.ticket)):
                arrow = "📈" if o.type % 2 == 0 else "📉"  # even types = BUY family
                sl = f" · sl {self._px(o.symbol, o.sl)}" if o.sl else ""
                tp = f" · tp {self._px(o.symbol, o.tp)}" if o.tp else ""
                block = (f"{arrow} {o.symbol} · "
                         f"{ORDER_TYPE_LABEL.get(o.type, f'T{o.type}')} "
                         f"{_vol(o.volume_current)}\n"
                         f"№{o.ticket} · @ {self._px(o.symbol, o.price_open)}{sl}{tp}")
                label = (f"{arrow} {o.symbol} · "
                         f"{ORDER_TYPE_SHORT.get(o.type, f'T{o.type}')} "
                         f"{_vol(o.volume_current)}")
                items.append((o.ticket, block, label))
            return header, items

    def orders_text(self) -> str:
        header, items = self.orders_data()
        return header + "\n\n" + "\n\n".join(b for _, b, _ in items) if items else header

    def positions_data(self) -> tuple[str, list[tuple[int, str, str]]]:
        """Same contract as orders_data, for /mt5positions."""
        if not MT5_AVAILABLE:
            return "❌ Пакет MetaTrader5 не установлен.", []
        with self._lock:
            if not self._usable_locked():
                return self._offline_text(), []
            positions = mt5.positions_get()
            if positions is None:  # transient IPC failure is NOT "no positions"
                return self._offline_text(), []
            positions = list(positions)
            if not positions:
                return "Открытых позиций нет.", []
            for p in positions:  # prime price digits so _px never guesses
                if p.symbol and p.symbol not in self._digits:
                    si = mt5.symbol_info(p.symbol)
                    self._digits[p.symbol] = getattr(si, "digits", 0) or 0
            total = sum(p.profit + (p.swap or 0.0) for p in positions)
            tot = "🟢" if total >= 0 else "🔴"
            header = f"📋 Позиции: {len(positions)} · P&L {tot} {total:+.2f}"
            items: list[tuple[int, str, str]] = []
            for p in sorted(positions, key=lambda x: (x.symbol, x.ticket)):
                side = "BUY" if p.type == POSITION_TYPE_BUY else "SELL"
                pnl = p.profit + (p.swap or 0.0)
                # Color = P&L sign (the dot sits next to the number it colors);
                # direction stays as the BUY/SELL word — one dot, one meaning.
                dot = "🟢" if pnl >= 0 else "🔴"
                first = f"{dot} {p.symbol} · {side} {_vol(p.volume)} · {pnl:+.2f}"
                block = (f"{first}\n"
                         f"№{p.ticket} · {self._px(p.symbol, p.price_open)} → "
                         f"{self._px(p.symbol, p.price_current)}")
                items.append((p.ticket, block, first))
            return header, items

    def positions_text(self) -> str:
        header, items = self.positions_data()
        return header + "\n\n" + "\n\n".join(b for _, b, _ in items) if items else header

    # ---- trading (phase 2 core; every action double-gated) ------------------

    RETCODE_HINT = {
        10009: "готово",
        10013: "неверный запрос",
        10014: "неверный объём",
        10017: "торговля запрещена",
        10018: "рынок закрыт",
        10019: "недостаточно средств",
        10027: "AutoTrading выключен в терминале",
        10030: "режим заполнения не поддержан",
        10036: "позиция уже закрыта",
    }

    def _trade_gate(self) -> str | None:
        """Refusal text when trading is disabled; None = allowed to proceed."""
        if not MT5_AVAILABLE:
            return "❌ Пакет MetaTrader5 не установлен."
        if not self.cfg.allow_trading:
            return ("⛔ Торговые команды выключены: mt5.allow_trading: false в "
                    "config.yaml (наблюдение работает). Включи флаг и перезапусти "
                    "бота — действия появятся.")
        return None

    def _autotrade_gate_locked(self) -> str | None:
        term = mt5.terminal_info()
        if term is None or not term.trade_allowed:
            return ("⛔ Автоторговля выключена в терминале (кнопка «AutoTrading») — "
                    "order_send вернёт 10027. Включи кнопку и повтори.")
        return None

    def _send_result(self, res, ok_text: str) -> str:
        if res is None:
            return f"❌ Терминал отклонил запрос: {mt5.last_error()}"
        if res.retcode != 10009:  # TRADE_RETCODE_DONE
            hint = self.RETCODE_HINT.get(res.retcode)
            extra = f": {hint}" if hint else ""
            return f"❌ Не получилось (retcode {res.retcode}{extra}) — {res.comment}"
        return ok_text

    def _filling_candidates(self, symbol: str) -> list[int]:
        """Filling modes to try, broker-preferred first (10030 = unsupported)."""
        si = mt5.symbol_info(symbol)
        fm = (getattr(si, "filling_mode", 0) or 0) if si is not None else 0
        pref = []
        if fm & 2:
            pref.append(mt5.ORDER_FILLING_IOC)
        if fm & 1:
            pref.append(mt5.ORDER_FILLING_FOK)
        out: list[int] = []
        for f in pref + [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK,
                         mt5.ORDER_FILLING_RETURN]:
            if f not in out:
                out.append(f)
        return out

    def close_position(self, ticket: int) -> str:
        """Close one position at market (opposite DEAL with position=ticket)."""
        gate = self._trade_gate()
        if gate:
            return gate
        with self._lock:
            try:
                if not self._usable_locked():
                    return self._offline_text()
                gate = self._autotrade_gate_locked()
                if gate:
                    return gate
                plist = mt5.positions_get()
                if plist is None:  # IPC hiccup: the position may well be alive
                    return self._offline_text()
                p = {x.ticket: x for x in plist}.get(ticket)
                if p is None:
                    return f"Позиция №{ticket} уже не существует — обнови список."
                tick = mt5.symbol_info_tick(p.symbol)
                if tick is None:
                    return f"❌ Нет котировки по {p.symbol} — повтори через минуту."
                is_buy = p.type == POSITION_TYPE_BUY
                base = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": p.symbol,
                    "volume": p.volume,
                    "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
                    "position": ticket,  # the position this deal closes
                    "price": tick.bid if is_buy else tick.ask,
                    "deviation": 20,
                    "magic": 0,          # manual leg semantics for the EA
                    "comment": "GRE-TG",  # telegram-origin mark in the Journal
                    "type_time": mt5.ORDER_TIME_GTC,
                }
                res = None
                for filling in self._filling_candidates(p.symbol):
                    res = mt5.order_send({**base, "type_filling": filling})
                    if res is None or res.retcode != 10030:
                        break  # 10030 = filling unsupported, try the next mode
                px = f" @ {self._px(p.symbol, res.price)}" if getattr(res, "price", None) else ""
                ok = (f"✅ {p.symbol} {'BUY' if is_buy else 'SELL'} {_vol(p.volume)} "
                      f"закрыта{px} (ордер №{getattr(res, 'order', '?')})")
                return self._send_result(res, ok)
            except Exception as e:  # noqa: BLE001 — a tap must always get an answer
                log.warning("close_position(%s) failed: %s", ticket, e)
                return "❌ Связь с терминалом оборвалась — повтори действие."

    def delete_pending(self, ticket: int) -> str:
        """Remove one pending order (TRADE_ACTION_REMOVE)."""
        gate = self._trade_gate()
        if gate:
            return gate
        with self._lock:
            try:
                if not self._usable_locked():
                    return self._offline_text()
                gate = self._autotrade_gate_locked()
                if gate:
                    return gate
                olist = mt5.orders_get()
                if olist is None:  # IPC hiccup: the order may well be alive
                    return self._offline_text()
                o = {x.ticket: x for x in olist}.get(ticket)
                if o is None:
                    return f"Ордер №{ticket} уже не существует — обнови список."
                res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
                ok = (f"✅ Pending удалён: {o.symbol} "
                      f"{ORDER_TYPE_LABEL.get(o.type, f'T{o.type}')} "
                      f"{_vol(o.volume_current)} №{ticket}")
                return self._send_result(res, ok)
            except Exception as e:  # noqa: BLE001 — a tap must always get an answer
                log.warning("delete_pending(%s) failed: %s", ticket, e)
                return "❌ Связь с терминалом оборвалась — повтори действие."
