"""Configuration loading for the Claude <-> Telegram bridge.

Secrets come from .env (python-dotenv); non-secret settings come from config.yaml.
All paths are resolved relative to this file so the bot keeps working when launched
as a Windows service / Task Scheduler job with a different working directory.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "config.yaml"
SESSIONS_PATH = BASE_DIR / "sessions.json"

VALID_MODES = ("balanced", "full", "strict")


@dataclass(frozen=True)
class Project:
    name: str
    path: str  # absolute, normalized


@dataclass
class Settings:
    token: str
    allowed_user_ids: set[int]
    claude_exe: str
    default_mode: str
    timeout_minutes: int
    max_budget_usd: float
    max_turns: int
    long_output_threshold: int
    modes: dict  # name -> {"permission_mode": str, "deny_tools": list[str]}
    projects: list[Project] = field(default_factory=list)
    # speech-to-text
    stt_provider: str = "groq"           # "groq" | "local"
    stt_groq_model: str = "whisper-large-v3"
    stt_local_model: str = "small"       # tiny|base|small|medium|large-v3
    stt_language: str = "ru"             # ISO-639-1; "" or None for auto-detect
    groq_api_key: str = ""
    # text-to-speech (voice replies)
    tts_provider: str = "edge"           # "edge" | "silero"
    tts_edge_voice: str = "ru-RU-SvetlanaNeural"
    tts_silero_speaker: str = "aidar"
    tts_language: str = "ru"             # ISO-639-1 (used by Silero); "" = auto
    tts_rate: str = ""                   # e.g. "+10%" to speed up; "" = default
    tts_ffmpeg_path: str = ""            # absolute path; auto-detected if empty
    # dictations (voice -> file, no Claude)
    dictations_dir: str = str(BASE_DIR / "dictations")  # root folder for .md journals
    dictations_default_folder: str = "default"          # category used until /note folder <name>

    def project(self, name: str) -> Project | None:
        for p in self.projects:
            if p.name == name:
                return p
        return None


def _load_env() -> tuple[str, set[int], str]:
    load_dotenv(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
    ids = {int(x) for x in raw.replace(";", ",").split(",") if x.strip()}
    if not token:
        raise RuntimeError(f"TELEGRAM_BOT_TOKEN is not set in {ENV_PATH}")
    if not ids:
        raise RuntimeError(f"ALLOWED_USER_IDS is not set in {ENV_PATH}")
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    return token, ids, groq_key


def _resolve_claude_exe(cfg_value: str) -> str:
    if cfg_value and Path(cfg_value).exists():
        return str(Path(cfg_value))
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        log.warning("claude_exe %r not found; using %r from PATH", cfg_value, found)
        return found
    log.warning("claude_exe %r not found and 'claude' not on PATH", cfg_value)
    return cfg_value


def _load_projects(raw: list) -> list[Project]:
    projects: list[Project] = []
    seen = set()
    for entry in raw or []:
        name = str(entry.get("name", "")).strip()
        path = str(entry.get("path", "")).strip()
        if not name or not path:
            log.warning("skipping project entry without name/path: %r", entry)
            continue
        if name in seen:
            log.warning("duplicate project name %r skipped", name)
            continue
        seen.add(name)
        abs_path = str(Path(path).resolve())
        if not Path(abs_path).exists():
            log.warning("project %r path does not exist: %s", name, abs_path)
        projects.append(Project(name=name, path=abs_path))
    return projects


def load() -> Settings:
    token, ids, groq_key = _load_env()
    cfg = _load_yaml()

    stt = cfg.get("stt", {}) or {}
    tts = cfg.get("tts", {}) or {}
    dct = cfg.get("dictations", {}) or {}

    dct_dir_raw = str(dct.get("dir", "") or "").strip()
    dictations_dir = dct_dir_raw or str(BASE_DIR / "dictations")

    modes = cfg.get("modes", {})
    default_mode = cfg.get("default_mode", "balanced")
    if default_mode not in VALID_MODES:
        log.warning("default_mode %r invalid, falling back to 'balanced'", default_mode)
        default_mode = "balanced"
    for m in VALID_MODES:
        if m not in modes:
            log.warning("mode %r missing from config.modes", m)

    projects = _load_projects(cfg.get("projects", []))
    if not projects:
        log.warning("no projects configured — Claude will have nowhere to run")

    return Settings(
        token=token,
        allowed_user_ids=ids,
        claude_exe=_resolve_claude_exe(str(cfg.get("claude_exe", ""))),
        default_mode=default_mode,
        timeout_minutes=int(cfg.get("timeout_minutes", 15)),
        max_budget_usd=float(cfg.get("max_budget_usd", 2.0)),
        max_turns=int(cfg.get("max_turns", 0)),
        long_output_threshold=int(cfg.get("long_output_threshold_chars", 15000)),
        modes=modes,
        projects=projects,
        stt_provider=str(stt.get("provider", "groq")),
        stt_groq_model=str(stt.get("groq_model", "whisper-large-v3")),
        stt_local_model=str(stt.get("local_model", "small")),
        stt_language=str(stt.get("language", "ru") or ""),
        groq_api_key=groq_key,
        tts_provider=str(tts.get("provider", "edge")),
        tts_edge_voice=str(tts.get("edge_voice", "ru-RU-SvetlanaNeural")),
        tts_silero_speaker=str(tts.get("silero_speaker", "aidar")),
        tts_language=str(tts.get("language", "ru") or ""),
        tts_rate=str(tts.get("rate", "") or ""),
        tts_ffmpeg_path=str(tts.get("ffmpeg_path", "") or ""),
        dictations_dir=dictations_dir,
        dictations_default_folder=str(dct.get("default_folder", "default") or "default").strip(),
    )


def _load_yaml() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
