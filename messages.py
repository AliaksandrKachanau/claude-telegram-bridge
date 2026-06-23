"""Telegram output helpers.

A two-mode pipeline:
- ``render_markdown=True`` (default, used for Claude answers): convert the
  markdown Claude emits to Telegram ``ParseMode.HTML``, split into pages at
  block boundaries, paginate long answers with ◀️/▶️ buttons, and ship long
  code fences (> ``CODE_FILE_THRESHOLD``) as ``.py``/``.txt`` files.
- ``render_markdown=False`` (git/diff output): the legacy plain path — chunk at
  4096 chars, or attach as ``output.txt`` past the file threshold.

Pagination needs the per-chat page cache, so callers that want buttons pass
``state`` (the projects.State instance). Without it, multi-page output is sent
as successive messages instead. Both modes return the list of sent bot
``message_id``s (used later by reply-voice to speak a specific message).
"""
from __future__ import annotations

import html
import io
import logging
import re
from typing import Optional

from telegram import InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

log = logging.getLogger(__name__)

MSG_LIMIT = 4000  # Telegram hard limit is 4096; leave headroom for HTML tags + footer
CODE_FILE_THRESHOLD = 1500  # a code fence longer than this is sent as a file

_TAG_RE = re.compile(r"<[^>]+>")

# ---- markdown -> HTML -------------------------------------------------------

_FENCE_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# italic: *x* only (not _x_ — that mangles snake_case identifiers). Require no
# * or word-char touching the markers, and no space right after the opening *,
# so bullet lists like "* item" never turn italic.
_ITALIC_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)\*(?![*\w])")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_H_RE = re.compile(r"^#{1,6}\s+(.+)$", re.M)

# fence language -> file extension (everything else -> .txt)
_LANG_EXT = {
    "python": ".py", "py": ".py", "python3": ".py", "gyp": ".py",
    "js": ".js", "javascript": ".js", "mjs": ".js", "cjs": ".js",
    "ts": ".ts", "typescript": ".ts",
    "jsx": ".jsx", "tsx": ".tsx",
    "json": ".json", "json5": ".json",
    "yaml": ".yaml", "yml": ".yaml",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh", "bat": ".bat", "cmd": ".bat",
    "powershell": ".ps1", "ps1": ".ps1", "pwsh": ".ps1",
    "html": ".html", "xml": ".xml", "css": ".css", "scss": ".scss",
    "sql": ".sql",
    "rust": ".rs", "rs": ".rs",
    "go": ".go", "golang": ".go",
    "java": ".java", "kotlin": ".kt", "kt": ".kt",
    "c": ".c", "cpp": ".cpp", "cc": ".cpp", "c++": ".cpp", "h": ".h", "hpp": ".hpp",
    "cs": ".cs", "csharp": ".cs",
    "ruby": ".rb", "rb": ".rb",
    "php": ".php",
    "swift": ".swift",
    "scala": ".scala",
    "r": ".r", "perl": ".pl", "pl": ".pl", "lua": ".lua",
    "toml": ".toml", "ini": ".ini", "cfg": ".ini", "conf": ".conf",
    "dockerfile": ".dockerfile", "makefile": ".mk",
    "markdown": ".md", "md": ".md",
}


def _esc(s: str) -> str:
    """HTML-escape &, <, > (quote=False is fine — we never put text into an attribute)."""
    return html.escape(s or "", quote=False)


def _ext_for_lang(lang: str) -> str:
    return _LANG_EXT.get((lang or "").strip().lower(), ".txt")


def _inline_md(s: str) -> str:
    """Escape + apply inline markdown (bold/italic/inline-code/links/headers) to a text run.

    Escaping happens FIRST so user-controlled text can never inject HTML; the
    markdown syntax chars (`* _ [ ] ( ) ` #`) survive escaping unchanged.
    """
    s = _esc(s)
    s = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", s)
    s = _BOLD_RE.sub(r"<b>\1</b>", s)
    s = _ITALIC_RE.sub(r"<i>\1</i>", s)
    s = _LINK_RE.sub(r'<a href="\2">\1</a>', s)
    s = _H_RE.sub(r"<b>\1</b>", s)
    return s


def _md_to_segments(text: str) -> list[dict]:
    """Split markdown into ordered segments.

    Each segment is either ``{"kind": "html", "html": str}`` (an already-rendered
    HTML run, including short fenced code as <pre><code>…</code></pre>) or
    ``{"kind": "codefile", "lang": str, "content": str}`` (a long fence to ship
    as a file). Inline text between fences is rendered as HTML.
    """
    segs: list[dict] = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        if m.start() > pos:
            segs.append({"kind": "html", "html": _inline_md(text[pos:m.start()])})
        lang = (m.group(1) or "").strip()
        body = (m.group(2) or "").strip("\n")
        if len(body) > CODE_FILE_THRESHOLD:
            segs.append({"kind": "codefile", "lang": lang, "content": body})
        else:
            segs.append({"kind": "html", "html": f"<pre><code>{_esc(body)}</code></pre>"})
        pos = m.end()
    if pos < len(text):
        segs.append({"kind": "html", "html": _inline_md(text[pos:])})
    return segs


def _render_md(text: str, footer: str) -> tuple[list[str], list[dict]]:
    """Render markdown to (pages, codefiles).

    ``pages`` is a list of HTML strings each <= MSG_LIMIT (footer appended to the
    last page). ``codefiles`` is the list of long fences to send as documents.
    """
    segments = _md_to_segments(text)
    codefiles: list[dict] = []
    blocks: list[str] = []
    for seg in segments:
        if seg["kind"] == "codefile":
            codefiles.append(seg)
            lang = seg["lang"]
            nlines = seg["content"].count("\n") + 1
            ext = _ext_for_lang(lang)
            name_part = f" {lang}" if lang else ""
            label = "📄 Код" + name_part + f" — {nlines} строк → файл {ext} ниже"
            blocks.append(_esc(label))
        else:
            blocks.append(seg["html"])

    body = "\n\n".join(b for b in blocks if b.strip())
    if not body.strip():
        body = "(пустой ответ)"

    pages = chunk_text(body, MSG_LIMIT)
    if not pages:
        pages = ["(пустой ответ)"]
    if footer:
        pages[-1] = pages[-1] + "\n\n" + _esc(footer)
    return pages, codefiles


# ---- pagination -------------------------------------------------------------

def _page_kb(index: int, total: int) -> InlineKeyboardMarkup:
    row: list[InlineKeyboardButton] = []
    if index > 0:
        row.append(InlineKeyboardButton("◀️", callback_data=f"pg:{index - 1}"))
    row.append(InlineKeyboardButton(f"{index + 1}/{total}", callback_data="pg:info"))
    if index < total - 1:
        row.append(InlineKeyboardButton("▶️", callback_data=f"pg:{index + 1}"))
    return InlineKeyboardMarkup([row])


def _strip_tags(html_text: str) -> str:
    """Crude tag removal + entity unescape, for the broken-HTML fallback only."""
    out = _TAG_RE.sub("", html_text or "")
    return html.unescape(out)


async def _send_html(bot, chat_id: int, html_text: str,
                     reply_markup=None) -> int:
    """Send one HTML page; on a parse error (broken markup) retry as plain text."""
    try:
        m = await bot.send_message(
            chat_id=chat_id, text=html_text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=reply_markup,
        )
        return m.message_id
    except BadRequest:
        log.warning("HTML parse failed; retrying page as plain text")
        m = await bot.send_message(
            chat_id=chat_id, text=_strip_tags(html_text),
            disable_web_page_preview=True, reply_markup=reply_markup,
        )
        return m.message_id


async def _send_codefile(bot, chat_id: int, cf: dict) -> Optional[int]:
    ext = _ext_for_lang(cf["lang"])
    fname = "snippet" + ext
    content = cf["content"]
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = fname
    nlines = content.count("\n") + 1
    lang = cf["lang"]
    name_part = f" ({lang})" if lang else ""
    cap = "📄 Код" + name_part + f" — {nlines} строк"
    try:
        m = await bot.send_document(
            chat_id=chat_id, document=InputFile(buf, filename=fname), caption=cap,
        )
        return m.message_id
    except Exception as e:  # noqa: BLE001
        log.warning("send_codefile failed: %s", e)
        return None


# ---- chunking (legacy, reused by both paths) --------------------------------

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


def make_footer(cost_usd: Optional[float], session_id: Optional[str],
                num_turns: Optional[int]) -> str:
    parts: list[str] = []
    if cost_usd is not None:
        parts.append(f"${cost_usd:.4f}")
    if session_id:
        parts.append(f"session {session_id[:8]}")
    if num_turns is not None:
        parts.append(f"{num_turns} turns")
    return ("—" + " · ".join(parts)) if parts else ""


# ---- public entry point -----------------------------------------------------

async def reply_long(bot, chat_id: int, text: str, footer: str = "",
                     state=None, file_threshold: int = 15000,
                     render_markdown: bool = True) -> list[int]:
    """Send ``text`` to the chat. Returns the list of sent bot message_ids.

    - ``render_markdown=True``: HTML pipeline with pagination + code files.
      Pass ``state`` (projects.State) to enable ◀️/▶️ buttons; without it,
      multi-page output is sent as successive messages.
    - ``render_markdown=False``: legacy plain path (chunk or attach .txt).
    """
    text = (text or "").rstrip()
    if not text:
        text = "(пустой ответ)"

    sent_ids: list[int] = []

    if not render_markdown:
        total_len = len(text) + (len(footer) + 2 if footer else 0)
        if total_len > file_threshold:
            buf = io.BytesIO(text.encode("utf-8"))
            buf.name = "output.txt"
            caption = (footer[:200] if footer else None)
            m = await bot.send_document(chat_id=chat_id, document=InputFile(buf), caption=caption)
            sent_ids.append(m.message_id)
            return sent_ids
        full = f"{text}\n\n{footer}" if footer else text
        for chunk in chunk_text(full, MSG_LIMIT):
            m = await bot.send_message(chat_id=chat_id, text=chunk, disable_web_page_preview=True)
            sent_ids.append(m.message_id)
        return sent_ids

    # HTML pipeline
    pages, codefiles = _render_md(text, footer)

    if len(pages) == 1:
        sent_ids.append(await _send_html(bot, chat_id, pages[0]))
    else:
        page_cache = getattr(state, "page_cache", None)
        if page_cache is None:
            # no state: emit successively (no buttons)
            for p in pages:
                sent_ids.append(await _send_html(bot, chat_id, p))
        else:
            from projects import Pages  # local import: avoid a hard module dep at import time
            page_cache[chat_id] = Pages(pages=pages, index=0)
            first_id = await _send_html(
                bot, chat_id, pages[0], reply_markup=_page_kb(0, len(pages)),
            )
            sent_ids.append(first_id)

    for cf in codefiles:
        mid = await _send_codefile(bot, chat_id, cf)
        if mid is not None:
            sent_ids.append(mid)

    return sent_ids
