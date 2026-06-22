"""Telegram output helpers: chunking past the 4096-char limit, large outputs as a
.txt attachment, and a cost/session footer."""
from __future__ import annotations

import io
import logging
from typing import Optional

from telegram import InputFile

log = logging.getLogger(__name__)

MSG_LIMIT = 4000  # Telegram hard limit is 4096; leave headroom for the footer


def chunk_text(text: str, limit: int = MSG_LIMIT) -> list[str]:
    """Split text into chunks <= limit, preferring blank-line then newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    for block in text.split("\n\n"):
        if len(block) <= limit:
            chunks.append(block)
            continue
        # block itself too long: split on single newlines, then hard-wrap
        line_buf = ""
        for line in block.split("\n"):
            candidate = (line_buf + "\n" + line) if line_buf else line
            if len(candidate) <= limit:
                line_buf = candidate
            else:
                if line_buf:
                    chunks.append(line_buf)
                if len(line) <= limit:
                    line_buf = line
                else:
                    # hard-wrap a single over-long line
                    for i in range(0, len(line), limit):
                        chunks.append(line[i:i + limit])
                    line_buf = ""
        if line_buf:
            chunks.append(line_buf)
    return [c for c in chunks if c != ""]


def make_footer(cost_usd: Optional[float], session_id: Optional[str], num_turns: Optional[int]) -> str:
    parts: list[str] = []
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")
    if session_id:
        parts.append(f"session {session_id[:8]}")
    if num_turns is not None:
        parts.append(f"{num_turns} turns")
    return ("—" + " · ".join(parts)) if parts else ""


async def reply_long(bot, chat_id: int, text: str, footer: str = "",
                     file_threshold: int = 15000) -> None:
    """Send `text` to the chat, chunking or attaching as a file for large output.

    A non-empty footer is appended to the last chunk (or used as the file caption).
    """
    text = (text or "").rstrip()
    if not text:
        text = "(пустой ответ)"

    total_len = len(text) + (len(footer) + 2 if footer else 0)

    if total_len > file_threshold:
        buf = io.BytesIO(text.encode("utf-8"))
        buf.name = "output.txt"
        caption = (footer[:200] if footer else None)
        await bot.send_document(chat_id=chat_id, document=InputFile(buf), caption=caption)
        return

    if footer:
        text = f"{text}\n\n{footer}"

    for i, chunk in enumerate(chunk_text(text)):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            disable_web_page_preview=True,
        )
