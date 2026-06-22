"""Runtime state: current project per chat, Claude session UUIDs, running-task
tracking for /cancel, per-chat mode, and the global Claude lock.

A single global :class:`asyncio.Lock` serializes EVERY ``claude -p`` call (both
/ask and /task). This is mandatory on this machine: concurrent claude processes
corrupt the global ``~/.claude.json`` (the backup folder held 8 corrupted copies).
Per-project parallelism is not worth that risk for a single-user bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from config import SESSIONS_PATH, Project, Settings

log = logging.getLogger(__name__)

# How many bot messages per chat to keep text for, so reply-voice can speak them.
_BOT_TEXT_MAX = 50


@dataclass
class RunningTask:
    """Tracks the live Claude process of the currently-running request, for /cancel."""
    proc: object  # subprocess.Popen
    cancelled: bool = False


@dataclass
class BrowseCache:
    """Inline-button navigation cache for /note browse (per chat, in memory).

    Callback buttons carry INDEXES into these lists (callback_data is capped at 64 bytes
    and Cyrillic costs 2 bytes/char), so real folder/file names never travel through
    Telegram — only the bot-assembled indexes do. A forged callback therefore cannot
    reference an arbitrary path; the worst case is an out-of-range index.
    """
    folders: list[str] = field(default_factory=list)  # last shown folder buttons
    folder: str = ""                                  # currently-open folder
    files: list[str] = field(default_factory=list)    # last shown file dates ('YYYY-MM-DD')


@dataclass
class Pages:
    """Active pagination for a multi-page Claude answer (per chat, in memory).

    ``pg:<i>`` callback buttons reference an index into ``pages``; the cache is
    replaced wholesale whenever a new multi-page answer is rendered, so a stale
    button on an old message just yields an out-of-range -> friendly alert.
    """
    pages: list[str] = field(default_factory=list)
    index: int = 0


class State:
    def __init__(self, settings: Settings, start_paused: bool = False) -> None:
        self.settings = settings
        self.current: dict[int, str] = {}          # chat_id -> project name
        self.modes: dict[int, str] = {}             # chat_id -> mode name
        self.sessions: dict[str, str] = {}          # project name -> session UUID
        self.running: dict[str, RunningTask] = {}   # project name -> live task
        self.last_cost: dict[str, float] = {}       # project name -> last cost_usd
        self.last_answer: dict[int, str] = {}       # chat_id -> last Claude answer text
        self.voice_mode: dict[int, bool] = {}       # chat_id -> always reply by voice
        self.note_mode: dict[int, bool] = {}        # chat_id -> dictation mode on (voice->file, no Claude)
        self.note_folder: dict[int, str] = {}       # chat_id -> current dictation category folder
        self.note_browse: dict[int, BrowseCache] = {}  # chat_id -> /note browse inline-nav cache
        self.page_cache: dict[int, Pages] = {}  # chat_id -> active multi-page answer (◀️/▶️)
        self.confirm_voice: dict[int, bool] = {}  # chat_id -> confirm STT before sending (absent = ON)
        self.pending_voice: dict[int, tuple[str, bool]] = {}  # chat_id -> (prompt, speak_reply) awaiting ✅/✏️/🗑
        self.await_edit: dict[int, bool] = {}  # chat_id -> next text message replaces the pending prompt
        self.draft_mode: dict[int, bool] = {}  # chat_id -> accumulate voice into a draft (absent = OFF)
        self.draft: dict[int, list[str]] = {}  # chat_id -> accumulated voice fragments
        self.reply_voice: dict[int, bool] = {}  # chat_id -> reply-to-speak (absent = OFF)
        self.bot_text: dict[int, dict[int, str]] = {}  # chat_id -> {bot_message_id: plain_text} (for reply-voice)
        # Chat ids that start PAUSED. Only set when the bot is launched via autostart
        # (which exports BOT_START_PAUSED=1); a manual run_bot.bat starts active.
        # Owner must /resume before any Claude work runs.
        self.paused: set[int] = set(settings.allowed_user_ids) if start_paused else set()
        self.claude_lock = asyncio.Lock()           # global serialization of claude calls
        self._load_sessions()

    # ---- projects ----
    def project_for_chat(self, chat_id: int) -> Project:
        name = self.current.get(chat_id)
        if name:
            p = self.settings.project(name)
            if p:
                return p
        # default to the first configured project
        if self.settings.projects:
            self.current[chat_id] = self.settings.projects[0].name
            return self.settings.projects[0]
        raise RuntimeError("Не настроено ни одного проекта в config.yaml")

    def switch_project(self, chat_id: int, name: str) -> Project:
        p = self.settings.project(name)
        if not p:
            raise KeyError(name)
        self.current[chat_id] = name
        return p

    def add_project(self, name: str, path: str) -> Project:
        """Register a project in memory and persist it to config.yaml."""
        if self.settings.project(name):
            raise ValueError(f"проект '{name}' уже существует")
        import os
        from pathlib import Path
        if not os.path.isdir(path):
            raise FileNotFoundError(f"путь не существует: {path}")
        proj = Project(name=name, path=str(Path(path).resolve()))
        self.settings.projects.append(proj)
        self._persist_project(proj)
        return proj

    def _persist_project(self, proj: Project) -> None:
        from config import CONFIG_PATH
        import yaml
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("projects", []).append({"name": proj.name, "path": proj.path})
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        except Exception:  # noqa: BLE001
            log.exception("failed to persist new project to config.yaml")

    # ---- sessions ----
    def get_session(self, project_name: str) -> Optional[str]:
        return self.sessions.get(project_name)

    def set_session(self, project_name: str, session_id: str) -> None:
        self.sessions[project_name] = session_id
        self._save_sessions()

    def clear_session(self, project_name: str) -> None:
        self.sessions.pop(project_name, None)
        self._save_sessions()

    def _load_sessions(self) -> None:
        try:
            with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
                self.sessions = json.load(f) or {}
        except FileNotFoundError:
            self.sessions = {}
        except Exception:  # noqa: BLE001
            log.exception("failed to load sessions.json; starting fresh")
            self.sessions = {}

    def _save_sessions(self) -> None:
        try:
            with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.sessions, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            log.exception("failed to save sessions.json")

    # ---- running task / cancel ----
    def set_running(self, project_name: str, task: RunningTask) -> None:
        self.running[project_name] = task

    def get_running(self, project_name: str) -> Optional[RunningTask]:
        return self.running.get(project_name)

    def clear_running(self, project_name: str) -> Optional[RunningTask]:
        return self.running.pop(project_name, None)

    # ---- mode ----
    def get_mode(self, chat_id: int) -> str:
        return self.modes.get(chat_id, self.settings.default_mode)

    def set_mode(self, chat_id: int, mode: str) -> None:
        self.modes[chat_id] = mode

    # ---- voice reply mode ----
    def get_voice(self, chat_id: int) -> bool:
        return self.voice_mode.get(chat_id, False)

    def set_voice(self, chat_id: int, on: bool) -> None:
        self.voice_mode[chat_id] = on

    # ---- dictation mode (/note): voice -> file, no Claude ----
    def get_note_mode(self, chat_id: int) -> bool:
        return self.note_mode.get(chat_id, False)

    def set_note_mode(self, chat_id: int, on: bool) -> None:
        self.note_mode[chat_id] = on

    def get_note_folder(self, chat_id: int) -> str:
        return self.note_folder.get(chat_id, self.settings.dictations_default_folder)

    def set_note_folder(self, chat_id: int, folder: str) -> None:
        self.note_folder[chat_id] = folder

    def get_browse(self, chat_id: int) -> BrowseCache:
        bc = self.note_browse.get(chat_id)
        if bc is None:
            bc = BrowseCache()
            self.note_browse[chat_id] = bc
        return bc

    # ---- pause / resume (gates all claude calls for a chat; in-memory only) ----
    def get_pause(self, chat_id: int) -> bool:
        return chat_id in self.paused

    def set_pause(self, chat_id: int, on: bool) -> None:
        if on:
            self.paused.add(chat_id)
        else:
            self.paused.discard(chat_id)

    # ---- /confirm: gate a transcribed prompt behind ✅/✏️/🗑 buttons (per chat) ----
    def get_confirm(self, chat_id: int) -> bool:
        return self.confirm_voice.get(chat_id, True)  # default ON

    def set_confirm(self, chat_id: int, on: bool) -> None:
        self.confirm_voice[chat_id] = on

    def get_pending_voice(self, chat_id: int) -> Optional[tuple[str, bool]]:
        return self.pending_voice.get(chat_id)

    def set_pending_voice(self, chat_id: int, value) -> None:
        if value is None:
            self.pending_voice.pop(chat_id, None)
        else:
            self.pending_voice[chat_id] = value

    def get_await_edit(self, chat_id: int) -> bool:
        return self.await_edit.get(chat_id, False)

    def set_await_edit(self, chat_id: int, on: bool) -> None:
        if on:
            self.await_edit[chat_id] = True
        else:
            self.await_edit.pop(chat_id, None)

    # ---- /draft: accumulate voice fragments into one prompt (per chat) ----
    def get_draft_mode(self, chat_id: int) -> bool:
        return self.draft_mode.get(chat_id, False)

    def set_draft_mode(self, chat_id: int, on: bool) -> None:
        if on:
            self.draft_mode[chat_id] = True
        else:
            self.draft_mode.pop(chat_id, None)

    def get_draft(self, chat_id: int) -> list[str]:
        return self.draft.get(chat_id, [])

    def set_draft(self, chat_id: int, frags: list[str]) -> None:
        self.draft[chat_id] = frags

    def clear_draft(self, chat_id: int) -> None:
        self.draft.pop(chat_id, None)

    # ---- /reply_voice: speak a specific bot message you reply to (per chat) ----
    def get_reply_voice(self, chat_id: int) -> bool:
        return self.reply_voice.get(chat_id, False)

    def set_reply_voice(self, chat_id: int, on: bool) -> None:
        if on:
            self.reply_voice[chat_id] = True
        else:
            self.reply_voice.pop(chat_id, None)

    def remember_bot_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Cache a bot message's plain text by its message_id (for reply-voice)."""
        d = self.bot_text.setdefault(chat_id, {})
        d[message_id] = text
        # trim oldest beyond the cap (dict preserves insertion order)
        if len(d) > _BOT_TEXT_MAX:
            for k in list(d.keys())[:-_BOT_TEXT_MAX]:
                del d[k]

    def get_bot_text(self, chat_id: int, message_id: int) -> Optional[str]:
        return self.bot_text.get(chat_id, {}).get(message_id)
