"""Text-to-speech for voice replies.

Two interchangeable backends, selected by settings.tts_provider:
- "edge":   Microsoft Edge TTS (edge-tts). Free, no key, great Russian neural
            voices, cloud. Default.
- "silero": Silero TTS on this PC. Free, offline, private. Optional (needs
            `pip install torch`; first run fetches the model from GitHub).

Output is always OGG/Opus (the format Telegram requires for voice messages),
produced via ffmpeg. ffmpeg is located by: config path -> PATH -> WinGet package.
"""
from __future__ import annotations

import asyncio
import glob
import io
import logging
import os
import shutil
import subprocess
import tempfile

from config import Settings

log = logging.getLogger(__name__)

# Monotonic counter so each temp file is unique without Math.random/Date.
_SEQ = 0

# Russian neural voices on Edge TTS; default is female Svetlana.
DEFAULT_EDGE_VOICE = "ru-RU-SvetlanaNeural"


def _find_ffmpeg(settings: Settings) -> str:
    candidates = []
    if settings.tts_ffmpeg_path:
        candidates.append(settings.tts_ffmpeg_path)
    candidates.append(shutil.which("ffmpeg") or "")
    # WinGet default install location (ffmpeg installed there in this setup)
    candidates.extend(glob.glob(os.path.join(
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"),
        "Gyan.FFmpeg*", "ffmpeg-*", "bin", "ffmpeg.exe")))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError(
        "ffmpeg не найден. Установите: winget install Gyan.FFmpeg "
        "(и перезапустите), либо укажите tts.ffmpeg_path в config.yaml"
    )


def _to_opus_sync(audio_bytes: bytes, in_suffix: str, ffmpeg: str) -> bytes:
    """Convert arbitrary audio bytes (mp3/wav) to OGG/Opus via ffmpeg."""
    fd_in, in_path = tempfile.mkstemp(suffix=in_suffix)
    fd_out, out_path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd_in)
    os.close(fd_out)
    try:
        with open(in_path, "wb") as f:
            f.write(audio_bytes)
        cmd = [
            ffmpeg, "-y", "-i", in_path,
            "-c:a", "libopus", "-b:a", "48k", "-ac", "1", "-ar", "48000",
            out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg error: {proc.stderr[-500:]}")
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


async def _edge(text: str, settings: Settings) -> bytes:
    import edge_tts  # imported lazily so the bot still boots if optional deps lag
    voice = settings.tts_edge_voice or DEFAULT_EDGE_VOICE
    kwargs = {}
    if settings.tts_rate:  # edge-tts rejects None/"" -> only pass when set
        kwargs["rate"] = settings.tts_rate
    communicate = edge_tts.Communicate(text, voice, **kwargs)
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    data = buf.getvalue()
    if not data:
        raise RuntimeError("edge-tts вернул пустой аудио (проверьте голос/текст)")
    return data


def _silero_sync(text: str, settings: Settings) -> bytes:
    try:
        import torch  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Silero не установлен. Выполните: .venv\\Scripts\\pip install torch"
        ) from e
    # Silero loads its model from GitHub on first use (needs internet once).
    lang = settings.tts_language or "ru"
    model, _ = torch.hub.load(
        repo_or_dir="snakers4/silero-models", model="silero_tts",
        language=lang, speaker="v4_" + lang, trust_repo=True,
    )
    speaker = settings.tts_silero_speaker or "aidar"
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        model.save_wav(text=text, speaker=speaker, audio_path=wav_path)
        with open(wav_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


async def speak(text: str, settings: Settings) -> bytes:
    """Return OGG/Opus audio bytes for `text` (empty input -> empty bytes)."""
    text = (text or "").strip()
    if not text:
        return b""
    ffmpeg = _find_ffmpeg(settings)

    provider = settings.tts_provider
    if provider == "edge":
        audio = await _edge(text, settings)
        in_suffix = ".mp3"
    elif provider == "silero":
        audio = await asyncio.get_running_loop().run_in_executor(None, _silero_sync, text, settings)
        in_suffix = ".wav"
    else:
        raise ValueError(f"unknown tts provider: {provider!r}")

    return await asyncio.get_running_loop().run_in_executor(
        None, _to_opus_sync, audio, in_suffix, ffmpeg,
    )


# ---- smoke test -------------------------------------------------------------
if __name__ == "__main__":
    from config import load

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _s = load()

    async def _main():
        out = await speak("Привет! Это проверка голосовых ответов бота.", _s)
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print("opus bytes:", len(out))
        # write to a file so it can be played / inspected
        with open("speak_test.ogg", "wb") as f:
            f.write(out)
        print("wrote speak_test.ogg")

    asyncio.run(_main())
