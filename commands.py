"""Telegram command handlers for the Claude bridge.

Dependencies (SETTINGS, STATE) are injected once via :func:`init` from bot.py.
"""
from __future__ import annotations

import asyncio
import hmac
import io
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from dotenv import set_key
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import messages as M
import speak as TTS
import transcribe as STT
import health
from claude_runner import _kill_proc_tree, run_claude
from config import ENV_PATH, VALID_MODES, Settings, save_field
from projects import RunningTask, State

log = logging.getLogger(__name__)

SETTINGS: Settings = None  # type: ignore[assignment]
STATE: State = None  # type: ignore[assignment]

MAX_SPEAK_CHARS = 5000  # cap TTS input so a huge answer doesn't yield a giant voice clip

# Phrases that ask the bot to reply by voice (matched case-insensitively as substrings).
VOICE_PHRASES = [
    "ответь голосом", "ответить голосом", "ответь вслух", "ответить вслух",
    "скажи голосом", "скажите голосом", "голосом ответь", "голосом ответить",
    "ответь аудио", "ответить аудио", "голосом пожалуйста",
    "озвучь ответ", "озвучь", "озвучить", "проговори", "проговорить",
    "прочитай вслух", "прочти вслух", "зачитай вслух",
]


def _extract_voice_request(text: str) -> tuple[bool, str]:
    """If `text` asks for a voice reply, return (True, text_without_phrase)."""
    import re
    low = text.lower()
    for phrase in VOICE_PHRASES:
        idx = low.find(phrase)
        if idx != -1:
            cleaned = text[:idx] + text[idx + len(phrase):]
            return True, cleaned.strip(" \t,.;:!?-—\n")
    return False, text


async def _speak_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Render `text` as a voice message and send it. Returns True on success."""
    chat_id = update.effective_chat.id
    bot = context.bot
    truncated = False
    if len(text) > MAX_SPEAK_CHARS:
        text = text[:MAX_SPEAK_CHARS]
        truncated = True

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
    note = await bot.send_message(chat_id=chat_id, text=f"🔊 Озвучиваю ({SETTINGS.tts_provider})…")
    try:
        opus = await TTS.speak(text, SETTINGS)
    except Exception as e:  # noqa: BLE001
        log.warning("speak failed: %s", e)
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Озвучка не удалась: {e}")
        return False
    finally:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=note.message_id)
        except Exception:  # noqa: BLE001
            pass

    if not opus:
        await bot.send_message(chat_id=chat_id, text="⚠️ Не получилось сгенерировать аудио.")
        return False

    caption = "🔊 (озвучены первые символы длинного ответа)" if truncated else None
    buf = io.BytesIO(opus)
    buf.name = "answer.ogg"
    await bot.send_voice(chat_id=chat_id, voice=buf, caption=caption)
    return True

HELP = (
    "🤖 *Claude Code Bridge*\n\n"
    "*/ask <текст>* — вопрос/анализ (только чтение, безопасно)\n"
    "*/task <текст>* — поручить работу (правка файлов по текущему режиму)\n"
    "*(или просто напишите текст)* — то же, что /task\n\n"
    "*/new* — начать новый диалог Claude (забыть контекст)\n"
    "*/speak* — озвучить голосом последний ответ 🔊\n"
    "*/voice on|off* — всегда отвечать голосом / обратно текстом\n"
    "*/confirm on|off* — показывать распознанный голос с кнопками ✅/✏️/🗑 перед отправкой\n"
    "*/draft on|off* — копить голосовые в черновик, отправить скопом («отправляй»/📤)\n"
    "*/reply_voice on|off* — ответ (reply) на сообщение бота озвучивает именно его\n"
    "*(или напишите «ответь голосом …», «озвучь»)*\n"
    "*(или надиктуйте 🎤 — голос распознаётся и выполнится как задача)*\n\n"
    "*/diff* — git diff текущего проекта\n"
    "*/git status|log|diff|commit <msg>* — операции git\n"
    "*/project* — список проектов · */project <имя>* — переключить · */project add <путь>*\n"
    "*/mode* — текущий режим · */mode balanced|full|strict* — сменить\n"
    "*/cancel* — прервать текущий запрос к Claude\n"
    "*/pause* · */resume* — приостановить/возобновить обработку запросов к Claude\n"
    "*/note* — диктовка без Claude (переключатель вкл/выкл); folder <имя>, browse (читать)\n"
    "\n"
    "*🎤 Голосом (без Claude)* — «пауза»/«продолжи», «новый диалог»,\n"
    "«режим balanced|full|strict», «проект <имя>», «голосовые вкл|выкл», «статус», «отмена»,\n"
    "«покажи записи» — открыть диктовки кнопками\n"
    "*/status* — состояние бота\n"
    "*/config* — настройки бота (секреты — после `/config unlock <пароль>`)\n"
)


def init(settings: Settings, state: State) -> None:
    global SETTINGS, STATE
    SETTINGS = settings
    STATE = state


# ---- helpers ----------------------------------------------------------------

def _text_after_command(text: str) -> str:
    """'/ask hello world' -> 'hello world' (handles /cmd@botname form)."""
    _, _, rest = (text or "").partition(" ")
    return rest.strip()


async def _typing_loop(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            continue


async def _do_claude(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     prompt: str, is_task: bool, speak_reply: bool = False) -> None:
    chat_id = update.effective_chat.id
    bot = context.bot
    if not prompt:
        await bot.send_message(chat_id=chat_id, text="Пустой запрос.")
        return
    if STATE.get_pause(chat_id):
        await bot.send_message(
            chat_id=chat_id,
            text="⏸ Claude на паузе. /resume — продолжить обработку запросов.",
        )
        return
    if STATE.claude_lock.locked():
        await bot.send_message(
            chat_id=chat_id,
            text="⏳ Уже выполняется запрос к Claude. Дождитесь окончания или /cancel.",
        )
        return

    async with STATE.claude_lock:
        try:
            proj = STATE.project_for_chat(chat_id)
        except RuntimeError as e:
            await bot.send_message(chat_id=chat_id, text=str(e))
            return
        mode = STATE.get_mode(chat_id)
        mode_cfg = SETTINGS.modes[mode]
        session_id = STATE.get_session(proj.name)

        task = RunningTask(proc=None)
        STATE.set_running(proj.name, task)

        def register(proc) -> None:
            task.proc = proc

        stop = asyncio.Event()
        typing = asyncio.create_task(_typing_loop(bot, chat_id, stop))
        try:
            result = await run_claude(
                prompt, proj, mode_cfg, SETTINGS,
                is_task=is_task,
                session_id=session_id,
                new_session=(session_id is None),
                register_proc=register,
            )
        finally:
            stop.set()
            typing.cancel()
            try:
                await typing
            except asyncio.CancelledError:
                pass
            STATE.clear_running(proj.name)

        if task.cancelled:
            await bot.send_message(chat_id=chat_id, text="🛑 Запрос отменён.")
            return

        if result.ok:
            if result.session_id:
                STATE.set_session(proj.name, result.session_id)
                STATE.last_cost[proj.name] = result.cost_usd
            STATE.last_answer[chat_id] = result.text or ""
            footer = M.make_footer(result.cost_usd, result.session_id, result.num_turns)
            sent_ids = await M.reply_long(
                bot, chat_id, result.text or "(пустой ответ)", footer=footer, state=STATE)
            if sent_ids:
                # Cache the plain answer text by its first message_id so a later
                # reply (when /reply_voice is on) can speak THIS specific answer.
                STATE.remember_bot_text(chat_id, sent_ids[0], result.text or "")
            if (speak_reply or STATE.get_voice(chat_id)) and (result.text or "").strip():
                await _speak_answer(update, context, result.text)
        else:
            tag = "⏱" if result.timed_out else "⚠️"
            await M.reply_long(bot, chat_id, f"{tag} Claude не завершил запрос:\n\n{result.text}",
                               state=STATE, render_markdown=False,
                               file_threshold=SETTINGS.long_output_threshold)


async def _git(proj_path: str, args: list[str]) -> tuple[int, str]:
    loop = asyncio.get_running_loop()

    def run():
        try:
            p = subprocess.run(
                ["git", "-C", proj_path, "--no-pager", *args],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            out = (p.stdout or "")
            if p.stderr and p.stderr.strip():
                out += ("\n" if out else "") + p.stderr.strip()
            return p.returncode, out
        except FileNotFoundError:
            return -1, "git не найден в PATH"
        except subprocess.TimeoutExpired:
            return -2, "git превысил время ожидания"

    return await loop.run_in_executor(None, run)


# ---- dictation helpers (/note: voice -> file, no Claude) --------------------

# Category folder names: letters (incl. Cyrillic), digits, space, _ and -.
# Everything else collapses to '_'; '.'/'..' are rejected (no path traversal).
_SAFE_FOLDER_RE = re.compile(r"[^A-Za-zА-Яа-я0-9 _\-]")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _safe_folder(name: str) -> str | None:
    """Return a sanitized folder name, or None if it is unusable/unsafe."""
    cleaned = _SAFE_FOLDER_RE.sub("_", (name or "").strip()).strip()
    if not cleaned or cleaned in (".", ".."):
        return None
    return cleaned


def _dictation_dir(folder: str) -> Path:
    """Resolve dictations_dir/<folder> and refuse anything escaping dictations_dir.

    Two layers: reject any path separator / '..' in the name BEFORE joining (so Windows
    can't collapse 'foo\\..\\bar' back inside the base), then a resolve() containment check.
    """
    if (not folder or folder in (".", "..")
            or any(sep in folder for sep in ("\\", "/", ":"))):
        raise ValueError(f"недопустимая папка: {folder!r}")
    base = Path(SETTINGS.dictations_dir).resolve()
    target = (base / folder).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"недопустимая папка: {folder!r}")
    return target


def _list_folders() -> list[str]:
    """Existing category folders under dictations/ (sorted); 'default' always listed."""
    base = Path(SETTINGS.dictations_dir).resolve()
    folders: list[str] = []
    if base.is_dir():
        for p in base.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                folders.append(p.name)
    if "default" not in folders:
        folders.append("default")
    return sorted(folders)


def _list_files(folder: str) -> list[str]:
    """Dates 'YYYY-MM-DD' of .md journals in <folder>, newest first."""
    d = _dictation_dir(folder)
    if not d.is_dir():
        return []
    dates = [p.stem for p in d.glob("*.md") if _DATE_RE.fullmatch(p.stem)]
    return sorted(dates, reverse=True)


async def _save_dictation(text: str, folder: str) -> Path:
    """Append a timestamped block to dictations/<folder>/YYYY-MM-DD.md (creates the dir)."""
    now = datetime.now()
    d = _dictation_dir(folder)
    fname = d / f"{now:%Y-%m-%d}.md"
    block = f"\n## {now:%H:%M:%S}\n\n{text.strip()}\n\n---\n"

    def _write() -> Path:
        d.mkdir(parents=True, exist_ok=True)
        with open(fname, "a", encoding="utf-8") as f:
            f.write(block)
        return fname

    return await asyncio.get_running_loop().run_in_executor(None, _write)


async def _run_dictation(update: Update, context: ContextTypes.DEFAULT_TYPE, voice) -> None:
    """Transcribe a voice/audio message and append it to the daily .md journal.

    Shared with cmd_voice; never calls Claude and never touches claude_lock, so it also
    works while the chat is /pause'd.
    """
    chat_id = update.effective_chat.id
    bot = context.bot
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    note = await bot.send_message(
        chat_id=chat_id, text=f"🎙 Распознаю голос ({SETTINGS.stt_provider}) → 📝 файл…"
    )

    fd, tmp_path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        try:
            tg_file = await bot.get_file(voice.file_id)
            await tg_file.download_to_drive(tmp_path)
        except Exception as e:  # noqa: BLE001
            log.warning("voice download failed: %s", e)
            await bot.send_message(chat_id=chat_id, text=f"Не удалось скачать голосовое: {e}")
            return

        try:
            text = await STT.transcribe(tmp_path, SETTINGS)
        except Exception as e:  # noqa: BLE001
            log.warning("transcribe failed: %s", e)
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Распознавание не удалось: {e}")
            return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    text = (text or "").strip()
    if not text:
        await bot.send_message(chat_id=chat_id, text="🎙 Не удалось распознать речь (пустой результат).")
        return

    folder = STATE.get_note_folder(chat_id)
    try:
        path = await _save_dictation(text, folder)
    except ValueError as e:  # noqa: BLE001
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Не записать в папку «{folder}»: {e}")
        return

    # Show the date WITHOUT ".md": Telegram linkifies "<date>.md" as a URL (.md is a
    # real TLD -> a tap opens a browser trying to resolve it -> DNS error).
    rel = f"{folder}/{path.stem}"
    await bot.edit_message_text(
        chat_id=chat_id, message_id=note.message_id,
        text=f"📝 Записано → {rel}\n\n{text[:800]}",
        disable_web_page_preview=True,
    )


# ---- voice control: local command interception (no Claude) -----------------
# A transcribed voice message is matched against a small set of Russian command
# intents and executed LOCALLY (pause/resume, voice-mode, new dialog, project,
# mode, cancel, status) — Claude is never called. The match happens BEFORE the
# pause gate in cmd_voice, so voice-control also works while Claude is paused
# (e.g. «продолжи» resumes a paused bot). Triggers require an explicit keyword
# so ordinary questions/requests still reach Claude.

_CMD_NORM = re.compile(r"[^\w\s]", re.UNICODE)


def _norm_cmd(text: str) -> str:
    """Lowercase, ё→е, strip punctuation to spaces, collapse whitespace."""
    t = (text or "").lower().replace("ё", "е")
    t = _CMD_NORM.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# canonical mode <- synonym prefixes (matched against a single normalized word)
# Russian (what Whisper usually transcribes) + English fallbacks.
_MODE_SYNONYMS = [
    (["баланс", "балансед", "balanced"], "balanced"),
    (["фулл", "фул", "полная свобода", "полный режим", "полного режима", "полный", "full"], "full"),
    (["стрикт", "строг", "стрит", "только чтение", "strict"], "strict"),
]

# verbs that mark "this is a switch command" (vs. a question like "объясни режим")
_SWITCH_VERBS = ("переключи", "выбери", "сделай", "поставь", "открой", "запусти", "включи")
_MODE_VERBS = ("поставь", "смени", "переключи", "включи", "установи", "сделай", "вруб")
# verbs that open the /note browse navigator when paired with a "записи/диктовки" word
_SHOW_VERBS = ("покажи", "открой", "посмотри", "листай", "листать")


def _match_mode_word(word: str) -> Optional[str]:
    for syns, mode in _MODE_SYNONYMS:
        for s in syns:
            if word == s or word.startswith(s):
                return mode
    return None


def _match_voice_command(text: str) -> Optional[tuple[str, object]]:
    """Return (intent, args) or None.

    intents: pause | resume | voice(bool) | new | project(str) | project_list
             | mode(str) | cancel | status
    """
    s = _norm_cmd(text)
    if not s:
        return None
    words = s.split()

    # project: bare "проект <name>" at the start, OR "<switch-verb> проект <name>"
    proj_idx = next((k for k, w in enumerate(words) if w.startswith("проект")), None)
    if proj_idx is not None:
        tail = " ".join(words[proj_idx + 1:])
        before = words[:proj_idx]
        is_bare = not before
        has_verb = any(w in _SWITCH_VERBS for w in before)
        if is_bare and not tail:
            return ("project_list", None)
        if tail and (is_bare or has_verb):
            return ("project", tail)
        # "расскажи про проект X" -> a question, falls through to Claude
        return None

    # mode: bare "режим <name>" at the start, OR "<verb> режим <name>"
    rez_idx = next((k for k, w in enumerate(words) if w.startswith("режим")), None)
    if rez_idx is not None:
        before = words[:rez_idx]
        is_bare = not before
        has_verb = any(w in _MODE_VERBS for w in before)
        if (is_bare or has_verb) and rez_idx + 1 < len(words):
            w1 = words[rez_idx + 1]
            m = _match_mode_word(w1)
            if m is None and rez_idx + 2 < len(words):
                m = _match_mode_word(w1 + " " + words[rez_idx + 2])  # "только чтение"
            if m:
                return ("mode", m)
        return None  # "объясни режим …" / "какой режим" -> question -> Claude

    # resume BEFORE pause (both may contain "пауз")
    if (any(w.startswith("возобнов") or w.startswith("продолж") or w == "резюме" for w in words)
            or "сними паузу" in s or "отмени паузу" in s or "снять паузу" in s):
        return ("resume", None)
    if ("на паузу" in s or "на паузе" in s or "приостанов" in s
            or "поставь паузу" in s or "включи паузу" in s or "поставь на пауз" in s):
        return ("pause", None)

    # voice replies on/off
    has_voice = any(w in ("голосовые", "голосовым", "голосовых", "голосовая", "голосовой")
                    for w in words) or "отвечай голосом" in s or "отвечать голосом" in s
    if has_voice or "только текст" in s:
        if "только текст" in s or any(w in ("выкл", "off", "текст", "текстом", "только") for w in words):
            return ("voice", False)
        if any(w in ("вкл", "on", "всегда") for w in words) or "всегда голосом" in s:
            return ("voice", True)
        return ("voice_status", None)

    # new dialog
    if ("новый диалог" in s or "новая сессия" in s or "новый чат" in s
            or "забудь контекст" in s or "сбрось контекст" in s or "начни заново" in s):
        return ("new", None)

    # browse dictations: "<show-verb> записи/диктовки" -> open /note browse
    has_notes = any(w.startswith("запис") or w.startswith("диктов") for w in words)
    if has_notes and any(w in _SHOW_VERBS for w in words):
        return ("browse_notes", None)

    # cancel
    if (any(w in ("отмена", "отмени", "отменить", "стоп", "прервать", "прервано") for w in words)
            or "отмени запрос" in s or "останови запрос" in s):
        return ("cancel", None)

    # status
    if ("статус" in words or "состояние" in words
            or "что происходит" in s or "чем занят" in s):
        return ("status", None)

    return None


def _fuzzy_projects(query: str) -> list[str]:
    """Projects whose normalized name matches `query` (exact > substring > token)."""
    q = _norm_cmd(query)
    names = [p.name for p in SETTINGS.projects]
    if not q or not names:
        return []
    exact = [n for n in names if _norm_cmd(n) == q]
    if exact:
        return exact
    substr = [n for n in names if q in _norm_cmd(n) or _norm_cmd(n) in q]
    if substr:
        return substr
    qw = set(q.split())
    return [n for n in names if qw & set(_norm_cmd(n).split())]


def _do_pause(chat_id: int, on: bool) -> str:
    if on:
        STATE.set_pause(chat_id, True)
        return ("⏸ Обработка запросов к Claude приостановлена.\n"
                "Идущий запрос (если есть) доработает; новые — до «продолжи».")
    if not STATE.get_pause(chat_id):
        return "✅ Паузы нет — Claude уже работает."
    STATE.set_pause(chat_id, False)
    return "▶️ Обработка запросов к Claude возобновлена."


def _set_voice_mode(chat_id: int, on: bool) -> str:
    STATE.set_voice(chat_id, on)
    if on:
        return "🔊 Голосовые ответы ВКЛ — теперь каждый ответ приходит и голосом."
    return "🔇 Голосовые ответы ВЫКЛ — ответы только текстом."


def _do_new_dialog(chat_id: int) -> str:
    proj = STATE.project_for_chat(chat_id)
    STATE.clear_session(proj.name)
    return f"🆕 Новый диалог для проекта {proj.name}."


def _switch_project(chat_id: int, name: str) -> str:
    candidates = _fuzzy_projects(name)
    if len(candidates) == 1:
        proj = STATE.switch_project(chat_id, candidates[0])
        return f"📂 Выбран проект {proj.name}."
    if not candidates:
        avail = ", ".join(p.name for p in SETTINGS.projects) or "(нет)"
        return f"Нет проекта, похожего на «{name}». Доступно: {avail}"
    return ("Несколько проектов подходят — уточните:\n"
            + "\n".join(f"• {c}" for c in candidates))


def _set_mode(chat_id: int, mode: str, confirm: bool) -> tuple[str, bool]:
    """Return (reply_text, needs_full_confirm_button)."""
    if mode not in ("balanced", "full", "strict"):
        return ("Режим должен быть: balanced, full или strict", False)
    if mode == "full" and not confirm:
        return ("⚠️ *full* отключает ВСЕ проверки прав — Claude сможет выполнять любые команды. "
                "Подтвердите кнопкой ниже.", True)
    STATE.set_mode(chat_id, mode)
    return (f"Режим: *{mode}*", False)


def _do_cancel(chat_id: int) -> str:
    proj = STATE.project_for_chat(chat_id)
    task = STATE.get_running(proj.name)
    if task and task.proc is not None:
        task.cancelled = True
        _kill_proc_tree(task.proc)
        return "🛑 Отменяю текущий запрос к Claude…"
    return "Сейчас ничего не выполняется."


def _status_text(chat_id: int) -> str:
    proj = STATE.project_for_chat(chat_id)
    mode = STATE.get_mode(chat_id)
    sid = STATE.get_session(proj.name)
    running = STATE.get_running(proj.name)
    sid_str = (sid[:8] + "…") if sid else "нет"
    lines = [
        f"📂 Проект: {proj.name}",
        f"⚙️ Режим: {mode}",
        f"⏸ Пауза: {'да' if STATE.get_pause(chat_id) else 'нет'}",
        f"🧠 Сессия: {sid_str}",
        f"▶️ Выполняется: {'да' if (running and running.proc is not None) else 'нет'}",
    ]
    return "\n".join(lines)


async def _dispatch_voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  intent: str, args: object) -> None:
    """Execute a matched voice command locally (no Claude)."""
    chat_id = update.effective_chat.id
    bot = context.bot
    if intent == "pause":
        await bot.send_message(chat_id=chat_id, text=_do_pause(chat_id, True),
                               parse_mode=ParseMode.MARKDOWN)
    elif intent == "resume":
        await bot.send_message(chat_id=chat_id, text=_do_pause(chat_id, False),
                               parse_mode=ParseMode.MARKDOWN)
    elif intent == "voice":
        await bot.send_message(chat_id=chat_id, text=_set_voice_mode(chat_id, bool(args)))
    elif intent == "voice_status":
        cur = STATE.get_voice(chat_id)
        cur_str = "ВКЛ" if cur else "ВЫКЛ"
        await bot.send_message(chat_id=chat_id,
                               text=f"🔊 Голосовые ответы: {cur_str}. (скажите «голосовые вкл/выкл»)")
    elif intent == "new":
        await bot.send_message(chat_id=chat_id, text=_do_new_dialog(chat_id))
    elif intent == "project":
        await bot.send_message(chat_id=chat_id, text=_switch_project(chat_id, str(args)))
    elif intent == "project_list":
        cur = STATE.current.get(chat_id)
        lines = ["*Проекты:*"]
        for p in SETTINGS.projects:
            mark = "✅ " if p.name == cur else "   "
            lines.append(f"{mark}`{p.name}`")
        await bot.send_message(chat_id=chat_id, text="\n".join(lines),
                               parse_mode=ParseMode.MARKDOWN)
    elif intent == "mode":
        text, pending = _set_mode(chat_id, str(args), confirm=False)
        if pending:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Включить full", callback_data="mc:full"),
                InlineKeyboardButton("❌ Отмена", callback_data="mc:no"),
            ]])
            await bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    elif intent == "cancel":
        await bot.send_message(chat_id=chat_id, text=_do_cancel(chat_id))
    elif intent == "status":
        await bot.send_message(chat_id=chat_id, text=_status_text(chat_id))
    elif intent == "browse_notes":
        # voice-triggered /note browse: same inline-button navigator
        await _note_browse_start(update, context)


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """mc:full / mc:no — confirm or decline voice-triggered `/mode full`."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = (query.data or "")[3:]
    bot = context.bot
    if data == "full":
        STATE.set_mode(chat_id, "full")
        msg = "Режим: *full* (подтверждено)."
        try:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    else:
        try:
            await query.edit_message_text("full НЕ включён — режим без изменений.")
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id=chat_id, text="full НЕ включён — режим без изменений.")
    await query.answer()


async def voice_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """vc:send|edit|cancel — the ✅/✏️/🗑 buttons under a recognized voice prompt."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = (query.data or "")[3:]
    bot = context.bot

    if data == "cancel":
        STATE.set_pending_voice(chat_id, None)
        STATE.set_await_edit(chat_id, False)
        try:
            await query.edit_message_text("🗑 Запрос отменён.")
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id=chat_id, text="🗑 Запрос отменён.")
        await query.answer()
        return

    pending = STATE.get_pending_voice(chat_id)
    if pending is None:
        await query.answer("Запрос уже не активен.", show_alert=True)
        return
    prompt, speak_reply = pending

    if data == "send":
        STATE.set_pending_voice(chat_id, None)
        await query.answer("Отправляю…")
        try:
            await query.delete_message()
        except Exception:  # noqa: BLE001
            pass
        await _do_claude(update, context, prompt, is_task=True, speak_reply=speak_reply)
        return

    if data == "edit":
        STATE.set_await_edit(chat_id, True)
        STATE.set_pending_voice(chat_id, None)
        try:
            await query.edit_message_text(
                "✏️ Пришлите исправленный текст следующим сообщением — "
                "он заменит распознанный и уйдёт в Claude.")
        except Exception:  # noqa: BLE001
            pass
        await query.answer()
        return

    await query.answer()


def _is_draft_send_marker(text: str) -> bool:
    """True if the utterance is (essentially) just a flush cue for the voice draft."""
    s = _norm_cmd(text)
    return s.startswith("отправ") or s in ("готово", "всё", "все готово", "вот и всё")


async def _dispatch_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           prompt: str, speak_reply: bool, note_msg_id: int) -> None:
    """Route a finished voice/text prompt to Claude through the /confirm gate.

    Shared by cmd_voice (single utterance) and the /draft flush (glued fragments).
    """
    chat_id = update.effective_chat.id
    bot = context.bot
    if STATE.get_confirm(chat_id):
        if STATE.get_pending_voice(chat_id) is not None:
            await bot.send_message(
                chat_id=chat_id,
                text="ℹ️ Предыдущий неподтверждённый голосовой запрос заменён новым.",
            )
        STATE.set_pending_voice(chat_id, (prompt, speak_reply))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="vc:send"),
            InlineKeyboardButton("✏️ Править", callback_data="vc:edit"),
            InlineKeyboardButton("🗑 Отмена", callback_data="vc:cancel"),
        ]])
        await bot.edit_message_text(
            chat_id=chat_id, message_id=note_msg_id,
            text=f"🎙 Распознано:\n\n{prompt[:1500]}\n\nПодтвердите отправку Claude:",
            reply_markup=kb,
        )
        return
    await bot.edit_message_text(
        chat_id=chat_id, message_id=note_msg_id,
        text=f"🎙 Распознано: {prompt[:800]}",
    )
    await _do_claude(update, context, prompt, is_task=True, speak_reply=speak_reply)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """dr:send|clear — the 📤/🗑 buttons under an accumulating voice draft.

    dr:send goes straight to Claude (the tap IS the confirmation; the draft
    message is deleted, so the /confirm gate has no message to edit).
    """
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = (query.data or "")[3:]
    bot = context.bot
    if data == "clear":
        STATE.clear_draft(chat_id)
        try:
            await query.edit_message_text("🗑 Черновик очищен.")
        except Exception:  # noqa: BLE001
            await bot.send_message(chat_id=chat_id, text="🗑 Черновик очищен.")
        await query.answer()
        return
    if data == "send":
        draft = STATE.get_draft(chat_id)
        if not draft:
            await query.answer("Черновик пуст.", show_alert=True)
            return
        joined = "\n\n".join(draft)
        STATE.clear_draft(chat_id)
        await query.answer("Отправляю…")
        try:
            await query.delete_message()
        except Exception:  # noqa: BLE001
            pass
        await _do_claude(update, context, joined, is_task=True,
                         speak_reply=STATE.get_voice(chat_id))
        return
    await query.answer()


# ---- handlers ---------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_help(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text=HELP, parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_claude(update, context, _text_after_command(update.message.text), is_task=False)


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _do_claude(update, context, _text_after_command(update.message.text), is_task=True)


async def cmd_freetext(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # ✏️ /config value input: when a field edit is pending, the next text message
    # becomes the new value (validated + applied + persisted). Lives BEFORE the pause
    # gate so config editing works even while Claude is paused (like /note).
    pending = STATE.get_config_pending(chat_id)
    if pending:
        STATE.set_config_pending(chat_id, None)
        await _config_consume_value(update, context, pending)
        return
    if STATE.get_pause(chat_id):
        return  # paused: silently ignore free text — only slash-commands are answered
    # 🔊 reply-voice: when on, replying to a bot message speaks THAT message.
    if STATE.get_reply_voice(chat_id):
        rt = update.message.reply_to_message
        if rt is not None:
            txt = STATE.get_bot_text(chat_id, rt.message_id)
            if txt is not None:
                await _speak_answer(update, context, txt)
                return
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔇 Это сообщение уже не в кэше (старое) — ответьте на более свежее.",
            )
            return
    # ✏️ edit-replacement: if the user clicked "Править" on a voice-confirm
    # prompt, their next text message REPLACES the recognized prompt (not a new
    # task). Stays under the pause gate (you can't edit-confirm while paused).
    if STATE.get_await_edit(chat_id):
        STATE.set_await_edit(chat_id, False)
        text = (update.message.text or "").strip()
        if text:
            await _do_claude(update, context, text, is_task=True)
        return
    text = (update.message.text or "").strip()
    wants_voice, cleaned = _extract_voice_request(text)
    if wants_voice:
        if not cleaned:
            # bare "ответь голосом" with no question -> read back the last answer
            await cmd_speak(update, context)
            return
        await _do_claude(update, context, cleaned, is_task=True, speak_reply=True)
        return
    await _do_claude(update, context, text, is_task=True)


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a voice message.

    Order: dictation (/note) -> voice-control command (no Claude, works while
    paused) -> pause gate -> «ответь голосом» -> /confirm gate -> Claude task.
    """
    chat_id = update.effective_chat.id
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    if STATE.get_note_mode(chat_id):
        # Dictation: voice -> file, no Claude. Ignores the pause gate (it never calls Claude).
        await _run_dictation(update, context, voice)
        return
    # 🔊 reply-voice: a voice reply on a bot message speaks THAT message (no STT).
    if STATE.get_reply_voice(chat_id):
        rt = update.message.reply_to_message
        if rt is not None:
            txt = STATE.get_bot_text(chat_id, rt.message_id)
            if txt is not None:
                await _speak_answer(update, context, txt)
                return
    bot = context.bot

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    note = await bot.send_message(chat_id=chat_id, text=f"🎙 Распознаю голос ({SETTINGS.stt_provider})…")

    # download the ogg to a temp file
    fd, tmp_path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        try:
            tg_file = await bot.get_file(voice.file_id)
            await tg_file.download_to_drive(tmp_path)
        except Exception as e:  # noqa: BLE001
            log.warning("voice download failed: %s", e)
            await bot.send_message(chat_id=chat_id, text=f"Не удалось скачать голосовое: {e}")
            return

        try:
            text = await STT.transcribe(tmp_path, SETTINGS)
        except Exception as e:  # noqa: BLE001
            log.warning("transcribe failed: %s", e)
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Распознавание не удалось: {e}")
            return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    text = (text or "").strip()
    if not text:
        await bot.send_message(chat_id=chat_id, text="🎙 Не удалось распознать речь (пустой результат).")
        return

    # Voice-control interception — executed locally WITHOUT Claude, and BEFORE
    # the pause gate so it also works while Claude is paused («продолжи»).
    vcmd = _match_voice_command(text)
    if vcmd:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=note.message_id)
        except Exception:  # noqa: BLE001
            pass
        await _dispatch_voice_command(update, context, vcmd[0], vcmd[1])
        return

    # Non-command voice while paused: silently ignored (the transcribe above
    # only happened so voice-control could fire).
    if STATE.get_pause(chat_id):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=note.message_id)
        except Exception:  # noqa: BLE001
            pass
        return

    # Вырезаем запрос «ответь голосом» ДО отправки Claude — иначе модель видит
    # его и отказывается, ссылаясь на неспособность выдать аудио (озвучкой сам
    # занимается бот через speak.py). Та же логика, что в cmd_freetext.
    wants_voice, cleaned = _extract_voice_request(text)

    if wants_voice and not cleaned:
        # одно лишь «ответь голосом» без вопроса -> озвучить последний ответ
        try:
            await bot.delete_message(chat_id=chat_id, message_id=note.message_id)
        except Exception:  # noqa: BLE001
            pass
        await cmd_speak(update, context)
        return

    prompt = cleaned if wants_voice else text

    # /draft: accumulate voice fragments until a send-marker («отправляй»/
    # «готово») or the 📤 button, then flush as one Claude request.
    if STATE.get_draft_mode(chat_id):
        draft = STATE.get_draft(chat_id)
        if _is_draft_send_marker(text) and draft:
            joined = "\n\n".join(draft)
            STATE.clear_draft(chat_id)
            await _dispatch_prompt(update, context, joined,
                                   speak_reply=STATE.get_voice(chat_id),
                                   note_msg_id=note.message_id)
            return
        draft.append(prompt)
        STATE.set_draft(chat_id, draft)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📤 Отправить", callback_data="dr:send"),
            InlineKeyboardButton("🗑 Очистить", callback_data="dr:clear"),
        ]])
        preview = "\n\n".join(draft)[:1200]
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=note.message_id,
                text=(f"📝 Черновик ({len(draft)} фрагм.):\n\n{preview}\n\n"
                      f"Скажите «отправляй»/«готово» или нажмите 📤."),
                reply_markup=kb,
            )
        except Exception:  # noqa: BLE001
            pass
        return

    await _dispatch_prompt(update, context, prompt,
                           speak_reply=wants_voice, note_msg_id=note.message_id)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    proj = STATE.project_for_chat(chat_id)
    STATE.clear_session(proj.name)
    await context.bot.send_message(
        chat_id=chat_id, text=f"🆕 Новый диалог для проекта {proj.name}.",
    )


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    proj = STATE.project_for_chat(chat_id)
    rc, out = await _git(proj.path, ["diff"])
    if not out.strip():
        rc2, stat = await _git(proj.path, ["status", "-sb"])
        out = "Нет незакоммиченных изменений в tracked-файлах.\n\n" + stat
    await M.reply_long(context.bot, chat_id, out or "(пусто)", render_markdown=False,
                       file_threshold=SETTINGS.long_output_threshold)


async def cmd_git(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        await context.bot.send_message(chat_id=chat_id, text="Использование: /git status|log|diff|commit <msg>")
        return
    proj = STATE.project_for_chat(chat_id)
    sub = args[0].lower()
    if sub == "status":
        rc, out = await _git(proj.path, ["status", "-sb"])
    elif sub == "log":
        rc, out = await _git(proj.path, ["log", "--oneline", "-n", "20"])
    elif sub == "diff":
        rc, out = await _git(proj.path, ["diff"])
    elif sub == "commit":
        msg = " ".join(args[1:]).strip()
        if not msg:
            await context.bot.send_message(chat_id=chat_id, text="/git commit <сообщение коммита>")
            return
        await _git(proj.path, ["add", "-A"])
        rc, out = await _git(proj.path, ["commit", "-m", msg])
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"Неизвестная git-команда: {sub}")
        return
    await M.reply_long(context.bot, chat_id, out or "(без вывода)", render_markdown=False,
                       file_threshold=SETTINGS.long_output_threshold)


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        cur = STATE.current.get(chat_id)
        # Plain text — p.path has '_' (breaks Markdown), same as /status.
        lines = ["Проекты:"]
        for p in SETTINGS.projects:
            mark = "✅ " if p.name == cur else "   "
            lines.append(f"{mark}{p.name} → {p.path}")
        lines.append("\nПереключение: /project <имя> · добавить: /project add <путь>")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
        return
    if args[0].lower() == "add":
        path = " ".join(args[1:]).strip().strip('"')
        name = path.rstrip("\\/").split("\\")[-1].split("/")[-1] or "project"
        try:
            proj = STATE.add_project(name, path)
        except (KeyError, ValueError, FileNotFoundError) as e:
            await context.bot.send_message(chat_id=chat_id, text=f"Не удалось добавить: {e}")
            return
        STATE.current[chat_id] = proj.name
        await context.bot.send_message(chat_id=chat_id, text=f"➕ Добавлен и выбран проект {proj.name}.")
        return
    name = args[0]
    try:
        proj = STATE.switch_project(chat_id, name)
    except KeyError:
        await context.bot.send_message(chat_id=chat_id, text=f"Нет такого проекта: {name}")
        return
    await context.bot.send_message(chat_id=chat_id, text=f"📂 Выбран проект {proj.name}.")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        cur = STATE.get_mode(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"Текущий режим: *{cur}*\n\nДоступно: balanced (правка кода, блок rm -rf), "
                  f"full (полная свобода), strict (только чтение).\nСмена: /mode <режим>"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    mode = context.args[0].lower()
    if mode not in ("balanced", "full", "strict"):
        await context.bot.send_message(chat_id=chat_id, text="Режим должен быть: balanced, full или strict")
        return
    if mode == "full" and not (len(context.args) > 1 and context.args[1].lower() == "confirm"):
        await context.bot.send_message(
            chat_id=chat_id,
            text=("⚠️ *full* отключает ВСЕ проверки прав — Claude сможет выполнять любые команды. "
                  "Для подтверждения: /mode full confirm"),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    STATE.set_mode(chat_id, mode)
    await context.bot.send_message(chat_id=chat_id, text=f"Режим: *{mode}*", parse_mode=ParseMode.MARKDOWN)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    proj = STATE.project_for_chat(chat_id)
    task = STATE.get_running(proj.name)
    if task and task.proc is not None:
        task.cancelled = True
        _kill_proc_tree(task.proc)
        await context.bot.send_message(chat_id=chat_id, text="🛑 Отменяю текущий запрос к Claude…")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Сейчас ничего не выполняется.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    proj = STATE.project_for_chat(chat_id)
    mode = STATE.get_mode(chat_id)
    sid = STATE.get_session(proj.name)
    running = STATE.get_running(proj.name)
    cost = STATE.last_cost.get(proj.name)
    # Plain text — no Markdown: proj.path has '_' (C:\_AI\_Claude_Telegramm) which
    # Telegram's Markdown parser rejects as unmatched, so /status would get no reply.
    # (Same reason the server block below is sent without parse_mode.)
    lines = [
        f"📂 Проект: {proj.name}",
        f"📁 {proj.path}",
        f"⚙️ Режим: {mode}",
        f"⏸ Пауза: {'да (до /resume)' if STATE.get_pause(chat_id) else 'нет'}",
        f"🧠 Сессия: {sid[:8] + '…' if sid else 'нет (новый)'}",
        f"▶️ Выполняется: {'да' if (running and running.proc is not None) else 'нет'}",
        f"💲 Последний запрос: {('$%.4f' % cost) if cost is not None else '—'}",
    ]
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))
    # Server connectivity (sent separately, no Markdown — hosts/errors may contain _ or .)
    try:
        srv = health.render(await health.check_all(SETTINGS))
        await context.bot.send_message(chat_id=chat_id, text="\n".join(srv))
    except Exception as e:  # noqa: BLE001
        log.warning("health check in /status failed: %s", e)


async def cmd_speak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render the last Claude answer as a voice message."""
    chat_id = update.effective_chat.id
    text = STATE.last_answer.get(chat_id)
    if not text:
        await context.bot.send_message(chat_id=chat_id, text="Нет последнего ответа для озвучки. Сначала задайте вопрос.")
        return
    await _speak_answer(update, context, text)


async def cmd_voice_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle always-voice replies for this chat: /voice on|off."""
    chat_id = update.effective_chat.id
    if not context.args:
        cur = STATE.get_voice(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"🔊 Голосовые ответы: {'ВКЛ' if cur else 'ВЫКЛ'}.\n"
                  "/voice on — все ответы дублируются голосом\n/voice off — только текст"),
        )
        return
    val = context.args[0].lower()
    if val in ("on", "вкл", "1", "да", "yes", "true"):
        STATE.set_voice(chat_id, True)
        await context.bot.send_message(chat_id=chat_id, text="🔊 Голосовые ответы ВКЛ — теперь каждый ответ приходит и голосом.")
    elif val in ("off", "выкл", "0", "нет", "no", "false"):
        STATE.set_voice(chat_id, False)
        await context.bot.send_message(chat_id=chat_id, text="🔇 Голосовые ответы ВЫКЛ — ответы только текстом.")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Использование: /voice on или /voice off")


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle the ✅/✏️/🗑 confirmation gate on transcribed voice (/confirm on|off)."""
    chat_id = update.effective_chat.id
    if not context.args:
        cur = STATE.get_confirm(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"📝 Подтверждение распознанного: {'ВКЛ' if cur else 'ВЫКЛ'}.\n"
                  "/confirm on — показывать распознанный текст с кнопками перед отправкой Claude\n"
                  "/confirm off — отправлять голос в Claude сразу"),
        )
        return
    val = context.args[0].lower()
    if val in ("on", "вкл", "1", "да", "yes", "true"):
        STATE.set_confirm(chat_id, True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Подтверждение ВКЛ — распознанный текст будет показан с кнопками ✅/✏️/🗑 перед отправкой.",
        )
    elif val in ("off", "выкл", "0", "нет", "no", "false"):
        STATE.set_confirm(chat_id, False)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Подтверждение ВЫКЛ — распознанный голос отправляется в Claude сразу.",
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text="Использование: /confirm on или /confirm off")


async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice draft: accumulate transcribed fragments and flush them as one request.

    /draft            — status (+ preview)
    /draft on|off     — toggle accumulation (OFF keeps any saved fragments)
    /draft show       — print the accumulated draft
    /draft clear      — drop the draft
    """
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        on = STATE.get_draft_mode(chat_id)
        draft = STATE.get_draft(chat_id)
        preview = ("\n\n".join(draft)[:600]) if draft else "(пусто)"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"📝 Черновик голосовых: {'ВКЛ' if on else 'ВЫКЛ'}.\n"
                  f"Фрагментов: {len(draft)}\n\n{preview}\n\n"
                  f"/draft on|off · /draft show · /draft clear\n"
                  f"Пока вкл — голос копится; скажите «отправляй»/«готово» или жмите 📤."),
        )
        return
    sub = args[0].lower()
    if sub in ("on", "вкл", "1", "да", "yes", "true"):
        STATE.set_draft_mode(chat_id, True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Черновик ВКЛ — голосовые копятся, пока не скажете «отправляй»/«готово» или не нажмёте 📤.",
        )
    elif sub in ("off", "выкл", "0", "нет", "no", "false"):
        STATE.set_draft_mode(chat_id, False)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Черновик ВЫКЛ — каждое голосовое снова уходит отдельным запросом. "
                 "(Накопленные фрагменты сохранены — /draft clear чтобы стереть.)",
        )
    elif sub in ("show", "показать", "покажи"):
        draft = STATE.get_draft(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Черновик:\n\n" + (("\n\n".join(draft))[:3000] if draft else "(пусто)"),
        )
    elif sub in ("clear", "сбросить", "очистить", "стереть"):
        STATE.clear_draft(chat_id)
        await context.bot.send_message(chat_id=chat_id, text="🗑 Черновик очищен.")
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Использование: /draft · /draft on|off · /draft show · /draft clear",
        )


async def cmd_reply_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle reply-voice: when ON, replying to a bot message speaks THAT message.

    Default OFF (opt-in, as requested). Works for text and voice replies; the
    target must still be in the in-memory bot-text cache (~50 recent answers).
    """
    chat_id = update.effective_chat.id
    if not context.args:
        cur = STATE.get_reply_voice(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"🔊 Reply-озвучка: {'ВКЛ' if cur else 'ВЫКЛ'}.\n"
                  "Когда вкл — ответ (reply) на сообщение бота озвучивает именно его "
                  "(работает и текстом, и голосовым).\n/reply_voice on|off"),
        )
        return
    val = context.args[0].lower()
    if val in ("on", "вкл", "1", "да", "yes", "true"):
        STATE.set_reply_voice(chat_id, True)
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔊 Reply-озвучка ВКЛ — ответьте (reply) на любое сообщение бота, чтобы озвучить его.",
        )
    elif val in ("off", "выкл", "0", "нет", "no", "false"):
        STATE.set_reply_voice(chat_id, False)
        await context.bot.send_message(chat_id=chat_id, text="🔇 Reply-озвучка ВЫКЛ.")
    else:
        await context.bot.send_message(chat_id=chat_id, text="Использование: /reply_voice on или /reply_voice off")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pause Claude processing for this chat: gate /ask, /task and voice.

    An already-running request is allowed to finish; only NEW requests are blocked
    (the gate is checked in _do_claude before the claude lock is acquired). State is
    in-memory only -> a bot restart always resumes (safe default).
    """
    chat_id = update.effective_chat.id
    STATE.set_pause(chat_id, True)
    await context.bot.send_message(
        chat_id=chat_id,
        text=("⏸ Обработка запросов к Claude приостановлена.\n"
              "Идущий запрос (если есть) доработает; новые — до /resume."),
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resume Claude processing for this chat (clears the /pause gate)."""
    chat_id = update.effective_chat.id
    if not STATE.get_pause(chat_id):
        await context.bot.send_message(chat_id=chat_id, text="✅ Паузы нет — Claude уже работает.")
        return
    STATE.set_pause(chat_id, False)
    await context.bot.send_message(chat_id=chat_id, text="▶️ Обработка запросов к Claude возобновлена.")


# ---- /note: dictation (voice -> file, no Claude) ----------------------------

async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dictation mode: transcribed voice is appended to a daily .md journal, no Claude.

    /note              — status
    /note on|off       — toggle dictation (while ON, all voice messages go to file)
    /note folder       — show current folder + existing folders
    /note folder <nm>  — switch/create category folder
    /note browse       — inline buttons to read past dictations
    """
    chat_id = update.effective_chat.id
    bot = context.bot
    args = context.args

    if not args:
        # /note with no args = TOGGLE dictation on/off
        new_on = not STATE.get_note_mode(chat_id)
        STATE.set_note_mode(chat_id, new_on)
        folder = STATE.get_note_folder(chat_id)
        if new_on:
            await bot.send_message(
                chat_id=chat_id,
                text=(f"📝 Диктовка ВКЛ.\n"
                      f"Голосовые теперь пишутся в dictations/{folder}/ (без Claude).\n"
                      f"/note — выключить · /note folder <имя> · /note browse — листать и читать."),
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text="🎙 Диктовка ВЫКЛ — голос снова идёт в Claude.",
            )
        return

    sub = args[0].lower()

    if sub in ("on", "вкл", "1", "да", "yes", "true"):
        STATE.set_note_mode(chat_id, True)
        folder = STATE.get_note_folder(chat_id)
        await bot.send_message(
            chat_id=chat_id,
            text=(f"📝 Диктовка ВКЛ.\n"
                  f"Голосовые теперь пишутся в dictations/{folder}/ (без Claude).\n"
                  f"/note off — вернуть голос в Claude."),
        )
        return

    if sub in ("off", "выкл", "0", "нет", "no", "false"):
        STATE.set_note_mode(chat_id, False)
        await bot.send_message(chat_id=chat_id, text="🎙 Диктовка ВЫКЛ — голос снова идёт в Claude.")
        return

    if sub in ("folder", "папка", "dir"):
        if len(args) < 2:
            folder = STATE.get_note_folder(chat_id)
            folders = _list_folders()
            lines = [f"Текущая папка: {folder}", "", "Папки:"]
            for f in folders:
                lines.append(f"{'✅ ' if f == folder else '   '}{f}")
            lines.append("\nСменить: /note folder <имя>")
            await bot.send_message(chat_id=chat_id, text="\n".join(lines))
            return
        safe = _safe_folder(" ".join(args[1:]))
        if not safe:
            await bot.send_message(
                chat_id=chat_id,
                text="Имя папки пустое или содержит недопустимые символы. "
                     "Допустимы буквы, цифры, пробел, _ и -.",
            )
            return
        STATE.set_note_folder(chat_id, safe)
        await bot.send_message(
            chat_id=chat_id,
            text=f"📂 Папка диктовок: {safe}.\nСоздастся при первой записи (dictations/{safe}/).",
        )
        return

    if sub in ("browse", "list", "ls", "список", "читать", "read"):
        await _note_browse_start(update, context)
        return

    await bot.send_message(
        chat_id=chat_id,
        text="Использование: /note · /note on|off · /note folder [имя] · /note browse",
    )


def _folders_kb(folders: list[str]) -> InlineKeyboardMarkup:
    """Build the folder-selection keyboard (callback nb:f:<i>)."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, name in enumerate(folders):
        row.append(InlineKeyboardButton(name, callback_data=f"nb:f:{i}"))
        if len(row) >= 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _edit_or_send(query, bot, chat_id: int, text: str, reply_markup) -> int:
    """Update the browsed message in place; if editing fails (e.g. older than 48h), send new.

    Returns the message_id the content ended up on (the edited message, or the fresh one).
    """
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
        return query.message.message_id
    except BadRequest:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        return sent.message_id


async def _note_browse_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initial /note browse: send a fresh message with folder buttons."""
    chat_id = update.effective_chat.id
    bc = STATE.get_browse(chat_id)
    bc.folders = _list_folders()
    text = "📂 Папки диктовок — нажмите нужную папку:"
    await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=_folders_kb(bc.folders),
    )


async def _show_folders(query, bot, chat_id: int) -> None:
    """nb:root — redraw folder buttons on the current message."""
    bc = STATE.get_browse(chat_id)
    bc.folders = _list_folders()
    await _edit_or_send(query, bot, chat_id, "📂 Папки диктовок — нажмите нужную папку:", _folders_kb(bc.folders))
    await query.answer()


async def _show_files(query, bot, chat_id: int, folder: str) -> None:
    """nb:f:<i> — list date-buttons for <folder>; remember folder+files in the browse cache."""
    bc = STATE.get_browse(chat_id)
    bc.folder = folder
    bc.files = _list_files(folder)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(date, callback_data=f"nb:r:{i}")] for i, date in enumerate(bc.files)
    ]
    rows.append([InlineKeyboardButton("← папки", callback_data="nb:root")])
    text = (f"📂 {folder} — {len(bc.files)} файл(ов). Нажмите дату:"
            if bc.files else f"📂 {folder} — записей ещё нет.")
    await _edit_or_send(query, bot, chat_id, text, InlineKeyboardMarkup(rows))
    await query.answer()


async def _show_file_menu(query, bot, chat_id: int, idx: int) -> None:
    """nb:r:<i> — open a choice menu for one file: view text / download .md / back."""
    bc = STATE.get_browse(chat_id)
    if not (0 <= idx < len(bc.files)):
        await query.answer("Файл не найден (список изменился) — откройте /note browse заново.", show_alert=True)
        return
    date = bc.files[idx]
    folder = bc.folder
    try:
        path = _dictation_dir(folder) / f"{date}.md"
    except ValueError:
        await query.answer("Папка недопустима.", show_alert=True)
        return
    if not path.is_file():
        await query.answer("Файл не найден на диске.", show_alert=True)
        return
    text = f"📄 {folder}/{date}\n\nЧто сделать?"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Просмотреть", callback_data=f"nb:v:{idx}"),
         InlineKeyboardButton("📄 Скачать .md", callback_data=f"nb:d:{idx}")],
        [InlineKeyboardButton("← назад", callback_data="nb:back")],
    ])
    await _edit_or_send(query, bot, chat_id, text, kb)
    await query.answer()


async def _show_file(query, bot, chat_id: int, idx: int, download_md: bool = False) -> None:
    """nb:v:<i> (view text) or nb:d:<i> (download .md). Indexes into the browse cache."""
    bc = STATE.get_browse(chat_id)
    if not (0 <= idx < len(bc.files)):
        await query.answer("Файл не найден (список изменился) — откройте /note browse заново.", show_alert=True)
        return
    date = bc.files[idx]
    folder = bc.folder
    try:
        path = _dictation_dir(folder) / f"{date}.md"
    except ValueError:
        await query.answer("Папка недопустима.", show_alert=True)
        return
    if not path.is_file():
        await query.answer("Файл не найден на диске.", show_alert=True)
        return

    if download_md:
        await query.answer("Отправляю .md…")
        try:
            with open(path, "rb") as fh:
                await bot.send_document(
                    chat_id=chat_id, document=InputFile(fh, filename=f"{date}.md"),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("send_document failed: %s", e)
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось отправить .md: {e}")
        return

    def _read() -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    try:
        content = await asyncio.get_running_loop().run_in_executor(None, _read)
    except Exception as e:  # noqa: BLE001
        log.warning("read dictation failed: %s", e)
        await query.answer("Не удалось прочитать файл.", show_alert=True)
        return

    await query.answer()
    await M.reply_long(bot, chat_id, content or "(пусто)", footer=f"📂 {folder}/{date}")
    # "Back" returns to the file list of the current folder (bc.folder).
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("← назад к списку", callback_data="nb:back")]])
    await bot.send_message(chat_id=chat_id, text=".", reply_markup=kb)


async def note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single CallbackQueryHandler for /note browse buttons (callback_data `nb:<op>[:i]`).

    Indexes reference the in-memory BrowseCache, so real folder/file names never travel
    through Telegram. A stale button (user navigated elsewhere) yields an out-of-range index
    -> a friendly alert, never an arbitrary file.
    """
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = query.data or ""
    parts = data.split(":")
    op = parts[1] if len(parts) > 1 else ""

    bot = context.bot
    try:
        if op == "root":
            await _show_folders(query, bot, chat_id)
        elif op == "f":
            bc = STATE.get_browse(chat_id)
            i = int(parts[2])
            if not (0 <= i < len(bc.folders)):
                await query.answer("Папка не найдена — /note browse.", show_alert=True)
                return
            await _show_files(query, bot, chat_id, bc.folders[i])
        elif op == "r":
            await _show_file_menu(query, bot, chat_id, int(parts[2]))
        elif op == "v":
            await _show_file(query, bot, chat_id, int(parts[2]), download_md=False)
        elif op == "d":
            await _show_file(query, bot, chat_id, int(parts[2]), download_md=True)
        elif op == "back":
            bc = STATE.get_browse(chat_id)
            await _show_files(query, bot, chat_id, bc.folder)
        else:
            await query.answer()
    except (ValueError, IndexError):
        await query.answer("Кнопка устарела — откройте /note browse.", show_alert=True)
    except Exception as e:  # noqa: BLE001
        log.warning("note_callback error: %s", e)
        try:
            await query.answer("Ошибка обработки кнопки.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass


# ---- pagination of multi-page Claude answers (callback_data `pg:<i>`) --------

async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """◀️/▶️ navigation for a paginated answer. Indexes into State.page_cache[chat_id].pages."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = (query.data or "")[3:]  # strip "pg:"
    bot = context.bot
    if data == "info":
        await query.answer()
        return
    pc = STATE.page_cache.get(chat_id)
    if not pc:
        await query.answer("Страница устарела (ответ уже не активен).", show_alert=True)
        return
    try:
        i = int(data)
    except ValueError:
        await query.answer()
        return
    if not (0 <= i < len(pc.pages)):
        await query.answer("Страница не найдена.", show_alert=True)
        return
    pc.index = i
    kb = M._page_kb(i, len(pc.pages))
    try:
        await query.edit_message_text(
            text=pc.pages[i], parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=kb,
        )
    except BadRequest:
        # broken markup in this page -> retry as plain text
        try:
            await query.edit_message_text(
                text=M._strip_tags(pc.pages[i]),
                disable_web_page_preview=True, reply_markup=kb,
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        # "message is not modified", network blip, etc — not fatal
        pass
    await query.answer()


# ---- /config: edit bot settings via inline buttons (callback_data `cf:`) --------
#
# A single CONFIG_FIELDS registry drives rendering, validation, application and
# persistence for all editable settings. enum/mode_perm fields are set by tapping a
# choice; scalar/secret fields prompt for the next text message (State.config_pending).
# Non-secret edits mutate SETTINGS in memory (= live, the object is shared by reference)
# and persist via config.save(); secret edits go to .env via dotenv.set_key and require
# `/config unlock <password>`. Secrets are shown in full when unlocked (owner's choice)
# and are NEVER logged. Removing one's own chat_id from allowed_user_ids is refused
# (it would lock the owner out — @authorized reads that set live).

from dataclasses import dataclass as _dc  # noqa: E402


@_dc
class FieldSpec:
    key: str            # short ASCII id used in callback_data (cf:f:<index>)
    group: str          # one of CONFIG_GROUPS
    label: str          # human label (ru)
    attr: str           # Settings attr, or "modes:<mode>:<sub>" for mode flags
    kind: str           # enum|mode_perm|int|float|str|secret_str|id_list|tool_list
    live: bool          # True = applies immediately; False = needs restart (⚠️)
    secret: bool = False
    env: str | None = None        # if secret: .env var name (written via dotenv.set_key)
    yaml_path: tuple = ()         # if non-secret: config.yaml key path (e.g. ("tts","edge_voice"))
    choices: tuple = ()
    mn: float | None = None
    mx: float | None = None
    help: str = ""


_PERM_CHOICES = ("plan", "acceptEdits", "bypassPermissions", "default")
_LOCAL_MODELS = ("tiny", "base", "small", "medium", "large-v3")

CONFIG_GROUPS = ["Общее", "Режимы", "STT", "TTS", "Диктовки", "Секреты"]

CONFIG_FIELDS: list[FieldSpec] = [
    # --- Общее ---
    FieldSpec("mode", "Общее", "Режим по умолчанию", "default_mode", "enum", True,
              choices=VALID_MODES, yaml_path=("default_mode",),
              help="Пока чат не выбрал свой через /mode."),
    FieldSpec("budget", "Общее", "Бюджет на запрос ($)", "max_budget_usd", "float", True,
              mn=0, yaml_path=("max_budget_usd",),
              help="Передаётся в claude как --max-budget-usd."),
    FieldSpec("timeout", "Общее", "Таймаут (мин)", "timeout_minutes", "int", True,
              mn=1, yaml_path=("timeout_minutes",),
              help="Сколько ждать Claude до принудительной остановки."),
    FieldSpec("maxturns", "Общее", "Max turns", "max_turns", "int", True,
              mn=0, yaml_path=("max_turns",),
              help="Best-effort (CLI может игнорировать). 0 = без лимита."),
    FieldSpec("longout", "Общее", "Порог длинного вывода (зн.)", "long_output_threshold", "int", True,
              mn=100, yaml_path=("long_output_threshold_chars",),
              help="Длиннее этого — ответ уходит .txt-файлом."),
    FieldSpec("claudeexe", "Общее", "claude.exe (путь)", "claude_exe", "str", False,
              yaml_path=("claude_exe",),
              help="⚠️ Проверка PATH идёт при старте — нужно перезапустить бота."),
    # --- Режимы (modes.<name>.{permission_mode,deny_tools}) ---
    FieldSpec("m_balanced_perm", "Режимы", "balanced: permission_mode",
              "modes:balanced:permission_mode", "mode_perm", True,
              choices=_PERM_CHOICES, yaml_path=("modes", "balanced", "permission_mode")),
    FieldSpec("m_balanced_deny", "Режимы", "balanced: deny_tools",
              "modes:balanced:deny_tools", "tool_list", True,
              yaml_path=("modes", "balanced", "deny_tools"),
              help="Запрещённые инструменты. Пришлите новый список (по строке)."),
    FieldSpec("m_full_perm", "Режимы", "full: permission_mode",
              "modes:full:permission_mode", "mode_perm", True,
              choices=_PERM_CHOICES, yaml_path=("modes", "full", "permission_mode")),
    FieldSpec("m_full_deny", "Режимы", "full: deny_tools",
              "modes:full:deny_tools", "tool_list", True,
              yaml_path=("modes", "full", "deny_tools")),
    FieldSpec("m_strict_perm", "Режимы", "strict: permission_mode",
              "modes:strict:permission_mode", "mode_perm", True,
              choices=_PERM_CHOICES, yaml_path=("modes", "strict", "permission_mode")),
    FieldSpec("m_strict_deny", "Режимы", "strict: deny_tools",
              "modes:strict:deny_tools", "tool_list", True,
              yaml_path=("modes", "strict", "deny_tools")),
    # --- STT ---
    FieldSpec("sttprov", "STT", "Провайдер STT", "stt_provider", "enum", True,
              choices=("groq", "local"), yaml_path=("stt", "provider")),
    FieldSpec("sttgroqmodel", "STT", "Groq модель", "stt_groq_model", "str", True,
              yaml_path=("stt", "groq_model")),
    FieldSpec("sttlocalmodel", "STT", "Локальная модель", "stt_local_model", "enum", False,
              choices=_LOCAL_MODELS, yaml_path=("stt", "local_model"),
              help="⚠️ Модель кэшируется — нужен рестарт."),
    FieldSpec("sttlang", "STT", "Язык STT", "stt_language", "str", True,
              yaml_path=("stt", "language"), help="ISO-639-1 (ru, en…). Пусто = авто."),
    # --- TTS ---
    FieldSpec("ttsprov", "TTS", "Провайдер TTS", "tts_provider", "enum", True,
              choices=("edge", "silero"), yaml_path=("tts", "provider")),
    FieldSpec("ttsedge", "TTS", "Edge голос", "tts_edge_voice", "str", True,
              yaml_path=("tts", "edge_voice")),
    FieldSpec("ttssilero", "TTS", "Silero спикер", "tts_silero_speaker", "str", True,
              yaml_path=("tts", "silero_speaker")),
    FieldSpec("ttslang", "TTS", "Язык TTS", "tts_language", "str", True,
              yaml_path=("tts", "language")),
    FieldSpec("ttsrate", "TTS", "Скорость TTS", "tts_rate", "str", True,
              yaml_path=("tts", "rate"), help="Напр. +10% / -10%. Пусто = норма."),
    FieldSpec("ttsffmpeg", "TTS", "ffmpeg (путь)", "tts_ffmpeg_path", "str", True,
              yaml_path=("tts", "ffmpeg_path"), help="Пусто = авто-поиск."),
    # --- Диктовки ---
    FieldSpec("dictfolder", "Диктовки", "Папка по умолчанию", "dictations_default_folder", "str", True,
              yaml_path=("dictations", "default_folder")),
    FieldSpec("dictdir", "Диктовки", "Каталог диктовок", "dictations_dir", "str", True,
              yaml_path=("dictations", "dir"),
              help="⚠️ Существующие файлы не переносятся."),
    # --- Секреты (требуют разблокировки) ---
    FieldSpec("token", "Секреты", "Bot token", "token", "secret_str", False,
              secret=True, env="TELEGRAM_BOT_TOKEN",
              help="⚠️ Применится только после рестарта бота."),
    FieldSpec("groqkey", "Секреты", "Groq API key", "groq_api_key", "secret_str", True,
              secret=True, env="GROQ_API_KEY"),
    FieldSpec("allowed", "Секреты", "Allowed user IDs", "allowed_user_ids", "id_list", True,
              secret=True, env="ALLOWED_USER_IDS",
              help="ID через запятую/пробел. СВОЙ id удалять нельзя."),
]

CONFIG_FIELDS_BY_KEY: dict[str, FieldSpec] = {f.key: f for f in CONFIG_FIELDS}


def _group_index(group: str) -> int:
    try:
        return CONFIG_GROUPS.index(group)
    except ValueError:
        return 0


def _config_get(spec: FieldSpec):
    if spec.attr.startswith("modes:"):
        _, mode, sub = spec.attr.split(":")
        m = SETTINGS.modes.get(mode) or {}
        if sub == "permission_mode":
            return m.get("permission_mode", "plan")
        return list(m.get("deny_tools") or [])
    return getattr(SETTINGS, spec.attr)


def _config_set(spec: FieldSpec, value) -> None:
    if spec.attr.startswith("modes:"):
        _, mode, sub = spec.attr.split(":")
        m = SETTINGS.modes.setdefault(mode, {"permission_mode": "plan", "deny_tools": []})
        m[sub] = value
    else:
        setattr(SETTINGS, spec.attr, value)


def _config_range(v, spec: FieldSpec) -> None:
    if spec.mn is not None and v < spec.mn:
        raise ValueError(f"минимум {spec.mn:g}")
    if spec.mx is not None and v > spec.mx:
        raise ValueError(f"максимум {spec.mx:g}")


def _config_validate(spec: FieldSpec, raw: str):
    """Coerce + validate a raw text value. Returns the Python value or raises ValueError."""
    k = spec.kind
    if k in ("enum", "mode_perm"):
        if raw not in spec.choices:
            raise ValueError(f"допустимо: {', '.join(spec.choices)}")
        return raw
    if k == "int":
        v = int(raw)  # ValueError on garbage
        _config_range(v, spec)
        return v
    if k == "float":
        v = float(raw)
        _config_range(v, spec)
        return v
    if k in ("str", "secret_str"):
        return (raw or "").strip()
    if k == "id_list":
        ids: set[int] = set()
        for part in re.split(r"[,\s]+", (raw or "").strip()):
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                raise ValueError(f"не число: {part!r}")
        return ids
    if k == "tool_list":
        out, seen = [], set()
        for ln in (raw or "").splitlines():
            t = ln.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out
    raise ValueError(f"неподдерживаемый тип: {k}")


def _config_env_str(spec: FieldSpec, value) -> str:
    if spec.attr == "allowed_user_ids":
        return ",".join(str(i) for i in sorted(value))
    return str(value)


def _config_short(spec: FieldSpec, value, limit: int = 40) -> str:
    """Compact value caption for list buttons / toasts."""
    if spec.kind == "id_list":
        s = ", ".join(str(i) for i in sorted(value)) if value else "—"
    elif spec.kind == "tool_list":
        s = f"{len(value)} шт." if value else "нет"
    else:
        s = str(value) if value != "" else "(пусто)"
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _config_field_value_text(spec: FieldSpec, chat_id: int) -> str:
    """Full current value for the field-detail view (secrets shown only if unlocked)."""
    if spec.secret and not STATE.is_config_unlocked(chat_id):
        return "🔒 заблокировано — /config unlock <пароль>"
    v = _config_get(spec)
    if spec.kind == "id_list":
        return ", ".join(str(i) for i in sorted(v)) if v else "—"
    if spec.kind == "tool_list":
        return "\n".join(f"  • {t}" for t in v) if v else "(нет)"
    return str(v) if v != "" else "(пусто)"


def _config_apply_value(spec: FieldSpec, raw: str, chat_id: int) -> tuple[bool, str]:
    """Validate, mutate SETTINGS live, persist. Returns (ok, message)."""
    try:
        value = _config_validate(spec, raw)
    except ValueError as e:
        return False, f"⚠️ Неверное значение: {e}"

    # Never let the owner remove their own chat_id — @authorized reads this set live,
    # so a bad edit would lock them out of the bot with no recovery.
    if spec.attr == "allowed_user_ids" and chat_id not in value:
        return False, "⚠️ Нельзя удалить свой собственный chat_id — иначе вы потеряете доступ к боту."

    _config_set(spec, value)

    try:
        if spec.secret and spec.env:
            os.environ[spec.env] = _config_env_str(spec, value)
            set_key(str(ENV_PATH), spec.env, _config_env_str(spec, value))
        else:
            save_field(spec.yaml_path, value)
    except Exception as e:  # noqa: BLE001
        log.exception("config persist failed for %s", spec.key)
        return False, f"⚠️ Применено в памяти, но не записано на диск: {e}"

    restart = "" if spec.live else "\n⚠️ Применится только после рестарта бота."
    if spec.secret:
        return True, f"✅ {spec.label} обновлён.{restart}"
    return True, f"✅ {spec.label}: {_config_short(spec, value)}.{restart}"


def _config_root_text(chat_id: int) -> str:
    if not SETTINGS.config_password:
        sec = "🔒 Секреты: отключено (CONFIG_PASSWORD не задан в .env)"
    elif STATE.is_config_unlocked(chat_id):
        sec = "🔓 Секреты: разблокировано"
    else:
        sec = "🔒 Секреты: заблокировано (/config unlock <пароль>)"
    return f"⚙️ Конфигурация\n\nРазделы:\n{sec}\n\nНажмите раздел, чтобы изменить."


def _config_root_kb(chat_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, g in enumerate(CONFIG_GROUPS):
        row.append(InlineKeyboardButton(g, callback_data=f"cf:g:{i}"))
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if SETTINGS.config_password and STATE.is_config_unlocked(chat_id):
        rows.append([InlineKeyboardButton("🔒 Заблокировать секреты", callback_data="cf:lock")])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data="cf:close")])
    return InlineKeyboardMarkup(rows)


async def _config_show_root(query, bot, chat_id: int) -> None:
    mid = await _edit_or_send(query, bot, chat_id, _config_root_text(chat_id), _config_root_kb(chat_id))
    STATE.set_config_msg_id(chat_id, mid)
    await query.answer()


async def _config_show_group(query, bot, chat_id: int, gi: int) -> None:
    if not (0 <= gi < len(CONFIG_GROUPS)):
        await query.answer("Раздел не найден — /config.", show_alert=True)
        return
    gname = CONFIG_GROUPS[gi]
    fields = [(i, f) for i, f in enumerate(CONFIG_FIELDS) if f.group == gname]
    lines = [f"⚙️ {gname}", ""]
    rows: list[list[InlineKeyboardButton]] = []
    if gname == "Секреты" and not SETTINGS.config_password:
        lines += ["🔒 Редактирование секретов отключено.",
                  "Задайте CONFIG_PASSWORD в .env и перезапустите бота."]
    elif gname == "Секреты" and not STATE.is_config_unlocked(chat_id):
        lines += ["🔒 Секреты заблокированы.", "Введите: /config unlock <пароль>"]
    else:
        if gname == "Секреты":
            lines.append("🔓 Разблокировано. Значения видны полностью.")
        for i, f in fields:
            cap = f.label if f.secret else f"{f.label}: {_config_short(f, _config_get(f))}"
            rows.append([InlineKeyboardButton(cap, callback_data=f"cf:f:{i}")])
        if gname == "Секреты":
            rows.append([InlineKeyboardButton("🔒 Заблокировать", callback_data="cf:lock")])
    rows.append([InlineKeyboardButton("← назад", callback_data="cf:root")])
    mid = await _edit_or_send(query, bot, chat_id, "\n".join(lines), InlineKeyboardMarkup(rows))
    STATE.set_config_msg_id(chat_id, mid)
    await query.answer()


def _config_field_payload(i: int, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build (text, keyboard) for a field's detail view. Reused by the inline view and by
    the typed-value consumer (which has no query and edits the tracked message directly)."""
    spec = CONFIG_FIELDS[i]
    lines = [f"⚙️ {spec.label}", "", f"Текущее: {_config_field_value_text(spec, chat_id)}"]
    if spec.help:
        lines += ["", spec.help]
    if not spec.live:
        lines.append("⚠️ Применится только после рестарта.")
    rows: list[list[InlineKeyboardButton]] = []
    if spec.secret and not STATE.is_config_unlocked(chat_id):
        rows.append([InlineKeyboardButton("🔒 /config unlock <пароль>", callback_data="cf:root")])
    elif spec.kind in ("enum", "mode_perm"):
        cur = _config_get(spec)
        for j, c in enumerate(spec.choices):
            mark = "✅ " if c == cur else ""
            rows.append([InlineKeyboardButton(f"{mark}{c}", callback_data=f"cf:set:{i}:{j}")])
    else:
        rows.append([InlineKeyboardButton("✏️ Изменить", callback_data=f"cf:edit:{i}")])
    rows.append([InlineKeyboardButton("← назад", callback_data=f"cf:g:{_group_index(spec.group)}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _config_field_view(query, bot, chat_id: int, i: int) -> None:
    if not (0 <= i < len(CONFIG_FIELDS)):
        await query.answer("Поле не найдено — /config.", show_alert=True)
        return
    text, kb = _config_field_payload(i, chat_id)
    mid = await _edit_or_send(query, bot, chat_id, text, kb)
    STATE.set_config_msg_id(chat_id, mid)
    await query.answer()


async def _config_edit_prompt(query, bot, chat_id: int, i: int) -> None:
    if not (0 <= i < len(CONFIG_FIELDS)):
        await query.answer("Поле не найдено — /config.", show_alert=True)
        return
    spec = CONFIG_FIELDS[i]
    if spec.secret and not STATE.is_config_unlocked(chat_id):
        await query.answer("Секреты заблокированы — /config unlock.", show_alert=True)
        return
    STATE.set_config_pending(chat_id, spec.key)
    if spec.kind == "tool_list":
        prompt = (f"✏️ {spec.label}\nПришлите новый список deny_tools "
                  f"(по одному в строке). /config — отмена.")
    elif spec.kind == "id_list":
        prompt = (f"✏️ {spec.label}\nПришлите id через запятую или пробел. "
                  f"Свой id удалять нельзя. /config — отмена.")
    else:
        prompt = f"✏️ {spec.label}\nПришлите новое значение. Кнопка «отмена» — выйти."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("отмена", callback_data=f"cf:f:{i}")]])
    mid = await _edit_or_send(query, bot, chat_id, prompt, kb)
    STATE.set_config_msg_id(chat_id, mid)
    await query.answer()


async def _config_do_set(query, bot, chat_id: int, i: int, j: int) -> None:
    if not (0 <= i < len(CONFIG_FIELDS)) or not (0 <= j < len(CONFIG_FIELDS[i].choices)):
        await query.answer("Вариант не найден — /config.", show_alert=True)
        return
    spec = CONFIG_FIELDS[i]
    if spec.secret and not STATE.is_config_unlocked(chat_id):
        await query.answer("Секреты заблокированы — /config unlock.", show_alert=True)
        return
    ok, msg = _config_apply_value(spec, spec.choices[j], chat_id)
    if ok:
        await _config_field_view(query, bot, chat_id, i)  # redraws + answers
    else:
        await query.answer(msg, show_alert=True)


async def _config_consume_value(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    chat_id = update.effective_chat.id
    bot = context.bot
    spec = CONFIG_FIELDS_BY_KEY.get(key)
    if spec is None:
        await bot.send_message(chat_id, "Поле не найдено — /config.")
        return
    if spec.secret and not STATE.is_config_unlocked(chat_id):
        await bot.send_message(chat_id, "🔒 Секреты снова заблокированы (истёк таймаут). "
                                        "/config unlock <пароль>.")
        return
    raw = (update.message.text or "").strip()
    if not raw:
        await bot.send_message(chat_id, "Пустое значение. /config — открыть заново.")
        return
    # Best-effort: delete the user's typed value so it doesn't clutter the chat.
    try:
        await bot.delete_message(chat_id, update.message.message_id)
    except Exception:  # noqa: BLE001
        pass
    ok, apply_msg = _config_apply_value(spec, raw, chat_id)
    # Edit the tracked config message back to the field view (shows the new value + nav),
    # replacing the stale edit-prompt and its "отмена" button. Prefix the apply result.
    i = CONFIG_FIELDS.index(spec)
    text, kb = _config_field_payload(i, chat_id)
    text = f"{apply_msg}\n\n{text}"
    mid = STATE.get_config_msg_id(chat_id)
    if mid is not None:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=mid, text=text, reply_markup=kb)
            return
        except Exception:  # noqa: BLE001 — message gone / too old: fall through to a new message
            STATE.set_config_msg_id(chat_id, None)
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    STATE.set_config_msg_id(chat_id, sent.message_id)


async def _config_do_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, pw_parts: list[str]) -> None:
    bot = context.bot
    if not SETTINGS.config_password:
        await bot.send_message(chat_id, "🔒 Редактирование секретов отключено. "
                                        "Задайте CONFIG_PASSWORD в .env и перезапустите бота.")
        return
    # Best-effort: delete the message that contains the password so it doesn't linger.
    deleted = True
    try:
        await bot.delete_message(chat_id, update.message.message_id)
    except Exception:  # noqa: BLE001
        deleted = False
    pw = " ".join(pw_parts).strip()
    if not pw:
        await bot.send_message(chat_id, "Использование: /config unlock <пароль>")
        return
    if hmac.compare_digest(pw, SETTINGS.config_password):
        STATE.unlock_config(chat_id)
        mins = max(1, SETTINGS.config_unlock_seconds // 60)
        extra = "" if deleted else "\n⚠️ Не удалось удалить сообщение с паролем — удалите его вручную."
        await bot.send_message(chat_id, f"✅ Секреты разблокированы на ~{mins} мин.{extra}\n"
                                        "Откройте /config → Секреты.")
    else:
        await bot.send_message(chat_id, "❌ Неверный пароль.")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit bot settings via inline buttons. /config unlock <pw> | /config lock."""
    chat_id = update.effective_chat.id
    bot = context.bot
    # Opening /config cancels any pending typed-value input.
    STATE.set_config_pending(chat_id, None)
    args = context.args
    if args:
        sub = args[0].lower()
        if sub in ("unlock", "разблок", "разблокировать"):
            await _config_do_unlock(update, context, chat_id, args[1:])
            return
        if sub in ("lock", "заблок", "заблокировать"):
            STATE.lock_config(chat_id)
            await bot.send_message(chat_id, "🔒 Секреты заблокированы.")
            return
    # A fresh /config opens a NEW root message (like /note browse). The message id is
    # tracked so the typed-value consumer can edit THIS message back to the field view.
    sent = await bot.send_message(chat_id=chat_id, text=_config_root_text(chat_id),
                                  reply_markup=_config_root_kb(chat_id))
    STATE.set_config_msg_id(chat_id, sent.message_id)


async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route `cf:` inline-button taps for /config (mirror of note_callback's safety)."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = query.data or ""
    parts = data.split(":")
    op = parts[1] if len(parts) > 1 else ""
    bot = context.bot
    # Any navigation cancels a pending typed-value edit; the "edit" op re-sets it.
    # Without this, tapping "отмена" then typing text would still consume that text.
    if op != "edit":
        STATE.set_config_pending(chat_id, None)
    try:
        if op == "root":
            await _config_show_root(query, bot, chat_id)
        elif op == "g":
            await _config_show_group(query, bot, chat_id, int(parts[2]))
        elif op == "f":
            await _config_field_view(query, bot, chat_id, int(parts[2]))
        elif op == "set":
            await _config_do_set(query, bot, chat_id, int(parts[2]), int(parts[3]))
        elif op == "edit":
            await _config_edit_prompt(query, bot, chat_id, int(parts[2]))
        elif op == "lock":
            STATE.lock_config(chat_id)
            await _config_show_root(query, bot, chat_id)  # redraws + answers
        elif op == "close":
            # Exit /config: drop the inline keyboard and end the session.
            STATE.set_config_msg_id(chat_id, None)
            try:
                await query.edit_message_text("⚙️ Закрыто. /config — открыть снова.",
                                              reply_markup=None)
            except BadRequest:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:  # noqa: BLE001
                    pass
            await query.answer()
        else:
            await query.answer()
    except (ValueError, IndexError):
        await query.answer("Кнопка устарела — откройте /config.", show_alert=True)
    except Exception as e:  # noqa: BLE001
        log.warning("config_callback error: %s", e)
        try:
            await query.answer("Ошибка обработки кнопки.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
