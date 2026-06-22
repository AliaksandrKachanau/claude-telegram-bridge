"""Telegram command handlers for the Claude bridge.

Dependencies (SETTINGS, STATE) are injected once via :func:`init` from bot.py.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import messages as M
import speak as TTS
import transcribe as STT
import health
from claude_runner import _kill_proc_tree, run_claude
from config import Settings
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
    "*(или напишите «ответь голосом …», «озвучь»)*\n"
    "*(или надиктуйте 🎤 — голос распознаётся и выполнится как задача)*\n\n"
    "*/diff* — git diff текущего проекта\n"
    "*/git status|log|diff|commit <msg>* — операции git\n"
    "*/project* — список проектов · */project <имя>* — переключить · */project add <путь>*\n"
    "*/mode* — текущий режим · */mode balanced|full|strict* — сменить\n"
    "*/cancel* — прервать текущий запрос к Claude\n"
    "*/pause* · */resume* — приостановить/возобновить обработку запросов к Claude\n"
    "*/note* — диктовка без Claude: on|off, folder <имя>, browse (листать/читать .md)\n"
    "*/status* — состояние бота\n"
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
            await M.reply_long(bot, chat_id, result.text or "(пустой ответ)", footer=footer)
            if (speak_reply or STATE.get_voice(chat_id)) and (result.text or "").strip():
                await _speak_answer(update, context, result.text)
        else:
            tag = "⏱" if result.timed_out else "⚠️"
            await M.reply_long(bot, chat_id, f"{tag} Claude не завершил запрос:\n\n{result.text}")


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
    if STATE.get_pause(chat_id):
        return  # paused: silently ignore free text — only slash-commands are answered
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
    """Transcribe a voice message: run as a task, or — under /note mode — append to file."""
    chat_id = update.effective_chat.id
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    if STATE.get_note_mode(chat_id):
        # Dictation: voice -> file, no Claude. Ignores the pause gate (it never calls Claude).
        await _run_dictation(update, context, voice)
        return
    if STATE.get_pause(chat_id):
        return  # paused: silently ignore voice (also skips the STT transcription)
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

    # Вырезаем запрос «ответь голосом» ДО отправки Claude — иначе модель видит
    # его и отказывается, ссылаясь на неспособность выдать аудио (озвучкой сам
    # занимается бот через speak.py). Та же логика, что в cmd_freetext.
    wants_voice, cleaned = _extract_voice_request(text)

    await bot.edit_message_text(
        chat_id=chat_id, message_id=note.message_id,
        text=f"🎙 Распознано: {text[:800]}",   # показываем всё, что распознали
    )

    if wants_voice and not cleaned:
        # одно лишь «ответь голосом» без вопроса -> озвучить последний ответ
        await cmd_speak(update, context)
        return

    await _do_claude(
        update, context,
        cleaned if wants_voice else text,
        is_task=True,
        speak_reply=wants_voice,
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    proj = STATE.project_for_chat(chat_id)
    STATE.clear_session(proj.name)
    await context.bot.send_message(
        chat_id=chat_id, text=f"🆕 Новый диалог для проекта *{proj.name}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    proj = STATE.project_for_chat(chat_id)
    rc, out = await _git(proj.path, ["diff"])
    if not out.strip():
        rc2, stat = await _git(proj.path, ["status", "-sb"])
        out = "Нет незакоммиченных изменений в tracked-файлах.\n\n" + stat
    await M.reply_long(context.bot, chat_id, out or "(пусто)")


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
    await M.reply_long(context.bot, chat_id, out or "(без вывода)")


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    args = context.args
    if not args:
        cur = STATE.current.get(chat_id)
        lines = ["*Проекты:*"]
        for p in SETTINGS.projects:
            mark = "✅ " if p.name == cur else "   "
            lines.append(f"{mark}`{p.name}` → {p.path}")
        lines.append("\nПереключение: /project <имя> · добавить: /project add <путь>")
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
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
        await context.bot.send_message(chat_id=chat_id, text=f"➕ Добавлен и выбран проект *{proj.name}*.", parse_mode=ParseMode.MARKDOWN)
        return
    name = args[0]
    try:
        proj = STATE.switch_project(chat_id, name)
    except KeyError:
        await context.bot.send_message(chat_id=chat_id, text=f"Нет такого проекта: {name}")
        return
    await context.bot.send_message(chat_id=chat_id, text=f"📂 Выбран проект *{proj.name}*.", parse_mode=ParseMode.MARKDOWN)


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
    lines = [
        f"📂 Проект: *{proj.name}*",
        f"📁 {proj.path}",
        f"⚙️ Режим: {mode}",
        f"⏸ Пауза: {'да (до /resume)' if STATE.get_pause(chat_id) else 'нет'}",
        f"🧠 Сессия: {sid[:8] + '…' if sid else 'нет (новый)'}",
        f"▶️ Выполняется: {'да' if (running and running.proc is not None) else 'нет'}",
        f"💲 Последний запрос: {('$%.4f' % cost) if cost is not None else '—'}",
    ]
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
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
        folder = STATE.get_note_folder(chat_id)
        today = f"{datetime.now():%Y-%m-%d}"  # no ".md" -> not linkified by Telegram
        on = STATE.get_note_mode(chat_id)
        await bot.send_message(
            chat_id=chat_id,
            text=(f"📝 Диктовка: {'ВКЛ — голос пишется в файл' if on else 'ВЫКЛ — голос идёт в Claude'}.\n"
                  f"📂 Папка: {folder}\n"
                  f"📁 Сегодня: {folder}/{today}\n\n"
                  f"/note on|off · /note folder <имя> · /note browse"),
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


async def _edit_or_send(query, bot, chat_id: int, text: str, reply_markup) -> None:
    """Update the browsed message in place; if editing fails (e.g. older than 48h), send new."""
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


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
