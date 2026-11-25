"""
=====================================================================
  roster.py — MTProto member roster  (OPTIONAL, default OFF)
=====================================================================
  Telegram ka **Bot API** poora member list kabhi nahi deta. Par MTProto
  ka `channels.getParticipants` officially **bot-callable** hai
  ("Both users and bots can use this method") — shart: bot us chat me
  admin ho, aur bot apne hi token se MTProto par login kare.

  Ye module WAHI karta hai. Do cheezein chahiye:
    1) api_id + api_hash   (my.telegram.org > API development tools)
    2) `telethon` package  (requirements me add karna hoga)

  Dono na ho to module chup-chaap OFF rehta hai — bot par koi asar nahi,
  koi crash nahi (import bhi lazy hai).

  Ye kya NAHI karta:
    • cold DM nahi (jisne bot start nahi kiya, usko message nahi)
    • broadcast channel ke subscribers nahi (Telegram wahan list nahi deta)
    • kuch store nahi karta — sirf padh kar deta hai
=====================================================================
"""

import logging

from config import (
    BOT_TOKEN,
    ROSTER_MAX,
    TG_API_HASH,
    TG_API_ID,
)

log = logging.getLogger("roster")

_client = None


def configured() -> bool:
    return bool(TG_API_ID) and bool(TG_API_HASH)


def available() -> str:
    """'' = ready. Warna insaan ki bhasha me wajah."""
    if not TG_API_ID or not TG_API_HASH:
        return "not configured"
    try:
        import importlib.util
        if importlib.util.find_spec("telethon") is None:
            return "telethon not installed"
    except Exception:
        return "telethon not installed"
    return ""


def _hint(e) -> str:
    s = str(e).lower()
    if "admin" in s or "chat_admin_required" in s:
        return "bot ko us chat me ADMIN banana padega (ye raasta admin ke bina nahi chalta)"
    if "flood" in s or "too many" in s:
        return "Telegram ne rate limit lagaya (FLOOD_WAIT) — kuch minute baad try karo"
    if "private" in s or "not found" in s or "invalid" in s:
        return "chat nahi mila — bot us chat me hai? id sahi hai?"
    if "auth" in s or "api_id" in s or "api hash" in s:
        return "api_id / api_hash galat lag raha hai (my.telegram.org se dobara copy karo)"
    return type(e).__name__


async def _get_client():
    """Bot token se MTProto client (StringSession — disk par kuch nahi likhta)."""
    global _client
    if _client is None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        _client = TelegramClient(
            StringSession(), TG_API_ID, TG_API_HASH,
            connection_retries=2, request_retries=2, timeout=20,
        )
        await _client.start(bot_token=BOT_TOKEN)
        log.info("MTProto roster client ready (bot token se login)")
    return _client


async def fetch_members(chat_id, limit=50):
    """(rows, error). rows = [{id, name, username, bot}]. error = '' ya wajah."""
    why = available()
    if why:
        return [], why
    try:
        limit = max(1, min(int(limit or 50), max(1, ROSTER_MAX)))
    except (TypeError, ValueError):
        limit = 50
    try:
        c = await _get_client()
        ent = await c.get_entity(chat_id)
        rows = []
        async for u in c.iter_participants(ent, limit=limit):
            name = " ".join(x for x in (getattr(u, "first_name", ""),
                                        getattr(u, "last_name", "")) if x)
            rows.append({"id": u.id, "name": name.strip(),
                         "username": getattr(u, "username", "") or "",
                         "bot": bool(getattr(u, "bot", False))})
        return rows, ""
    except Exception as e:
        log.warning("Roster fetch fail (%s): %s", chat_id, e)
        return [], _hint(e)


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
