"""Connectivity health checks for the bot's external servers.

Surfaces whether the servers the bot depends on are reachable — in the startup
message and /status — so the owner sees, BEFORE sending a request, that e.g. the
Claude API is down (the classic "ConnectionRefused" storm comes from queuing many
requests against an unreachable API).

Probes run in parallel with a short timeout:
- Telegram   — GET /getMe (proves token + connectivity)
- Claude API — the host from ANTHROPIC_BASE_URL (custom proxy for glm-5.1), or
               the default api.anthropic.com if unset
- Groq STT   — only when stt_provider == "groq"
- Edge TTS   — TCP/443 to speech.platform.bing.com, only when tts_provider == "edge"
Local backends (faster-whisper, silero) are offline and reported as such.

A 5xx is still "server alive" — only a network failure (timeout, refused, DNS,
TLS) counts as down. Secrets (bot token, auth token, Groq key) are never put in
Check.detail; the token is also redacted defensively in render().
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import Settings

log = logging.getLogger(__name__)

_TIMEOUT = 5.0  # seconds per probe

# Defensive: strip anything that looks like a Telegram bot token out of any error text.
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


@dataclass
class Check:
    name: str                    # display label
    ok: Optional[bool]           # True=up, False=down, None=offline / not used
    detail: str = ""             # host or short error (NEVER secrets)


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("bot***", text or "")


async def _probe_http(name: str, url: str, *, headers=None, ok_detail: str = "") -> Check:
    """GET `url`; any HTTP response => up, network failure => down."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            await client.get(url, headers=headers)
        return Check(name=name, ok=True, detail=ok_detail)
    except Exception as e:  # noqa: BLE001
        return Check(name=name, ok=False, detail=_redact(str(e))[:80])


async def _probe_tcp(name: str, host: str, port: int = 443, ok_detail: str = "") -> Check:
    """TLS TCP-connect probe — for hosts with no simple anonymous GET (Edge TTS)."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=True), timeout=_TIMEOUT,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return Check(name=name, ok=True, detail=ok_detail)
    except Exception as e:  # noqa: BLE001
        return Check(name=name, ok=False, detail=_redact(str(e))[:80])


async def check_all(settings: Settings) -> list[Check]:
    """Run all applicable probes in parallel; offline providers are noted statically."""
    probes: list = []
    static: list[Check] = []

    # 1) Telegram
    probes.append(_probe_http("Telegram", f"https://api.telegram.org/bot{settings.token}/getMe"))

    # 2) Claude API (glm-5.1 via custom proxy if ANTHROPIC_BASE_URL is set)
    claude_base = (os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    claude_url = claude_base or "https://api.anthropic.com"
    claude_host = urlparse(claude_url).netloc or claude_url
    probes.append(_probe_http("Claude API", claude_url, ok_detail=claude_host))

    # 3) STT
    if settings.stt_provider == "groq":
        headers = {"Authorization": f"Bearer {settings.groq_api_key}"} if settings.groq_api_key else None
        probes.append(_probe_http("Groq STT", "https://api.groq.com/openai/v1/models",
                                  headers=headers, ok_detail="api.groq.com"))
    else:
        static.append(Check("STT", None, f"локально ({settings.stt_provider})"))

    # 4) TTS
    if settings.tts_provider == "edge":
        probes.append(_probe_tcp("Edge TTS", "speech.platform.bing.com", ok_detail="speech.platform.bing.com"))
    else:
        static.append(Check("TTS", None, f"локально ({settings.tts_provider})"))

    results = await asyncio.gather(*probes)
    return list(results) + static


def render(checks: list[Check]) -> list[str]:
    """Render checks as chat lines (emoji + name + optional detail)."""
    lines = ["🌐 Серверы:"]
    for c in checks:
        if c.ok is True:
            lines.append(f"  ✅ {c.name}" + (f" · {c.detail}" if c.detail else ""))
        elif c.ok is False:
            lines.append(f"  ❌ {c.name}" + (f": {c.detail}" if c.detail else " недоступен"))
        else:
            lines.append(f"  — {c.name} ({c.detail})" if c.detail else f"  — {c.name}")
    return lines
