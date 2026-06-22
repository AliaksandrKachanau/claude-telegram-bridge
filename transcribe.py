"""Speech-to-text for voice messages.

Two interchangeable backends, selected by settings.stt_provider:
- "groq":  cloud Whisper via Groq's OpenAI-compatible API (needs GROQ_API_KEY in .env).
           Fast, free tier, good Russian. Uses httpx (already a dependency).
- "local": faster-whisper running on this PC. Free forever, offline, private.
           Requires `pip install faster-whisper` (lazy-imported; first run downloads
           the model). Run on a worker thread since inference is CPU/GPU-bound.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from config import Settings

log = logging.getLogger(__name__)

_LOCAL_MODEL = None  # cached faster-whisper model instance


async def transcribe(audio_path: str, settings: Settings) -> str:
    provider = settings.stt_provider
    if provider == "groq":
        return await _groq(audio_path, settings)
    if provider == "local":
        return await asyncio.get_running_loop().run_in_executor(None, _local, audio_path, settings)
    raise ValueError(f"unknown stt provider: {provider!r}")


async def _groq(audio_path: str, settings: Settings) -> str:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY не задан в .env (нужен для stt.provider=groq)")
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    data = {"model": settings.stt_groq_model, "response_format": "text"}
    if settings.stt_language:
        data["language"] = settings.stt_language

    with open(audio_path, "rb") as f:
        files = {"file": ("voice.ogg", f, "audio/ogg")}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, headers=headers, data=data, files=files)
        except httpx.HTTPError as e:
            log.warning("groq request failed: %s", e)
            raise RuntimeError(f"Сеть/Groq недоступен: {e}") from e

    if resp.status_code != 200:
        log.warning("groq HTTP %s: %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"Groq API вернул {resp.status_code}: {resp.text[:200]}")
    return resp.text.strip()


def _local(audio_path: str, settings: Settings) -> str:
    global _LOCAL_MODEL
    try:
        from faster_whisper import WhisperModel  # lazy import; optional dep
    except ImportError as e:
        raise RuntimeError(
            "Локальное распознавание не установлено. Выполните: "
            ".venv\\Scripts\\pip install faster-whisper"
        ) from e

    if _LOCAL_MODEL is None:
        log.info("loading faster-whisper model %r (first run downloads it)...", settings.stt_local_model)
        _LOCAL_MODEL = WhisperModel(settings.stt_local_model, device="cpu", compute_type="int8")

    lang = settings.stt_language or None
    segments, _info = _LOCAL_MODEL.transcribe(audio_path, language=lang, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()
