"""Authorization: only allowlisted Telegram users may drive the bot.

Implemented as a decorator applied to every handler. Unauthorized updates are
dropped silently (a single bot reply would leak that the bot exists).
"""
from __future__ import annotations

import functools
import logging

from config import Settings

log = logging.getLogger(__name__)


def authorized(settings: Settings):
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(update, context):
            user = update.effective_user
            uid = getattr(user, "id", None)
            if uid is None or uid not in settings.allowed_user_ids:
                log.warning("rejected update from user_id=%s username=%s text=%r",
                            uid, getattr(user, "username", None),
                            getattr(getattr(update, "message", None), "text", None)[:80])
                return
            return await handler(update, context)
        return wrapper
    return decorator
