"""
=====================================================================
  bot.py — AVENOR ORBIT (poora bot ek hi file mein)
=====================================================================
  Isme sab kuch hai:
    • Database (users / referrals / ban)   -> section "DATABASE"
    • AI wrapper                            -> section "AI"
    • Telegram handlers + webhook server    -> baaki sab

  Sirf "config.py" edit karni hai. Is file ko chhedne ki zaroorat nahi.

  Features:
    1) Force-join gate (config.py ke LINK_BLOCKS se) + join requests auto-approve
    2) Welcome message (photo + styled caption)
    3) AI chat — DM mein, aur group mein sirf @mention par (membership check ke saath)
    4) Referral / invite growth system

  Text styling fonts.py se hoti hai (Unicode small-caps / bold) — isliye
  ParseMode ki zaroorat nahi, aur naam mein "_" ya "*" hone par bhi
  message fail nahi hota.

  NOTE: bot apni identity (kaunse model par chalta hai) kabhi reveal nahi
  karta — wo AI_SYSTEM_PROMPT se handle hota hai.
=====================================================================
"""

import asyncio
import base64
import difflib
import json
import hmac
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from urllib.parse import quote
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import telegram
from telegram import (
    ChatPermissions,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ChatMember,
)
# Telegram ke asli error types — inhe pakadna ZAROORI hai:
#   RetryAfter -> 429 flood control (wait karke retry)
#   Forbidden  -> 403 (user ne bot block kiya / bot ko chat se nikal diya)
#   Migrated   -> group -> supergroup ho gaya (chat id badal gayi)
#   TimedOut   -> network slow
from telegram.error import (
    RetryAfter,
    Forbidden,
    TimedOut,
    ChatMigrated,          # PTB 22.x ka naam (purane me Migrated tha)
    Conflict,
    BadRequest,
    TelegramError,
)
from telegram.constants import ChatType
from telegram.ext import (
    TypeHandler,
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    InlineQueryHandler,
    MessageReactionHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types

import fonts
import roster
import storage
from config import (
    BOT_TOKEN,
    BOT_USERNAME,
    BOT_BRAND,
    ADMIN_IDS,
    LINK_BLOCKS,
    JOIN_FAIL_MODE,
    WELCOME_TEMPLATE,
    WEBHOOK_URL,
    WEBHOOK_SECRET,
    PORT,
    DB_PATH,
    MEMBERSHIP_CACHE_TTL,
    MIN_JOINS_REQUIRED,
    GUEST_MODE,
    AUTO_APPROVE_JOIN,
    DONE_MARK,
    OWNER_ID,
    OWNER_NAME,
    admin_label,
    RULES_TEXT,
    NOTIFY_OWNER,
    AI_PROMPT_FROM_ENV,
    POST_COMMAND,
    POST_CHANNELS,
    ADMIN_CHANNELS,
    GATE_CHANNELS,
    POST_ASK_IMAGE,
    POST_CREDIT_TEXT,
    POST_CREDIT_URL,
    POST_EP_EMOJI,
    POST_EP_LINE,
    POST_BADGE_CENTER,
    POST_BADGE_WIDTH,
    POST_TYPES,
    POST_FIELDS,
    POST_FIELDS_BY_TYPE,
    POST_FIELDS_LEGACY,
    POST_BADGE_BY_TYPE,
    POST_BUTTON_BY_TYPE,
    POST_BUTTON_TEXT,
    JOIN_BUTTON_TEXT,
    GATE_MESSAGE,
    GATE_BUTTON_TEXT,
    JOIN_PROMPT,
    AI_API_KEY,
    AI_MODEL,
    COMMUNITY_LINK,
    MONGO_URI_SAFE,
    MONGO_HOST,
    MONGO_DB,
    MONGO_ENABLED,
    MONGO_PASSWORD,
    MONGO_PASSWORD_STRIPPED,
    MONGO_PLACEHOLDER,
    _clean_host,
    _host_looks_wrong,
    AI_SYSTEM_PROMPT,
    BOT_BRAND_ENV_IGNORED,
    BOT_USERNAME_ENV_IGNORED,
    AI_MAX_CHARS,
    AI_CAPABILITIES_IN_PROMPT,
    AUDIT_KEEP,
    AI_THINKING_LEVEL,
    AI_BUSY_MESSAGE,
    AI_USER_WINDOW,
    AI_USER_MAX,
    AI_DAILY_MAX,
    AI_GROUP_DAILY_MAX,
    AI_TIMEOUT,
    AI_TOTAL_BUDGET,
    AI_MODELS,
    AI_MODEL_CYCLES,
    AI_BOT_LINK,
    CHANNEL_LINK,
    AUTO_APPROVE_CHANNELS,
    AUTO_APPROVE_NAMES,
    INVITE_ENABLED,
    INVITE_MIN_TURNS,
    INVITE_COOLDOWN_DAYS,
    INVITE_DECLINE_DAYS,
    INVITE_HEAVY_HOURS,
    JOIN_DM,
    JOIN_DM_COOLDOWN_H,
    EPHEMERAL_WELCOME,
    REACTION_TRACK,
    AUTO_INTRO,
    AUTOPOST_ENABLED,
    INLINE_MODE,
    INLINE_CACHE,
    GROWTH_SNAPSHOT,
    GROWTH_KEEP_DAYS,
    HUMAN_PACING,
    HUMAN_MAX_DELAY,
    HUMAN_SPLIT_CHARS,
    MSG_DEBOUNCE,
    HUMAN_REACTIONS,
    REACTION_CHANCE,
    GAP_NOTICE_HOURS,
    AI_CONCURRENCY,
    AI_TYPING_EVERY,
    AI_NOTICE_COOLDOWN,
    CB_COOLDOWN,
    MSG_DEDUPE_WINDOW,
    UPDATE_MAX_ATTEMPTS,
    thinking_level_for,
    PRIVACY_TEXT,
    PRIVACY_URL,
    DEFAULT_PRIVACY_TEXT,
    CACHE_RETENTION_DAYS,
    USER_RETENTION_DAYS,
    DROP_PENDING_UPDATES,
    WEBHOOK_CONCURRENCY,
    STORAGE_FLUSH_EVERY,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx ke GET/POST logs chhupa do (shor kam)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Build stamp — /diag aur startup log me dikhta hai, taaki pata chale ki
# server par kaunsa code chal raha hai (purana build hone par turant dikhe).
BOT_BUILD = "2026-11-26 · mahiru-persona-33 · audit-fixes"

NOT_MEMBER_STATUSES = {ChatMember.LEFT, ChatMember.BANNED}


# =====================================================================
#  STORAGE  —  saara persistence storage.py me hai
# =====================================================================
#  Pehle ye poora hissa (users, referrals, bans, chats, posts, links +
#  Mongo write-behind) isi file me tha. Audit ne ARCH-01 aur DB-01 dono
#  isi jagah pakde the: ek 550-line block jisme recovery defect tha aur
#  jise alag se test karna mumkin nahi tha.
#
#  Ab storage.py ek state machine chalata hai (CONNECTING -> READY ->
#  DEGRADED -> RECONNECTING -> READY) aur DEGRADED me har write local JSON
#  par fsync ke saath jaati hai. Yahan sirf import hai — behaviour wahi
#  hai, isliye handlers me kuch nahi badla.
from storage import (
    prune as _prune,
    _now,
    _today,
    _cid_key,
    _learned_ids,
    db_replace_chat_id,
    db_set_chat_id,
    db_upsert_chat,
    db_get_chats,
    db_add_user,
    db_src_stats,
    db_save_member_count,
    db_growth,
    db_audience_add,
    db_audience_summary,
    db_autopost_set,
    db_autopost_del,
    db_autopost_all,
    db_chat_flag,
    db_chat_flag_set,
    db_admins,
    db_owner_get,
    db_owner_set,
    db_owner_name,
    db_owner_name_set,
    db_maintenance,
    db_maintenance_get,
    db_warn_add,
    db_warn_get,
    db_warn_clear,
    db_warns_all,
    db_ref_all,
    db_lock_set,
    db_lock_get,
    db_antispam_set,
    db_antispam_get,
    db_admin_add,
    db_admin_del,
    db_post_id_set,
    db_post_ids,
    db_gate,
    db_gate_on,
    db_gate_link_set,
    db_gate_links,
    db_gate_add,
    db_gate_del,
    db_user_exists,
    db_all_users,
    db_is_banned,
    db_set_ban,
    db_add_referral,
    db_ref_count,
    db_ref_rank,
    db_stats,
    db_mark_done,
    is_done,
    db_add_request,
    db_del_request,
    db_requests_all,
    has_request,
    db_link_add,
    db_links,
    db_links_dedupe,
    db_post_save,
    db_post_get,
    db_post_all,
    db_post_update,
    audit_add,
    audit_tail,
    ai_budget_ok,
    ai_budget_spend,
    ai_usage,
    delete_user_data,
    purge_stale,
    commit_now,
    user_present,
    doc_size_now,
    update_seen,
    mark_update_seen,
    health as storage_health,
    status_text as mongo_status,
    MONGO_PASSWORD_SET,
    ping as storage_ping,
    reset_client as storage_reset_client,
    set_alert_hook,
    connect as storage_connect,
    loop as storage_loop,
    close as storage_close,
    mongo_error_hint as _mongo_error_hint,
)
import storage as _storage


# =====================================================================
#  AI  (provider/identity kabhi expose nahi hoti)
# =====================================================================
_ai_client = None

# AI calls apne thread pool me chalte hain. Default executor (jo storage aur
# vision bhi use karta hai) bhar jaye to poora bot atak jata hai — isliye
# alag, bounded pool.
_AI_POOL = ThreadPoolExecutor(
    max_workers=max(4, AI_CONCURRENCY * 2), thread_name_prefix="ai")


def _get_ai():
    """AI client — SDK-level HTTP timeout ke saath.

    Kyun: `asyncio.wait_for` sirf AWAIT cancel karta hai, chal raha THREAD
    nahi. Provider atka to thread chalta rehta hai aur worker kha jata hai;
    kuch fail messages ke baad pool bhar jata hai aur naye messages queue me
    khade reh jaate hain (bot "dead" lagta hai). SDK timeout se thread khud
    lautaata hai. Agar ye option SDK par available na ho to plain client —
    bot phir bhi chalta rahega."""
    global _ai_client
    if _ai_client is None:
        try:
            _ai_client = genai.Client(
                api_key=AI_API_KEY,
                http_options=types.HttpOptions(
                    timeout=max(5, AI_TIMEOUT) * 1000))   # milliseconds
        except Exception as e:
            log.warning("AI client timeout set nahi hua (%s) — plain client.", e)
            _ai_client = genai.Client(api_key=AI_API_KEY)
    return _ai_client


_ai_configs: dict = {}
_thinking_off = False


def _capabilities_text() -> str:
    """AI ko batata hai ki bot SACH ME kya kar sakta hai — prose me.

    Koi heading, koi bullet nahi: ye block system instruction ke aakhir me
    judta hai, aur instruction ek hi flow me rehna chahiye. List bot ke apne
    commands se BANTI hai (_COMMANDS), isliye naya command jodne par ye khud
    update ho jata hai — prompt aur code kabhi alag nahi padte.

    Sirf member-level commands. Owner ke tools ka zikr nahi — design hi ye
    hai ki admin ko upar ka tier pata na chale.
    """
    cmds = ", ".join(
        f"/{POST_COMMAND if c == 'POSTCMD' else c}"
        for c, _d, r in _COMMANDS if r == "member")
    return (
        "Your real commands are " + cmds + ", and every member can use them; "
        "answer any question about what you can do from that list only. You also "
        "answer a private message without any command, answer in a group when "
        "someone mentions you or replies to you, and with guest mode you can "
        "answer in a group you never joined. Admins and the owner have extra "
        "tools; say only that they exist and never list or describe them. You "
        "cannot join a chat by yourself, cannot read messages where you are not "
        "mentioned, cannot browse the web, open a link or read a file someone "
        "sends, cannot message a person who never started you, and you keep only "
        "the last few turns of a conversation. If someone asks you to add a "
        "feature or fix something, say the owner decides what gets built and "
        "that you can pass it on, and never promise that it is already done."
    )


def _ai_system_prompt() -> str:
    """System prompt (owner ka naam ho to wahi jodo). Ek hi jagah se."""
    sys_prompt = AI_SYSTEM_PROMPT
    if OWNER_NAME:
        sys_prompt += (
            f"\n\nThe owner of this bot is {OWNER_NAME}. If asked who owns or runs "
            f"this bot, say it is {OWNER_NAME}. Never share any user's private data."
        )
    if AI_CAPABILITIES_IN_PROMPT:
        sys_prompt += "\n\n" + _capabilities_text()
    return sys_prompt


def _get_ai_config(model: str | None = None):
    """GenerateContentConfig — MODEL ke hisaab se, cache ke saath.

    Audit AI-01: pehle poore bot ke liye EK hi config banti thi aur usme
    AI_THINKING_LEVEL jaise-ka-taisa chala jata tha. Default "minimal" tha,
    jo Gemini 3.8 Flash accept hi nahi karta — matlab PEHLI request hamesha
    validation error deti thi aur phir retry me thinking band hota tha.
    Ab level pehle model ke hisaab se validate hota hai
    (config.thinking_level_for), isliye ek bhi request bekaar nahi jaati.
    """
    key = (str(model or ""), bool(_thinking_off))
    cached = _ai_configs.get(key)
    if cached is not None:
        return cached
    sys_prompt = _ai_system_prompt()
    kwargs = {"system_instruction": sys_prompt}
    level = "" if _thinking_off else thinking_level_for(model or AI_MODEL)
    wanted = (AI_THINKING_LEVEL or "").strip().lower()
    if level and wanted and level != wanted:
        log.warning("Thinking level '%s' is not supported by %s — using '%s' "
                    "instead (config.py: AI_THINKING_LEVELS_BY_MODEL).",
                    wanted, model or AI_MODEL, level)
    if level:
        try:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        except Exception as e:
            log.warning("ThinkingConfig ban nahi payi (%s) — bina thinking chalega.", e)
    try:
        cfg = types.GenerateContentConfig(**kwargs)
    except Exception:
        # SDK/model shape alag -> thinking ke bina
        cfg = types.GenerateContentConfig(system_instruction=sys_prompt)
    _ai_configs[key] = cfg
    return cfg


def _disable_thinking(model: str | None = None):
    """Provider ne thinking config reject kiya -> poore process ke liye band.
    Ye sirf tab chalta hai jab validation se pehle hi kuch aur ho gaya ho;
    normal raaste me thinking_level_for() pehle hi theek kar deta hai."""
    global _thinking_off
    if not _thinking_off:
        _thinking_off = True
        _ai_configs.clear()
        log.warning("Thinking config disabled — %s rejected it.", model or AI_MODEL)


# --- chhoti conversation memory (per USER, per chat) -------------------
#   private chat -> key "u<user_id>"
#   group        -> key "g<chat_id>:u<user_id>"
#   (pehle sirf chat_id par key thi, isliye EK GROUP me sabki baatein ek hi
#    history me mix ho jaati thi — ab har user ki apni memory hai)
_HISTORY_TURNS = 6          # per key kitne user+bot turns yaad rakhe
_HISTORY_CHATS = 1000       # kitni keys ki memory (bound, memory leak nahi)
_history: dict = {}


def _hist(mem_key):
    """COPY return karo — warna background thread me iterate karte waqt
    event loop list badal de to 'list changed size' error aa sakta hai.
    LRU: padhne par key ko dict ke END me le jao, taaki active user
    _prune() se evict na ho (pehle ye FIFO tha)."""
    h = _history.get(mem_key)
    if h is not None:
        _history.pop(mem_key, None)
        _history[mem_key] = h
    return list(h or [])


def _hist_add(mem_key, role, text):
    h = _history.setdefault(mem_key, [])
    h.append((role, text))
    if len(h) > _HISTORY_TURNS * 2:
        del h[: len(h) - _HISTORY_TURNS * 2]
    _prune(_history, _HISTORY_CHATS)


# Emoji ko insaan jaisa rakho. Research: 6.06 billion messages ke ek keyboard
# dataset me sirf 7.1% messages me kam se kam ek emoji tha (Lu et al., 2016),
# aur do message lagatar emoji ke saath almost nahi hote.
_EMOJI_RX = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2764]")


def _limit_emoji(text, mem_key=None) -> str:
    """Reply me emoji ko rare rakho.

    • pichhle do replies me emoji tha -> is baar hata do (lagatar nahi)
    • ek message me ek se zyada -> sirf pehla rakho
    """
    if not text:
        return text
    found = _EMOJI_RX.findall(text)
    if not found:
        return text
    hist = _hist(mem_key) if mem_key is not None else []
    prev = [t for r, t in hist[-4:] if r == "model"][-2:]
    if any(_EMOJI_RX.search(t or "") for t in prev):
        cleaned = _EMOJI_RX.sub("", text)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        return cleaned.strip()
    if len(found) > 1:
        out, seen = [], False
        for ch in text:
            if _EMOJI_RX.match(ch):
                if seen:
                    continue
                seen = True
            out.append(ch)
        return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()
    return text


_MD_PAIRS = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"~~(.+?)~~", re.S), r"\1"),
    (re.compile(r"`+([^`]+)`+"), r"\1"),
    (re.compile(r"\*(.+?)\*", re.S), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
]


def _strip_md(t: str) -> str:
    """AI ke jawab se markdown hata do. Bot parse_mode use nahi karta, isliye
    * aur ` seedhe dikhte hain (user ko ganda lagta hai)."""
    if not t:
        return t or ""
    for rx, rp in _MD_PAIRS:
        t = rx.sub(rp, t)
    t = t.replace("**", "").replace("`", "")
    # bache hue * hatao, par "2*3" jaisa kuch bacha rahe
    t = re.sub(r"(?<!\d)\*(?!\d)", "", t)
    return t.strip()


def _run_ai(model: str, prompt: str, history=None) -> str:
    client = _get_ai()
    contents = []
    for role, text in (history or []):
        contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))
    resp = client.models.generate_content(
        model=model,
        contents=contents,
        config=_get_ai_config(model),
    )
    return (getattr(resp, "text", None) or "").strip()


# Retry sirf un errors par jo waqai dobara try karne layak hain.
# Pehle har error par retry hota tha — galat API key / galat model par bhi
# do-do koshish jaati thi aur user ko sirf "busy" dikhta tha.
_AI_RETRYABLE = (
    "500", "502", "503", "504", "unavailable", "internal", "deadline",
    "timeout", "timed out", "overloaded", "high demand", "429",
    "resource_exhausted", "connection", "reset by peer",
)
_AI_FATAL = ("api key", "api_key", "permission", "unauthenticated",
             "invalid argument", "not found", "400", "403", "401")


# Model ko thodi der ke liye skip karne ke liye: agar us model ka id galat
# hai, key/permission fail hai, to use dobara try karna sirf waqt barbaad
# karta hai. 10 min baad khud wapas try hota hai.
_AI_DEAD: dict = {}
_AI_DEAD_TTL = 600


def _ai_fatal(e) -> bool:
    """Ye error dobara try karne layak nahi? Sirf fatal par model skip hota
    hai — timeout / 429 / network par nahi (wo dobara try hone chahiye)."""
    s = str(e).lower()
    return any(h in s for h in _AI_FATAL)


def _mark_model_dead(model) -> None:
    _AI_DEAD[str(model)] = time.time() + _AI_DEAD_TTL
    log.warning("AI model skip (%s min): %s", _AI_DEAD_TTL // 60, model)


def _live_chain() -> list:
    """Chain se wo models hata do jo fatal error de chuke hain.

    Sab dead ho jayein to POORI chain wapas — warna user ko sirf "busy"
    milega, jabki shayad ab key theek ho chuki ho."""
    now = time.time()
    for m, until in list(_AI_DEAD.items()):
        if until <= now:
            _AI_DEAD.pop(m, None)
    chain = [m for m in AI_MODELS if m] or [AI_MODEL]
    live = [m for m in chain if m not in _AI_DEAD]
    return live or chain


# AI ki asli haalat — /diag me dikhti hai. "Kabhi jawab nahi aata" jaisi
# shikayat ka koi saboot hi nahi tha, isliye ab counters hain.
_ai_stats = {"calls": 0, "ok": 0, "fail": 0, "timeout": 0, "budget": 0, "rate": 0,
             "last_error": ""}
# Kaunsa model kitni baar fail hua — /diag me dikhta hai.
_ai_model_fails: dict = {}
# Ek saath chalne wali AI requests (provider flood + memory se bachne ke liye).
# Lazy banate hain: asyncio.Semaphore pehle await par event loop se bandh jata
# hai, aur tests har case me naya loop banate hain — warna "bound to a different
# event loop" aata.
_ai_sem = None


def _ai_limiter():
    global _ai_sem
    if _ai_sem is None:
        _ai_sem = asyncio.Semaphore(max(1, AI_CONCURRENCY))
    return _ai_sem


async def _typing_loop(bot, chat_id, stop: asyncio.Event):
    """"typing…" ko zinda rakho jab tak jawab na aaye.
    Telegram ka typing indicator ~5 second me khud gayab ho jata hai, isliye
    dheere jawab par user ko lagta tha ki bot ne suna hi nahi."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1, AI_TYPING_EVERY))
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def _typing(context, chat_id):
    """async with _typing(context, chat_id): ... — iske andar typing chalti rehti hai."""
    stop = asyncio.Event()
    task = asyncio.create_task(_typing_loop(context.bot, chat_id, stop))
    try:
        yield
    finally:
        stop.set()
        task.cancel()


# Rate limit / budget ka notice kitni der me ek baar (group me spam na ho)
_notice_at: dict = {}


def _notice_allowed(key) -> bool:
    if AI_NOTICE_COOLDOWN <= 0:
        return False
    now = time.time()
    if now - _notice_at.get(key, 0) < AI_NOTICE_COOLDOWN:
        return False
    _notice_at[key] = now
    _prune(_notice_at, 2000)
    return True


# Wahi message dobara aaye (double send / double tap) to AI dobara na chale.
# Sirf aakhri text yaad rakhte hain — per (chat, user) ek entry, bounded.
_last_text: dict = {}


def _is_duplicate_text(chat_id, uid, text) -> bool:
    """True = ye wahi message hai jo abhi-abhi aaya tha."""
    if MSG_DEDUPE_WINDOW <= 0:
        return False
    key = (int(chat_id), int(uid))
    now = time.time()
    body = str(text or "").strip()[:500]
    prev = _last_text.get(key)
    _last_text[key] = (body, now)
    _prune(_last_text, 5000)
    if not prev or not body:
        return False
    prev_body, prev_ts = prev
    return prev_body == body and (now - prev_ts) < MSG_DEDUPE_WINDOW


def _ai_link() -> str:
    """{ai_link} placeholder me jaane wala ASLI link.

    Pehle yahan LINK_BLOCKS me "ai" naam ka block dhoondha jaata tha — aisa
    block config me kabhi tha hi nahi, isliye user ko literally
    "talk to the AI directly (AI)" dikhta tha (audit F-06).
    """
    blk = next((b for b in LINK_BLOCKS
                if b["name"].strip().lower() in ("ai", "ai bot")), None)
    if blk:
        return blk["url"]
    return AI_BOT_LINK or CHANNEL_LINK or "AI"


def busy_message() -> str:
    """AI busy/down hone par user ko bhejne wala message."""
    if "{ai_link}" not in AI_BUSY_MESSAGE:
        return AI_BUSY_MESSAGE
    return AI_BUSY_MESSAGE.replace("{ai_link}", _ai_link())


# =====================================================================
#  INVITE TIMING  —  Mahiru kab apna channel / group kholti hai
# =====================================================================
#  Random NAHI. Do layer:
#    1) ye code sirf ELIGIBILITY tay karta hai (kitni baat hui, cooldown,
#       "nahi" bola ya nahi, mood heavy hai ya nahi, owner/admin hai kya)
#    2) "is waqt bolna theek hai ya nahi" AI tay karti hai (rules prompt me)
#  Isse wo kabhi bhi "join karo" nahi chipkati — sirf tab jab baat me fit ho.
_invite: dict = {}      # uid -> {"turns","last","declined","heavy"}

_HEAVY_RX = re.compile(
    r"\b(sad|depress|lonely|alone|cry|crying|hurt|broken|tired|exhausted|"
    r"anxious|panic|scared|afraid|hate myself|worthless|hopeless|suicide|"
    r"kill myself|end it|self.?harm|abuse|died|death|funeral|"
    r"akela|akeli|udaas|dukhi|rona|ro raha|thak gaya|thak gayi|pareshan|"
    r"tanha|mar jaana|marna|atmahatya)\b", re.I)

_DECLINE_RX = re.compile(
    r"^\s*(no|nahi|nah|nope|not interested|no thanks|no thank you|"
    r"mat bhejo|mat karo|band karo|stop|skip)\b", re.I)


def _invite_state(uid) -> dict:
    st = _invite.get(uid)
    if st is None:
        st = {"turns": 0, "last": 0.0, "declined": 0.0, "heavy": 0.0}
        _invite[uid] = st
        _prune(_invite, 5000)
    return st


def _note_message(uid, text) -> dict:
    """Ek hi jagah se: turn gino, heavy mood note karo, "nahi" pakdo.

    Pehle iske do function the (_note_turn + _note_decline) jo hamesha
    saath-saath, ek hi jagah se bulaaye jaate the — ek hi function kaafi hai.
    """
    st = _invite_state(uid)
    st["turns"] = int(st.get("turns", 0)) + 1
    if text and _HEAVY_RX.search(str(text)):
        st["heavy"] = time.time()
    last = float(st.get("last") or 0)
    if (last and time.time() - last < 6 * 3600 and text
            and _DECLINE_RX.match(str(text))):
        st["declined"] = time.time()
        log.info("Invite declined by %s — %s din chup.", uid, INVITE_DECLINE_DAYS)
    return st


def _invite_allowed(uid) -> bool:
    """Sirf eligibility. Sach me bolna hai ya nahi — AI tay karti hai."""
    if not INVITE_ENABLED:
        return False
    if is_privileged(uid):        # owner/admin ko kuch bechna nahi hai
        return False
    st = _invite_state(uid)
    now = time.time()
    if int(st.get("turns", 0)) < INVITE_MIN_TURNS:
        return False
    if st.get("last") and now - float(st["last"]) < INVITE_COOLDOWN_DAYS * 86400:
        return False
    if (st.get("declined")
            and now - float(st["declined"]) < INVITE_DECLINE_DAYS * 86400):
        return False
    if st.get("heavy") and now - float(st["heavy"]) < INVITE_HEAVY_HOURS * 3600:
        return False
    return True


def _mark_invited(uid) -> None:
    _invite_state(uid)["last"] = time.time()


_IST = timezone(timedelta(hours=5, minutes=30))


def _day_part(h: int) -> str:
    """24-hour clock se din ka hissa. 4 baje dopahar ko 'raat' nahi kehna."""
    if h < 5:
        return "late night"
    if h < 8:
        return "early morning"
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    if h < 21:
        return "evening"
    return "night"


# User ne waqt poocha? (sirf tab clock bhejte hain)
_TIME_ASK_RX = re.compile(
    r"\b(time|waqt|samay|clock|baje|baj[ae]|kitne baje|kya baja)\b", re.I)


def _now_context(user_text: str = "") -> str:
    """Background me sirf din ka hissa — CLOCK sirf jab user ne poocha ho.

    Do galtiyan ho chuki hain is line me:
      1) pehle 07:00-22:00 ke beech waqt bheja hi nahi jaata tha -> 4 PM par
         bot ne apne aap "itni raat ko?" maan liya (screenshot 1).
      2) phir har message par poora clock bheja -> bot ne jawab me hi dohra
         diya: "it is 4 pm and the room feels a bit too quiet" (screenshot 2).
    Insaan clock nahi bolta. Ab digits sirf tab jaate hain jab user poochhe;
    warna sirf "afternoon on Friday" — jisse na raat invent hogi, na "4 pm"
    likha ja sakega.
    """
    now = datetime.now(_IST)
    part = _day_part(now.hour)
    day = now.strftime("%A")
    if user_text and _TIME_ASK_RX.search(str(user_text)):
        clock = now.strftime("%I:%M %p").lstrip("0")
        return (f"they asked about the time: it is {clock} on {day} ({part}, "
                f"IST). Answer once, plainly, and do not repeat it")
    return (f"it is {part} on {day} where they are. Background only — never "
            f"mention the time, the hour or the day unless they ask")


_DEV_RX = re.compile(r"[\u0900-\u097F]")
_HINGLISH_RX = re.compile(
    r"\b(hai|hoon|nahi|nahin|kya|kaise|kaisi|kaisa|tum|tumhe|tumhara|aap|mera|meri|"
    r"tera|teri|accha|acha|theek|thik|karo|batao|bata|matlab|kuch|bahut|abhi|kal|aaj|"
    r"yaar|bhai|dost|pyaar|mujhe|tujhe|hum|kyun|kyu|kaun|hoga|hogi|raha|rahi|rahe|"
    r"gaya|gayi|lagta|lagti|bore|akela|akeli|udaas|khush|suno|bolo|chal|chalo)\b",
    re.I)

# Generic/robotic phrases — sirf LOG karne ke liye (user ko kuch nahi dikhta).
# Inse pata chalta hai ki prompt ka "NEVER SAY" list asar kar raha hai ya nahi.
_GENERIC_RX = re.compile(
    r"(that'?s kind of you|i'?m here for you|i'?m always here|i understand how you feel|"
    r"how can i help|anything else i can|let me know if you need|i hope this helps|"
    r"take care of yourself|stay positive|great question|as an ai\b|as a language model|"
    r"glad to be here with you|thank you for sharing)", re.I)


def _lang_hint(text) -> str:
    """User ki bhasha pakad kar AI ko saaf-saaf batao.

    Prompt me "same language" pehle se likha tha, phir bhi Hinglish sawaal ka
    jawab English me aa gaya (screenshot). Ye hint deterministic hai.
    """
    t = str(text or "")
    if _DEV_RX.search(t):
        return ("the person wrote in Hindi (Devanagari) — reply in Hindi, "
                "same script")
    if _HINGLISH_RX.search(t):
        return ("the person wrote in Hinglish (Hindi words in Latin letters) — "
                "reply in Hinglish, same style")
    return ""


def _last_question(history) -> str:
    """Pichhle jawab ka aakhri sawaal — taaki wahi sawaal dobara na poochhe.

    Screenshot me bot ne "dopahar kaisi chal rahi hai?" do message me poochha
    tha. History me jawab hote hain, isliye wahan se sawaal nikaal kar context
    me daal dete hain: "ye already poochha hai, dobara mat poochho".
    """
    for role, text in reversed(list(history or [])):
        if role != "model":
            continue
        lines = [x.strip() for x in str(text).split("\n") if x.strip()]
        for line in reversed(lines):
            if line.endswith("?"):
                return line[:140]
    return ""


async def ask_ai(prompt: str, lite: bool = False, where: str = "", mem_key=None,
                 note: str = "", invite_uid=None, gap_h=None, name="") -> str:
    """Prompt bhejo, reply lo.
    where      = context line (group ka naam ya 'a private chat').
    note       = extra context (jaise 'this user is the owner').
    mem_key    = iske hisaab se thodi conversation memory rakhi jaati hai.
    invite_uid = eligible user ho to AI ko "darwaza khol sakti ho" ki
                 permission milti hai (timing ke rules prompt me hain)."""
    raw = prompt
    history = _hist(mem_key) if mem_key is not None else []
    bits = []
    if where:
        bits.append(f"you are in {where}")
    now_bit = _now_context(raw)
    if now_bit:
        bits.append(now_bit)
    lang = _lang_hint(raw)
    if lang:
        bits.append(lang)
    # Naam har message par NAHI. 1:1 text messages ke corpus me sirf 13.6%
    # messages me naam/vocative hota hai (Asprey & Tagg, 412 messages), aur wo
    # bhi greeting me. Isliye naam sirf session ke shuru me bhejte hain.
    _nm = str(name or "").strip().replace('"', "").replace("\n", " ")[:32]
    if _nm and (gap_h is None or gap_h >= 6):
        bits.append(f"their name is {_nm} — a greeting at most, never a tag "
                    f"after a question")
    if gap_h is not None and GAP_NOTICE_HOURS > 0 and gap_h >= GAP_NOTICE_HOURS:
        bits.append(f"they were away for about {int(round(gap_h))} hour(s) — you "
                    f"may notice it once, casually, only if it fits")
    _q = _last_question(history)
    if _q:
        bits.append(f'you already asked them: "{_q}" — do not ask it again '
                    f"and do not repeat that topic")
    if note:
        bits.append(note)
    # Chhota re-anchor: persona lambi baat-cheet me drift karti hai, isliye har
    # turn par ek line — app ki taraf se, user ke text me nahi (warna user
    # khud apna 'reminder' likh sakta hai).
    bits.append("stay mahiru: warm, short, lowercase, no markdown")
    # INVITE TIMING: eligible hai ya nahi wo code tay karta hai; bolna hai ya
    # nahi wo AI tay karti hai. Isliye yahan sirf permission jata hai.
    if invite_uid is not None and _invite_allowed(invite_uid):
        bits.append(
            "you MAY open the door once, at the very end, in one short line — "
            f"channel {CHANNEL_LINK} (apps and AI tools land there) or group "
            f"{COMMUNITY_LINK} (people talk there) — but only if it truly fits "
            "what they just said. If they are sad, upset, in crisis, or just "
            "said no, do not mention it at all. It is not required: if it does "
            "not fit, say nothing about it")
    prompt = f"(Context: {'; '.join(bits)}.)\n\n{prompt}"
    # ---- MODEL CHAIN -------------------------------------------------
    # Owner ka order: Gemma 4 26B -> Gemma 4 31B -> Gemini 2.5 Flash Lite
    # -> Gemini 3.1 Flash Lite -> Gemini 3 Flash. Chain khatam ho to (time
    # bacha ho to) pehle model se dobara. Bounded: AI_MODEL_CYCLES passes +
    # AI_TOTAL_BUDGET. Bina hadd ke "dobara shuru" = infinite loop aur bill.
    chain = _live_chain()
    last_err = None
    _ai_stats["calls"] += 1
    deadline = time.monotonic() + max(1, AI_TOTAL_BUDGET)
    attempt = 0
    for cycle in range(max(1, AI_MODEL_CYCLES)):
        if deadline - time.monotonic() <= 2:
            break
        if cycle:
            # Owner ka rule: last model bhi fail ho to PEHLE model se dobara.
            log.info("AI chain: saare model fail — pehle se dobara (pass %s/%s)",
                     cycle + 1, AI_MODEL_CYCLES)
            await asyncio.sleep(min(0.4 * cycle + random.random() * 0.3, 2))
        # Har pass ko apna hissa do. Warna pehla pass poora budget kha jata
        # hai aur "wapas 1 se shuru" wala restart kabhi chalta hi nahi.
        passes_left = max(1, AI_MODEL_CYCLES - cycle)
        pass_deadline = time.monotonic() + max(
            4.0, (deadline - time.monotonic()) / passes_left)
        for mi, model in enumerate(chain):
            left = min(deadline, pass_deadline) - time.monotonic()
            if left <= 2:
                break
            remaining = max(1, len(chain) - mi)
            # Pass ka waqt bache hue models me BARABAR baanto, taaki ek atka
            # model poora waqt na kha jaye aur baaki models ko mauka hi na
            # mile (pehle har koshish ko poora 45s milta tha -> ek hang ne
            # poora chain kha liya). 8s floor: isse kam me koi model jawab
            # nahi de payega.
            per_try = max(8.0, min(float(AI_TIMEOUT), left / remaining))
            # Floor kabhi bache hue waqt se bada na ho, warna ek koshish
            # deadline ke baad bhi chalti rehti hai (total wait badh jata hai).
            per_try = max(2.0, min(per_try, left))
            attempt += 1
            try:
                # ZAROORI ("jawab kabhi aata nahi"): har koshish ka apna
                # deadline, aur semaphore se ek saath chalti requests bounded.
                async with _ai_limiter():
                    text = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            _AI_POOL, _run_ai, model, prompt, history),
                        timeout=per_try)
                if text:
                    text = _strip_md(text)      # markdown hatao (* aur ` )
                    text = _limit_emoji(text, mem_key)
                    if len(text) > AI_MAX_CHARS:
                        text = text[: AI_MAX_CHARS - 20].rstrip() + "\n\n… (shortened)"
                    if mem_key is not None:
                        _hist_add(mem_key, "user", raw)
                        _hist_add(mem_key, "model", text)
                    if invite_uid is not None:
                        low = text.lower()
                        # Link ya community ka naam aaya = invite ho gaya.
                        if ("avenor" in low or "t.me/" in low
                                or COMMUNITY_LINK.lower() in low
                                or CHANNEL_LINK.lower() in low):
                            _mark_invited(invite_uid)
                    _gm = _GENERIC_RX.search(text)
                    if _gm:
                        log.info("AI generic phrase aa gaya (%r) — prompt tune "
                                 "karna hoga.", _gm.group(0))
                    _ai_stats["ok"] += 1
                    return text
                last_err = last_err or "empty response"
                log.warning("AI ne khaali jawab diya (model=%s #%s)", model, attempt)
            except asyncio.TimeoutError:
                _ai_stats["timeout"] += 1
                last_err = f"timeout after {per_try:.0f}s"
                _ai_stats["last_error"] = f"{model}: {last_err}"
                log.warning("AI timeout (model=%s #%s, %.0fs) — agla model",
                            model, attempt, per_try)
                continue
            except Exception as e:
                last_err = e
                _ai_model_fails[str(model)] = _ai_model_fails.get(str(model), 0) + 1
                _ai_stats["last_error"] = f"{model}: {str(e)[:150]}"
                log.warning("AI fail (model=%s #%s): %s", model, attempt, e)
                low = str(e).lower()
                if "thinking" in low:
                    # Provider ne level reject kiya -> config theek karo, agla.
                    _disable_thinking(model)
                    continue
                if _ai_fatal(e):
                    # Galat key / galat model id / permission — ise dobara
                    # try karna sirf waqt barbaad karega. Agla model turant.
                    _mark_model_dead(model)
                    continue
                # Chain me har koshish DOOSRA model hai, isliye yahan backoff
                # nahi — sirf agla model try karo. (Pehle har fail par 1-3s
                # sone se, 5 model fail hone par user 8 second extra wait
                # karta tha.) Backoff sirf naye pass ki shuruaat me.
                continue
    _ai_stats["fail"] += 1
    _ai_stats["last_error"] = str(last_err or "no model answered")[:180]
    log.error("AI poora fail (%s models, %s attempts): %s",
              len(chain), attempt, last_err)
    # User ko kabhi provider ka naam nahi dikhna chahiye.
    return busy_message()


# =====================================================================
#  HELPERS
# =====================================================================
def is_admin(uid: int) -> bool:
    """Admin = env ADMIN_IDS + jo owner ne /admin se add kiye."""
    try:
        if int(uid) in ADMIN_IDS:
            return True
        return str(int(uid)) in db_admins()
    except (TypeError, ValueError):
        return False


def admin_name(uid) -> str:
    """Admin ka naam — DB wala pehle, warna ADMIN_NAMES, warna 'Admin N'."""
    try:
        nm = db_admins().get(str(int(uid))) or ""
    except (TypeError, ValueError):
        nm = ""
    return nm or admin_label(uid)


def owner_id() -> int:
    """Owner = env OWNER_ID, warna DB wala (ek baar /claim se set)."""
    try:
        return int(OWNER_ID) or db_owner_get()
    except (TypeError, ValueError):
        return db_owner_get()


def is_owner(uid: int) -> bool:
    oid = owner_id()
    return oid != 0 and int(uid) == oid


def is_privileged(uid: int) -> bool:
    """Owner ya admin — inhe force-join gate NAHI dikhta."""
    return is_owner(uid) or is_admin(uid)


def _owner_note(uid) -> str:
    """Owner bol raha hai to AI ko bata do (gate ke baahar)."""
    if is_owner(uid):
        return "the person talking to you is the owner of this bot"
    return ""


async def _notify_owner(bot, text):
    """Owner ko DM — SIRF jab NOTIFY_OWNER on ho. Warna sirf log (spam nahi).
    Sab jaankari /diag se kabhi bhi mil jaati hai."""
    log.info("NOTIFY: %s", text.replace("\n", " | "))
    if not NOTIFY_OWNER:
        return
    target = owner_id()
    if not target:
        return
    try:
        await bot.send_message(target, text)
    except Exception:
        pass


def _norm(s) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _clean_title(t) -> str:
    """Group title AI ko bhejne se pehle saaf karo — warna koi apne group ka naam
    'ignore all previous instructions...' rakh ke prompt-inject kar sakta hai."""
    if not t:
        return ""
    t = " ".join(str(t).split())              # newline / tab hata do
    t = t.replace('"', "'").replace("(", "[").replace(")", "]")
    return t[:64]


def _tokens(s):
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) >= 3]


def _fuzzy_in(needle, hay, cutoff=0.8):
    return any(difflib.SequenceMatcher(None, needle, h).ratio() >= cutoff for h in hay)


def match_block_by_title(title):
    """Title ko LINK_BLOCKS ke naam se match karo.
    1) exact (normalized)   2) substring   3) token-based (naam thoda alag ho)
    #3 isliye: block ka naam config me chhota hota hai aur asli channel ka naam
    lamba — pehle ye bind hi nahi hota tha."""
    t = _norm(title)
    if not t:
        return None
    for b in LINK_BLOCKS:
        if _norm(b["name"]) == t:
            return b
    for b in LINK_BLOCKS:
        n = _norm(b["name"])
        if len(n) >= 6 and (n in t or t in n):
            return b
    hay = _tokens(title)
    if not hay:
        return None
    hits = []
    for b in LINK_BLOCKS:
        toks = _tokens(b["name"])
        if toks and all(_fuzzy_in(w, hay) for w in toks):
            hits.append(b)
    return hits[0] if len(hits) == 1 else None   # ambiguous ho to kuch na karo


def _is_tap_block(b) -> bool:
    """Ye block "tap" se verify hota hai? (Kisi bot ka link — wahan getChatMember
    chalta hi nahi, isliye membership check mumkin nahi.)"""
    return (b.get("verify") or "member") == "tap"


def _gate_blocks() -> list:
    """Force-join ke liye zaroori blocks.

    Do jagah se aate hain:
      1) config LINK_BLOCKS jinme require_join=True
      2) /gate se runtime add kiye gaye (DB me) — inke liye redeploy nahi
         chahiye. Isse owner 5 channel bana kar turant strict gate laga sakta hai.
    """
    out, seen = [], set()

    def _add(b):
        # Ek hi channel do baar na aaye (naam duplicate ho sakta hai — id key hai)
        k = _cid_key(b.get("id") or b.get("name"))
        if k in seen:
            return
        seen.add(k)
        out.append(b)

    links = db_gate_links()
    # 1) config me permanent gate channels (owner ne ids di hain) — 2 per row
    for i, b in enumerate(GATE_CHANNELS):
        if not (b.get("id") or b.get("url")):
            continue
        cid = b.get("id")
        url = links.get(str(cid)) or b.get("url")
        _add({**b, "url": url, "require_join": True, "row": 100 + i // 2})
    # 2) /gate se runtime add kiye gaye
    g = db_gate()
    if g.get("on"):
        for b in g.get("blocks") or []:
            if b.get("url"):
                _add({**b, "require_join": True, "row": 99})
    # 3) config LINK_BLOCKS jinme require_join=True
    for b in LINK_BLOCKS:
        if b.get("require_join"):
            _add(b)
    return out


def enforced_blocks():
    """Jo blocks force-join ke liye zaroori hain.
    "tap" wale blocks ki chat id nahi hoti — unhe bhi shamil karo, warna wo
    gate se chup-chaap bahar ho jaate."""
    learned = _learned_ids()
    out = []
    for b in _gate_blocks():
        if not b.get("require_join"):
            continue
        if _is_tap_block(b):
            out.append({**b})
            continue
        cid = b.get("id") or learned.get(b["name"])
        if cid:
            out.append({**b, "id": cid})
    return out


# chhota membership cache: getChatMember API calls kam karne ke liye
_member_cache: dict[tuple[int, int], tuple[bool, float]] = {}

# Check button ka anti-spam cooldown (user_id -> last time)
_check_cd: dict = {}

# AI ke liye per-user rate limit (paid quota bachane ke liye)
_ai_hits: dict = {}


def _ai_allowed(uid) -> bool:
    """Ek user AI_USER_WINDOW second me max AI_USER_MAX AI calls kar sakta hai.
    LRU: access par key end me — active user evict na ho."""
    now = time.time()
    hits = _ai_hits.pop(uid, None)
    if hits is None:
        hits = []
    _ai_hits[uid] = hits
    while hits and now - hits[0] > AI_USER_WINDOW:
        hits.pop(0)
    if len(hits) >= AI_USER_MAX:
        return False
    hits.append(now)
    _prune(_ai_hits, 5000)
    return True


def _ai_budget_blocked(group_id=None) -> bool:
    """Roz ka poora budget (aur group ka budget) khatam?
    Per-user limit 15/60s tha — par 200 users milkar wahi 15/60s use karte
    hain, to poore bot ka kharcha kabhi nahi rukta tha. Ab ek global cap bhi."""
    return not ai_budget_ok(group_id, AI_DAILY_MAX, AI_GROUP_DAILY_MAX)


def _ai_count(group_id=None):
    """Ek AI call ka hisaab rakho (budget ke liye)."""
    try:
        ai_budget_spend(group_id)
    except Exception as e:
        log.warning("AI usage ledger fail: %s", e)


AI_BUDGET_MESSAGE = (
    "I have reached today's answer limit, so I cannot reply right now.\n\n"
    "Please try again tomorrow — or talk to the AI directly ({ai_link})."
)


def budget_message() -> str:
    if "{ai_link}" not in AI_BUDGET_MESSAGE:
        return AI_BUDGET_MESSAGE
    return AI_BUDGET_MESSAGE.replace("{ai_link}", _ai_link())


# bot ka apna info — har group message par get_me() API call na ho (speed)
_bot_me = None


async def _get_me(context):
    global _bot_me
    if _bot_me is None:
        _bot_me = await context.bot.get_me()
    return _bot_me


async def _is_member(context, chat_id, user_id, force=False):
    """True = member, False = not member, None = check hi nahi ho paya."""
    key = (int(user_id), str(chat_id))
    now = time.time()
    cached = _member_cache.get(key)
    if not force and cached and now - cached[1] < MEMBERSHIP_CACHE_TTL:
        return cached[0]
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        ok = member.status not in NOT_MEMBER_STATUSES
    except Exception as e:
        # Bot us chat mein admin nahi / id galat -> verify nahi ho paya.
        # None = "pata nahi". JOIN_FAIL_MODE "open" ho to joined maano.
        log.warning("Membership check fail for %s: %s", chat_id, e)
        result = True if JOIN_FAIL_MODE == "open" else None
        _member_cache[key] = (result, now)
        _prune(_member_cache, 5000)   # warna fail hote hote cache badhta rehta
        return result
    _member_cache[key] = (ok, now)
    _prune(_member_cache, 5000)
    return ok


def required_count(total=None) -> int:
    """Kitne channels join/request karna zaroori hai (MIN_JOINS_REQUIRED se).
    total=None -> config me jo bhi require_join: True blocks hain, WO SAARE.
    Pehle ye sirf VERIFIABLE blocks ginta tha (jin ki id ho) — isliye jin
    channels ki id nahi thi wo chup-chaap maaf ho jaate the aur "all" ka
    matlab 4 ke bajaye kam ho jata tha. (Asli bug.)"""
    if total is None:
        total = len(_gate_blocks())
    if total <= 0:
        return 0
    val = str(MIN_JOINS_REQUIRED).strip().lower()
    if val == "all":
        return total
    try:
        return max(1, min(int(val), total))
    except ValueError:
        return total


def has_required_blocks() -> bool:
    """config me koi bhi require_join block hai?"""
    return bool(_gate_blocks())


async def join_status(context, user_id, force=False):
    """(joined, missing, unknown, need).
    unknown = jinhe check hi nahi kar paye (bot admin nahi / id galat).
    need SIRF un blocks par count hota hai jo sach me verify ho sake — isliye
    agar kuch bhi verify na ho to need 0, aur gate user ko block nahi karta.
    force=True -> cache ignore karo, fresh check (Check button ke liye)."""
    joined, missing, unknown = [], [], []
    for chat in enforced_blocks():
        if _is_tap_block(chat):
            (joined if is_done(user_id, chat["name"]) else missing).append(chat)
            continue
        # Join REQUEST hamara apna record hai (chat_join_request se) — iske liye
        # channel check ki zaroorat HI nahi. Pehle ye dekho, warna jab bot admin
        # na ho (_is_member -> None) to request count hi nahi hoti thi aur
        # keyboard par tick kabhi nahi lagta tha. (Asli bug.)
        if has_request(user_id, chat["id"]):
            joined.append(chat)
            continue
        r = await _is_member(context, chat["id"], user_id, force=force)
        if r is True:
            joined.append(chat)
        elif r is False:
            missing.append(chat)
        else:
            unknown.append(chat)
    # need KUL require_join blocks par — sirf verifiable par nahi, warna
    # unverifiable blocks chup-chaap maaf ho jaate the.
    need = required_count()
    return joined, missing, unknown, need


# chat_id -> bot ka last dekha status (duplicate notification rokne ke liye)
_chat_status: dict = {}


def gate_report():
    """Gate ki ASLI haalat: kaunse require_join blocks verify ho sakte hain,
    aur kaunse nahi (unki chat id hi nahi hai).
    Bina id ke bot us chat ki membership check hi nahi kar sakta — isliye gate
    wahan sirf dikhawe ka hota hai. Ye function us baat ko saaf karta hai."""
    learned = _learned_ids()
    ready, no_id = [], []
    for blk in _gate_blocks():
        if not blk.get("require_join"):
            continue
        if _is_tap_block(blk):
            ready.append(blk["name"])      # tap se verify hota hai, id ki zaroorat nahi
            continue
        cid = blk.get("id") or learned.get(blk["name"])
        (ready if cid else no_id).append(blk["name"])
    return ready, no_id


async def needs_join(context, user_id, force=False) -> bool:
    """User ko gate dikhana hai? SIRF OWNER ko nahi — admin aur member dono
    ko dikhta hai (owner ke paas sab pehle se hai, usko kuch join nahi karna)."""
    if is_owner(user_id):
        return False
    if not has_required_blocks():
        return False
    joined, _missing, _unknown, need = await join_status(context, user_id, force=force)
    if need > 0:
        return len(joined) < need
    # Kuch bhi verify nahi ho paya (chat id nahi / bot admin nahi).
    # Gate ko CHHUPAO MAT — pehle ye ek hi baar dikhta tha, isliye
    # "Verify" button ek baar dikh kar hamesha gayab ho jata tha.
    # Ab har baar dikhta hai. (Verify fail-closed hai — link tab tak nahi milega.)
    return True


# Kya button colour sach me payload me ja raha hai? (PTB version ki parwah nahi)
def _style_ok() -> bool:
    try:
        b = InlineKeyboardButton("x", url="https://t.me", api_kwargs={"style": "primary"})
        return b.to_dict().get("style") == "primary"
    except Exception:
        return False


_STYLE_OK = _style_ok()


def _btn(label, **kw):
    """InlineKeyboardButton + button colour.

    `style` ko api_kwargs ke through bhejte hain, PTB ke `style=` param se nahi.
    Wajah: `style=` param sirf PTB >= 22.7 me hai — purane PTB par wo TypeError
    deta (aur colour chupchaap gayab ho jata). api_kwargs purane PTB par bhi
    seedha payload me chala jata hai, isliye colour har version par jaata hai.
    Values: "primary" (blue), "success" (green), "danger" (red)."""
    style = kw.pop("style", None)
    api_kwargs = dict(kw.pop("api_kwargs", None) or {})
    if style:
        api_kwargs["style"] = style
    if api_kwargs:
        kw["api_kwargs"] = api_kwargs
    return InlineKeyboardButton(label, **kw)


def _post_only_chats() -> set:
    """Jo chats sirf POST TARGET hain (LINK_BLOCKS me nahi).

    Owner ne kaha: "in channels ko bot yaad nahi rakhna chahiye, warna unke
    users bhi apne count me aa jayenge." Isliye in chats ke liye bot
    learn_chat / audience / growth — kuch nahi karta.
    """
    block_ids = {_cid_key(b.get("id")) for b in LINK_BLOCKS if b.get("id")}
    block_ids |= {_cid_key(v) for v in _learned_ids().values()}
    out = set()
    for row in POST_CHANNELS:
        for _name, cid in row:
            k = _cid_key(_norm_chat_id(cid))
            if k not in block_ids:
                out.add(k)
    # Admin ke channels bhi sirf post-target hain — bot unhe yaad nahi rakhta
    # (warna unke users bhi apne count me aa jate).
    for _name, cid in ADMIN_CHANNELS:
        k = _cid_key(_norm_chat_id(cid))
        if k not in block_ids:
            out.add(k)
    # Username wale post targets ka numeric id bhi (jo humne seekha)
    for key, cid in db_post_ids().items():
        if _cid_key(cid) not in block_ids:
            out.add(_cid_key(cid))
            out.add(str(key))
    return out


def _post_only_username(chat) -> bool:
    """Kya ye chat POST_CHANNELS me username se hai (LINK_BLOCKS me nahi)?

    Audit F-05: dono taraf case ko lowercase karo. Pehle candidate lowercase
    hota tha par block ids case-preserving the, isliye owner ke apne channel
    ("@AvenorFeed") "post-only target" maan liye jaate the — learn_chat
    skip ho jata tha aur /chats me nahi dikhte the.
    """
    uname = str(getattr(chat, "username", "") or "").strip().lower()
    if not uname:
        return False
    block_ids = {_cid_key(b.get("id")).lower() for b in LINK_BLOCKS if b.get("id")}
    for row in POST_CHANNELS:
        for _name, cid in row:
            c = str(cid).strip().lower().lstrip("@")
            if c == uname and _cid_key("@" + c).lower() not in block_ids:
                return True
    for _name, cid in ADMIN_CHANNELS:
        c = str(cid).strip().lower().lstrip("@")
        if c == uname and _cid_key("@" + c).lower() not in block_ids:
            return True
    return False


def _is_post_only(chat_id) -> bool:
    return _cid_key(chat_id) in _post_only_chats()


# _cid_key ab storage.py se aata hai (ek hi jagah define hai).


def _block_chat_id(blk):
    """Us block ki chat id (config se ya runtime me seekhi hui).
    int(-100123...) YA "@username" — dono ho sakte hain."""
    cid = blk.get("id") or _learned_ids().get(blk["name"])
    if not cid:
        return None
    try:
        return int(cid)
    except (TypeError, ValueError):
        return str(cid).strip()


def link_keyboard(extra_rows=None, joined=None, tap_done=None, blocks=None):
    """config.py ke LINK_BLOCKS se buttons banao.
    Jinke "row" number same hai, wo ek hi row mein (side-by-side) aate hain.
    `joined` = jin chat ids ki membership verify ho chuki (joined ya request) —
    unke button par ✓ lagta hai aur wo green ho jate hain, taaki user ko turant
    dikhe kaunsa channel ho gaya aur kaunsa baaki hai."""
    done = {_cid_key(x) for x in (joined or [])}
    taps = set(tap_done or [])
    rows = []
    by_row: dict = {}
    order = []
    for b in (blocks if blocks is not None else LINK_BLOCKS):
        r = b.get("row", len(order) + 1)
        if r not in by_row:
            by_row[r] = []
            order.append(r)
        if _is_tap_block(b):
            # Bot ka link — membership check mumkin nahi, tap par mark hota hai.
            ok = b["name"] in taps
            label = (DONE_MARK + " " if ok else "") + fonts.bold(b["name"])
            by_row[r].append(_btn(label, callback_data=f"gateai:{b['name']}",
                                  style="success" if ok else "primary"))
            continue
        cid = _block_chat_id(b)
        ok = cid is not None and _cid_key(cid) in done
        label = (DONE_MARK + " " if ok else "") + fonts.bold(b["name"])   # buttons par emoji nahi
        by_row[r].append(_btn(label, url=b["url"],
                              style="success" if ok else "primary"))
    for r in order:
        rows.append(by_row[r])
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def build_welcome(member, chat_title: str) -> str:
    """Styled welcome caption (small-caps + bold)."""
    sc, b = fonts.small_caps, fonts.bold
    name = member.first_name or "Member"
    full = member.full_name or name
    return (
        f"{sc('Welcome')}, {b(name)}\n\n"
        f"{sc('You have joined')} {b(chat_title or 'the group')}\n\n"
        f"{sc('Name')}   : {b(full)}\n"
        f"{sc('ID')}     : {member.id}\n"   # plain digits — copy karke config me daal sakein
        f"{sc('Joined')} : {b(_today())}\n\n"
        f"{sc('Ask anything any time')} — {b(BOT_BRAND)} {sc('is here to help')}."
    )


def _tap_done_names(user_id) -> list:
    """Jo "tap" wale blocks user ne ho-gaya mark kar liye."""
    return [b["name"] for b in LINK_BLOCKS if _is_tap_block(b) and is_done(user_id, b["name"])]


async def _ensure_gate_links(context) -> int:
    """Gate channels ke ASLI join link banao (invite link), ek hi baar.

    Zaroori kyun: private channel ka `t.me/c/...` link sirf member ke liye
    khulta hai — jo user join karne aaya hai wo member hi nahi hai, uske liye
    wo dead link hai. Invite link (`t.me/+...`) hi kaam karta hai, aur wo bot
    bana sakta hai (us channel me admin hone par).
    """
    links = db_gate_links()
    made = 0
    for b in _gate_blocks():
        cid = b.get("id")
        if not cid or str(cid) in links:
            continue
        try:
            link = await context.bot.create_chat_invite_link(
                cid, name=f"{BOT_BRAND[:20]} gate")
            db_gate_link_set(cid, link.invite_link)
            made += 1
        except Exception as e:
            log.warning("Gate invite link fail (%s): %s", b.get("name"), e)
    if made:
        log.info("Gate invite links banaye: %s", made)
    return made


async def send_join_gate(update, context, edit=False, joined=None):
    await _ensure_gate_links(context)
    text = GATE_MESSAGE
    kb = link_keyboard(extra_rows=[
        [_btn(GATE_BUTTON_TEXT, callback_data="check_join", style="success")],
    ], joined=joined, tap_done=_tap_done_names(update.effective_user.id),
        blocks=_gate_blocks())
    sent = None
    if edit and update.callback_query:
        sent = await _safe(update.callback_query.edit_message_text, text, reply_markup=kb)
        if sent is None:
            sent = update.callback_query.message
    else:
        sent = await _safe(update.effective_message.reply_text, text, reply_markup=kb)
    uid = update.effective_user.id
    if sent is not None and getattr(sent, "message_id", None):
        _gate_msg[uid] = (getattr(sent, "chat_id", update.effective_chat.id),
                          sent.message_id)
        _prune(_gate_msg, 5000)


async def _set_commands(app: Application):
    """Telegram ke '/' menu me commands dikhao — warna user ko lagta hai ki
    commands hain hi nahi. Default = member ke commands; ADMIN_IDS ko uske
    admin commands; OWNER ko sabhi."""
    from telegram import (BotCommand, BotCommandScopeDefault, BotCommandScopeChat)
    desc = {c: d for c, d, _r in _COMMANDS}

    def _bc(name):
        c = POST_COMMAND if name == "POSTCMD" else name
        return BotCommand(c, desc.get(name, name))

    # MEMBER ko sirf 6 commands dikhte hain — baaki sab menu se gayab.
    # (Handlers waise hi kaam karte hain, par menu saaf rehta hai.)
    # MENU MINIMAL rakho (owner ne kaha: "menu me bahut saare commands
    # clutter lag rahe hain"). Baaki sab /owner panel ke buttons me hai —
    # commands type karne par ab bhi chalti hain, bas menu me nahi dikhti.
    #
    #   member : user ke 6
    #   admin  : wahi 6 + /admin
    #   owner  : wahi 6 + /admin + /owner   (total 8)
    member = [_bc(c) for c in _MEMBER_MENU]
    admin = member + [_bc("admin")]
    owner = member + [_bc("admin"), _bc("owner")]
    try:
        await app.bot.set_my_commands(member, scope=BotCommandScopeDefault())
        log.info("Commands menu set (%s default commands).", len(member))
    except Exception as e:
        log.warning("setMyCommands (default) fail: %s", e)
    for uid in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=uid))
        except Exception:
            pass
    _oid = owner_id()
    if _oid:
        try:
            await app.bot.set_my_commands(owner, scope=BotCommandScopeChat(chat_id=_oid))
            log.info("Owner command menu set (%s commands).", len(owner))
        except Exception:
            pass


async def verify_blocks_on_startup(app: Application):
    """Start par check: har enforced chat ki id hai? bot admin hai?
    Jo problem mile wo admin ko DM bhi bhejo — sirf log me chhodo mat."""
    log.info("Storage: %s", mongo_status())
    log.info("Build %s | name: %s | username: %s", BOT_BUILD, BOT_BRAND, BOT_USERNAME)
    try:
        _me0 = await app.bot.get_me()
        _sup = getattr(_me0, "supports_guest_queries", None)
        log.info("Guest mode: %s | BotFather supports_guest_queries: %s",
                 "ON" if GUEST_MODE else "OFF",
                 "unknown (PTB is field ko nahi deta)" if _sup is None else _sup)
    except Exception:
        pass
    if BOT_BRAND_ENV_IGNORED or BOT_USERNAME_ENV_IGNORED:
        log.warning("Env me PURANA naam pada hai (BOT_BRAND / BOT_USERNAME) — "
                    "ignore kiya ja raha hai, code se '%s' chal raha hai. "
                    "Render > Environment se unhe delete ya update karo.", BOT_BRAND)
    log.info("PTB %s | button colour: %s | AI prompt: %s",
             telegram.__version__,
             "supported" if _STYLE_OK else "NOT supported",
             "ENV OVERRIDE (Render me AI_SYSTEM_PROMPT set hai!)"
             if AI_PROMPT_FROM_ENV else "built-in")
    try:
        _sample = link_keyboard(extra_rows=[
            [_btn(GATE_BUTTON_TEXT, callback_data="check_join", style="success")]]).to_dict()
        log.info("button payload jo Telegram ko ja raha hai: %s", json.dumps(_sample))
    except Exception as e:
        log.warning("Button payload sample fail: %s", e)
    if not owner_id():
        log.warning("Koi owner set nahi hai — /claim se ek baar owner banao "
                    "(ya Render me OWNER_ID daalo). Tab tak owner commands band.")
    _ready, _no_id = gate_report()
    if _no_id:
        log.warning("GATE NOT ENFORCING — no chat id for: %s. "
                    "Add the bot as ADMIN there (or /setid). Until then the gate "
                    "shows but cannot block anyone.", ", ".join(_no_id))
    else:
        log.info("Gate is READY — checking %s channel(s).", len(_ready))
    await _set_commands(app)
    try:
        me = await app.bot.get_me()
    except Exception as e:
        log.warning("Startup check skip — get_me fail: %s", e)
        return
    learned = _learned_ids()
    problems = []
    for b in LINK_BLOCKS:
        if not b.get("require_join"):
            continue
        cid = b.get("id") or learned.get(b["name"])
        if not cid:
            log.warning("Block '%s' ki numeric id nahi hai — verify nahi hoga.", b["name"])
            problems.append(f"• {b['name']}: no ID")
            continue
        try:
            member = await app.bot.get_chat_member(chat_id=cid, user_id=me.id)
            if member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                log.warning("Bot '%s' mein ADMIN nahi hai.", b["name"])
                problems.append(f"• {b['name']}: the bot is not an admin")
        except Exception as e:
            log.warning("Block '%s' verify fail (id=%s): %s", b["name"], cid, e)
            problems.append(f"• {b['name']}: check failed ({type(e).__name__})")
    # DB corrupt hui thi? (rename ho ke <db>.corrupt-* bani hai) -> owner ko batao
    try:
        _d = os.path.dirname(DB_PATH) or "."
        _base = os.path.basename(DB_PATH)
        _bad = sorted(f for f in os.listdir(_d) if f.startswith(_base + ".corrupt-"))
        if _bad:
            problems.append(
                f"• The DB was corrupt — {len(_bad)} file(s) set aside (your data is safe): "
                + ", ".join(_bad[-3:]))
    except Exception:
        pass
    if problems:
        text = (
            "⚠️ The force-join gate will not fully work yet:\n\n"
            + "\n".join(problems)
            + "\n\nFix: make the bot an ADMIN in that channel/group — it learns the id "
            "the moment it is added. Or use /getid (forward a message) / /setid."
        )
        await _notify_owner(app.bot, text)


# =====================================================================
#  COMMANDS
# =====================================================================
def _start_source(args) -> str:
    """Deep link se source tag nikalo.

    t.me/bot?start=src_youtube  -> "youtube"
    t.me/bot?start=ref_123      -> "ref"
    t.me/bot?start=pCODE        -> post link, koi source nahi
    Yehi growth ka sabse kaam ka hissa hai: har jagah ka apna code, aur
    bot ko pata rehta hai kaun kahan se aaya.
    """
    if not args:
        return ""
    a = str(args[0])
    if a.startswith("src_"):
        return a[4:].strip().lower()[:40] or "src"
    if a.startswith("ref_"):
        return "ref"
    return ""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_add_user(user, bump_start=True, src=_start_source(context.args))

    if db_is_banned(user.id):
        await update.effective_message.reply_text("You have been blocked from using this bot.")
        return
    # Maintenance: AI band, sirf owner ke commands chalte hain.
    if db_maintenance_get() and not is_owner(user.id):
        await _safe(update.effective_message.reply_text,
                    "The bot is under maintenance right now. Please try again later.")
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"Hi {user.first_name} — please message me in private: @{context.bot.username}"
        )
        return

    # Inline mode se aaya sawaal: /start ask_<base64>
    if context.args:
        _ask_q = _unpack_ask(context.args[0])
        if _ask_q:
            if not _ai_allowed(user.id):
                await update.effective_message.reply_text(
                    f"{fonts.small_caps('Slow down')} — {fonts.small_caps('try again in a little while')}.")
                return
            if _ai_budget_blocked():
                _ai_stats["budget"] += 1
                await update.effective_message.reply_text(budget_message())
                return
            await _answer_private(context, update.effective_chat.id, user, _ask_q,
                                  _gap_hours(user.id))
            return

    # /post ka deep link: /start p<code>
    #   Flow: link click -> bot chat (auto start) -> PEHLE channel gate
    #   (4 links join/request) -> pass hone par hi asli "Request to Join" link.
    if context.args and context.args[0].startswith("p"):
        code = context.args[0][1:]
        p = db_post_get(code)
        if p:
            if await needs_join(context, user.id, force=True):
                _pending_post[user.id] = code
                _prune(_pending_post, 5000)
                await send_join_gate(update, context)
            else:
                await _post_welcome(update, context, p)
            return
        # Post nahi mili (DB reset / code purana) — chup na raho, batao.
        await update.effective_message.reply_text(
            "This post link has expired or is no longer available.")
        return

    # Referral: /start ref_12345
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                # Referrer asli hona chahiye aur banned na ho — warna koi bhi
                # fake id bhej ke referral count badha sakta hai.
                if (referrer_id != user.id
                        and db_user_exists(referrer_id)
                        and not db_is_banned(referrer_id)
                        and db_add_referral(referrer_id, user.id)):
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"Someone joined through your link. Total invites: {db_ref_count(referrer_id)}",
                        )
                    except Exception:
                        pass
            except ValueError:
                pass

    # DM me bot se baat karne ke liye koi join/request zaroori NAHI — bas
    # channels ke 5 links yahan bhi dikhte hain (join karna chahe to kar le).
    # Owner/admin ko verify ki zaroorat NAHI — unke liye seedha. Sirf links
    # reference ke liye, Verify button nahi.
    # Owner ke liye kuch nahi (na channels, na Verify) — seedha welcome.
    # Admin aur member dono ko wahi links + Verify milte hain.
    # Welcome — bas ek saaf message. Koi channel button nahi, koi Verify
    # button nahi (owner ne kaha: ye dono hata do). Channel ke naam bas
    # message me aate hain; link Mahiru zaroorat par khud share karti hai.
    head = (f"{fonts.bold('Welcome, ' + (user.first_name or 'friend'))}\n\n"
            f"I am {fonts.bold(BOT_BRAND)}. Say hi — or just tell me what is "
            f"on your mind.")
    # Channel ke naam yahan NAHI likhte — Mahiru khud zaroorat par batati hai
    # (invite timing system). Welcome bas welcome hai.
    await update.effective_message.reply_text(head)


# (command, short description, min role)   role: member | admin | owner
# POSTCMD = POST_COMMAND ka placeholder (naam config se aata hai).
_COMMANDS = [
    # (command, chhota description, min role)   role: member | admin | owner
    # Description JITNI CHHOTI, UTNA SAFAF — lambi line mobile par 2-3 line
    # le leti hai aur poora list bhara-bhara lagta hai.
    ("start",    "open the bot, see our channels",     "member"),
    ("help",     "show this list",                     "member"),
    ("id",       "your Telegram user ID",              "member"),
    ("ai",       "ask the assistant a question",       "member"),
    ("rules",    "read the community rules",           "member"),
    ("report",   "report a message to the admins",     "member"),
    ("ref",      "your invite link and count",         "member"),
    ("privacy",  "what data is stored, and how to erase it", "member"),
    ("delete_my_data", "erase everything stored about you", "member"),
    ("admins",     "list the team",                    "owner"),
    ("settitle",   "rename this group",                "owner"),
    ("invitelink", "create an invite link",            "owner"),
    ("kick",       "remove someone",                   "admin"),
    ("unkick",     "undo a kick or ban",               "admin"),
    ("del",        "delete a message (reply)",         "admin"),
    ("pin",        "pin a message (reply)",            "admin"),
    ("promote",    "make someone an admin",            "owner"),
    ("POSTCMD",    "build a channel post",             "admin"),
    ("posts",      "list your posts",                  "admin"),
    ("links",      "saved links, duplicates merged",   "owner"),
    ("stats",     "bot and community overview",        "owner"),
    ("diag",      "setup check: gate, IDs, perms",     "owner"),
    ("mongo",     "test the MongoDB connection",       "owner"),
    ("chats",     "every chat the bot is in",          "owner"),
    ("audience",  "who is in your audience, and from where", "owner"),
    ("sync",      "re-check every chat the bot knows",    "owner"),
    ("members",   "real member list (needs MTProto setup)", "owner"),
    ("autopost",  "daily auto message in this chat",      "owner"),
    ("admin",     "admin panel — post and tools",        "admin"),
    ("claim",     "first-run owner setup",               "hidden"),
    ("gate",      "force-join channels (strict)",        "owner"),
    ("export",    "backup every post link",              "owner"),
    ("maintenance", "pause or resume the AI",            "owner"),
    ("user",      "full card for one user",              "owner"),
    ("top",       "top referrers",                       "owner"),
    ("backup",    "send the database file",              "owner"),
    ("mute",      "stop a member from talking",          "owner"),
    ("unmute",    "let a member talk again",             "owner"),
    ("warn",      "warn a member (3 = removed)",         "owner"),
    ("unwarn",    "clear someone's warnings",            "owner"),
    ("warns",     "who has warnings here",               "owner"),
    ("purge",     "delete messages from a reply",        "owner"),
    ("lock",      "freeze the group",                    "owner"),
    ("unlock",    "unfreeze the group",                  "owner"),
    ("antispam",  "auto-delete link spam",               "owner"),
    ("unpin",     "unpin a message",                     "owner"),
    ("perms",     "the bot's permissions here",        "owner"),
    ("getid",     "get a chat's numeric ID",           "owner"),
    ("setid",     "bind a block to a chat ID",         "owner"),
    ("broadcast", "message everyone who started",      "owner"),
    ("ban",       "ban someone from the bot",          "owner"),
    ("unban",     "lift a bot ban",                    "owner"),
    ("audit",     "recent admin actions",               "owner"),
    ("btnstyle",  "test button colours",               "owner"),
    ("owner",     "owner panel — everything in one place", "owner"),
]


# Menu me kaun-kaun dikhega (handlers sabhi kaam karte rahenge).
# MENU MINIMAL: rules / report / privacy menu me NAHI — wo /help ke buttons
# hain. Commands type karne par ab bhi chalti hain.
_MEMBER_MENU = ["start", "help", "ai"]
# ADMIN ko sirf ye — posting + halki moderation. Owner wali koi cheez nahi.
_ADMIN_MENU = ["POSTCMD", "posts", "del", "pin", "kick", "unkick"]


def _owner_actions() -> dict:
    """Owner panel ke buttons -> command function. Lazy dict (functions neeche
    define hain, isliye call ke waqt hi dekhe jaate hain)."""
    return {
        "stats": cmd_stats, "diag": cmd_diag, "chats": cmd_chats,
        "audience": cmd_audience, "sync": cmd_sync, "mongo": cmd_mongo,
        "links": cmd_links, "posts": cmd_posts, "audit": cmd_audit,
        "broadcast": cmd_broadcast, "btnstyle": cmd_btnstyle,
        "admins": cmd_admins, "perms": cmd_perms, "getid": cmd_getid,
        "setid": cmd_setid, "ban": cmd_ban, "unban": cmd_unban,
        "members": cmd_members, "autopost": cmd_autopost,
        "admin": cmd_admin, "gate": cmd_gate, "export": cmd_export,
        "maintenance": cmd_maintenance, "user": cmd_user, "top": cmd_top,
        "backup": cmd_backup, "mute": cmd_mute, "unmute": cmd_unmute,
        "warn": cmd_warn, "unwarn": cmd_unwarn, "warns": cmd_warns,
        "purge": cmd_purge, "lock": cmd_lock, "unlock": cmd_unlock,
        "antispam": cmd_antispam, "unpin": cmd_unpin,
    }


def _owner_panel_kb() -> InlineKeyboardMarkup:
    # Sirf wo buttons jo DM se dabane par ASLI kaam karte hain.
    # Jo group me chalte hain ya argument maangte hain (perms, getid, setid,
    # ban, unban, autopost, btnstyle) wo panel se hata diye — command se
    # ab bhi chalte hain, par button dabane par sirf "usage" dikhta tha.
    acts = ["stats", "diag", "chats", "audience", "sync", "mongo",
            "links", "posts", "audit", "broadcast", "admins",
            "members", "admin", "gate", "export",
            "maintenance", "user", "top", "backup"]
    labels = {"stats": "📊 Stats", "diag": "🩺 Diag", "chats": "💬 Chats",
              "audience": "👥 Audience", "sync": "🔄 Sync", "mongo": "🗄 Mongo",
              "links": "🔗 Links", "posts": "📝 Posts", "audit": "🧾 Audit",
              "broadcast": "📣 Broadcast", "btnstyle": "🎨 Buttons",
              "admins": "🛡 Admins", "perms": "🔑 Perms", "getid": "🆔 Get ID",
              "setid": "📌 Set ID", "ban": "⛔ Ban", "unban": "✅ Unban",
              "members": "📇 Members", "autopost": "⏰ Auto-post",
              "admin": "🛡 Add Admin", "gate": "🚪 Gate",
              "export": "📤 Export", "maintenance": "🛠 Maintenance",
              "user": "👤 User", "top": "🏆 Top", "backup": "💾 Backup"}
    rows, row = [], []
    for a in acts:
        row.append(_btn(labels.get(a, a), callback_data=f"owner:{a}",
                        style="primary"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """EK HI BAAR: agar koi owner set nahi hai to tum owner ban jao.

    Bootstrap ke liye — OWNER_ID Render me daalna zaroori nahi. Ek baar
    owner ban jane ke baad ye command dead ho jati hai (dusra koi claim
    nahi kar sakta).
    """
    uid = update.effective_user.id
    cur = owner_id()
    if cur:
        if cur == uid:
            await update.effective_message.reply_text(
                "You are already the owner. Open /owner for the panel.")
        else:
            await update.effective_message.reply_text(
                "An owner is already set, so claiming is closed.")
        return
    db_owner_set(uid)
    try:
        db_owner_name_set(update.effective_user.first_name or "")
    except Exception:
        pass
    log.warning("OWNER CLAIMED: %s (%s)", uid, update.effective_user.full_name)
    await update.effective_message.reply_text(
        f"✅ owner set: {uid}\n\n"
        f"ab /owner se poora panel khulega, aur /admin se admins bana sakte ho.")


async def cmd_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER PANEL — saari owner powers ek jagah.

    Non-owner ko kuch bhi nahi milta: na panel, na hint, na "not allowed".
    (Agar pata chal jaye ki upar koi tier hai, to admin bhi apne se kam
    samajhne lagega — isliye poora silence.)
    """
    if not is_owner(update.effective_user.id):
        return
    sc, bd = fonts.small_caps, fonts.bold
    text = (f"{bd('OWNER PANEL')}\n\n"
            f"{sc('sab kuch yahin se')}. {sc('jo button dabao, wahi command chalega')}.\n\n"
            f"{sc('text commands')}: /stats /diag /audience /sync /broadcast …")
    await update.effective_message.reply_text(
        text, reply_markup=_owner_panel_kb())


def _help_kb() -> InlineKeyboardMarkup:
    """Rules | Report  (half-half), neeche Privacy (full)."""
    return InlineKeyboardMarkup([
        [_btn("📜 Rules", callback_data="help:rules", style="primary"),
         _btn("🚩 Report", callback_data="help:report", style="primary")],
        [_btn("🔒 Privacy", callback_data="help:privacy", style="primary")],
    ])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help — har role ke liye ALAG. Member ko sirf apni, admin ko apni,
    owner ko poori. Sab ek hi list me thoos kar nahi bhejte."""
    uid = update.effective_user.id
    sc, bd = fonts.small_caps, fonts.bold
    sep = "━━━━━━━━━━━━━━━━━"

    if is_owner(uid):
        out = [f"{bd('HELP')} · {bd('OWNER')}", "",
               f"{sc('you run everything')}", sep,
               bd("PANEL"),
               "  /owner — the whole panel in one place",
               "  /admin — post and admin tools", "",
               bd("COMMUNITY"),
               "  /chats — chats the bot is in",
               "  /sync — re-check them now",
               "  /audience — who is where",
               "  /links — saved links",
               "  /gate — which channels must be joined",
               "  /export — back up every post link", "",
               bd("POSTS"),
               "  /post — build a post (your 2 channels)",
               "  /posts — posts you made",
               "  /autopost — daily auto-post", "",
               bd("MODERATION"),
               "  /mute /unmute — stop someone talking",
               "  /warn /unwarn /warns — warnings (3 = removed)",
               "  /purge — delete from a reply onward",
               "  /lock /unlock — freeze the group",
               "  /antispam — auto-delete link spam",
               "  /pin /unpin /del — message tools",
               "  /ban /unban — bot access",
               "  /settitle /invitelink /promote — group tools", "",
               bd("GROWTH"),
               "  /stats /top /members /ref",
               "  /broadcast — message every user", "",
               bd("SYSTEM"),
               "  /diag /mongo /backup /maintenance",
               "  /user — full card for one person",
               "  /audit /perms /getid /setid /btnstyle /claim", "",
               f"{sc('member commands')}: /start /ai /rules /report /privacy"]
    elif is_admin(uid):
        out = [f"{bd('HELP')} · {bd('ADMIN')}", "",
               f"{sc('your job: turn an anime or movie link into a share link')}",
               sep,
               bd("YOUR PANEL"),
               "  /admin — open it (Post · Posts)", "",
               bd("MAKE A LINK"),
               "  /post — pick Anime or Movie, send the link,",
               "          and you get a share link back",
               "  /posts — links you already made", "",
               bd("IN A GROUP"),
               "  reply to a message, then:",
               "  /del — delete it",
               "  /pin — pin it",
               "  /kick — remove the sender",
               "  /unkick — let them back in", "",
               f"{sc('you do not post into any channel yourself')}"]
    else:
        out = [f"{bd('HELP')}", "",
               f"{sc('I am Mahiru - talk to me like a person')}", sep,
               bd("TALK"),
               "  just write to me — anything, in any language",
               "  /ai — ask me a question directly", "",
               bd("HERE"),
               "  /start — say hello again",
               "  /help — this list", "",
               bd("THE REST"),
               "  use the buttons below"]
    await _send_long(update.effective_message, "\n".join(out),
                     reply_markup=_help_kb())


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ID PLAIN digits me — bold font math digits (𝟱𝟱𝟱) deta tha, jinhe copy
    # karke OWNER_ID / ADMIN_IDS me daalne par int() fail karta tha.
    await update.effective_message.reply_text(
        f"{fonts.small_caps('Your Telegram ID')}: {update.effective_user.id}"
    )


async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    link = f"https://t.me/{context.bot.username}?start=ref_{user.id}"
    count = db_ref_count(user.id)
    rank = db_ref_rank(user.id)
    rank_line = f"\n{fonts.small_caps('Rank')}: {fonts.bold('#' + str(rank))}" if rank else ""
    text = (
        f"{fonts.small_caps('Your referral link')}\n\n"
        f"{link}\n\n"
        f"{fonts.small_caps('Your invites')}: {fonts.bold(str(count))}{rank_line}\n\n"
        "Share this link — when people join, your count goes up."
    )
    kb = InlineKeyboardMarkup([[_btn(
        "Share", url=("https://t.me/share/url?url="
                      + quote(link, safe="") + "&text=Try%20this%20bot!"),
        style="primary")]])
    await update.effective_message.reply_text(text, reply_markup=kb)


# -------- admin --------
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: poora overview — apne users, AUR har channel ka LIVE member count
    (getChatMemberCount se — Telegram ka asli aankda, guess nahi)."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    s = db_stats()
    out = [bd("STATS"), "", bd("YOUR BOT"), ""]
    out.append(f"{sc('users who started')} : {bd(str(s['total_users']))}")
    # "reachable by DM" jhooth tha: jin logon ne bot block kar diya hai wo bhi
    # isi ginti me aate hain. Ab wahi likha hai jo sach me pata hai.
    out.append(f"{sc('users not banned')}   : {bd(str(max(0, s['total_users'] - s['banned'])))}")
    out.append(f"{sc('banned')}            : {bd(str(s['banned']))}")
    out.append(f"{sc('/start count')}     : {bd(str(s['total_started']))}")
    out.append(f"{sc('referrals')}         : {bd(str(s['referrals']))}")

    # Channels — Telegram se LIVE member count
    learned = _learned_ids()
    out += ["", bd("YOUR CHANNELS"), ""]
    total_members = 0
    counted = 0
    for blk in LINK_BLOCKS:
        cid = blk.get("id") or learned.get(blk["name"])
        if not cid:
            out.append(f"{blk['name']}  -  {sc('no chat ID yet')}")
            continue
        try:
            n = await context.bot.get_chat_member_count(cid)
            total_members += int(n)
            counted += 1
            out.append(f"{blk['name']}  -  {bd(str(n))} {sc('members')}")
        except Exception as e:
            out.append(f"{blk['name']}  -  {sc('?')} ({_err_hint(e)})")
    if counted:
        out += ["", f"{sc('total across channels')} : {bd(str(total_members))}"]

    # Chats by type
    chats = db_get_chats()
    by_type = {}
    for c in chats.values():
        t = str(c.get("type") or "?")
        by_type[t] = by_type.get(t, 0) + 1
    out += ["", bd("CHATS THE BOT IS IN"),
            f"{sc('total')} : {bd(str(len(chats)))}"]
    for t, n in sorted(by_type.items()):
        out.append(f"   {sc(t)} : {bd(str(n))}")
    if not counted:
        out += ["", f"{sc('tip')}: make the bot an ADMIN in a channel and its "
                    "member count shows up here."]
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sirf un users ko bhejta hai jinhone bot ko /start kiya hai (opt-in).
    Ye OWNER-only hai (poori list par mass DM — bada asar)."""
    if not await _need_owner(update):
        return
    msg = update.effective_message
    if not context.args and not msg.reply_to_message:
        # Bina message ke -> poora TOTAL dikhao (sirf is chat ka nahi).
        chats = db_get_chats()
        s = db_stats()
        by_type = {}
        for c in chats.values():
            t = str(c.get("type") or "?")
            by_type[t] = by_type.get(t, 0) + 1
        sc, bd = fonts.small_caps, fonts.bold
        out = [bd("BROADCAST"), "",
               f"{sc('total users')}      : {bd(str(s['total_users']))}",
               f"{sc('users not banned')}  : {bd(str(max(0, s['total_users'] - s['banned'])))}",
               f"{sc('banned (skipped)')} : {bd(str(s['banned']))}",
               f"{sc('total started')}    : {bd(str(s['total_started']))}",
               "",
               f"{sc('chats the bot is in')} : {bd(str(len(chats)))}"]
        for t, n in sorted(by_type.items()):
            out.append(f"   {sc(t)} : {bd(str(n))}")
        out += ["",
                f"{sc('note')}: the bot can only DM people who have started it. "
                "It cannot list the members of a group or channel.",
                "",
                "Use: /broadcast <message>  (or reply to a message with /broadcast)"]
        await _send_long(msg, "\n".join(out))
        return
    if context.args:
        text = " ".join(context.args)
    else:
        r = msg.reply_to_message
        text = (r.text or r.caption or "").strip()   # media par text None hota hai
    if not text:
        await msg.reply_text(
            "No text found (no caption on the media either). Use: /broadcast <message>"
        )
        return
    users = db_all_users()
    sent = blocked = failed = 0
    for u in users:
        if db_is_banned(u["id"]):
            continue
        try:
            await context.bot.send_message(u["id"], text)
            sent += 1
        except RetryAfter as e:
            # 429 flood — batai hui der ruk kar EK retry
            await asyncio.sleep(min(int(getattr(e, "retry_after", 3) or 3) + 1, 60))
            try:
                await context.bot.send_message(u["id"], text)
                sent += 1
            except Exception:
                failed += 1
        except Forbidden:
            blocked += 1          # user ne bot block kar diya / account deleted
        except Exception:
            failed += 1
        # Telegram: ~1 msg/sec per chat, bulk me ~30/sec. 0.05-0.15s jitter
        # safe hai, par 1000+ users par RetryAfter aane par upar wait ho jata hai.
        await asyncio.sleep(random.uniform(0.05, 0.15))
    await msg.reply_text(
        f"Broadcast done.\n"
        f"Sent: {sent}\n"
        f"Blocked the bot: {blocked}\n"
        f"Failed: {failed}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot-wide ban — OWNER-only."""
    if not await _need_owner(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Use: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
        db_set_ban(uid, True)
        await update.effective_message.reply_text(f"Banned: {uid}")
    except ValueError:
        await update.effective_message.reply_text("Invalid user id.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot-wide unban — OWNER-only."""
    if not await _need_owner(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Use: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
        db_set_ban(uid, False)
        await update.effective_message.reply_text(f"Unbanned: {uid}")
    except ValueError:
        await update.effective_message.reply_text("Invalid user id.")


def _forwarded_chat(msg):
    """Forwarded message se ORIGINAL chat nikalo.
    Bot API 7.0 (aur PTB 22.8) me `forward_from_chat` hata diya gaya hai —
    ab `forward_origin` aata hai. Dono handle karte hain, warna /getid
    hamesha 'Forward a message...' hi kehta rehta tha."""
    o = getattr(msg, "forward_origin", None)
    if o is not None:
        # user/hidden forward -> None
        return getattr(o, "chat", None) or getattr(o, "sender_chat", None)
    return getattr(msg, "forward_from_chat", None)   # purani API


async def _report_forwarded_chat(msg, fch):
    """Forwarded chat ka id batao aur (match kare to) block me save kar do."""
    b = match_block_by_title(fch.title) if getattr(fch, "title", None) else None
    saved = ""
    if b:
        db_set_chat_id(b["name"], fch.id)
        saved = (f"\n\nSaved: \"{b['name']}\" -> {fch.id}\n"
                 "Force-join verification will use this now.")
    await msg.reply_text(
        f"Chat: {getattr(fch, 'title', '-')}\n"
        f"Numeric id: {fch.id}\n"
        f"Type: {getattr(fch, 'type', '-')}{saved}"
    )


async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: channel/group ka message bot ko FORWARD karo -> numeric id milega
    (aur seedha save bhi ho jayega — config edit karne ki zaroorat nahi)."""
    if not await _need_owner(update):
        return
    msg = update.effective_message
    # (a) khud ye message forward ho, (b) ya iske reply me forward ho — dono chalao.
    fch = _forwarded_chat(msg)
    if fch is None and msg.reply_to_message is not None:
        fch = _forwarded_chat(msg.reply_to_message)
    if fch is not None:
        await _report_forwarded_chat(msg, fch)
    else:
        await msg.reply_text(
            "Forward any message from that channel/group here — I will give you the "
            "numeric id and save it myself.\n"
            "(The bot must be an admin in that chat.)"
        )


async def cmd_setid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /setid <block name> <numeric id> — id yaad rakho (config edit nahi)."""
    if not await _need_owner(update):
        return
    args = context.args or []
    if len(args) < 2:
        names = "\n".join(f"  - {b['name']}" for b in LINK_BLOCKS)
        await update.effective_message.reply_text(
            "Use: /setid <block name> <numeric id>\n\nBlocks:\n" + names
        )
        return
    *name_parts, raw_id = args
    name = " ".join(name_parts)
    try:
        cid = int(raw_id)
    except ValueError:
        await update.effective_message.reply_text("A numeric id is needed, e.g. -1001234567890")
        return
    b = match_block_by_title(name)
    if not b:
        await update.effective_message.reply_text(
            f"Block '{name}' not found. Send /setid with nothing after it to see the list."
        )
        return
    db_set_chat_id(b["name"], cid)
    await update.effective_message.reply_text(
        f"Saved: \"{b['name']}\" -> {cid}\nVerification will use this now."
    )


_RIGHTS = (
    "can_invite_users", "can_delete_messages", "can_restrict_members",
    "can_pin_messages", "can_manage_chat", "can_post_messages", "can_edit_messages",
    "can_promote_members",
)


async def _safe(fn, *a, _retries=1, **k):
    """Koi bhi Telegram API call isi se chalao. Crash nahi hota:
      • 429 (RetryAfter) -> batai hui der ruk kar dobara try
      • 403 (Forbidden)  -> chhod do (bot block/kick ho gaya) — dobara try bekaar
      • Migrated         -> group supergroup ban gaya, log karo
      • TimedOut         -> ek retry
      • baaki Telegram error -> log karo, None return
    Return: response ya None."""
    for attempt in range(_retries + 1):
        try:
            return await fn(*a, **k)
        except RetryAfter as e:
            wait = min(int(getattr(e, "retry_after", 3) or 3) + 1, 60)
            log.warning("Flood control (429) — %ss ruk raha hoon", wait)
            await asyncio.sleep(wait)
        except Forbidden as e:
            log.info("Forbidden — bot block/kick ho gaya: %s", e)
            return None
        except ChatMigrated as e:
            # Group supergroup ban gaya — purani chat id dead ho gayi.
            log.warning("Chat migrate ho gayi -> %s", getattr(e, "new_chat_id", "?"))
            # ChatMigrated me old_chat_id hota hi NAHI — sirf new_chat_id.
            # Isliye purani id ka pata nahi chalta; on_migrate handler us
            # service-message se theek karta hai jo migration par aata hai.
            return None
        except Conflict as e:
            log.error("409 Conflict — do bot instances chal rahe hain? %s", e)
            return None
        except TimedOut:
            if attempt >= _retries:
                log.warning("Telegram request timeout")
                return None
            await asyncio.sleep(1)
        except BadRequest as e:
            log.warning("BadRequest: %s", e)
            return None
        except TelegramError as e:
            log.warning("Telegram error: %s", e)
            return None
    return None


def _err_hint(e) -> str:
    """Telegram error ko simple bhasha me."""
    s = str(e).lower()
    if "forbidden" in s:
        return "the bot is not in that chat (removed?)"
    if "not found" in s:
        return "the id is wrong"
    if "inaccessible" in s:
        return "the bot is not an admin"
    if "not enough rights" in s or "chat_admin_required" in s:
        return "the bot does not have that permission"
    if "method is available only" in s or "chat_write_forbidden" in s:
        return "that command does not work in this chat"
    if "user not found" in s or "participant" in s:
        return "that user is not in this chat"
    return type(e).__name__


_SEP = "━━━━━━━━━━━━━━━━━"


def _split_text(text, limit) -> list:
    """Text ko limit ke andar tukdon me todo — line par, aur lambi line ko
    word boundary par. _send_long aur _bubbles dono YAHI use karte hain
    (pehle dono ke apne alag splitter the — wahi logic do baar likha hua)."""
    out, cur = [], ""
    for line in (text or "").split("\n"):
        while len(line) > limit:          # ek hi line bahut lambi -> tod do
            if cur:
                out.append(cur)
                cur = ""
            cut = line.rfind(" ", 0, limit)
            if cut < limit // 2:          # space bahut pehle -> beech se kaato
                cut = limit
            out.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


async def _send_long(message, text, limit=3800, reply_markup=None):
    """Telegram ki 4096 limit — lamba text tukdon me bhejo, warna error aata hai.
    Har chunk _safe se jaata hai (429/403 par crash nahi).

    reply_markup sirf aakhri chunk par lagta hai (ek hi keyboard, neeche)."""
    if len(text) <= limit:
        await _safe(message.reply_text, text, reply_markup=reply_markup)
        return
    chunks = _split_text(text, limit)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await _safe(message.reply_text, chunk,
                    reply_markup=reply_markup if last else None)


# =====================================================================
#  HUMAN PACING  —  insaan jaisa timing
# =====================================================================
#  Research: dynamic delay + typing cue se "humanness" aur satisfaction dono
#  badhte hain; aur asli chat me jawab ek bhaari paragraph me nahi, 2-3 chhote
#  messages me aata hai. Isliye lamba jawab bubbles me jaata hai, har bubble
#  se pehle thodi "typing" ke saath. Sab capped: poora extra intezaar
#  HUMAN_MAX_DELAY se zyada nahi (user ko intezaar ka ehsaas na ho).
def _bubbles(text: str) -> list:
    """Ek lamba jawab 2-3 chhote messages me todo. Chhota reply ek hi rehta hai."""
    t = (text or "").strip()
    if not t:
        return []
    if not HUMAN_PACING or len(t) <= HUMAN_SPLIT_CHARS:
        return [t]
    parts = [x for x in _split_text(t, HUMAN_SPLIT_CHARS) if x.strip()]
    # 3 se zyada bubble spam lagte hain — baaki aakhri message me jodo.
    if len(parts) > 3:
        parts = parts[:2] + ["\n".join(parts[2:])]
    return parts or [t]


def _typing_delay(text: str) -> float:
    """Ek bubble ke liye typing ka andaza: 0.2s + ~90 char per second, aur
    per bubble 0.9s se zyada nahi.

    Pehle 55 char/s tha aur cap HUMAN_MAX_DELAY (4s) — isliye jawab AI se aa
    chuka hota tha par user ko 3-4 second baad dikhta tha. Wahi "bot slow
    hai" wali feeling thi; asli AI latency isse alag cheez hai."""
    return min(0.9, 0.2 + len(text or "") / 90.0)


async def _send_human(bot, chat_id, text, reply_to=None):
    """Jawab insaan ki tarah bhejo: thodi der typing, phir bubble."""
    parts = _bubbles(text)
    if not parts:
        return
    budget = HUMAN_MAX_DELAY
    for i, part in enumerate(parts):
        if HUMAN_PACING:
            # Aakhri bubbles ke liye thodi jagah bacha kar rakho, warna pehla
            # bubble poora budget kha jaata hai aur baaki turant gir jaate hain.
            reserve = 0.3 * (len(parts) - 1 - i)
            delay = min(max(0.0, budget - reserve), _typing_delay(part))
            budget -= delay
            if delay > 0:
                try:
                    await bot.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
                await asyncio.sleep(delay)
        kw = {"reply_to_message_id": reply_to} if (reply_to and i == 0) else {}
        await _safe(bot.send_message, chat_id, part, **kw)


async def _known_chats_lines(context, me, skip=None) -> list:
    """Bot jin chats me hai unki LIVE list (id, title, type, bot ka status,
    aur kaunse block se juda hai)."""
    known = db_get_chats()
    sc, bd = fonts.small_caps, fonts.bold
    out = [_SEP, f"{bd('CHATS BOT IS IN')} ({len(known)})", _SEP]
    if not known:
        out.append(f"{sc('no chat recorded yet')}.")
        out.append("")
        out.append(f"{sc('how to fill it')}:")
        out.append("  • send any message in that group (works if the bot is an admin)")
        out.append("  • post something in the channel")
        out.append("  • or re-promote the bot — it will pick it up at once")
        out.append("")
        return out
    learned = _learned_ids()
    bound = {}
    for blk in LINK_BLOCKS:
        c = blk.get("id") or learned.get(blk["name"])
        if c:
            bound[_cid_key(c)] = blk["name"]   # @username bhi chalega
    items = sorted(known.items(), key=lambda kv: kv[1].get("title", "").lower())
    _skip = skip or set()
    # Jo chat upar block ke roop me already dikh chuki hai, use dobara na dikhao
    # (yahi wajah thi ki ek hi channel 2-3 baar dikh raha tha).
    items = [(k, c) for k, c in items
             if _cid_key(c.get("id") or k) not in _skip]
    if not items:
        return []
    out[1] = f"{bd('CHATS BOT IS IN')} ({len(items)})"
    for _k, c in items[:15]:
        cid = c.get("id")
        try:
            bm = await context.bot.get_chat_member(chat_id=cid, user_id=me.id)
            st = bm.status
        except Exception as e:
            st = f"FAIL - {_err_hint(e)}"
        out.append(f"{c.get('title') or '?'}  [{c.get('type') or '?'}]")
        out.append(f"   {sc('id')}    : {cid}")
        out.append(f"   {sc('bot')}   : {st}")
        try:
            n = await context.bot.get_chat_member_count(cid)
            out.append(f"   {sc('members')} : {n}")
        except Exception:
            pass
        out.append(f"   {sc('block')} : {bound.get(_cid_key(cid), '-') if cid else '-'}")
    if len(items) > 15:
        out.append(f"… +{len(items) - 15} more")
    out.append("")
    _ready, _no_id = gate_report()
    if _no_id:
        out.append(_SEP)
        out.append(f"{bd('SET THE GATE IDS')}")
        out.append(_SEP)
        out.append(f"{sc('these blocks have no chat id yet, so the gate cannot check them')}:")
        for n in _no_id:
            out.append(f"  - {n}")
        out.append("")
        out.append(f"{sc('copy a chat id from above, then paste')}:")
        for n in _no_id:
            out.append(f"  /setid {n} <chat_id>")
        out.append("")
    return out


async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: bot jin chats me hai, unki poori list."""
    if not await _need_owner(update):
        return
    me = await _get_me(context)
    lines = await _known_chats_lines(context, me)
    await _send_long(update.effective_message, "\n".join(lines).strip())


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: is chat ke members ki ASLI list (MTProto roster).

    Bot API poora member list kabhi nahi deta. MTProto ka
    `channels.getParticipants` officially bot-callable hai — par uske liye
    api_id/api_hash + `telethon` chahiye, aur bot ko us chat me ADMIN hona
    chahiye. Ye read-only list hai: isse DM nahi bhejte.
    """
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    why = roster.available()
    if why == "not configured":
        await update.effective_message.reply_text(
            f"{bd('ROSTER OFF')}\n\n"
            f"{sc('you need three things')}:\n"
            f"1) my.telegram.org > API development tools > app banao "
            f"-> api_id + api_hash milenge\n"
            f"2) Render > Environment: TG_API_ID, TG_API_HASH\n"
            f"3) add telethon to requirements\n\n"
            f"{sc('this feature is optional - the bot runs fine without it')}")
        return
    if why:
        await update.effective_message.reply_text(
            f"{bd('ROSTER OFF')} — {why}\n\n"
            f"{sc('add telethon to requirements, then redeploy')}.")
        return
    chat = update.effective_chat
    if str(chat.type) not in ("group", "supergroup", "channel"):
        await update.effective_message.reply_text(
            f"{sc('ye command kisi group/channel ke andar chalao')}.")
        return
    limit = 50
    if context.args:
        try:
            limit = max(1, min(int(context.args[0]), 200))
        except ValueError:
            pass
    rows, err = await roster.fetch_members(chat.id, limit)
    if err:
        await update.effective_message.reply_text(f"{bd('FAIL')} — {err}")
        return
    if not rows:
        await update.effective_message.reply_text(f"{sc('no members found')}.")
        return
    out = [bd("MEMBERS"),
           f"{sc('chat')} : {chat.title or chat.id}",
           f"{sc('showing')} : {len(rows)}", ""]
    for r in rows:
        tag = f"@{r['username']}" if r["username"] else "-"
        mark = " [bot]" if r["bot"] else ""
        out.append(f"{r['name'] or '-'}{mark}")
        out.append(f"   {sc('id')} {r['id']}   {tag}")
    out += ["", f"{sc('read-only')} - {sc('no DMs are sent from this list')}"]
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: bot jin chats me hai, unhe dobara check karo aur id refresh karo.

    Bot khud kisi chat me join NAHI kar sakta. Jab koi use add karta hai tab
    `my_chat_member` update aata hai — par agar us waqt bot band tha (deploy),
    to wo chat DB me aa hi nahi paati. /sync usi gap ko bharta hai: DB ke
    saare chats + config ke ids par getChat chalata hai, title/id/status
    refresh karta hai, aur batata hai kaunsa chat pahunch me hai.
    """
    if not await _need_owner(update):
        return
    me = await _get_me(context)
    sc, bd = fonts.small_caps, fonts.bold
    targets, seen = [], set()
    for c in db_get_chats().values():
        cid = c.get("id")
        if cid is not None and str(cid) not in seen:
            seen.add(str(cid))
            targets.append((str(cid), c.get("title") or "?"))
    for blk in LINK_BLOCKS:
        cid = blk.get("id") or _learned_ids().get(blk["name"])
        if cid and str(cid) not in seen:
            seen.add(str(cid))
            targets.append((str(cid), blk["name"]))
    out = [bd("SYNC"), ""]
    if not targets:
        out.append(sc("nothing to check yet — add the bot to a group first"))
        await _send_long(update.effective_message, "\n".join(out))
        return
    ok = bad = 0
    for cid, label in targets:
        try:
            ch = await context.bot.get_chat(cid)
            bm = await context.bot.get_chat_member(chat_id=ch.id, user_id=me.id)
            db_upsert_chat(ch.id, ch.title or "", str(ch.type or ""), bm.status)
            b = match_block_by_title(ch.title or "")
            if b:
                db_set_chat_id(b["name"], ch.id)
            out.append(f"✅ {ch.title or label}")
            out.append(f"   {sc('id')} {ch.id}   {sc('bot')} {bm.status}")
            ok += 1
        except Exception as e:
            out.append(f"❌ {label} — {_err_hint(e)}")
            bad += 1
    out += ["", f"{sc('reachable')}: {bd(str(ok))}   {sc('failed')}: {bd(str(bad))}"]
    if bad:
        out.append("")
        out.append(f"{sc('what failed means')}: the bot is not in that chat, or the id is wrong.")
        out.append(f"{sc('fix')}: add the bot there as an admin - the id will come on its own.")
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: saare post links ek text me — backup ke liye.

    Admin jo post banata hai, uska code + deep link DB me rehta hai. Bot
    update/redeploy hone par wo tabhi bacha rehta hai jab MongoDB connected
    ho (Render ka local disk ephemeral hai). Isliye /export se copy kar lo.
    """
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    allp = db_post_all()
    if not allp:
        await update.effective_message.reply_text(
            f"{sc('no posts yet')} - create one with /post")
        return
    out = [bd("EXPORT"), f"{sc('posts')} : {len(allp)}",
           f"{sc('storage')} : {mongo_status()}", ""]
    items = sorted(allp.items(), key=lambda kv: (kv[1] or {}).get("created", ""))
    for code, p in items:
        out.append(f"{p.get('Name') or '-'}")
        out.append(f"   {sc('code')} : {code}")
        out.append(f"   {_deep_link(context, code)}")
        ch = p.get("channel_ids") or []
        out.append(f"   {sc('channels')} : {', '.join(str(c) for c in ch) or '-'}")
        out.append(f"   {sc('posted')} : {p.get('posted')}")
        out.append("")
    out.append(f"{sc('copy this text somewhere safe - it is your backup if Mongo goes down')}")
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: audience ka SACH, aur Telegram ki asli hadd.

    "bot ko group me daalo, saare members ki list nikaalo, sabko DM karo" —
    Bot API me ye possible NAHI hai:
      • bot khud kisi chat me join nahi kar sakta (aisa method hi nahi)
      • kisi bhi method se poora member LIST nahi milta (sirf count, aur
        ek-ek user ka status jab uski id pehle se pata ho)
      • jisne bot ko /start nahi kiya, usko DM nahi ja sakta (403 Forbidden)
    Jo possible hai wo yahan hai: apna audience khud banana — jo log bot se
    mile, source ke hisaab se — aur live counts + growth.
    """
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    me = await _get_me(context)
    s = db_stats()
    src = db_src_stats()
    learned = _learned_ids()
    out = [bd("AUDIENCE"), "", _SEP,
           f"{sc('users bot knows')} : {bd(str(s['total_users']))}",
           f"{sc('not banned')}      : {bd(str(max(0, s['total_users'] - s['banned'])))}",
           _SEP]
    for blk in LINK_BLOCKS:
        cid = blk.get("id") or learned.get(blk["name"])
        if not cid:
            continue
        try:
            n = await context.bot.get_chat_member_count(cid)
        except Exception as e:
            out.append(f"{blk['name']} — {sc('?')} ({_err_hint(e)})")
            continue
        g = db_growth(cid)
        days = sorted(g)[-7:]
        line = f"{blk['name']} — {bd(str(n))} {sc('members')}"
        if len(days) >= 2:
            delta = int(g[days[-1]]) - int(g[days[0]])
            line += f"  ({'+' if delta >= 0 else ''}{delta} {sc('in')} {len(days)}d)"
        out.append(line)
    # APNA LEDGER — Telegram list nahi deta, isliye jo updates aate hain unse
    # banta hai: join request, join/leave, aur message bhejne wale.
    summ = db_audience_summary()
    if summ:
        out += ["", bd("PEOPLE THE BOT KNOWS IN EACH CHAT"), ""]
        for cid, info in list(summ.items())[:6]:
            title = next((b["name"] for b in LINK_BLOCKS
                          if _cid_key(b.get("id")) == _cid_key(cid)), "")
            if not title:
                title = str((db_get_chats().get(str(cid)) or {}).get("title") or cid)
            out.append(f"{title} — {bd(str(info['known']))} {sc('known')} "
                       f"({info['in_chat']} {sc('in')}, {info['left']} {sc('left')})")
            top = sorted(info["sources"].items(), key=lambda kv: -kv[1])[:3]
            if top:
                out.append("   " + ", ".join(f"{k}:{v}" for k, v in top))

    out += ["", bd("WHERE THEY CAME FROM"), ""]
    if not src:
        out.append(f"  {sc('nothing recorded yet')}")
    for k, v in sorted(src.items(), key=lambda kv: -kv[1])[:8]:
        out.append(f"  {k} : {bd(str(v))}")
    out += ["", bd("TELEGRAM'S RULE"), "",
            f"  {sc('a bot cannot join a chat by itself and cannot list members.')}",
            f"  {sc('it also cannot DM someone who never started it — except one case:')}",
            f"  {sc('a join REQUEST to a chat where the bot is admin. that one is allowed,')}",
            f"  {sc('and this bot already uses it.')}", "",
            f"  {sc('so the audience is built like this')}:",
            f"  • {sc('join-request DM')} — {sc('the only legal door to a stranger')}",
            f"  • {sc('inline mode')} — @{me.username} {sc('in any chat, no adding')}",
            f"  • {sc('one deep link per source')} — t.me/{me.username}?start=src_instagram",
            f"  • {sc('referral links')} — /ref",
            f"  • {sc('bot in a group')} — {sc('one group of 2000 reaches 2000 people')}"]
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_mongo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: MongoDB ka LIVE test — username/host dikhao aur abhi connect karke
    dekho. Password kabhi print nahi hota."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    from urllib.parse import urlsplit
    from urllib.parse import parse_qs
    host_field = _clean_host(MONGO_HOST)
    host_bad = _host_looks_wrong(host_field) if MONGO_HOST else ""
    parts = urlsplit(MONGO_URI_SAFE)
    user = (parts.username or "-") if MONGO_URI_SAFE else "-"
    host = (parts.hostname or "-") if MONGO_URI_SAFE else "-"
    q = parse_qs(parts.query)
    pw_len = len(MONGO_PASSWORD) if MONGO_PASSWORD_SET else len(parts.password or "")
    out = [bd("MONGODB TEST"), "",
           f"{sc('mode')}      : {bd('MONGO_HOST (3 fields)') if MONGO_HOST else sc('MONGO_URI (ek string)')}",
           f"{sc('enabled')}   : {bd(str(MONGO_ENABLED))}",
           f"{sc('host')}      : {host}",
           f"{sc('user')}      : {user}",
           f"{sc('password')}  : {bd('alag var se') if MONGO_PASSWORD_SET else sc('URI ke andar')}",
           f"{sc('pw length')} : {bd(str(pw_len))}",
           f"{sc('authSource')}: {q.get('authSource', ['admin (default)'])[0]}",
           f"{sc('database')}  : {MONGO_DB}",
           ""]
    if host_bad and MONGO_HOST:
        out.append(f"{bd('HOST ME GALTI')}: {sc(host_bad)}")
        out.append("")
    if MONGO_PASSWORD_STRIPPED:
        out.append(f"{sc('note')}: password ke aage/peeche se {MONGO_PASSWORD_STRIPPED} "
                   f"extra space/newline hata diya gaya tha.")
        out.append("")
    out.append(f"{sc('pw length wahi hai jo Atlas me dikh raha hai? Agar nahi,')}")
    out.append(f"{sc('to password dobara copy kar — Atlas ka Copy button')}")
    out.append(f"{sc('kabhi purana password copy kar deta hai. Show dabakar')}")
    out.append(f"{sc('haath se select karke copy kar.')}")
    out.append("")
    if MONGO_PLACEHOLDER:
        out.append(f"{bd('HOST ME PLACEHOLDER PADA HAI')}")
        out.append(f"{sc('asli host Atlas se COPY karo — haath se type MAT karo')}")
        out.append(f"{sc('(1 aur l, 0 aur o dekhne me ek jaise lagte hain)')}")
        out.append("")
    if not MONGO_ENABLED:
        out.append(f"{sc('MONGO_URI khali hai ya usme placeholder pada hai.')}")
        await _send_long(update.effective_message, "\n".join(out))
        return
    # LIVE test — purana (shayad dead) client chhod kar naya banate hain.
    storage_reset_client()
    try:
        await storage_ping()
        out.append(f"{sc('result')}   : {bd('CONNECTED')}")
        out.append(f"{sc('saving to MongoDB is working')}")
        out.append(f"{sc('state')}    : {bd(_storage.state())}")
    except Exception as e:
        out.append(f"{sc('result')}   : {bd('FAILED')}")
        out.append("")
        out.append(_mongo_error_hint(e))
        out.append("")
        out.append(f"{sc('raw error')}: {str(e)[:200]}")
    await _send_long(update.effective_message, "\n".join(out))


def _diag_blocks() -> list:
    """Diagnostics me kaunse blocks dikhein: config ke links + gate channels,
    ek hi channel ek hi baar (id se dedupe)."""
    out, seen = [], set()
    for b in list(LINK_BLOCKS) + list(_gate_blocks()):
        k = _cid_key(b.get("id") or b.get("name"))
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
    return out


async def cmd_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: poora status — har channel ALAG BLOCK me, spacing + font ke saath,
    aur neeche total kitne channel join karne hain wo summary."""
    if not await _need_owner(update):
        return
    me = await _get_me(context)
    learned = _learned_ids()
    uid = update.effective_user.id
    sc, bd = fonts.small_caps, fonts.bold
    sep = "━━━━━━━━━━━━━━━━━"
    req_blocks = _gate_blocks()
    joined_count = 0
    _ready, _no_id = gate_report()
    # Koi require_join block hi nahi = gate jaan-boojh kar OFF hai -> READY.
    gate_ok = (not has_required_blocks()) or (bool(_ready) and not _no_id)
    _st = storage_health()
    _usage = ai_usage()
    try:
        _doc_kb = (await doc_size_now()) / 1024
    except Exception:
        _doc_kb = 0
    out = [bd("BOT STATUS"), "", sep,
           # build/naam PLAIN — bold math digits padhne/copy karne me gadbad karte hain
           f"{sc('build')}   : {BOT_BUILD}",
           f"{sc('name')}    : {BOT_BRAND} "
           f"({('@' + me.username) if getattr(me, 'username', None) else BOT_USERNAME})",
           f"{sc('storage')} : {bd(mongo_status())}",
           f"{sc('state')}   : {bd(_st['state'])}",
           f"{sc('durability')} : {bd(_st.get('durability') or '?')}",
           f"{sc('ready')}  : {bd('200') if _st['durable'] else bd('503 (not durable)')}",
           f"{sc('doc size')} : {bd(f'{_doc_kb:.0f} KB')} {sc('of 16384 KB')}",
           f"{sc('gate')}   : {bd('READY') if gate_ok else bd('NOT READY')}",
           f"{sc('guest')}  : {bd('ON') if GUEST_MODE else bd('OFF')} — "
           f"{sc('BotFather')}: {bd('supported') if getattr(me, 'supports_guest_queries', False) else bd('NOT enabled')}",
           f"{sc('privacy')} : {bd('off (bot sees all group messages)') if getattr(me, 'can_read_all_group_messages', False) else bd('on (only mentions and replies)')}",
           f"{sc('groups')} : {bd('can be added') if getattr(me, 'can_join_groups', True) else bd('BLOCKED — BotFather me groups off hain')}",
           f"{sc('checks')} : {len(_ready)}/{len(_ready) + len(_no_id)} {sc('channels')}",
           f"{sc('ai today')} : {bd(str(_usage.get('total', 0)))}/{AI_DAILY_MAX or '∞'}",
           f"{sc('roster')} : {bd('ready') if not roster.available() else roster.available()}",
           f"{sc('dm sent')} : {bd(str(_eph_stats['sent']))} "
           f"({sc('skip admin')} {_eph_stats['skip_admin']}, "
           f"{sc('unknown')} {_eph_stats['skip_unknown']}, "
           f"{sc('cool')} {_eph_stats['skip_cool']}, "
           f"{sc('fail')} {_eph_stats['fail']})",
           f"{sc('ai calls')} : {bd(str(_ai_stats['calls']))} "
           f"({sc('ok')} {_ai_stats['ok']}, {sc('fail')} {_ai_stats['fail']}, "
           f"{sc('timeout')} {_ai_stats['timeout']})",
           f"{sc('ai models')} : {bd(str(len(_live_chain())))}/{len(AI_MODELS)} "
           f"{sc('live')}" + (f" · {sc('skipped')}: "
                              + ", ".join(sorted(_AI_DEAD)) if _AI_DEAD else ""),
           f"{sc('model fails')} : " + (", ".join(
               f"{m} x{n}" for m, n in sorted(_ai_model_fails.items(),
                                              key=lambda kv: -kv[1])[:4]) or "-"),
           f"{sc('last ai error')} : {(_ai_stats.get('last_error') or '-')[:150]}",
           f"{sc('ai skipped')} : {sc('rate')} {_ai_stats['rate']}, "
           f"{sc('budget')} {_ai_stats['budget']}",
           sep, ""]
    if _st.get("durability") == "EPHEMERAL":
        out.append(f"{bd('NOTE')}: Mongo down hai aur ye disk ephemeral hai — "
                   "redeploy par data jayega. /ready 503 dega.")
        out.append("")
    elif not _st["durable"]:
        out.append(f"{bd('WARNING')}: data kisi durable store par nahi hai.")
        out.append("")
    if not gate_ok:
        out.append(f"{sc('the gate cannot block anyone yet')}.")
        out.append(f"{sc('add the bot as an ADMIN in')}: {', '.join(_no_id) if _no_id else '-'}")
        out.append(f"{sc('or bind by hand')} — /chats {sc('shows the ready-made /setid line')}.")
        out.append("")
    _shown = set()
    for blk in _diag_blocks():
        emoji = blk.get("emoji", "")
        out.append(sep)
        out.append(f"{emoji} {bd(blk['name'])}".strip())
        out.append(sep)
        if not blk.get("require_join"):
            out.append(f"{sc('type')} : {sc('link only')}")
            out.append("")
            continue
        cid = blk.get("id") or learned.get(blk["name"])
        if not cid:
            out.append(f"{sc('id')}   : {bd('NOT FOUND')}")
            out.append(f"{sc('fix')}  : /getid  {sc('or')}  /setid")
            out.append("")
            continue
        _shown.add(_cid_key(cid))
        out.append(f"{sc('id')}   : {cid}")   # plain rakha - copy karne me aasan
        try:
            bm = await context.bot.get_chat_member(chat_id=cid, user_id=me.id)
            st = bm.status
            out.append(f"{sc('bot')}  : {bd(st)}")
            if st in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                on = [r.replace("can_", "") for r in _RIGHTS if getattr(bm, r, None)]
                off = [r.replace("can_", "") for r in _RIGHTS if not getattr(bm, r, None)]
                out.append(f"{sc('on')}   : {', '.join(on) if on else '-'}")
                out.append(f"{sc('off')}  : {', '.join(off) if off else '-'}")
            else:
                out.append(f"{sc('note')} : {sc('the bot is not an admin - this chat cannot be verified')}")
        except Exception as e:
            out.append(f"{sc('bot')}  : {bd('FAIL')} - {_err_hint(e)}")
        try:
            um = await context.bot.get_chat_member(chat_id=cid, user_id=uid)
            is_in = um.status not in NOT_MEMBER_STATUSES
            out.append(f"{sc('you')}  : {bd('joined' if is_in else 'not joined')}")
            if is_in:
                joined_count += 1
        except Exception as e:
            out.append(f"{sc('you')}  : {bd('?')} - {_err_hint(e)}")
        out.append("")
    # Bot jin chats me hai (chahe block se match kare ya na) — poora section.
    out.extend(await _known_chats_lines(context, me, skip=_shown))

    need = required_count()   # gate wala hi hisaab (saare require_join blocks)
    out.append(sep)
    out.append(f"{sc('total channels')} : {bd(str(len(req_blocks)))}")
    out.append(f"{sc('you joined')}     : {bd(str(joined_count))}")
    out.append(f"{sc('you need')}       : {bd(str(need))}")
    out.append(sep)
    await _send_long(update.effective_message, "\n".join(out))


# =====================================================================
#  CALLBACKS
# =====================================================================
_warned: set = set()


async def _warn_admin_unverifiable(context, blocks):
    """Bot in chats ko verify nahi kar paya -> owner ko ek baar saaf-saaf batao."""
    names = sorted(b["name"] for b in blocks)
    key = tuple(names)
    if key in _warned:
        return
    _warned.add(key)
    text = (
        "⚠️ GATE IS NOT ENFORCING — I cannot check membership in these chats:\n\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n\nThe gate is showing, but it cannot block anyone yet.\n\n"
        "Two things fix it:\n"
        "1. Add the bot as an ADMIN in each of those channels (it learns the id "
        "the moment it is added).\n"
        "2. Or bind the id by hand — run /chats to see the ready-made /setid line.\n\n"
        "Send /diag any time to see whether the gate is ready."
    )
    await _notify_owner(context.bot, text)


# Webhook: ek update kitni baar fail hua (dead-letter ke liye).
_update_fails: dict = {}


# Ek hi button do baar dabaya (ya 10 baar) -> kaam EK baar.
# Pehle /post par "Post now" do baar dabane se DO post jaate the, aur
# "Delete my data" do baar dabane se delete do baar chalta tha.
_cb_seen: dict = {}


def _cb_duplicate(uid, data) -> bool:
    """True = ye wahi tap abhi-abhi ho chuka hai, ignore karo."""
    if CB_COOLDOWN <= 0:
        return False
    key = (int(uid), str(data))
    now = time.time()
    last = _cb_seen.get(key)
    _cb_seen[key] = now
    _prune(_cb_seen, 5000)
    return last is not None and (now - last) < CB_COOLDOWN


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user
    data = q.data or ""

    # Duplicate tap guard. q.answer() hamesha dena padta hai, warna Telegram
    # client me spinner chalta rehta hai.
    if _cb_duplicate(user.id, data):
        log.info("Duplicate callback from %s (%s) — ignore.", user.id, data)
        await q.answer("Already done")
        return

    # Join-request ka Approve button (sirf owner/admin dabaa sakta hai)
    if data.startswith("gateai:"):
        # "tap" wala block (kisi bot ka link). Membership check mumkin nahi —
        # tap ko "ho gaya" maan lo, aur link khol do.
        name = data.split(":", 1)[1]
        blk = next((x for x in LINK_BLOCKS if x["name"] == name), None)
        if not blk:
            await q.answer()
            return
        db_mark_done(user.id, name)
        await q.answer("Marked as done — opening it now.")
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                f"{fonts.bold(name)}\n{blk['url']}\n\n"
                f"{fonts.small_caps('come back and tap Verify')}")
        except Exception:
            pass
        return

    if data.startswith("deldata:"):
        # Privacy: user apna data khud delete kar sakta hai (PRIV-01).
        if data.split(":", 1)[1] != "yes":
            await q.answer("Cancelled")
            try:
                await q.edit_message_text("Nothing was deleted.")
            except Exception:
                pass
            return
        await q.answer("Deleting…")
        res = await _do_delete_user_data(user.id)
        # Sach batao: delete hua ya nahi, aur kahan tak.
        if res.get("verified"):
            state_line = "Your record is gone from the database."
        elif res.get("committed") == "disk":
            state_line = ("Removed from the bot and saved to disk. The database "
                          "is unreachable right now, so its copy will be purged "
                          "as soon as it reconnects.")
        else:
            state_line = ("Removed from the bot, but the storage write failed. "
                          "It is queued and will be written as soon as storage "
                          "is reachable — please tell the owner if it does not "
                          "settle within a day.")
        try:
            await q.edit_message_text(
                "Deleted.\n\n"
                f"Account record : {'removed' if res['user'] else 'nothing was stored'}\n"
                f"Invite count   : {res['referrals']} removed\n"
                f"Referral links : {res['referred_by']} cleaned\n"
                f"Conversation   : {res['memory']} memory key(s) cleared\n"
                f"Group ledger   : {res.get('audience', 0)} chat(s) cleaned\n"
                f"Warnings       : {res.get('warns', 0)} cleared\n\n"
                f"{state_line}\n\n"
                "You can keep using the bot; a new record starts only if you "
                "send /start again.")
        except Exception:
            pass
        # Deletion ka record audit log me NAHI daalte — warna wahi user ka
        # data dobara likh jata. Sirf server log me.
        log.info("PRIVACY: user %s ne apna data delete kiya (%s)", user.id, res)
        return

    if data.startswith("help:"):
        # /help ke 3 buttons — Rules | Report | Privacy.
        which = data.split(":", 1)[1]
        _fn = {"rules": cmd_rules, "report": cmd_report,
               "privacy": cmd_privacy}.get(which)
        await q.answer()
        if _fn is not None:
            await _fn(update, context)
        return

    if data.split(":", 1)[0] in ("mtn", "lck", "asp", "gat"):
        # ON/OFF buttons — "on" type karne ki zaroorat nahi.
        if not is_owner(user.id):
            await q.answer()
            return
        _key, _val = data.split(":", 1)
        _fn = {"mtn": cmd_maintenance, "lck": cmd_lock,
               "asp": cmd_antispam, "gat": cmd_gate}[_key]
        await q.answer()
        _old_args = context.args
        try:
            context.args = [_val]
            await _fn(update, context)
        except Exception as e:
            log.warning("toggle %s fail: %s", data, e)
        finally:
            context.args = _old_args
        return

    if data.startswith("jreq:"):
        # Join request ke 3 buttons — sirf admin/owner chala sakte hain.
        if not is_privileged(user.id):
            await q.answer()
            log.warning("jreq callback blocked for %s (%s)", user.id, data)
            return
        parts = data.split(":")
        if len(parts) != 4:
            await q.answer()
            return
        _act, _cid, _uid = parts[1], parts[2], parts[3]
        try:
            _cid, _uid = int(_cid), int(_uid)
        except ValueError:
            await q.answer("Bad request")
            return
        _base = (q.message.text or "") if q.message else ""
        if _act == "p":
            await q.answer("Marked pending - no action taken")
            await _safe(q.edit_message_text,
                        f"{_base}\n\n⏳ pending")
            return
        await q.answer("…")
        try:
            if _act == "a":
                await context.bot.approve_chat_join_request(chat_id=_cid, user_id=_uid)
                _mark = f"✅ approved — {user.full_name}"
            else:
                await context.bot.decline_chat_join_request(chat_id=_cid, user_id=_uid)
                _mark = f"❌ rejected — {user.full_name}"
            await _safe(q.edit_message_text, f"{_base}\n\n{_mark}",
                        reply_markup=None)
            log.info("Join request %s: %s by %s", _act, _uid, user.id)
        except Exception as e:
            await _safe(q.edit_message_text,
                        f"{_base}\n\n⚠️ {_err_hint(e)}", reply_markup=None)
            log.warning("Join request action fail (%s): %s", data, e)
        return

    if data == "ownerpanel":
        if not is_owner(user.id):
            await q.answer()
            return
        await q.answer()
        await cmd_owner(update, context)
        return

    if data.startswith("admpanel:"):
        if not is_privileged(user.id):
            await q.answer()
            return
        act = data.split(":", 1)[1]
        await q.answer()
        if act == "post":
            # Scope seedha pass hota hai — shared dict me kuch chhoda nahi jata.
            await _start_post(update, context, scope="admin")
        elif act == "posts":
            await cmd_posts(update, context)
        return

    if data.startswith("owner:"):
        # BYPASS-PROOF: permission YAHAN check hoti hai, button render par nahi.
        # Non-owner ko na action milta hai, na hint.
        if not is_owner(user.id):
            await q.answer()
            log.warning("Owner panel callback blocked for %s (%s)", user.id, data)
            return
        act = data.split(":", 1)[1]
        fn = _owner_actions().get(act)
        await q.answer()
        if fn is None:
            return
        try:
            await fn(update, context)
        except Exception as e:
            log.warning("Owner panel action %s fail: %s", act, e)
            await _safe(context.bot.send_message, update.effective_chat.id,
                        f"Failed: {_err_hint(e)}")
        return

    if data.startswith("bs:"):
        await q.answer("Colour OK" if _STYLE_OK else "Colour is not supported")
        return

    if data.startswith("posttype:"):
        if not is_privileged(user.id):
            await q.answer()
            return
        t = data.split(":", 1)[1]
        _sc = _post_scope.pop(user.id, None)
        # Button render par nahi — CHALANE ke waqt check (bypass-proof).
        if t not in _post_types_for(user.id, _sc):
            await q.answer("That type is not available to you", show_alert=False)
            log.warning("Post type blocked: %s tried %r", user.id, t)
            return
        if _sc == "admin":
            # Telegram har callback ka jawab maangta hai — warna button par
            # spinner chalta rehta hai. Is branch me q.answer() hi nahi tha
            # (audit F-08).
            await q.answer()
            # LINK FLOW — seedha link maango, koi channel/poster nahi.
            _post_flow[user.id] = {"type": t, "mode": "link", "step": 0,
                                   "data": {}, "channels": [],
                                   "scope": "admin", "_t": time.time()}
            try:
                await q.edit_message_text(f"{t} — let us begin.")
            except Exception:
                pass
            await _safe(
                context.bot.send_message, update.effective_chat.id,
                f"{fonts.bold('Send the link')}")
            return
        if not _channels_for(user.id, _sc):
            await q.answer("No channel is set up", show_alert=True)
            return
        _post_flow[user.id] = {"type": t, "step": 0, "data": {}, "channels": [],
                               "scope": _sc, "_t": time.time()}
        await q.answer()
        try:
            await q.edit_message_text(f"{t} — let us begin.")
        except Exception:
            pass
        await _post_channel_kb(update.effective_chat.id, context, user.id)
        return

    if data.startswith("postch:"):
        if not is_privileged(user.id):
            await q.answer()
            return
        fl = _post_flow.get(user.id)
        if not fl:
            await q.answer("Flow expired — send /post again.", show_alert=True)
            return
        cid = _norm_chat_id(data.split(":", 1)[1])   # @username bhi chalega
        sel = fl.setdefault("channels", [])
        if cid in sel:
            sel.remove(cid)
            await q.answer("Removed")
        else:
            sel.append(cid)
            await q.answer("Selected")
        await _post_channel_kb(update.effective_chat.id, context, user.id, edit_msg=q.message)
        return

    if data == "postch_done":
        if not is_privileged(user.id):
            await q.answer()
            return
        fl = _post_flow.get(user.id)
        if not fl or not fl.get("channels"):
            await q.answer("Pick at least one channel first.", show_alert=True)
            return
        await q.answer()
        try:
            await q.edit_message_text(f"Selected channels: {len(fl['channels'])}")
        except Exception:
            pass
        if POST_ASK_IMAGE:
            fl["await_image"] = True
            await context.bot.send_message(
                update.effective_chat.id,
                f"{fonts.bold('Send the banner image')}")
        else:
            await _post_ask(update.effective_chat.id, context, user.id)
        return

    if data.startswith("postedit:"):
        if not is_privileged(user.id):
            await q.answer()
            return
        fl = _post_flow.get(user.id)
        if not fl:
            await q.answer("Flow expired — send /post again.", show_alert=True)
            return
        arg = data.split(":", 1)[1]
        if arg == "start":
            await q.answer()
            rows = [[_btn(f, callback_data=f"postedit:{f}", style="primary")]
                    for f in _post_fields(fl["type"]) if f != "Link"]
            await context.bot.send_message(
                update.effective_chat.id, f"{fonts.bold('Which field?')}",
                reply_markup=InlineKeyboardMarkup(rows))
            return
        await q.answer()
        fl["await_edit"] = arg
        await context.bot.send_message(
            update.effective_chat.id,
            f"{fonts.bold('Send the new')} {fonts.bold(arg)}")
        return

    if data.startswith("postgo:"):
        if not is_privileged(user.id):
            await q.answer()
            return
        fl = _post_flow.get(user.id)
        if not fl or not fl.get("channels"):
            await q.answer("Flow expired — send /post again.", show_alert=True)
            return
        await q.answer()
        if data.split(":", 1)[1] == "sched":
            fl["await_time"] = True
            await context.bot.send_message(
                update.effective_chat.id,
                "When should it go out? Format:\n2026-10-18 20:30   (or just 20:30 — IST)")
            return
        # ZAROORI: flow PEHLE hata do, phir post bhejo. Warna do tap (ya
        # slow network par double delivery) dono _send_post tak pahunch
        # jaate the aur post do baar jaata tha.
        _post_flow.pop(user.id, None)
        code = _new_code()
        data2 = dict(fl["data"])
        data2.update({"code": code, "type": fl["type"],
                      "channel_ids": list(fl.get("channels") or []),
                      "ep_line": fl.get("ep_line"),
                      "image": fl.get("image"), "posted": False, "created": _now()})
        db_post_save(code, data2)
        ok = await _send_post(context, code)
        await context.bot.send_message(
            update.effective_chat.id,
            ("Posted ✅\n" if ok else "Could not post (is the bot an admin in that channel?)\n")
            + f"Link: {_deep_link(context, code)}")
        return

    if data != "check_join":
        await q.answer()
        return

    # Anti-spam: ek user 2 second me sirf ek baar check kar sakta hai
    # (warna Check dabate rehne se getChatMember flood ho jayega).
    now = time.time()
    if now - _check_cd.get(user.id, 0) < 2:
        await q.answer("Please wait a moment, then try again.")
        return
    _check_cd[user.id] = now
    _prune(_check_cd, 5000)

    # Strict: cache ignore karke fresh check — join karke turant chhod diya
    # ho, ya cache purana ho, to bhi pakda jaye.
    joined, missing, unknown, need = await join_status(context, user.id, force=True)

    # STRICT GATE: "Request to Join" SIRF tab milega jab join/request confirm ho
    # jaye. Do soorat me block:
    #   1) kuch channels join/request nahi hue
    #   2) bot check hi nahi kar paya (chat id nahi / bot admin nahi) -> FAIL CLOSED
    # STRICT + FAIL-CLOSED. Pass sirf tab jab SACH ME verify ho gaya.
    #   need == 0  -> bot kuch bhi check nahi kar paya (chat id nahi, ya bot us
    #                 channel me ADMIN nahi). Iska matlab verify NAHI hua —
    #                 pehle ye galti se PASS ho jata tha (user bina join kiye
    #                 "verified" mil jata tha). Ab BLOCK.
    #   joined < need -> kuch channels baaki hain -> BLOCK.
    joined_ids = [c["id"] for c in joined if c.get("id")]
    ready, no_id = gate_report()
    blocked = (need == 0) or (len(joined) < need)
    if blocked:
        await q.answer()
        if missing:
            text = JOIN_PROMPT + "\n\n" + ", ".join(b["name"] for b in missing)
        else:
            text = ("I could not confirm your membership yet. Please join the "
                    "channels below, then tap the button again.")
        kb = link_keyboard(
            extra_rows=[[_btn(GATE_BUTTON_TEXT, callback_data="check_join",
                              style="success")]],
            joined=joined_ids, tap_done=_tap_done_names(user.id),
            blocks=_gate_blocks())
        # USI message ko edit karo jis par user ne tap kiya — isse ✓ wahin
        # dikhta hai jahan user dekh raha hai (naya message bhejne se wo purane
        # message me ✓ dhundhta rehta tha).
        edited = await _safe(q.edit_message_text, text, reply_markup=kb)
        if edited is not None:
            _gate_msg[user.id] = (update.effective_chat.id,
                                  getattr(q.message, "message_id", 0))
        else:
            _m = await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, reply_markup=kb)
            if getattr(_m, "message_id", None):
                _gate_msg[user.id] = (update.effective_chat.id, _m.message_id)
        _prune(_gate_msg, 5000)
        if no_id or unknown:
            await _warn_admin_unverifiable(
                context, [blk for blk in LINK_BLOCKS
                          if blk.get("require_join") and not _is_tap_block(blk)
                          and _block_chat_id(blk) is None])
        return

    # Pass. (Ek hi baar answer karo — dobara answer karne par Telegram error deta hai.)
    await q.answer()
    log.info("Gate PASSED for %s (%s/%s joined)", user.id, len(joined), need)
    if unknown:
        await _warn_admin_unverifiable(context, unknown)

    # Post deep link se aaya tha? Ab asli link (Request to Join) do.
    code = _pending_post.pop(user.id, None)
    p = db_post_get(code) if code else None
    if p:
        await _post_welcome(update, context, p, chat_id=update.effective_chat.id)
    else:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "Verified. Now just type your question.")
        except Exception:
            pass
    # Verify ho gaya -> purana "5 links + Verify" wala message HATA do.
    # (Telegram delete par khud fade animation dikhata hai.)
    for cid, mid in filter(None, [_gate_msg.pop(user.id, None)]):
        try:
            await context.bot.delete_message(chat_id=cid, message_id=mid)
        except Exception as e:
            log.warning("Gate message delete fail: %s", e)
    try:
        await q.message.delete()
    except Exception as e:
        log.warning("Gate delete fail: %s", e)


# =====================================================================
#  INLINE MODE  —  bot kisi bhi chat me, bina us group me add hue
# =====================================================================
#  Telegram me bot KHUD kisi group/channel me join nahi kar sakta (aisa
#  method hi nahi hai). Uske sabse kareeb do cheezein hain:
#    1) Guest mode  (bot ko mention karo, wo wahin jawab deta hai)
#    2) Inline mode (yahi) — user @MahiruOrbitBot likhta hai aur result
#       wahin bhej deta hai. Har result par bot ka naam hota hai, matlab
#       har use ek chhota advertisement hai.
#  BotFather > Bot Settings > Inline Mode ON hona chahiye.
def _pack_ask(q: str) -> str:
    """Inline result se sawaal DM tak le jao.

    Telegram ka deep link sirf `start` payload leta hai (max ~64 char),
    isliye sawaal ko base64 karke bhejte hain: start=ask_<b64>.
    """
    raw = str(q or "").strip()
    if not raw:
        return ""
    # Audit F-07: Telegram ka start payload 64 chars ka hai — "ask_" (4) +
    # base64 (4 chars = 3 bytes) => zyada se zyada 45 bytes. Pehle
    # `("ask_" + b)[:64]` base64 ko beech se kaat deta tha, isliye lamba
    # sawaal CHUP-CHAAP adhoora pahunchta tha. Ab jaan-boojh kar faisla:
    # poora sawaal fit hote ho to bhejo, warna khaali string — adhoora
    # sawaal bhejne se behtar hai inline card dikhe hi na.
    if len(raw.encode("utf-8")) > 45:
        return ""
    b = base64.urlsafe_b64encode(raw.encode("utf-8")).decode().rstrip("=")
    payload = "ask_" + b
    return payload if len(payload) <= 64 else ""


def _unpack_ask(arg: str) -> str:
    if not str(arg or "").startswith("ask_"):
        return ""
    b = str(arg)[4:]
    try:
        return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode(
            "utf-8", "ignore").strip()
    except Exception:
        return ""


def _inline_results(context, query: str) -> list:
    bot_name = getattr(context.bot, "username", None) or "MahiruOrbitBot"
    q = (query or "").strip()
    out = []

    def _art(rid, title, desc, text, url, label):
        return InlineQueryResultArticle(
            id=rid,
            title=title,
            description=desc,
            input_message_content=InputTextMessageContent(text),
            reply_markup=InlineKeyboardMarkup([[_btn(label, url=url)]]),
        )

    out.append(_art(
        "mahiru", "Mahiru — talk to her",
        "opens a private chat with Mahiru",
        "found her ☁",
        f"https://t.me/{bot_name}?start=src_inline", "say hi"))
    out.append(_art(
        "channel", "Avenor Feed — channel",
        "apps, AI tools and updates",
        "new apps and AI tools land here first.",
        CHANNEL_LINK, "open channel"))
    out.append(_art(
        "group", "Avenor Club — group",
        "people talk here",
        "people talk here.",
        COMMUNITY_LINK, "open group"))
    if q:
        # Query ko base64 karke bot tak le jao — DM khulega aur jawab turant
        # aayega. Isse inline mode "ad" nahi, kaam ki cheez ban jata hai.
        # Sawaal payload me fit nahi baithta to card dikhao hi nahi — warna
        # user ko lagta hai poora sawaal gaya, jabki aadha jaata hai (F-07).
        _payload = _pack_ask(q)
        if _payload:
            out.append(_art(
                "ask", f"ask Mahiru: {q[:40]}",
                "sends this question to her in private",
                f"ask her: {q[:120]}",
                f"https://t.me/{bot_name}?start={_payload}", "ask her"))
    return out


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline mode — kisi bhi chat me bot ke results."""
    if not INLINE_MODE:
        return
    q = update.inline_query
    if q is None:
        return
    try:
        await q.answer(_inline_results(context, q.query),
                       cache_time=INLINE_CACHE, is_personal=False)
    except Exception as e:
        log.warning("Inline answer fail: %s", e)


# =====================================================================
#  WELCOME  (naye member par)
# =====================================================================
_HERE = os.path.dirname(os.path.abspath(__file__))


def _welcome_image_path() -> str:
    """welcome.png dhundo. CWD par bharosa mat karo — Render ka working
    directory badal sakta hai, aur tab image chup-chaap na milti thi."""
    cand = []
    env = (os.getenv("WELCOME_IMAGE") or "").strip()
    if env:
        cand.append(env)
    cand += ["welcome.png", "assets/welcome.png",
             os.path.join(_HERE, "welcome.png"),
             os.path.join(_HERE, "assets", "welcome.png")]
    for p in cand:
        if os.path.exists(p):
            return p
    return env or "welcome.png"


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Service message = bot ko HAMESHA milti hai (chahe privacy on ho). Isliye
    # jab bhi koi group me join kare, hum us group ki id seekh lete hain.
    learn_chat(update)
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        _note_audience(msg.chat_id, member, src="join", status="member")
        # Group ke andar hi private welcome — sirf usi member ko dikhta hai
        # (aur admin/owner ko bilkul nahi).
        await _ephemeral_welcome(context, msg.chat, member)
        if WELCOME_TEMPLATE:
            try:
                caption = WELCOME_TEMPLATE.format(
                    first_name=member.first_name or "Member",
                    group_name=msg.chat.title or "the group",
                    full_name=member.full_name or member.first_name or "Member",
                    user_id=member.id,
                    join_date=_today(),
                    bot_brand=BOT_BRAND,
                )
            except Exception as e:
                # Env me galat {placeholder} ho to welcome fail na ho.
                log.warning("WELCOME_TEMPLATE galat hai (%s) — default use kar raha hoon.", e)
                caption = build_welcome(member, msg.chat.title)
        else:
            caption = build_welcome(member, msg.chat.title)
        sent = False
        path = _welcome_image_path()
        try:
            with open(path, "rb") as photo:
                await context.bot.send_photo(chat_id=msg.chat_id, photo=photo, caption=caption)
            sent = True
        except FileNotFoundError:
            log.warning("Welcome image nahi mili (%s) — sirf text bhej raha hoon.", path)
        except Exception as e:
            log.warning("Welcome photo fail (%s): %s", msg.chat_id, e)
        if not sent:
            try:
                await context.bot.send_message(chat_id=msg.chat_id, text=caption)
            except Exception as e:
                log.warning("Welcome text fail (%s): %s", msg.chat_id, e)


# =====================================================================
#  JOIN REQUESTS  (owner approval, auto nahi)
# =====================================================================
_join_dm_at: dict = {}      # user_id -> last join-DM ka time
_aud_seen: dict = {}        # (chat_id, user_id) -> last ledger write


def _note_audience(chat_id, user, src=None, status="member") -> None:
    """Apna member-ledger bharo (Telegram list nahi deta, isliye khud banate)."""
    try:
        db_audience_add(chat_id, user.id,
                        name=getattr(user, "full_name", "") or "",
                        src=src, status=status)
    except Exception as e:
        log.info("Audience note skip: %s", e)


async def _send_ephemeral(bot, chat_id, user_id, text, kb=None):
    """Group me EK hi user ko message — baaki members ko dikhta hi nahi.

    Bot API 10.3+ ka "ephemeral message". PTB 22.8 me typed parameter nahi
    hai, par PTB ka **official extension point** `do_api_request()` hai
    (api_kwargs hack se behtar — yahi docs me recommend kiya gaya hai).
    Shart: bot us group me ADMIN ho; delivery guaranteed nahi.
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
        "ephemeral_message_parameters": {"receiver_user_id": int(user_id)},
    }
    if kb is not None:
        payload["reply_markup"] = kb.to_dict()
    try:
        return await bot.do_api_request("sendMessage", api_kwargs=payload)
    except Exception as e:
        log.warning("Ephemeral send fail (%s -> %s): %s", chat_id, user_id, e)
        return None


# Kis-kis ko private message NAHI karna (aur kyun).
_eph_stats = {"sent": 0, "skip_admin": 0, "skip_unknown": 0, "skip_cool": 0,
              "skip_own": 0, "fail": 0}
_eph_at: dict = {}          # uid -> last ephemeral message ka time


async def _chat_role(context, chat_id, uid):
    """Us chat me user ka status ("creator"/"administrator"/"member"/...).
    None = pata nahi chala (bot admin nahi / call fail) — fail-closed."""
    try:
        m = await context.bot.get_chat_member(chat_id=chat_id, user_id=uid)
        return str(getattr(m, "status", "") or "")
    except Exception as e:
        log.info("Role check fail (%s, %s): %s", chat_id, uid, e)
        return None


async def _dm_allowed(context, chat_id, user):
    """Kya is user ko group ke andar private message bhejna chahiye?

    NAHI in cases (aur ye jaan-boojh kar):
      • wo us chat ka ADMIN ya OWNER hai — usko unsolicited DM spam lagta hai,
        aur wahi ek click me bot ko group se nikaal sakta hai
      • uska role pata na chale (getChatMember fail) — fail-closed, chup raho
      • wo hamara khud ka owner/admin hai
      • wo bot hai
      • usne pehle "nahi" bol diya tha
      • usko thodi der pehle hi message bheja gaya (cooldown)
    Returns (allowed, reason).
    """
    uid = int(user.id)
    if getattr(user, "is_bot", False):
        return False, "bot"
    if is_privileged(uid):
        _eph_stats["skip_own"] += 1
        return False, "own"
    st = _invite_state(uid)
    if (st.get("declined")
            and time.time() - float(st["declined"]) < INVITE_DECLINE_DAYS * 86400):
        _eph_stats["skip_cool"] += 1
        return False, "declined"
    if JOIN_DM_COOLDOWN_H > 0:
        if time.time() - _eph_at.get(uid, 0) < JOIN_DM_COOLDOWN_H * 3600:
            _eph_stats["skip_cool"] += 1
            return False, "cooldown"
    role = await _chat_role(context, chat_id, uid)
    if role is None:
        _eph_stats["skip_unknown"] += 1
        return False, "unknown"
    if role in ("administrator", "creator", "owner"):
        _eph_stats["skip_admin"] += 1
        log.info("Private message SKIP: %s is %s in %s (admin ko DM nahi)",
                 uid, role, chat_id)
        return False, "admin"
    return True, role or "member"


async def _ephemeral_welcome(context, chat, member) -> None:
    """Naye member ko group ke andar hi private welcome (sirf wahi dekhta hai).

    ADMIN/OWNER ko kabhi nahi — dekho `_dm_allowed`.
    """
    if not EPHEMERAL_WELCOME:
        return
    if str(getattr(chat, "type", "")) not in ("group", "supergroup"):
        return                      # ephemeral sirf groups me chalta hai
    ok, why = await _dm_allowed(context, chat.id, member)
    if not ok:
        log.info("Ephemeral welcome skip (%s) — %s", member.id, why)
        return
    _u = getattr(context.bot, "username", None) or BOT_USERNAME
    rows = [[_btn("say hi to Mahiru",
                  url=f"https://t.me/{_u}?start=src_ephemeral")],
            [_btn("Avenor Feed", url=CHANNEL_LINK)]]
    name = (member.first_name or "you").strip()
    text = (f"hey {name} — welcome.\n\n"
            f"I am Mahiru. I live in this group - ask me anything here, or "
            f"message me privately.\n\n"
            f"(only you can see this)")
    sent = await _send_ephemeral(context.bot, chat.id, member.id, text,
                                 InlineKeyboardMarkup(rows))
    if sent is not None:
        _eph_at[member.id] = time.time()
        _prune(_eph_at, 5000)
        _eph_stats["sent"] += 1
        log.info("Ephemeral welcome bheja: %s (%s)", member.id, chat.id)
        _note_audience(chat.id, member, src="ephemeral", status="member")
    else:
        _eph_stats["fail"] += 1


async def on_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kisi ne channel/group post par react kiya -> KAUN kiya, ye pata chalta hai.

    Telegram channel ke subscribers ki list nahi deta. Par bot admin ho to
    usse `message_reaction` update milta hai jisme reactor ka user hota hai —
    isliye reaction hi wo signal hai jisse channel ka asli audience banta hai.
    """
    if not REACTION_TRACK:
        return
    r = getattr(update, "message_reaction", None)
    if r is None:
        return
    usr = getattr(r, "user", None)
    if usr is None or getattr(usr, "is_bot", False):
        return
    if not getattr(r, "new_reaction", None):      # reaction hataya
        return
    _note_audience(r.chat.id, usr, src="reaction", status="member")


async def _join_welcome_dm(context, req, chat, approved=True) -> None:
    """Join REQUEST bhejne wale ko EK message — chahe usne bot start na kiya ho.

    Bot API 5.5+ ka rule: jis chat me bot admin hai aur uske paas
    can_invite_users hai, wahan join request aane par Telegram `user_chat_id`
    deta hai, aur bot us user ko message kar sakta hai — bina uske /start
    kiye. Window chhoti hai (request process hone se pehle), aur message EK.
    Agar user jawab de de, to chat hamesha ke liye khul jata hai — yahi asli
    "DM ka darwaza" hai.
    """
    if not JOIN_DM:
        return
    usr = req.from_user
    if is_privileged(usr.id) or getattr(usr, "is_bot", False):
        return
    _st = _invite_state(usr.id)
    if (_st.get("declined")
            and time.time() - float(_st["declined"]) < INVITE_DECLINE_DAYS * 86400):
        return
    now = time.time()
    if now - _join_dm_at.get(usr.id, 0) < JOIN_DM_COOLDOWN_H * 3600:
        return
    target = getattr(req, "user_chat_id", None) or usr.id
    name = (usr.first_name or "you").strip()
    rows = [[_btn("Avenor Feed", url=CHANNEL_LINK)],
            [_btn("Avenor Club", url=COMMUNITY_LINK)],
            [_btn("say hi to Mahiru",
                  url=f"https://t.me/{context.bot.username}?start=src_joinreq")]]
    if approved:
        text = (f"hey {name} — request mil gayi, andar aa sakte ho.\n\n"
                f"I am Mahiru. There are two more places you might like:\n"
                f"apps and AI tools in the channel, people in the group.\n\n"
                f"kuch bhi poochna ho to yahin likh do, main padh rahi hoon.")
    else:
        # Anime / admin channels: abhi approve nahi hua, sirf admin ke paas
        # hai. Jhooth nahi bolte — "andar aa sakte ho" nahi likhte.
        text = (f"hey {name} — tumhari request mil gayi hai.\n\n"
                f"admin ke paas hai, approve hote hi andar aa jaoge. "
                f"kuch poochna ho to yahin likh do.\n\n"
                f"I am Mahiru. apps and AI tools yahan aate hain:")
    sent = await _safe(context.bot.send_message, target, text,
                       reply_markup=InlineKeyboardMarkup(rows))
    if sent is not None:
        _join_dm_at[usr.id] = now
        _prune(_join_dm_at, 5000)
        log.info("Join-request DM bheja: %s (%s)", usr.id, chat.id)


def _is_admin_channel(chat_id) -> bool:
    """Kya ye ADMIN_CHANNELS (5 anime private channels) me se hai?"""
    k = _cid_key(_norm_chat_id(chat_id))
    for _n, cid in ADMIN_CHANNELS:
        if _cid_key(_norm_chat_id(cid)) == k:
            return True
    return False


def _auto_approve_keys() -> set:
    """Join request jinke liye TURANT approve hota hai — sirf owner ke rooms.

    Keys: lowercase username (bina @) aur numeric chat id, dono. Config me
    poora link ho ya @username ya numeric id — teeno chalte hain."""
    out = set()
    for v in AUTO_APPROVE_CHANNELS:
        s = str(v or "").strip()
        if not s:
            continue
        if s.startswith(("http://", "https://")):
            s = s.rstrip("/").rsplit("/", 1)[-1]
        s = s.lstrip("@").strip().lower()
        if s and not s.startswith("+"):      # invite link (t.me/+abc) ka koi
            out.add(s)                       # username nahi hota
    for b in LINK_BLOCKS:
        cid = str(b.get("id") or "").strip().lstrip("@").lower()
        if cid:
            out.add(cid)
    return out


def _is_auto_approve_chat(chat) -> bool:
    """Is chat ke join request khud approve honge? (Feed / Club / Lab)

    Telegram join-request me har chat ka @username nahi bhejta (private room
    ho to sirf id + title). Isliye teen tarah se pehchante hain, warna
    auto-approve chup-chaap fail ho kar Admin 1 ke paas chala jata:
      1) @username ya numeric id config me ho
      2) bot ne pehle is chat ki id seekh li ho (_learned_ids, naam se)
      3) chat ka title bilkul wahi naam ho (private room)
    """
    keys = _auto_approve_keys()
    uname = str(getattr(chat, "username", "") or "").strip().lstrip("@").lower()
    if uname and uname in keys:
        return True
    cid = _cid_key(_norm_chat_id(getattr(chat, "id", 0)))
    if cid in keys:
        return True
    names = {_norm(n) for n in AUTO_APPROVE_NAMES if n}
    if not names:
        return False
    for name, learned in (_learned_ids() or {}).items():
        if _norm(name) in names and _cid_key(_norm_chat_id(learned)) == cid:
            return True
    title = _norm(getattr(chat, "title", "") or "")
    return bool(title and title in names)


def _first_admin_id() -> int:
    """Admin 1 — pehla admin (env ADMIN_IDS, phir DB admins)."""
    ids = [int(a) for a in ADMIN_IDS] + [int(k) for k in db_admins()]
    return ids[0] if ids else 0


async def _notify_admin_join_request(context, chat, usr) -> None:
    """Jo chat khud approve nahi hoti, uski request -> Admin 1 ko DM + 3 buttons.

    Yahan aate hain: admin ke 5 anime channels, aur koi bhi chat jo
    AUTO_APPROVE_CHANNELS me nahi hai. Owner ke Feed / Club / Lab yahan
    nahi aate — wahan auto-approve + welcome DM chalta hai.
    """
    target = _first_admin_id() or owner_id()
    if not target:
        log.warning("Join request aayi par koi admin/owner set nahi: %s", chat.id)
        return
    sc, bd = fonts.small_caps, fonts.bold
    text = (f"{bd('JOIN REQUEST')}\n\n"
            f"{sc('user')}    : {usr.full_name}\n"
            f"{sc('id')}      : {usr.id}\n"
            f"{sc('channel')} : {chat.title or chat.id}")
    kb = InlineKeyboardMarkup([
        [_btn("✅ Approve", callback_data=f"jreq:a:{chat.id}:{usr.id}", style="success"),
         _btn("❌ Reject", callback_data=f"jreq:r:{chat.id}:{usr.id}", style="danger")],
        [_btn("⏳ Pending", callback_data=f"jreq:p:{chat.id}:{usr.id}")],
    ])
    sent = await _safe(context.bot.send_message, target, text, reply_markup=kb)
    if sent is not None:
        log.info("Join request Admin %s ko bheji: %s (%s)", target, usr.id, chat.id)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Join request aayi -> record, phir DM (agar on hai), phir approve.

    ZAROORI order: DM PEHLE, approve BAAD me. Telegram ka `user_chat_id`
    sirf tab tak chalta hai jab tak request process na ho — approve karne ke
    baad window band ho jati hai.
    """
    req = update.chat_join_request
    chat, usr = req.chat, req.from_user
    log.info("Join request: %s (id=%s) from %s (%s)",
             chat.title, chat.id, usr.id, usr.full_name)
    b_ = match_block_by_title(chat.title) if chat.title else None
    if b_:
        db_set_chat_id(b_["name"], chat.id)
    db_add_request(usr.id, chat.id)
    # invite link se source (kaunsa link laaya)
    _src = ""
    if getattr(req, "invite_link", None) is not None:
        _src = str(getattr(req.invite_link, "name", "") or "invite")[:40]
    _note_audience(chat.id, usr, src=_src or "join_request", status="requested")

    # DO HI BUCKET:
    #   1) Avenor Feed / Club / Lab -> turant approve (+ welcome DM)
    #   2) baaki har chat (admin ke 5 anime channels bhi) -> approve NAHI,
    #      seedha Admin 1 ke private chat me Approve / Reject / Pending
    # Requester ko welcome DM nahi jaata, kyunki wo abhi member hai hi nahi.
    if _is_admin_channel(chat.id) or not _is_auto_approve_chat(chat):
        # User ko DM yahan bhi jaata hai (pending text ke saath), par approve
        # NAHI hota aur koi reject bhi nahi hota — sirf Admin 1 decide karta hai.
        await _join_welcome_dm(context, req, chat, approved=False)
        await _notify_admin_join_request(context, chat, usr)
        return

    await _join_welcome_dm(context, req, chat)
    if not AUTO_APPROVE_JOIN:
        return
    try:
        await context.bot.approve_chat_join_request(chat_id=chat.id, user_id=usr.id)
        log.info("Join request auto-approved: %s (%s)", usr.full_name, usr.id)
    except Exception as e:
        # Approve nahi hua -> request record bhi hata do. Warna gate us user
        # ko 'joined' maan leta tha jabki wo chat me hai hi nahi.
        db_del_request(usr.id, chat.id)
        log.warning("Join auto-approve fail (%s): %s", chat.id, e)


_REPLAY_MAX = 200          # ek restart par itni purani requests
_REPLAY_AGE_DAYS = 30      # isse purani request chhod do


async def _replay_pending_requests(app) -> None:
    """Restart ke baad PURANI (pending) join requests dobara dekho.

    Bot API me "pending requests ki list" ka koi method nahi hai — sirf
    `chat_join_request` update aata hai, wo bhi ek hi baar. Isliye bot apne
    record (`requests`) se replay karta hai: jo request pehle aayi par
    approve nahi ho payi (crash, restart, ya us waqt bot admin nahi tha),
    wo ab approve ho jaati hai.

    Sirf auto-approve rooms (Feed / Club / Lab). Anime aur anjaan chats me
    kuch nahi hota — wahan Admin 1 decide karta hai, koi reject nahi hota.
    """
    try:
        pend = db_requests_all()
    except Exception as e:
        log.warning("Purani requests padh nahi paye: %s", e)
        return
    if not pend:
        return
    cutoff = time.time() - _REPLAY_AGE_DAYS * 86400
    ok = clean = fail = skip = 0
    seen = 0
    for uid_s, per in pend.items():
        try:
            uid = int(uid_s)
        except (TypeError, ValueError):
            continue
        for cid_s, ts in list(per.items()):
            if seen >= _REPLAY_MAX:
                break
            seen += 1
            try:
                if ts and float(ts) < cutoff:      # bahut purani — chhod do
                    db_del_request(uid, cid_s)
                    clean += 1
                    continue
            except (TypeError, ValueError):
                pass
            try:
                cid = int(cid_s)
            except (TypeError, ValueError):
                cid = cid_s
            if _is_admin_channel(cid):
                skip += 1                          # Admin 1 ka kaam, API call bhi nahi
                continue
            try:
                ch = await app.bot.get_chat(cid)
            except Exception:
                db_del_request(uid, cid_s)         # chat mila hi nahi
                clean += 1
                continue
            if not _is_auto_approve_chat(ch):
                skip += 1
                continue
            try:
                m = await app.bot.get_chat_member(cid, uid)
                if m.status not in NOT_MEMBER_STATUSES:
                    db_del_request(uid, cid_s)     # pehle se member hai
                    clean += 1
                    continue
            except Exception:
                pass
            try:
                await app.bot.approve_chat_join_request(cid, uid)
                db_del_request(uid, cid_s)
                ok += 1
                log.info("Purani request approve: %s (%s)", uid, cid)
            except Exception as e:
                # request valid nahi rahi (pehle hi approve/decline ho chuki)
                db_del_request(uid, cid_s)
                fail += 1
                log.info("Purani request approve nahi hui (%s/%s): %s", cid, uid, e)
            await asyncio.sleep(0.2)               # flood se bachne ke liye
    if ok or clean or fail:
        log.info("Purani requests: approved %s, cleaned %s, failed %s, skipped %s",
                 ok, clean, fail, skip)


async def on_guest_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """GUEST MODE (Bot API 10.0). User ne kisi group/chat me @botname mention
    kiya jahan bot MEMBER HI NAHI hai. Bot wahin seedha jawab post karta hai.

    Ye hi wo "kisi bhi group me jaakar baat karna" hai — bot khud kisi group me
    join NAHI kar sakta (Telegram me aisa method nahi hai), par guest mode se
    wo bina add hue wahan jawab de sakta hai.
    BotFather > Bot Settings > Guest Mode ON hona chahiye."""
    if not GUEST_MODE:
        return
    msg = getattr(update, "guest_message", None)
    if not msg:
        return
    qid = getattr(msg, "guest_query_id", None)
    if not qid:
        return
    user = getattr(msg, "from_user", None)
    if user and db_is_banned(user.id):
        return
    me = await _get_me(context)
    raw = (msg.text or "").strip()
    prompt = _strip_mention(raw, me.username) or "Someone mentioned you. Greet them warmly."
    where = "a Telegram group"
    title = _clean_title(getattr(getattr(msg, "chat", None), "title", None))
    if title:
        where = f'the Telegram group "{title}"'
    try:
        await context.bot.send_chat_action(msg.chat_id, "typing")
    except Exception:
        pass
    # Per-user rate limit guest mode me bhi lagu — pehle sirf roz ka budget
    # dekha jaata tha, isliye ek user guest mode me flood kar sakta tha.
    if user and not _ai_allowed(user.id):
        _ai_stats["rate"] += 1
        return
    if _ai_budget_blocked(msg.chat_id):
        _ai_stats["budget"] += 1
        return
    _ai_count(msg.chat_id)
    async with _typing(context, msg.chat_id):
        answer = await ask_ai(prompt, where=where,
                              mem_key=f"g{msg.chat_id}:u{user.id}" if user else None,
                              note=_owner_note(user.id) if user else "")
    answer = _strip_urls(answer) or answer
    # Jawab ke saath buttons. Guest mode me bot us group ka member hi nahi
    # hota, isliye yahi ek jagah hai jahan se wo log apne paas la sakti hai —
    # aur har button par `src_guest` tag hai, taaki /audience me dikhe ki
    # kitne log isi raaste se aaye.
    _u = getattr(context.bot, "username", None) or BOT_USERNAME
    rows = [[_btn("say hi to Mahiru",
                  url=f"https://t.me/{_u}?start=src_guest", style="primary")]]
    rows += [[_btn(x["name"], url=x["url"], style="primary")] for x in LINK_BLOCKS[:3]]
    # Ledger: kis group me guest reply gaya (audience ka hissa).
    if user:
        _note_audience(msg.chat_id, user, src="guest")
    try:
        from telegram import InlineQueryResultArticle, InputTextMessageContent
        res = InlineQueryResultArticle(
            id="guest1",
            title=BOT_BRAND,
            input_message_content=InputTextMessageContent(answer[:4000]),
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )
        await _safe(context.bot.answer_guest_query, guest_query_id=qid, result=res)
        log.info("Guest reply bheja (%s)", msg.chat_id)
    except Exception as e:
        log.warning("Guest reply fail: %s", e)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group supergroup ban gaya -> chat id badal gayi. Purani id ki jagah
    nayi yaad rakho, warna gate purani (dead) id par check karta rehta."""
    msg = update.effective_message
    new_id = getattr(msg, "migrate_to_chat_id", None)
    if not new_id:
        return
    old_id = getattr(msg, "migrate_from_chat_id", None) or getattr(msg, "chat_id", None)
    log.info("Chat migrated: %s -> %s", old_id, new_id)
    try:
        db_replace_chat_id(old_id, new_id)
    except Exception as e:
        log.warning("Migrate update fail: %s", e)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kisi MEMBER ki membership badli (join / leave / kick).
    Leave par uski join-request yaadash hata do — warna gate usko hamesha
    'verified' maanta rehta tha (asli bug)."""
    cm = getattr(update, "chat_member", None)
    if not cm or not cm.chat:
        return
    chat = cm.chat
    if chat.type == ChatType.PRIVATE:
        return
    usr = getattr(cm.new_chat_member, "user", None)
    if not usr or getattr(usr, "is_bot", False):
        return
    status = getattr(cm.new_chat_member, "status", "")
    _member_cache.pop((int(usr.id), str(chat.id)), None)   # purana cache saaf
    db_del_request(usr.id, chat.id)
    # Ledger: kaun aaya/kya gaya, aur kaunse invite link se (attribution).
    _src = ""
    if getattr(cm, "invite_link", None) is not None:
        _src = str(getattr(cm.invite_link, "name", "") or "invite")[:40]
    _note_audience(chat.id, usr, src=_src or "member_update",
                   status="left" if status in NOT_MEMBER_STATUSES else "member")
    if status in NOT_MEMBER_STATUSES:
        log.info("User %s LEFT %s — request record hataya", usr.id, chat.id)
    else:
        log.info("User %s joined %s — request record hataya (asli membership hai)", usr.id, chat.id)


def _intro_kb(context) -> InlineKeyboardMarkup:
    _u = getattr(context.bot, "username", None) or BOT_USERNAME
    return InlineKeyboardMarkup([
        [_btn("say hi to Mahiru",
              url=f"https://t.me/{_u}?start=src_intro", style="primary")],
        [_btn("Avenor Feed", url=CHANNEL_LINK),
         _btn("Avenor Club", url=COMMUNITY_LINK)],
    ])


async def _auto_intro(context, chat) -> None:
    """Bot ko chat me add kiya gaya -> WAHIN ek intro (bina kisi command ke).

    Yahi wo "auto" hai jo missing tha: pehle add hone par sirf owner ko DM
    jaata tha (aur wo bhi NOTIFY_OWNER=False par nahi), group me kuch nahi
    aata tha — isliye lagta tha bot chala hi nahi.
    """
    if not AUTO_INTRO:
        return
    if db_chat_flag(chat.id, "intro"):
        return                          # ek hi baar, hamesha ke liye
    is_channel = str(getattr(chat, "type", "")) == "channel"
    if is_channel:
        text = ("I am Mahiru - I will be here in this channel.\n\n"
                "New apps, tools and updates land here.")
    else:
        text = ("hey - I am Mahiru. I live here now.\n\n"
                "Ask me anything, or tag me. I will not sit quiet.")
    sent = await _safe(context.bot.send_message, chat.id, text,
                       reply_markup=_intro_kb(context))
    if sent is not None:
        db_chat_flag_set(chat.id, "intro")
        log.info("Auto-intro bheja: %s (%s)", chat.title, chat.id)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bot ko jab kisi channel/group me add ya admin banaya jaye -> Telegram
    turant us chat ki id + title bhejta hai. Yahi sabse aasan tareeka hai."""
    cm = update.my_chat_member
    if not cm or not cm.chat:
        return
    chat = cm.chat
    # PRIVATE chat = kisi ne bot ko /start kiya. Ye notify karne layak NAHI —
    # warna har naye user par owner ko DM spam hota hai.
    if chat.type == ChatType.PRIVATE:
        return
    status = getattr(cm.new_chat_member, "status", "")
    log.info("my_chat_member: %s (id=%s) status=%s", chat.title, chat.id, status)
    if _is_post_only(chat.id) or _post_only_username(chat):
        # Post-target channel: na DB me daalo, na intro, na owner ko DM.
        if getattr(chat, "username", None):
            db_post_id_set(chat.username, chat.id)
        log.info("Post-only chat — memory/analytics se bahar rakha (%s)", chat.id)
        return
    if status in (ChatMember.LEFT, ChatMember.BANNED):
        db_upsert_chat(chat.id, chat.title, str(chat.type or ""), status)
        return  # bot nikaal diya gaya

    # Poori chat detail + status yaad rakho (title match kare to block bhi bind).
    learn_chat(update, status=status, force=True)

    # NAYA add hua (pehle left/banned tha) -> wahin intro bhejo.
    _old = str(getattr(getattr(cm, "old_chat_member", None), "status", "") or "")
    if _old in (ChatMember.LEFT, ChatMember.BANNED, "left", "kicked"):
        await _auto_intro(context, chat)

    # Sirf tab notify karo jab status badla ho (same event baar baar na aaye).
    if _chat_status.get(chat.id) == status:
        return
    _chat_status[chat.id] = status
    _prune(_chat_status, 500)

    b = match_block_by_title(chat.title) if chat.title else None
    lines = [f"Chat: {chat.title}", f"ID: {chat.id}", f"Bot status: {status}"]
    if b:
        lines.append(f"Block match: {b['name']} — id saved")
    else:
        lines.append("Title did not match any block — /setid <block name> <id>")
    if status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        lines.append("⚠️ Bot is not ADMIN — this chat cannot be verified")
    await _notify_owner(context.bot, "\n".join(lines))


# =====================================================================
#  AI CHAT
# =====================================================================
def _strip_mention(text: str, username: str) -> str:
    """Mention hata do — case ki parwah nahi (@Bot, @BOT, @bot sab)."""
    if not text or not username:
        return (text or "").strip()
    return re.sub(rf"@{re.escape(username)}", " ", text, flags=re.IGNORECASE).strip()


# chat_id -> last DB write time (har message par DB write na ho)
_chat_seen: dict = {}


async def _learn_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sirf chat yaad rakho, kuch reply na karo (group 1 ka catch-all)."""
    learn_chat(update)


def learn_chat(update, status=None, force=False):
    """Bot jis chat me hai, uski POORI detail yaad rakho — id, title, type, status.
    Pehle sirf wahi chats yaad hoti thi jinka title kisi block se match kare,
    isliye /diag me baaki channels 'NOT FOUND' dikhate the — ab har chat
    record hoti hai, chahe title match kare ya na kare.
    Title match kare to block ki id bhi apne aap set kar dete hain."""
    chat = update.effective_chat
    if not chat or chat.type == ChatType.PRIVATE:
        return
    if _is_post_only(chat.id):
        return          # sirf post-target channel — yaad nahi rakhte
    if _post_only_username(chat):
        # Pehli baar username se pahchana -> id yaad karo (par chat ko nahi)
        db_post_id_set(chat.username, chat.id)
        return
    cid = chat.id
    now = time.time()
    if not force and now - _chat_seen.get(cid, 0) < 3600:
        return   # 1 ghante me ek hi baar DB write
    _chat_seen[cid] = now
    _prune(_chat_seen, 5000)
    title = chat.title or ""
    db_upsert_chat(cid, title, str(chat.type or ""), status)
    b = match_block_by_title(title) if title else None
    if b and _learned_ids().get(b["name"]) != cid:
        db_set_chat_id(b["name"], cid)
        log.info("Block '%s' -> chat id %s (title match)", b["name"], cid)


async def on_chat_seen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Channel post par bhi chat yaad rakho (bot us channel me admin ho to)."""
    learn_chat(update)


# =====================================================================
#  HUMAN BEHAVIOUR  —  jo asli insaan karta hai aur bot nahi karta
# =====================================================================
#  Teen cheezein yahan hain, teeno insaan ki asli aadatein:
#   1) ek saath 2-3 message aayein to har tukde ka jawab nahi — thoda ruk kar
#      poora padho, ek jawab do (debounce)
#   2) kabhi kabhi message par emoji reaction (Telegram feature)
#   3) ghanton baad wapas aaye to "kahan the" notice kar sake (gap)
_debounce: dict = {}       # uid -> {"texts": [...], "task": Task}
_last_msg_at: dict = {}    # uid -> last message ka time

_REACTIONS = ("\u2764", "\U0001fae6", "\u2601", "\U0001f319", "\u2728", "\U0001f60c")


def _gap_hours(uid):
    """Pichhle message se ab tak kitne ghante? (pehla message = None)"""
    prev = _last_msg_at.get(uid)
    _last_msg_at[uid] = time.time()
    _prune(_last_msg_at, 5000)
    if not prev:
        return None
    return (time.time() - prev) / 3600.0


async def _react(bot, msg) -> None:
    """Kabhi kabhi user ke message par emoji reaction — bilkul insaan jaisa."""
    try:
        await bot.set_message_reaction(
            chat_id=msg.chat_id, message_id=msg.message_id,
            reaction=random.choice(_REACTIONS))
    except Exception:
        pass          # reaction optional hai — fail ho to chup-chaap chhodo


async def _answer_private(context, chat_id, user, text, gap_h=None) -> None:
    """Ek hi jagah se AI ka jawab — debounced aur seedha, dono yahi use karte."""
    _t0 = time.monotonic()
    _ai_count()
    async with _typing(context, chat_id):
        answer = await ask_ai(
            text, where="a private chat", mem_key=f"u{user.id}",
            note=_owner_note(user.id), invite_uid=user.id,
            gap_h=gap_h, name=user.first_name or "")
    _ai_ms = (time.monotonic() - _t0) * 1000
    await _send_human(context.bot, chat_id, answer)
    # Asli number log me — warna "delay" ka andaza hi lagta rehta hai.
    log.info("Latency: ai=%.0fms, delivered=%.0fms",
             _ai_ms, (time.monotonic() - _t0) * 1000)


async def _debounced_answer(context, chat_id, user, slot, gap_h) -> None:
    """Thoda ruk kar ek saath saare tukde padho, phir EK jawab do."""
    try:
        await asyncio.sleep(max(0.0, MSG_DEBOUNCE))
    except asyncio.CancelledError:
        return          # naya message aa gaya — wo naya task sambhaal lega
    texts = [t for t in (slot.get("texts") or []) if t]
    slot["texts"] = []
    if not texts:
        return
    prompt = "\n".join(texts) if len(texts) > 1 else texts[0]
    try:
        await _answer_private(context, chat_id, user, prompt, gap_h)
    except Exception as e:
        log.warning("Debounced answer fail: %s", e)
        await _safe(context.bot.send_message, chat_id, busy_message())


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private chat mein AI bina mention chalta hai — seedha sawaal likho."""
    user = update.effective_user
    db_add_user(user)
    if db_is_banned(user.id):
        return
    # Maintenance: AI band (owner ke commands chalte rahenge).
    if db_maintenance_get() and not is_owner(user.id):
        await _safe(update.effective_message.reply_text,
                    "The bot is under maintenance right now. Please try again later.")
        return
    fl = _post_flow.get(user.id)
    if fl is not None:
        # Flow kabhi expire nahi hota tha: /post shuru karke chhod dene par
        # us user ke saare DM hamesha _post_step me jaate aur chup-chaap
        # nigal liye jaate. Ab 30 min baad flow khud hat jata hai.
        started = fl.get("_t")
        if started is None:
            fl["_t"] = time.time()          # purana flow — abhi se gino
            started = fl["_t"]
        if time.time() - started > 1800:
            _post_flow.pop(user.id, None)
        else:
            fl["_t"] = time.time()          # touch — active flow expire na ho
            await _post_step(update, context)
            return
    # Kisi channel/group ka message forward kiya? (chat id nikalne ke liye)
    # Owner/admin ke liye seedha id batao — warna AI us par baat karne lagta tha.
    fch = _forwarded_chat(update.effective_message)
    if fch is not None and is_privileged(user.id):
        await _report_forwarded_chat(update.effective_message, fch)
        return
    # Wahi message dobara (user ne do baar send kar diya / network ne duplicate
    # kar diya) -> AI dobara na chale. Ek sawaal, ek jawab.
    if _is_duplicate_text(update.effective_chat.id, user.id,
                          update.effective_message.text):
        log.info("Duplicate message from %s — AI skip (dedupe).", user.id)
        return
    # INVITE TIMING: turn gino, heavy mood note karo, "nahi" pakdo. Sab
    # eligibility hai — sach me bolna hai ya nahi wo AI tay karti hai.
    _note_message(user.id, update.effective_message.text)
    if not _ai_allowed(user.id):
        _ai_stats["rate"] += 1
        await update.effective_message.reply_text(
            f"{fonts.small_caps('Slow down')} — {fonts.small_caps('too many messages, try again in a little while')}."
        )
        return
    # Poore bot ka roz ka budget (per-user limit ke upar) — warna 200 log
    # milkar bill kabhi nahi rok paate.
    if _ai_budget_blocked():
        _ai_stats["budget"] += 1
        await _safe(update.effective_message.reply_text, budget_message())
        return
    gap_h = _gap_hours(user.id)
    if HUMAN_REACTIONS and random.random() < REACTION_CHANCE:
        await _react(context.bot, update.effective_message)
    text = update.effective_message.text or ""
    chat_id = update.effective_chat.id
    # DEBOUNCE: user ek saath 3 message bhej de (venting) to 3 jawab nahi —
    # poora padho, ek jawab do. Window chhota hai (0.35s), isliye akele
    # message par bhi kuch mehsoos nahi hota; pehle 1.2s tha aur wahi sabse
    # zyada "bot slow hai" lagta tha.
    if MSG_DEBOUNCE > 0:
        slot = _debounce.setdefault(user.id, {"texts": [], "task": None})
        slot["texts"].append(text)
        old = slot.get("task")
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(
            _debounced_answer(context, chat_id, user, slot, gap_h))
        slot["task"] = task
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        _prune(_debounce, 5000)
        return
    await _answer_private(context, chat_id, user, text, gap_h)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group/channel mein AI @mention par chalta hai — aur bot ke message ka
    reply bhi mention ke barabar hai (insaan reply se hi baat karta hai)."""
    msg = update.effective_message
    if not msg:
        return
    # Photo/document par bhi mention ho sakta hai — tab text nahi, caption
    # hota hai. Pehle sirf msg.text dekha jaata tha, isliye photo par mention
    # chup-chaap ignore ho jata tha.
    text = msg.text or msg.caption or ""
    if not text:
        return
    if db_maintenance_get():
        return                      # maintenance me group me AI chup
    entities = msg.entities or msg.caption_entities or []
    learn_chat(update)
    _note_message(update.effective_user.id, text)
    # Jo group me bolta hai, wo ledger me aa jata hai (throttled — har message
    # par DB write nahi).
    _k = (msg.chat.id, update.effective_user.id)
    if time.time() - _aud_seen.get(_k, 0) > 6 * 3600:
        _aud_seen[_k] = time.time()
        _prune(_aud_seen, 5000)
        _note_audience(msg.chat.id, update.effective_user, src="message")
    me = await _get_me(context)
    _uname = f"@{me.username.lower()}"
    mentioned = False
    # (a) bot ke message ka reply = mention
    _r = msg.reply_to_message
    if _r is not None and getattr(_r.from_user, "id", None) == me.id:
        mentioned = True
    # (b) asli @mention / text_mention
    for e in entities:
        if e.type == "mention":
            # Telegram ke offset/length UTF-16 code units me hote hain, Python
            # string ke code points me NAHI. Emoji ya koi bhi non-BMP character
            # ho to seedha slicing galat hoti hai aur mention miss ho jata hai.
            # parse_entity() PTB ka offset-safe helper hai.
            try:
                if msg.parse_entity(e).lower() == _uname:
                    mentioned = True
            except Exception:
                # parse_entity caption par RuntimeError deta hai; aur slice me
                # aage-peeche space aa sakta hai — strip zaroori hai.
                if text[e.offset:e.offset + e.length].strip().lower() == _uname:
                    mentioned = True
        elif e.type == "text_mention" and e.user and e.user.id == me.id:
            mentioned = True
    # Aakhri safety: entity kisi wajah se miss ho gayi (caption, emoji offset,
    # ya client ne entity bheji hi nahi) to seedha username dhoondh lo. Isse
    # mention chup-chaap ignore nahi hota.
    if not mentioned and _uname in text.lower():
        mentioned = True
    if not mentioned:
        return
    # Group/channel me gate NAHI — wahan seedha jawab do (gate sirf DM me).
    if not _ai_allowed(update.effective_user.id):
        # Pehle yahan CHUP-CHAAP return hota tha — user ko lagta tha bot ne
        # suna hi nahi ("kabhi dekhta nahi" wali shikayat). Ab ek baar
        # batate hain, phir cooldown tak chup.
        _ai_stats["rate"] += 1
        if _notice_allowed(("rate", msg.chat.id)):
            await _safe(msg.reply_text,
                        "Too many requests just now — please wait a moment and try again.")
        return
    # Group ka apna roz ka budget — ek group poore bot ka quota na kha jaye.
    if _ai_budget_blocked(msg.chat.id):
        _ai_stats["budget"] += 1
        if _notice_allowed(("budget", msg.chat.id)):
            await _safe(msg.reply_text, budget_message())
        return
    # Wahi message dobara mention hua -> dobara jawab na do.
    if _is_duplicate_text(msg.chat.id, update.effective_user.id, text):
        log.info("Duplicate mention in %s — AI skip (dedupe).", msg.chat.id)
        return
    _ai_count(msg.chat.id)
    prompt = _strip_mention(text, me.username) or "The user mentioned you. Greet them warmly."
    title = _clean_title(msg.chat.title)
    where = f'the Telegram group "{title}"' if title else "a Telegram group"
    _t0 = time.monotonic()
    async with _typing(context, update.effective_chat.id):
        answer = await ask_ai(
            prompt, where=where,
            mem_key=f"g{msg.chat.id}:u{update.effective_user.id}",
            note=_owner_note(update.effective_user.id),
        )
    log.info("Latency (group): ai=%.0fms", (time.monotonic() - _t0) * 1000)
    # Group me invite jaan-boojh kar NAHI (invite_uid nahi bhejte) — wahan
    # channel ki baat spam lagti hai. Timing/bubbles wahi insaan jaise.
    await _send_human(context.bot, msg.chat.id, answer, reply_to=msg.message_id)


# =====================================================================
#  ERROR HANDLER
# =====================================================================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Update error: %s", context.error, exc_info=context.error)


# =====================================================================
#  MEMBER COMMANDS
# =====================================================================
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kisi message par reply karke /report — admins ko DM jaata hai."""
    msg = update.effective_message
    r = msg.reply_to_message
    if not r:
        await msg.reply_text("Reply to the message you want to report, then send /report.")
        return
    who = update.effective_user
    sc, bd = fonts.small_caps, fonts.bold
    text = (
        f"🚩 {bd('REPORT')}\n\n"
        f"{sc('from')} : {who.full_name} (@{who.username or '-'})\n"
        f"{sc('id')}   : {who.id}\n"
        f"{sc('chat')} : {msg.chat.title or 'private'} [{msg.chat.id}]\n"
        f"{sc('msg')}  : {(r.text or r.caption or '[media]')[:300]}"
    )
    targets = list(ADMIN_IDS)
    _oid = owner_id()
    if _oid and _oid not in targets:
        targets.insert(0, _oid)       # owner ko report hamesha mile
    sent = 0
    for a in targets:
        try:
            await context.bot.send_message(a, text)
            sent += 1
        except Exception:
            pass
    await msg.reply_text("Report sent. Thank you."
                         if sent else "The report did not reach any admin.")


# =====================================================================
#  ADMIN / MODERATION COMMANDS  (bot ki chat permissions use karte hain)
# =====================================================================
#   (permission key, dikhne wala naam, iska matlab, kaunsi command)
_PERM_MEANING = [
    ("can_change_info",      "Change Group Info",    "can change the group name / photo",     "/settitle"),
    ("can_invite_users",     "Invite Users via Link", "can create invite links to add people",   "/invitelink"),
    ("can_restrict_members", "Ban Users",            "can ban / kick someone from the group", "/kick  /unkick"),
    ("can_delete_messages",  "Delete Messages",      "can delete anyone's message",  "/del"),
    ("can_promote_members",  "Add New Admins",       "can promote someone to admin",                 "/promote"),
    ("can_pin_messages",     "Pin Messages",         "can pin a message",                 "/pin"),
    ("can_manage_chat",      "Manage Chat",          "can manage the chat",              ""),
    ("can_post_messages",    "Post Messages",        "can post in the channel",             ""),
    ("can_edit_messages",    "Edit Messages",        "can edit channel posts",        ""),
]


def _is_group(update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def _target_uid(update, context):
    """Reply se ya pehle argument se user id nikaalo."""
    r = update.effective_message.reply_to_message
    if r and r.from_user:
        return r.from_user.id
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            return None
    return None


async def _need_owner(update):
    """Sirf owner. Baaki sab ke liye CHUP — koi message nahi.
    Wajah: agar admin ko "Only the owner can do this." dikhe to usko pata chal
    jata hai ki upar koi tier hai — aur admin ko ye pata nahi chalna chahiye.
    Commands menu bhi role ke hisaab se hi set hota hai (_set_commands).

    EK EXCEPTION: agar duniya me koi owner set hi nahi hai (na env, na DB),
    to chup rehna galat hai — owner ko lagega bot kharab hai. Us haalat me
    ek line ka hint jata hai. Owner set hote hi ye hint band.
    """
    uid = update.effective_user.id
    if is_owner(uid):
        return True
    if owner_id() == 0:
        await _safe(
            update.effective_message.reply_text,
            "Owner set nahi hai. Ek hi baar /claim bhejo (tum owner ban jaoge) "
            "- or set OWNER_ID=<your id> on Render.")
    return False


def _owner_core_ids() -> set:
    """Owner ke apne community/channel (LINK_BLOCKS) ki ids."""
    out = {_cid_key(b.get("id")) for b in LINK_BLOCKS if b.get("id")}
    out |= {_cid_key(v) for v in _learned_ids().values()}
    return out


def _is_owner_core(chat_id, chat=None) -> bool:
    """Owner ka apna chat? — id, title aur username teeno se pehchante hain.

    Sirf id se dekhna kaafi nahi tha: LINK_BLOCKS me ids @username ke roop me
    hote hain, isliye title/username match zaroori hai.
    """
    if _cid_key(chat_id) in _owner_core_ids():
        return True
    if chat is None:
        return False
    title = getattr(chat, "title", None)
    if not title:
        # Purane update me title na ho to DB se dekho (bot ne pehle seekha tha)
        title = str((db_get_chats().get(str(chat_id)) or {}).get("title") or "")
    if title and match_block_by_title(title):
        return True
    uname = str(getattr(chat, "username", "") or "").strip().lower().lstrip("@")
    if uname:
        for b in LINK_BLOCKS:
            if str(b.get("id") or "").strip().lower().lstrip("@") == uname:
                return True
    return False


async def _need(update, group_only=True):
    """Admin check + chat type check. True = aage badho."""
    if not is_privileged(update.effective_user.id):
        return False
    # ADMIN ko owner ke apne community/channel me moderation nahi milta.
    if (not is_owner(update.effective_user.id)
            and _is_owner_core(update.effective_chat.id,
                               update.effective_chat)):
        log.info("Admin moderation blocked in owner chat (%s) for %s",
                 update.effective_chat.id, update.effective_user.id)
        return False
    if group_only and not _is_group(update):
        await update.effective_message.reply_text("This command only works in a group.")
        return False
    return True


async def cmd_settitle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group ka naam badlo — OWNER only (admin ko ye permission nahi di)."""
    if not await _need_owner(update):
        return
    title = " ".join(context.args or []).strip()
    if not title:
        await update.effective_message.reply_text("Use: /settitle <new name>")
        return
    try:
        await context.bot.set_chat_title(update.effective_chat.id, title[:128])
        audit_add(update.effective_user.id, "settitle", None,
                  update.effective_chat.id, title[:80])
        await update.effective_message.reply_text(f"Name changed to: {title}")
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed: {_err_hint(e)}\n(The bot needs the 'Change Group Info' permission.)")


async def cmd_invitelink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Invite link banao — OWNER only (admin ko ye permission nahi di)."""
    if not await _need_owner(update):
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        await update.effective_message.reply_text("This command only works in a group or channel.")
        return
    try:
        link = await context.bot.create_chat_invite_link(update.effective_chat.id, name=BOT_BRAND)
        await update.effective_message.reply_text(
            f"{fonts.small_caps('invite link')}:\n{link.invite_link}")
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed: {_err_hint(e)}\n(The bot needs the 'Invite Users' permission.)")


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User ko group se ban/kick — 'Ban Users' permission chahiye."""
    if not await _need(update):
        return
    uid = _target_uid(update, context)
    if not uid:
        await update.effective_message.reply_text(
            "Use: /kick <user_id>  (or reply to their message with /kick)")
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        # KICK = ban + turant unban. Pehle sirf ban hota tha, isliye "kick"
        # kiya hua user invite link se wapas nahi aa sakta tha jab tak
        # /unkick na chale — jabki help text "let them back in" kehta hai
        # (audit F-09).
        await context.bot.unban_chat_member(update.effective_chat.id, uid,
                                            only_if_banned=True)
        # Journal: kaun, kya, kisko, kahan (appeal / galti sudhaarne ke liye).
        audit_add(update.effective_user.id, "kick", uid, update.effective_chat.id)
        await update.effective_message.reply_text(f"Kicked: {uid}")
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed: {_err_hint(e)}\n(The bot needs the 'Ban Users' permission.)")


async def cmd_unkick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban hatana — 'Ban Users' permission chahiye."""
    if not await _need(update):
        return
    uid = _target_uid(update, context)
    if not uid:
        await update.effective_message.reply_text(
            "Use: /unkick <user_id>  (or reply to their message with /unkick)")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid, only_if_banned=True)
        audit_add(update.effective_user.id, "unkick", uid, update.effective_chat.id)
        await update.effective_message.reply_text(f"Unbanned: {uid}")
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed: {_err_hint(e)}\n(The bot needs the 'Ban Users' permission.)")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply wala message delete — 'Delete Messages' permission chahiye."""
    if not await _need(update):
        return
    msg = update.effective_message
    r = msg.reply_to_message
    if not r:
        await msg.reply_text("Reply to the message you want to delete, then send /del.")
        return
    try:
        await context.bot.delete_message(r.chat_id, r.message_id)
        # Message ka TEXT audit me nahi likhte — wo user ka content hai aur
        # deletion ke baad bacha reh jata (audit AV-03). Sirf message id.
        audit_add(update.effective_user.id, "del", (r.from_user.id if r.from_user else None),
                  r.chat_id, f"msg {r.message_id}")
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        await msg.reply_text(f"Failed: {_err_hint(e)}\n(The bot needs the 'Delete Messages' permission.)")


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply wala message pin — 'Pin Messages' permission chahiye."""
    if not await _need(update):
        return
    msg = update.effective_message
    r = msg.reply_to_message
    if not r:
        await msg.reply_text("Reply to the message you want to pin, then send /pin.")
        return
    try:
        await context.bot.pin_chat_message(r.chat_id, r.message_id, disable_notification=True)
        audit_add(update.effective_user.id, "pin", None, r.chat_id, f"msg {r.message_id}")
        await msg.reply_text("Pinned.")
    except Exception as e:
        await msg.reply_text(f"Failed: {_err_hint(e)}\n(The bot needs the 'Pin Messages' permission.)")


async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Naya admin banao (group me) — OWNER only (admin ko ye permission nahi di)."""
    if not await _need_owner(update):
        return
    uid = _target_uid(update, context)
    if not uid:
        await update.effective_message.reply_text(
            "Use: /promote <user_id>  (or reply to their message with /promote)")
        return
    try:
        await context.bot.promote_chat_member(
            update.effective_chat.id, uid,
            can_manage_chat=True, can_delete_messages=True, can_restrict_members=True,
            can_invite_users=True, can_pin_messages=True, can_change_info=True,
        )
        audit_add(update.effective_user.id, "promote", uid, update.effective_chat.id)
        await update.effective_message.reply_text(f"Promoted: {uid}")
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed: {_err_hint(e)}\n(The bot needs the 'Add New Admins' permission.)")


async def cmd_perms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Is chat me bot ke paas kaunsi permission hai — matlab + command ke saath."""
    if not await _need_owner(update):
        return
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        await update.effective_message.reply_text("This command only works in a group or channel.")
        return
    chat = update.effective_chat
    me = await _get_me(context)
    sc, bd = fonts.small_caps, fonts.bold
    try:
        m = await context.bot.get_chat_member(chat_id=chat.id, user_id=me.id)
    except Exception as e:
        await update.effective_message.reply_text(f"Check fail: {_err_hint(e)}")
        return
    out = [bd("BOT PERMISSIONS"), "",
           f"{sc('chat')} : {chat.title or 'private'}",
           f"{sc('role')} : {bd(m.status)}", ""]
    if m.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
        out.append("The bot is not an ADMIN in this chat — no permissions.")
    else:
        for key, label, meaning, cmd in _PERM_MEANING:
            have = bool(getattr(m, key, False))
            out.append(f"{'✅' if have else '❌'} {bd(label)}")
            out.append(f"   {meaning}")
            if have and cmd:
                out.append(f"   {sc('cmd')}: {cmd}")
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ai <sawaal> — AI se seedha jawab (gate + rate limit ke saath)."""
    q = " ".join(context.args or []).strip()
    if not q:
        await update.effective_message.reply_text("Use: /ai <question>")
        return
    uid = update.effective_user.id
    if not _ai_allowed(uid):
        await update.effective_message.reply_text(
            f"{fonts.small_caps('Slow down')} — {fonts.small_caps('try again in a little while')}.")
        return
    chat = update.effective_chat
    priv = chat.type == ChatType.PRIVATE
    if _ai_budget_blocked(None if priv else chat.id):
        _ai_stats["budget"] += 1
        await update.effective_message.reply_text(budget_message())
        return
    _ai_count(None if priv else chat.id)
    mem = f"u{uid}" if priv else f"g{chat.id}:u{uid}"
    async with _typing(context, chat.id):
        answer = await ask_ai(q, where="a private chat" if priv else "a Telegram group",
                              mem_key=mem, note=_owner_note(uid))
    await update.effective_message.reply_text(answer)


async def _refresh_menu(context, uid) -> None:
    """Us user ka '/' menu turant set karo (role ke hisaab se).

    ZAROORI BUG FIX: /admin add se naya admin banta tha par uska command menu
    set hi nahi hota tha — usko /post menu me dikhta hi nahi tha aur lagta tha
    "kuch kaam nahi kar raha". Ab add/remove par turant refresh hota hai.
    """
    from telegram import BotCommand, BotCommandScopeChat
    desc = {c: d for c, d, _r in _COMMANDS}

    def _bc(name):
        c = POST_COMMAND if name == "POSTCMD" else name
        return BotCommand(c, desc.get(name, name))

    # Wahi minimal scheme jo _set_commands me hai (warna naye admin ko
    # 12 commands ka purana menu chala jata).
    member = [_bc(c) for c in _MEMBER_MENU]
    admin = member + [_bc("admin")]
    owner = member + [_bc("admin"), _bc("owner")]
    try:
        if is_owner(uid):
            cmds = owner
        elif is_admin(uid):
            cmds = admin
        else:
            cmds = member
        await context.bot.set_my_commands(
            cmds, scope=BotCommandScopeChat(chat_id=int(uid)))
        log.info("Command menu refresh: %s (%s commands)", uid, len(cmds))
    except Exception as e:
        log.warning("Command menu refresh fail (%s): %s", uid, e)


# /admin panel se shuru hua post -> ADMIN_CHANNELS (anime) me jayega.
_post_scope: dict = {}


def _admin_panel_kb(uid) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn("📝 Post", callback_data="admpanel:post", style="primary"),
        _btn("📄 Posts", callback_data="admpanel:posts", style="primary"),
    ]])


async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ADMIN PANEL — admin aur owner dono ke liye.

    /admin            -> panel (Post / Posts, owner ko Owner panel bhi)
    /admin add ...    -> admin add (sirf owner)
    /admin remove ... -> admin remove (sirf owner)
    /admin list       -> admins ki list (sirf owner)

    Panel se Post dabane par channels ADMIN_CHANNELS (anime) wale aate hain.
    """
    uid = update.effective_user.id
    if not is_privileged(uid):
        return
    if context.args:
        return await cmd_admin(update, context)
    sc, bd = fonts.small_caps, fonts.bold
    # Chhota aur saaf. "post kahan ja sakta hai / koi channel set nahi" wala
    # block hata diya — admin channel me post karta hi nahi, sirf link banata hai.
    text = (f"{bd('ADMIN PANEL')}\n\n"
            f"📝 {sc('Post')} — anime ya movie ka link banao\n"
            f"📄 {sc('Posts')} — banaye hue links\n\n"
            f"{sc('in a group, reply to a message')}: /del  /pin  /kick  /unkick")
    await update.effective_message.reply_text(text, reply_markup=_admin_panel_kb(uid))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: admins banao/hatao — bina redeploy.

    /admin add 123456789 Admin 1
    /admin remove 123456789
    /admin list
    Admin ko sirf: post, posts, del, pin, kick, unkick. Owner wali koi
    permission nahi.
    """
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    msg = update.effective_message
    args = context.args or []
    op = (args[0].lower() if args else "list")

    def _ids():
        seen, out = set(), []
        for a in list(ADMIN_IDS) + [int(k) for k in db_admins()]:
            if a not in seen:
                seen.add(a)
                out.append(a)
        return out

    if op in ("list", "show"):
        out = [bd("ADMINS"), ""]
        if not _ids():
            out.append(sc("no admins yet"))
        for a in _ids():
            src = "env" if a in ADMIN_IDS else "db"
            out.append(f"🛡 {bd(admin_name(a))}")
            out.append(f"   {sc('id')} {a}   ({src})")
            out.append(f"   {sc('has')} : post, posts, pin, del, kick, unkick")
        out += ["", f"{sc('add')}: /admin add <id> <naam>",
                f"{sc('remove')}: /admin remove <id>"]
        await _send_long(msg, "\n".join(out))
        return

    if op in ("add", "set"):
        if len(args) < 2:
            await msg.reply_text(f"{sc('use')}: /admin add <user_id> <naam>")
            return
        try:
            uid = int(args[1])
        except ValueError:
            await msg.reply_text(f"{sc('the user id must be a number')}")
            return
        name = " ".join(args[2:]).strip() or f"Admin {len(_ids()) + 1}"
        db_admin_add(uid, name)
        # Uska '/' menu TURANT set karo — warna naye admin ko /post menu me
        # dikhta hi nahi aur lagta hai "kuch kaam nahi kar raha".
        await _refresh_menu(context, uid)
        await msg.reply_text(
            f"{sc('admin add')}: {bd(name)} ({uid})\n"
            f"{sc('permissions')}: post, posts, del, pin, kick, unkick\n\n"
            f"{sc('their command menu is set')} - "
            f"{sc('they need to close and reopen the chat to see it')}")
        return

    if op in ("remove", "del", "rm"):
        if len(args) < 2:
            await msg.reply_text(f"{sc('use')}: /admin remove <user_id>")
            return
        try:
            uid = int(args[1])
        except ValueError:
            await msg.reply_text(f"{sc('user id numeric honi chahiye')}")
            return
        ok = db_admin_del(uid)
        if ok:
            await _refresh_menu(context, uid)  # wapis member menu
        await msg.reply_text(f"{sc('admin remove')}: {uid}" if ok
                             else f"{sc('that id is not in the database admins')}"
                                  f" ({sc('env wale ko Render se hatao')})")
        return

    await msg.reply_text(f"{sc('use')}: /admin add|remove|list")


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Team list — OWNER only."""
    if not await _need_owner(update):
        return
    viewer = update.effective_user.id
    sc, bd = fonts.small_caps, fonts.bold
    out = [bd("TEAM"), ""]
    if is_owner(viewer) and owner_id():
        out.append(f"👑 {bd('Owner')} : {OWNER_NAME or db_owner_name() or 'Owner'}")
        out.append(f"   {sc('id')} : {owner_id()}")
        out.append("")
    shown = 0
    _seen = set()
    _all = list(ADMIN_IDS) + [int(k) for k in db_admins()]
    for a in _all:
        if a in _seen:
            continue
        _seen.add(a)
        if not is_owner(viewer) and a == owner_id():
            continue          # admin ko owner ki id nahi dikhani
        out.append(f"🛡 {bd(admin_name(a))}")
        out.append(f"   {sc('id')} : {a}   ({sc('env') if a in ADMIN_IDS else sc('db')})")
        shown += 1
    if not shown:
        out.append("No admins configured — set ADMIN_IDS on Render.")
    await _send_long(update.effective_message, "\n".join(out))


# =====================================================================
#  /POST  —  post builder (Owner/Admin) + deep-link tracking
# =====================================================================
#   Flow:  /post -> type (APK/Update/AI Tool/News/Tip) -> CHANNEL buttons (multi-select
#          + Done) -> banner image (AI vision se padhi jaati hai, skip bhi kar
#          sakte ho) -> fields (POST_FIELDS) -> abhi/schedule -> post.
#   Post us/ un channels par image + caption + "Download" button ke saath
#   jaata hai. Button = bot ka deep link. Us par click karte hi bot chat
#   khulta hai, bot ko pata chalta hai kaunsa post + kaunse channels, aur
#   wo 1-2 line text + har channel ka "Request to Join" button bhejta hai.
# ---------------------------------------------------------------------
_post_flow: dict = {}      # user_id -> {"type","step","data","channels","image","await_*"}
# Post deep link se aaye par abhi gate paar na kiya ho -> user_id: code
_pending_post: dict = {}
# user_id -> (chat_id, message_id): jo gate message (5 links + Verify) bheja tha,
# taaki verify hone ke baad usko DELETE kar sakein.
_gate_msg: dict = {}




# Link registry aur posts ke db_* functions ab storage.py me hain.


def _new_code() -> str:
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:10]


def _deep_link(context, code) -> str:
    return f"https://t.me/{context.bot.username}?start=p{code}"


def _as_url(v) -> str:
    """Link field ko pura URL banao (https://t.me/xxx). Khali/galat -> ""."""
    v = str(v or "").strip()
    if not v:
        return ""
    if v.startswith(("http://", "https://")):
        return v
    if v.startswith(("t.me/", "www.")):
        return "https://" + v
    if re.match(r"^@?[A-Za-z0-9_]{4,}$", v):
        return "https://t.me/" + v.lstrip("@")
    return ""


def _norm_chat_id(x):
    """1001111111111 -> -1001111111111 (config me jo diya hai usko theek karo).
    "@AvenorFeed" jaisa username waisa hi rehta hai — Telegram dono leta hai."""
    try:
        cid = int(x)
    except (TypeError, ValueError):
        return str(x).strip()
    return cid if cid < 0 else -cid


def _esc(t) -> str:
    """HTML parse_mode ke liye & < > aur QUOTES escape karo.
    Quotes zaroori hain: user ka Link field `href="..."` me jaata hai — usme ek
    `"` aa gaya to HTML toot jata aur Telegram post reject kar deta."""
    return (str(t or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#x27;"))


def _post_fields(t) -> list:
    """Us post TYPE ke fields.

    Teen jagah se: naye types (POST_FIELDS_BY_TYPE) -> purane save pade posts
    ke types (POST_FIELDS_LEGACY) -> default (POST_FIELDS). Legacy sirf render
    ke liye hai, naya post un types me ban nahi sakta."""
    k = str(t or "").strip()
    return POST_FIELDS_BY_TYPE.get(k) or POST_FIELDS_LEGACY.get(k) or POST_FIELDS


# Pehli line me naam ke saath chhote se judne wale fields.
_TITLE_META = ("Version", "Size")


def _title_field(t) -> str:
    """Kaunsa field caption ki pehli line banta hai.

    "Name" ho to wahi; warna type ka pehla non-Link field ("Title" / "Headline").
    Yahi field detail lines me dobara NAHI aata — pehle News/Tip me title do
    baar chhapta tha."""
    fields = _post_fields(t)
    if "Name" in fields:
        return "Name"
    for f in fields:
        if f != "Link":
            return f
    return "Name"


def _title_text(p) -> str:
    """Caption ki pehli line: naam + version + size, ek hi line me.

    "OrbitBot v2.1 · 18 MB" — chat list preview me sirf pehli 2-3 line dikhti
    hain, isliye sabse zaroori cheez wahin jaati hai."""
    t = str(p.get(_title_field(p.get("type"))) or "").strip()
    bits = []
    v = str(p.get("Version") or "").strip()
    if v:
        bits.append(v if v[:1].lower() == "v" else f"v{v}")
    sz = str(p.get("Size") or "").strip()
    if sz:
        bits.append(sz)
    return " · ".join([t] + bits).strip(" ·")


def _post_lines(t) -> list:
    """Detail lines — jo fields pehli line me nahi gaye.

    Title field, Name, Link, Version aur Size chhod diye jaate hain (wo pehli
    line me ya button me jaate hain)."""
    skip = {"Name", "Link", _title_field(t)} | set(_TITLE_META)
    return [f for f in _post_fields(t) if f not in skip]


async def _ep_line(p) -> str:
    """'S03 EPISODE 01-02 ADDED' jaisi ek line — AI banata hai, warna season se."""
    if "Season" not in _post_fields(p.get("type")):
        # Jis type me Season nahi (APK / Update / AI Tool / News / Tip) usme
        # "S03 EPISODE 01 ADDED" ka koi matlab nahi. Config ke badge map se
        # seedha label lo, warna "<TYPE> ADDED".
        t = str(p.get("type") or "Post")
        return POST_BADGE_BY_TYPE.get(t) or f"NEW {t.upper()} ADDED"
    season = str(p.get("Season") or "").strip()
    if POST_EP_LINE:
        try:
            t = await ask_ai(
                f"{p.get('type') or 'Series'}: {p.get('Name', '')}. Season: {season}. "
                "Write one short line in exactly this style: 'S03 EPISODE 01 ADDED'. "
                "Use ONE episode number only — never a range like 01-02. "
                "Only that line, nothing else, no markdown, no quotes.",
                mem_key=None)
            if t and t != busy_message():
                line = t.strip().splitlines()[0].strip().strip('"').strip()
                line = re.sub(r"(\d+)\s*-\s*\d+", r"\1", line)   # 01-02 -> 01
                if line:
                    return _trim_words(line, 60)
        except Exception:
            pass
    s = re.sub(r"\D", "", season)
    return f"S{s} EPISODE ADDED" if s else "NEW EPISODE ADDED"


def _trim_words(s: str, limit: int) -> str:
    """limit par kaato — par WORD ke beech me nahi. Aakhri space par kaat ke
    '…' lagao. Pehle seedha s[:90] kar raha tha, jisse "AI re" jaisa aadha
    shabd aa jata tha."""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:          # space bahut pehle na ho to hi wahin kaato
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-·\n") + "…"


def _post_caption(p, ep_line="", link_url="", text_links=True) -> str:
    """Post ka caption (HTML) — blockquote (quote) style ke saath.
    Ep line aur WATCH & DOWNLOAD dono `link_url` par jaate hain (bot ka deep
    link — wahi jo neeche wale button me hai)."""
    bd = fonts.bold
    # Pehli line = naam + version + size (ek hi line, bold). Telegram ka
    # blockquote khud quote-mark dikhata hai, isliye "❝ ❞" nahi lagate.
    out = [f"<blockquote>{_esc(bd(_title_text(p) or '-'))}</blockquote>", ""]
    # Chhoti values ek saath inline jaati hain (►Version :- 1). Lambi ya
    # multi-line values (jaise 6-line changelog) ko APNA block milta hai,
    # apni line structure ke saath — pehle unhe 90 chars par kaat dete the
    # aur shabd beech me toot jata tha ("...and scan it. · 2. AI re").
    det, blocks = [], []
    for k in _post_lines(p.get("type")):
        v = p.get(k)
        if not v:
            continue
        raw = str(v).strip()
        lines = [x.strip() for x in raw.splitlines() if x.strip()]
        if len(lines) > 1 or len(raw) > 60:
            body = "\n".join(lines)
            blocks.append("<blockquote>" + _esc(bd(k)) + ":\n"
                          + _esc(_trim_words(body, 400)) + "</blockquote>")
        else:
            det.append(f"{_esc(bd(k))}: {_esc(raw)}")
    if det:
        out.append("<blockquote>" + "\n".join(det) + "</blockquote>")
        out.append("")
    for _blk in blocks:
        out.append(_blk)
        out.append("")
    _ct = _esc(bd(POST_CREDIT_TEXT))
    if text_links:
        out.append('<blockquote>[ Credit - <a href="' + _esc(POST_CREDIT_URL)
                   + '">' + _ct + "</a> ]</blockquote>")
    else:
        out.append(f"<blockquote>[ Credit - {_ct} ]</blockquote>")
    out.append("")
    # ── End block ───────────────────────────────────────────────────────
    # Pehle har line ke upar-neeche "━━━" separator tha, aur badge + CTA
    # beech me pade rehte the. Ab dono lines ek saath, sabse NEECHE, bina
    # separator ke — user ek nazar me padh ke click kar sake.
    # CTA line caption me NAHI — neeche button me wahi label hota hai, aur
    # dono jagah dikhne se "Get it" do baar aata tha. Caption me sirf badge.
    if ep_line:
        em = f"{POST_EP_EMOJI} " if POST_EP_EMOJI else ""
        tail = f" {POST_EP_EMOJI}" if POST_EP_EMOJI else ""
        # Badge ko BEECH me laao. Telegram me center karne ka koi option nahi
        # hai, isliye aage spaces lagate hain (Telegram spaces preserve karta
        # hai). Padding <a> tag ke BAAHAR hai, warna wo link ka hissa ban jati.
        lead = _badge_pad(f"{em}{bd(ep_line)}{tail}")
        if link_url and text_links:
            out.append(f'{lead}{em}<a href="{_esc(link_url)}">{_esc(bd(ep_line))}</a>{tail}')
        else:
            out.append(f"{lead}{em}{_esc(bd(ep_line))}{tail}")
    return "\n".join(out)


def _parse_when(txt):
    """'YYYY-MM-DD HH:MM' ya 'HH:MM' (IST) -> aware datetime (IST)."""
    txt = (txt or "").strip()
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}$", txt):
            dt = datetime.strptime(txt, "%Y-%m-%d %H:%M").replace(tzinfo=ist)
        elif re.match(r"^\d{1,2}:\d{2}$", txt):
            h, m = (int(x) for x in txt.split(":"))
            dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
        else:
            return None
        return dt if dt > now else None
    except Exception:
        return None


async def _channel_join_url(context, chat_id):
    """Us channel ka aisa link jise user join kar sake."""
    try:
        ch = await context.bot.get_chat(chat_id)
        if getattr(ch, "username", None):
            return f"https://t.me/{ch.username}"
    except Exception:
        pass
    try:
        link = await context.bot.create_chat_invite_link(chat_id, name=BOT_BRAND[:32])
        return link.invite_link
    except Exception:
        return None


# ---------- Gemini vision (image padhna) ----------
def _run_ai_vision(data: bytes, prompt: str) -> str:
    client = _get_ai()
    part = types.Part.from_bytes(data=data, mime_type="image/jpeg")
    resp = client.models.generate_content(
        model=AI_MODEL, contents=[part, prompt], config=_get_ai_config(AI_MODEL))
    return (getattr(resp, "text", None) or "").strip()


async def _vision_read(context, file_id, prompt) -> str:
    """Telegram se image ka data laao aur AI se padhwao."""
    try:
        f = await context.bot.get_file(file_id)
        data = bytes(await f.download_as_bytearray())
        txt = await asyncio.get_running_loop().run_in_executor(
            _AI_POOL, _run_ai_vision, data, prompt)
        return _strip_md(txt)
    except Exception as e:
        log.warning("Vision fail: %s", e)
        return ""


_URL_RX = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.I)


def _strip_urls(t: str) -> str:
    """Text se URL hata do (welcome line me raw link nahi dikhna chahiye —
    link to neeche wale button me hai)."""
    out = _URL_RX.sub("", t or "")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return "\n".join(ln.rstrip() for ln in out.splitlines()).strip()


VISION_PROMPT = (
    "Look at this banner image and give only these lines (only what is visible; "
    "use ? for anything you cannot read). No extra talk, no markdown:\n"
    "Name: \nVersion: \nSize: \nPlatform: "
)


_VISION_LINE_RX = re.compile(r"^\s*([A-Za-z][A-Za-z ]{1,20}?)\s*:\s*(.+?)\s*$", re.M)


def _fill_from_vision(fl, read) -> list:
    """Banner se padhi lines ko seedha fields me bharo.

    Sirf wahi fields jo (a) is type me hain, (b) abhi khali hain, aur
    (c) value saaf padhi gayi hai. Return: kaunse fields bhare — user ko
    batane ke liye (aur preview me Edit se badalne ke liye)."""
    if not read:
        return []
    fields = _post_fields(fl["type"])
    filled = []
    for name, val in _VISION_LINE_RX.findall(str(read)):
        v = val.strip().strip('"').strip()
        if not v or v in ("?", "-", "—"):
            continue
        real = next((f for f in fields if f.lower() == name.strip().lower()), None)
        if not real or real == "Link":
            continue
        if str(fl["data"].get(real) or "").strip():
            continue
        fl["data"][real] = v[:120]
        filled.append(real)
    return filled


# ---------- channel chunne wale buttons ----------
def _channel_rows(sel, channels=None) -> list:
    rows = []
    for row in (channels or POST_CHANNELS):
        r = []
        for name, cid in row:
            c = _norm_chat_id(cid)
            mark = DONE_MARK + " " if c in sel else ""
            r.append(_btn(f"{mark}{name}", callback_data=f"postch:{c}",
                          style="success" if c in sel else "primary"))
            # 2 se zyada button ek row me -> naam kat jate hain (5 channel
            # ek row me "ANIM…" ban gaye the). Isliye har 2 par row todo.
            if len(r) == 2:
                rows.append(r)
                r = []
        if r:
            rows.append(r)
    rows.append([_btn("Done", callback_data="postch_done", style="success")])
    return rows


# Admin ka kaam sirf LINK banana hai — do blocks: Anime / Movie.
LINK_TYPES = ["Anime", "Movie"]


def _post_types_for(uid, scope=None) -> list:
    """Owner -> poora post builder. Admin -> sirf Anime / Movie (link flow)."""
    if is_owner(uid) and scope != "admin":
        return list(POST_TYPES)
    return list(LINK_TYPES)


def _channels_for(uid, scope=None) -> list:
    """Kaunse channels post ke liye.

    scope="admin"  -> ADMIN_CHANNELS (anime wale), owner ke liye bhi.
    warna          -> owner: POST_CHANNELS, admin: ADMIN_CHANNELS.
    Khali ho to kuch nahi — fail-closed, warna admin owner ke channel me
    post kar deta hai.
    """
    if is_owner(uid) and scope != "admin":
        return POST_CHANNELS
    # Admin (aur /admin panel se aaya koi bhi) channel me post NAHI karta —
    # wo sirf link banata hai. Isliye yahan koi channel nahi.
    return []


async def _post_channel_kb(chat_id, context, uid, edit_msg=None):
    fl = _post_flow.get(uid)
    if not fl:
        return
    # Admin ko sirf ADMIN_CHANNELS dikhte hain (owner ke channels nahi).
    _ch = _channels_for(uid, fl.get("scope"))
    if not _ch:
        await _safe(context.bot.send_message, chat_id,
                    "Admin ke liye koi channel set nahi hai. Owner se bolo: "
                    "set ADMIN_CHANNELS on Render.")
        return
    kb = InlineKeyboardMarkup(_channel_rows(set(fl.get("channels") or []), _ch))
    txt = (f"{fonts.bold('Where should it be posted?')}\n"
           f"{fonts.small_caps('pick one or more, then tap Done')}")
    if edit_msg is not None:
        try:
            await edit_msg.edit_reply_markup(kb)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, txt, reply_markup=kb)


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/post` — OWNER ka poora builder (apne 2 channels ke liye)."""
    return await _start_post(update, context, scope=None)


async def _start_post(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      scope=None):
    """Post flow ka ek hi entry point.

    ZAROORI (bug fix): pehle `/post` purane `_post_scope` ko padh leta tha.
    Ek baar /admin panel se 📝 Post dabaya, to owner ka agla `/post` bhi
    anime wala flow dikhane lagta tha. Ab scope SIRF yahan se set hota hai —
    koi purana state padha nahi jata.
    """
    if not is_privileged(update.effective_user.id):
        return
    uid = update.effective_user.id
    # Admin ke liye hamesha link flow; owner ke liye sirf jab panel se aaya ho.
    _scope = "admin" if (not is_owner(uid) or scope == "admin") else None
    if _scope != "admin":
        _ch = _channels_for(uid, _scope)
        if not _ch:
            await update.effective_message.reply_text(
                "No channels configured. Set POST_CHANNELS in config.py first.")
            return
    # Flow ke saare sawaal DM me poochhe jaate hain (jawab sirf private
    # message handler uthata hai). Group me /post chalane par bot har field
    # par chup ho jata tha — ab saaf-saaf DM par bhej dete hain.
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            f"The post builder runs in a private chat. Message me here: "
            f"@{context.bot.username} — then send /{POST_COMMAND} there.")
        return
    _post_flow.pop(update.effective_user.id, None)
    # Admin ko sirf Anime / Movie dikhte hain (link flow). Owner ko apne
    # 5 types: Release, Update, AI Tool, Dev Log, Tip.
    types = _post_types_for(uid, _scope)
    _post_scope[uid] = _scope          # posttype callback ko chahiye
    if _scope == "admin":
        # Anime | Movie — do block, side by side (half half)
        kb = InlineKeyboardMarkup([[
            _btn(t, callback_data=f"posttype:{t}", style="primary") for t in types
        ]])
    else:
        kb = InlineKeyboardMarkup(
            [[_btn(t, callback_data=f"posttype:{t}", style="primary")] for t in types])
    await update.effective_message.reply_text(
        f"{fonts.bold('What are you posting?')}", reply_markup=kb)


async def _post_ask(chat_id, context, uid):
    """Flow ka agla sawaal — jo fields pehle se bhari hain unhe chhod do."""
    fl = _post_flow.get(uid)
    if not fl:
        return
    fields = _post_fields(fl["type"])
    while fl["step"] < len(fields) and fields[fl["step"]] in fl["data"]:
        fl["step"] += 1
    if fl["step"] < len(fields):
        f = fields[fl["step"]]
        await context.bot.send_message(
            chat_id, f"Send {fonts.bold(f)}  ({fonts.small_caps('type')} skip {fonts.small_caps('to leave it out')})")
        return
    await _post_preview(chat_id, context, uid)


async def _post_preview(chat_id, context, uid):
    """Preview + Post / Edit / Schedule. Field edit ke baad bhi yahi aata hai."""
    fl = _post_flow.get(uid)
    if not fl:
        return
    preview = {"type": fl["type"], **fl["data"]}
    if not fl.get("ep_line"):
        fl["ep_line"] = await _ep_line(preview)
    kb = InlineKeyboardMarkup([[
        _btn("Post", callback_data="postgo:now", style="success"),
        _btn("Edit", callback_data="postedit:start", style="primary"),
        _btn("Schedule", callback_data="postgo:sched", style="primary"),
    ]])
    chans = ", ".join(str(c) for c in (fl.get("channels") or []))
    img = "✅" if fl.get("image") else "—"
    _lnk = (preview.get("Link") or "").strip()
    _lnk_line = (f"{fonts.small_caps('link')}: {_lnk}\n"
                 f"{fonts.small_caps('goes to')}: {JOIN_BUTTON_TEXT} button "
                 f"({fonts.small_caps('not in the caption')})") if _lnk else \
                f"{fonts.small_caps('link')}: -"
    await context.bot.send_message(
        chat_id,
        f"{fonts.bold('PREVIEW')}\n\n{_post_caption(preview, fl['ep_line'], 'https://t.me/' + str(context.bot.username))}"
        f"\n\n{fonts.small_caps('channels')}: {chans}\n"
        f"{fonts.small_caps('image')}: {img}\n{_lnk_line}",
        reply_markup=kb, parse_mode="HTML")


async def _post_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flow ke dauraan user ka text — jawab maan lo."""
    uid = update.effective_user.id
    fl = _post_flow.get(uid)
    if not fl:
        return
    txt = (update.effective_message.text or "").strip()

    # --- LINK FLOW (admin): sirf link -> bot ka start link. Koi post nahi. ---
    if fl.get("mode") == "link":
        link = _as_url(txt)
        if not link:
            await update.effective_message.reply_text(
                f"{fonts.bold('Link sahi nahi lag raha')}\n"
                f"{fonts.small_caps('it must start with https://')} - send it again.")
            return
        code = _new_code()
        db_post_save(code, {
            "code": code, "type": fl["type"], "Name": fl["type"],
            "Link": link, "channel_ids": [], "posted": False,
            "created": _now(),
        })
        _post_flow.pop(uid, None)
        await update.effective_message.reply_text(
            f"{_deep_link(context, code)}\n\n"
            f"{fonts.small_caps('jisko dena ho, ye bhej do')}")
        return

    # --- preview se ek field edit ---
    if fl.get("await_edit"):
        f = fl.pop("await_edit")
        fl["data"][f] = "" if txt.lower() == "skip" else txt
        fl["ep_line"] = ""          # badge dobara banao
        await _post_preview(update.effective_chat.id, context, uid)
        return

    # --- banner image ka intezaar ---
    if fl.get("await_image"):
        fl.pop("await_image", None)
        if txt.lower() != "skip" and txt:
            # image nahi bheji par kuch likha -> usko pehla field maan lo
            fl["data"].setdefault(_post_fields(fl["type"])[0], txt)
        await _post_ask(update.effective_chat.id, context, uid)
        return

    # --- schedule ka time ---
    if fl.get("await_time"):
        dt = _parse_when(txt)
        if not dt:
            await update.effective_message.reply_text(
                "Could not read that. Use:  2026-10-18 20:30   (or just 20:30 — IST)")
            return
        fl.pop("await_time", None)
        code = _new_code()
        data = dict(fl["data"])
        data.update({"code": code, "type": fl["type"], "channel_ids": list(fl.get("channels") or []),
                     "ep_line": fl.get("ep_line"),
                     "image": fl.get("image"), "posted": False,
                     "created": _now(), "post_at": dt.strftime("%Y-%m-%d %H:%M")})
        db_post_save(code, data)
        jq = getattr(context.application, "job_queue", None)
        if jq is None:
            await update.effective_message.reply_text(
                "Scheduling is unavailable (job-queue not installed). Posting now instead.")
            await _send_post(context, code)
        else:
            jq.run_once(_job_post, when=dt, data=code, name=f"post:{code}")
            await update.effective_message.reply_text(
                f"Scheduled for {dt.strftime('%Y-%m-%d %H:%M')} IST\nCode: {code}")
        _post_flow.pop(uid, None)
        return

    # --- normal field ---
    fields = _post_fields(fl["type"])
    if fl["step"] < len(fields):
        _field = fields[fl["step"]]
        _val = "" if txt.lower() == "skip" else txt
        fl["data"][_field] = _val
        # Link field -> registry me bhi save (dedup ke saath). Wahi link
        # dobara aaya to naya entry NAHI banta, sirf uses badh jata hai.
        if _field == "Link" and _val:
            try:
                _lk, _new = db_link_add(_val, fl.get("type") or "")
                log.info("Link %s: %s", "saved" if _new else "reused", _lk)
            except Exception as e:
                log.warning("Link registry fail: %s", e)
        fl["step"] += 1
        await _post_ask(update.effective_chat.id, context, uid)
        return


# DM me text ke alawa jo bhi aaye — chup nahi rehna hai.
NO_MEDIA_TEXT = ("i can only read text right now — write your question "
                 "and i will answer.")


async def on_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private chat me photo — agar /post flow chal raha hai to banner maano."""
    user = update.effective_user
    msg = update.effective_message
    fl = _post_flow.get(user.id)
    if not fl or not fl.get("await_image"):
        # Pehle yahan chup-chaap return hota tha: user photo bhejta tha aur
        # bot bilkul kuch nahi kehta tha (lagta tha bot mar gaya hai).
        await _safe(msg.reply_text, NO_MEDIA_TEXT)
        return
    photos = getattr(msg, "photo", None) or []
    if not photos:
        await _safe(msg.reply_text, NO_MEDIA_TEXT)
        return
    fl["image"] = photos[-1].file_id
    fl.pop("await_image", None)
    await _safe(msg.reply_text, f"{fonts.small_caps('image received — reading it with AI')} …")
    read = await _vision_read(context, fl["image"], VISION_PROMPT)
    filled = _fill_from_vision(fl, read)
    if filled:
        await _safe(msg.reply_text,
            f"{fonts.bold('Read from the image')} — "
            f"{fonts.small_caps('filled')}: {', '.join(filled)}\n"
            f"{fonts.small_caps('asking the rest now')} · "
            f"{fonts.small_caps('preview me Edit se badal sakte ho')}")
    elif read:
        await _safe(msg.reply_text,
            f"{fonts.bold('Image read')} — "
            f"{fonts.small_caps('kuch saaf nahi mila, haath se bharo')}")
    await _post_ask(update.effective_chat.id, context, user.id)


async def on_private_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM me sticker / voice / file / video — chup-chaap ignore mat karo."""
    msg = update.effective_message
    fl = _post_flow.get(update.effective_user.id)
    if fl is not None and fl.get("await_image"):
        await _safe(msg.reply_text, "Send the banner as a photo, or type skip.")
        return
    await _safe(msg.reply_text, NO_MEDIA_TEXT)


CAPTION_LIMIT = 1024      # Telegram ki caption limit (photo ke saath)
TEXT_LIMIT = 3800         # bina photo wale post ke liye (send_message 4096 deta hai)
_TAG_ROOM = 40            # closing tags ke liye jagah


def _text_width(s: str) -> float:
    """Rendered width ka andaza, character count se behtar.
    Emoji ~2 character jagah lete hain aur bold math letters (fonts.bold) ~1.3x
    chaude hote hain — bina iske padding kam pad jati hai aur line phir bhi
    left lagti hai."""
    total = 0.0
    for ch in s or "":
        o = ord(ch)
        if o >= 0x1F000:            # emoji
            total += 2.0
        elif 0x1D400 <= o <= 0x1D7FF:   # math bold / mono / script
            total += 1.3
        else:
            total += 1.0
    return total


def _badge_pad(plain: str) -> str:
    """Badge line ke aage spaces — Telegram me center alignment hoti hi nahi,
    isliye ye hi ek tareeka hai.

    POST_BADGE_WIDTH = line ki chaudai (character me). Aapke phone par caption
    ki line ~34-40 character ki hoti hai. Beech se hat kar dikhe to:
      +2 width  -> ek space aage (daayein)
      -2 width  -> ek space peeche (baayein)
    """
    if not POST_BADGE_CENTER or POST_BADGE_WIDTH <= 0:
        return ""
    pad = int((POST_BADGE_WIDTH - _text_width(plain)) / 2)
    return " " * max(0, pad)


def _fit_caption(cap: str, limit: int = CAPTION_LIMIT) -> str:
    """1024 limit — line par kaato AUR koi HTML tag adhoora na chhode.

    Line par kaatna kaafi nahi hai: <blockquote> kai lines par phaila hota
    hai, to tag khula reh jata hai aur Telegram poora caption reject kar deta
    hai — wahi cheez jise rokne ke liye ye function hai.

    Aur ek chhoti galti thi: closing tags append karne ke baad caption phir
    se 1024 se lamba ho sakta tha (har band tag 13 character tak jodta hai),
    aur tab bhi Telegram reject karta. Isliye content ke liye 40 character
    pehle hi rok dete hain.
    """
    if len(cap) <= limit:
        return cap
    budget = limit - _TAG_ROOM
    keep, total = [], 0
    for line in cap.split("\n"):
        add = len(line) + (1 if keep else 0)
        if total + add > budget:
            # Pehli line hi limit se lambi ho to poora caption khaali na ho —
            # us line ko kaat ke rakho.
            if not keep:
                keep.append(line[:budget])
            break
        keep.append(line)
        total += add
    out = "\n".join(keep)
    # jitne tag khule hain, unhe band karo (blockquote / a / b)
    for tag in ("blockquote", "a", "b", "i"):
        opens = out.count(f"<{tag}") - out.count(f"</{tag}>")
        out += f"</{tag}>" * max(0, opens)
    # aakhri safety: tag band karne ke baad bhi limit ke andar raho.
    return out[:limit]


async def _post_send_one(context, cid, p, cap, kb, html=True):
    kw = {"parse_mode": "HTML"} if html else {}
    if p.get("image"):
        await context.bot.send_photo(cid, p["image"], caption=cap, reply_markup=kb, **kw)
    else:
        await context.bot.send_message(cid, cap, reply_markup=kb, **kw)


async def _send_post(context, code):
    """Post bhejo — saare chune channels par, image + caption + Download button.

    Mastermind: bot ke saare asli links BUTTON me hain (inline keyboard buttons
    message text nahi hote, isliye koi anti-spam / link filter unhe nahi rokta).
    Caption me sirf decoration ke liye text-links hote hain — agar kisi channel ne
    link wala text block kar rakha ho aur pehli koshish fail ho, to wahi caption
    BINA text-link ke dobara bhejte hain (button phir bhi kaam karta hai)."""
    p = db_post_get(code)
    if not p:
        return False
    kb = InlineKeyboardMarkup([[_btn(
        POST_BUTTON_BY_TYPE.get(str(p.get("type") or ""), POST_BUTTON_TEXT),
        url=_deep_link(context, code), style="primary")]])
    ep = p.get("ep_line") or await _ep_line(p)
    deep = _deep_link(context, code)
    # Photo wale post par Telegram ki caption limit 1024 hai; bina photo wale
    # post send_message se jaate hain jahan 4096 allowed hai. Pehle dono 1024
    # par kat jaate the, isliye lamba text-only post chup-chaap adhoora jaata tha.
    limit = CAPTION_LIMIT if p.get("image") else TEXT_LIMIT
    cap = _fit_caption(_post_caption(p, ep, deep), limit)
    chans = p.get("channel_ids") or ([p["channel_id"]] if p.get("channel_id") else [])
    ok = False
    failed = []
    for cid in chans:
        try:
            await _post_send_one(context, cid, p, cap, kb, html=True)
            ok = True
        except Exception as e:
            log.warning("Post fail (%s): %s", cid, e)
            failed.append(cid)
    if failed:
        plain = _fit_caption(_post_caption(p, ep, deep, text_links=False), limit)
        still = []
        for cid in failed:
            try:
                await _post_send_one(context, cid, p, plain, kb, html=False)
                ok = True
                log.info("Post plain fallback chal gaya (%s) — links buttons me hain.", cid)
            except Exception as e:
                log.warning("Post plain fallback bhi fail (%s): %s", cid, e)
                still.append(cid)
        if still:
            log.error("Post in channels me nahi gaya: %s", still)
    if ok:
        # Post-target channels ka id yaad karo (taaki bot unhe apne community
        # me na gine) — par chat ko learn NAHI karte.
        for cid in chans:
            try:
                _ch = await context.bot.get_chat(cid)
                if getattr(_ch, "username", None):
                    db_post_id_set(_ch.username, _ch.id)
            except Exception:
                pass
        db_post_update(code, posted=True, posted_at=_now())
    return ok


async def _job_post(context: ContextTypes.DEFAULT_TYPE):
    code = context.job.data
    ok = await _send_post(context, code)
    await _notify_owner(context.bot, f"Scheduled post {'sent' if ok else 'FAILED'}: {code}")


async def _rearm_posts(app):
    """Restart ke baad pending scheduled posts dobara set karo."""
    jq = getattr(app, "job_queue", None)
    if jq is None:
        return
    for code, p in db_post_all().items():
        if p.get("post_at") and not p.get("posted"):
            dt = _parse_when(p["post_at"])
            if dt:
                jq.run_once(_job_post, when=dt, data=code, name=f"post:{code}")
                log.info("Re-armed scheduled post %s at %s", code, dt)
            else:
                # Time nikal gaya (Render restart) — chup-chaap chhodo mat,
                # turant bhej do warna post hamesha ke liye gayab ho jata.
                log.warning("Scheduled post %s ka time nikal gaya — ab bhej raha hoon.", code)
                jq.run_once(_job_post, when=1, data=code, name=f"post:{code}")


_bg_tasks: set = set()


async def _welcome_upgrade(context, msg, name, p, kb):
    """Background me AI se behtar line lo aur message edit kar do.
    Message pehle hi turant ja chuka hota hai — isliye koi delay nahi."""
    try:
        line = await ask_ai(
            f"Someone just opened this bot to watch the {p.get('type', 'post').lower()} "
            f'titled "{name}". Write ONE short, plain, respectful welcome line '
            "(at most two short sentences), in English. Say only that they are "
            "welcome and that the channel link is right below. "
            "Do NOT use cheesy filler or hype, no exclamation marks, no emoji, and "
            'never lines like "sit back and enjoy", "relax", "buckle up", "you are '
            'in the right place" or "enjoy the show". Do not repeat the title back '
            "as a sentence. Do NOT paste any URL — the link is already on the "
            "button below. No markdown.",
            mem_key=None)
        if line and line != busy_message():
            line = _strip_urls(line)          # raw link text me nahi dikhana
            if line:
                await msg.edit_text(f"{line}\n\n{fonts.bold(name)}", reply_markup=kb)
    except Exception as e:
        log.info("Welcome AI upgrade skip: %s", e)


async def _post_welcome(update, context, p, chat_id=None):
    """Deep link se aaya user — TURANT text + Join button (koi AI wait nahi).
    AI ki line background me aati hai aur baad me edit ho jaati hai.
    chat_id diya ho (gate callback se) to wahan bhejo, warna reply karo."""
    name = p.get("Name") or "this"
    rows = []
    link = _as_url(p.get("Link"))          # user ne jo Link diya wahi join target
    if link:
        rows = [[_btn(JOIN_BUTTON_TEXT, url=link, style="primary")]]
    else:
        for cid in (p.get("channel_ids") or []):
            url = await _channel_join_url(context, cid)
            if url:
                rows.append([_btn(JOIN_BUTTON_TEXT, url=url, style="primary")])
    if not rows:
        rows = [[_btn(JOIN_BUTTON_TEXT, url="https://t.me", style="primary")]]
    kb = InlineKeyboardMarkup(rows)
    txt = (f"{fonts.bold(name)}\n\n"
           f"{fonts.small_caps('tap below to open the channel')}")
    if chat_id:
        sent = await _safe(context.bot.send_message, chat_id, txt, reply_markup=kb)
    else:
        sent = await _safe(update.effective_message.reply_text, txt, reply_markup=kb)
    if sent is None:
        return
    try:
        # create_task ka reference rakho — warna CPython running task ko
        # garbage-collect kar sakta hai aur upgrade beech me gayab ho jata.
        _t = asyncio.create_task(_welcome_upgrade(context, sent, name, p, kb))
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)
    except Exception:
        pass


async def cmd_btnstyle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: button colour test — 3 rangeen buttons. Agar ye rangeen dikhein to
    sab theek hai; sab plain dikhein to Telegram app purana hai (9 Feb 2026 se
    pehle ka)."""
    if not is_owner(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup([
        [_btn("Callback · blue", callback_data="bs:p", style="primary")],
        [_btn("Callback · green", callback_data="bs:s", style="success")],
        [_btn("Callback · red", callback_data="bs:d", style="danger")],
        [_btn("Link · blue", url="https://t.me/telegram", style="primary")],
        [_btn("Link · green", url="https://t.me/telegram", style="success")],
    ])
    payload = json.dumps(kb.to_dict())
    await update.effective_message.reply_text(
        f"{fonts.bold('Button colour test')}\n\n"
        "Top 3 are action buttons, bottom 2 are link buttons.\n"
        "All 5 should look coloured (blue / green / red).\n\n"
        f"{fonts.bold('If they look plain, it is your app, not the bot.')}\n"
        "Button colour is a CLIENT feature — Telegram silently ignores it on "
        "apps older than 9 Feb 2026, and on old messages already in the chat.\n\n"
        f"{fonts.bold('Two ways to be sure')}:\n"
        "1. Open https://web.telegram.org in a browser and send /btnstyle "
        "there. Telegram Web is always the latest build — if the buttons are "
        "coloured there, your phone app is the old one.\n"
        "2. Send /start fresh (old messages keep their old keyboard).\n\n"
        f"{fonts.small_caps('this is the payload the bot is sending')}:\n{payload[:280]}",
        reply_markup=kb)


async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: saare saved links. Duplicate ho to pehle dedupe karta hai."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    _removed = db_links_dedupe()
    links = db_links()
    out = [bd("SAVED LINKS"), ""]
    if _removed:
        out += [f"{sc('duplicates removed')}: {bd(str(_removed))}", ""]
    if not links:
        out.append(sc("no links saved yet"))
        await _send_long(update.effective_message, "\n".join(out))
        return
    out.append(f"{sc('total')}: {bd(str(len(links)))}")
    out.append("")
    items = sorted(links.items(), key=lambda kv: kv[1].get("last", ""), reverse=True)
    for k, v in items[:40]:
        out.append(f"{bd(k)}")
        out.append(f"   {sc('uses')}: {v.get('uses', 1)}   {sc('last')}: {v.get('last', '-')}")
        if v.get("note"):
            out.append(f"   {sc('type')}: {v['note']}")
    if len(items) > 40:
        out.append("")
        out.append(sc(f"...and {len(items) - 40} more"))
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: bane hue posts ki list."""
    if not is_privileged(update.effective_user.id):
        return
    allp = db_post_all()
    if not allp:
        await update.effective_message.reply_text("No posts yet. Create one with /post.")
        return
    sc, bd = fonts.small_caps, fonts.bold
    out = [bd("POSTS"), ""]
    for code, p in sorted(allp.items(), key=lambda kv: kv[1].get("created", ""), reverse=True)[:20]:
        st = "✅ posted" if p.get("posted") else ("🕒 " + str(p.get("post_at") or "draft"))
        out.append(f"{bd(p.get('Name') or '-')}")
        out.append(f"   {sc('code')} : {code}   {st}")
        out.append(f"   {sc('link')} : {_deep_link(context, code)}")
    await _send_long(update.effective_message, "\n".join(out))


# =====================================================================
#  AUTO-POST  —  bot khud bhejta hai, bina kisi command ke
# =====================================================================
async def _job_autopost(context: ContextTypes.DEFAULT_TYPE):
    """Roz ka auto-post. Text DB se aata hai (restart ke baad bhi chalta hai)."""
    cid = context.job.data
    conf = db_autopost_all().get(str(cid))
    if not conf or not conf.get("text"):
        return
    target = cid
    try:
        target = int(cid)
    except (TypeError, ValueError):
        pass
    await _safe(context.bot.send_message, target, conf["text"])
    log.info("Auto-post bheja: %s", cid)


async def _rearm_autopost(app):
    """Restart ke baad har chat ka auto-post dobara set karo."""
    jq = getattr(app, "job_queue", None)
    if jq is None:
        return
    from datetime import time as _t
    for cid, conf in db_autopost_all().items():
        try:
            h, m = (int(x) for x in str(conf.get("at") or "").split(":"))
            target = cid
            try:
                target = int(cid)
            except (TypeError, ValueError):
                pass
            jq.run_daily(_job_autopost, time=_t(h, m, tzinfo=_IST),
                         data=target, name=f"autopost:{cid}")
            log.info("Auto-post re-armed: %s at %02d:%02d IST", cid, h, m)
        except Exception as e:
            log.warning("Auto-post re-arm fail (%s): %s", cid, e)


async def cmd_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: is chat me roz ek message — bot khud bhejega, bina command ke.

    /autopost 09:30 good morning ☁
    /autopost off
    """
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    msg = update.effective_message
    if not AUTOPOST_ENABLED:
        await msg.reply_text(f"{bd('AUTO-POST OFF')} — {sc('AUTOPOST_ENABLED=False')}")
        return
    chat = update.effective_chat
    if str(chat.type) not in ("group", "supergroup", "channel"):
        await msg.reply_text(f"{sc('ye command kisi group/channel ke andar chalao')}.")
        return
    args = context.args or []
    conf = db_autopost_all().get(str(chat.id))
    if not args:
        if conf:
            await msg.reply_text(
                f"{bd('AUTO-POST')}\n{sc('time')} : {conf.get('at')} IST\n"
                f"{sc('text')} : {conf.get('text')}\n\n"
                f"{sc('band karne ke liye')}: /autopost off")
        else:
            await msg.reply_text(
                f"{bd('AUTO-POST')} — {sc('not set yet')}\n\n"
                f"/autopost 09:30 good morning ☁\n"
                f"/autopost off")
        return
    if str(args[0]).lower() in ("off", "stop", "band"):
        ok = db_autopost_del(chat.id)
        jq = getattr(context.application, "job_queue", None)
        if jq is not None:
            for j in jq.get_jobs_by_name(f"autopost:{chat.id}"):
                j.schedule_removal()
        await msg.reply_text(f"{sc('auto-post band')}" if ok
                             else f"{sc('already off')}")
        return
    _m = re.match(r"^(\d{1,2}):(\d{2})$", str(args[0]))
    if not _m:
        await msg.reply_text(
            f"{sc('format')}: /autopost 09:30 <message>\n"
            f"{sc('ya')}: /autopost off")
        return
    h, mi = int(_m.group(1)), int(_m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        await msg.reply_text(f"{sc('invalid time')} - 00:00 to 23:59")
        return
    text = " ".join(args[1:]).strip()
    if not text:
        await msg.reply_text(f"{sc('message bhi likho')}: /autopost 09:30 good morning")
        return
    db_autopost_set(chat.id, f"{h:02d}:{mi:02d}", text)
    jq = getattr(context.application, "job_queue", None)
    if jq is not None:
        from datetime import time as _t
        for j in jq.get_jobs_by_name(f"autopost:{chat.id}"):
            j.schedule_removal()
        jq.run_daily(_job_autopost, time=_t(h, mi, tzinfo=_IST),
                     data=chat.id, name=f"autopost:{chat.id}")
        await msg.reply_text(
            f"{bd('AUTO-POST SET')}\n{sc('roz')} {h:02d}:{mi:02d} IST\n"
            f"{sc('text')} : {text[:120]}")
    else:
        await msg.reply_text(
            f"{sc('saved, but no job queue')} - "
            f"{sc('it will start after a restart')}.")


async def cmd_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: strict force-join gate — runtime se, bina redeploy.

    /gate                       -> status
    /gate on | off
    /gate add @username Naam    -> ek channel gate me jodo
    /gate remove Naam
    Jab tak user in saare channels ko join nahi karta, post ka link NAHI
    milega (MIN_JOINS_REQUIRED="all").
    """
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    msg = update.effective_message
    args = context.args or []
    op = (args[0].lower() if args else "list")

    if op in ("list", "status", "show", ""):
        await _ensure_gate_links(context)
        blocks = _gate_blocks()
        out = [bd("GATE"), "",
               f"{sc('status')} : {bd('ON') if blocks else bd('OFF')}",
               f"{sc('channels')} : {bd(str(len(blocks)))}",
               f"{sc('need')} : {bd(str(required_count()))} "
               f"({sc('MIN_JOINS_REQUIRED')})", ""]
        for b in blocks:
            cid = _block_chat_id(b)
            out.append(f"• {b['name']} — {sc('id')} {cid if cid else bd('NO ID')}")
        if not blocks:
            out += [f"{sc('the gate is off - no channel has to be joined')}", "",
                    f"{sc('add')}: /gate add @channel Naam"]
        else:
            out += ["", f"{sc('add')}: /gate add @channel Naam"]
        await msg.reply_text("\n".join(out),
                             reply_markup=_onoff_kb("gat", bool(blocks)))
        return

    if op in ("on", "off"):
        db_gate_on(op == "on")
        await msg.reply_text(f"{sc('gate')}: {bd(op.upper())}")
        return

    if op in ("add", "set"):
        if len(args) < 2:
            await msg.reply_text(f"{sc('use')}: /gate add @channel Naam")
            return
        target = args[1].strip()
        name = " ".join(args[2:]).strip() or target.lstrip("@")
        if not target.startswith("@") and not target.lstrip("-").isdigit():
            await msg.reply_text(f"{sc('@username ya numeric id do')}")
            return
        cid = None
        try:
            ch = await context.bot.get_chat(target)
            cid = ch.id
            name = name or (ch.title or target)
        except Exception as e:
            await msg.reply_text(
                f"{bd('ID NOT FOUND')} - {_err_hint(e)}\n\n"
                f"{sc('make the bot an admin in that channel, then run')} /gate add")
            return
        url = f"https://t.me/{target.lstrip('@')}" if target.startswith("@") else None
        if not url:
            try:
                link = await context.bot.create_chat_invite_link(cid, name="gate")
                url = link.invite_link
            except Exception:
                url = None
        if not url:
            await msg.reply_text(f"{sc('could not create a link for it - give me an @username')}")
            return
        db_gate_add(name, url, cid)
        await msg.reply_text(
            f"{sc('added to the gate')}: {bd(name)}\n{sc('id')} {cid}\n"
            f"{sc('joining it is now required')}")
        return

    if op in ("remove", "del", "rm"):
        if len(args) < 2:
            await msg.reply_text(f"{sc('use')}: /gate remove <naam>")
            return
        name = " ".join(args[1:]).strip()
        ok = db_gate_del(name)
        await msg.reply_text(f"{sc('hata diya')}: {name}" if ok
                             else f"{sc('that name is not in the gate')}")
        return

    await msg.reply_text(f"{sc('use')}: /gate on|off|add|remove|list")


# =====================================================================
#  PRIVACY  (PRIV-01)  —  /privacy, /delete_my_data, /audit
# =====================================================================
def _forget_memory(uid) -> int:
    """AI ki conversation memory se is user ke SAARE keys hatao.

    Keys do tarah ki hoti hain: "u<id>" (DM) aur "g<chat>:u<id>" (group).
    Sirf "u<id>" hatana aadha kaam hota — group wali memory reh jaati."""
    suffix = f":u{uid}"
    keys = [k for k in list(_history.keys())
            if k == f"u{uid}" or k.endswith(suffix)]
    for k in keys:
        _history.pop(k, None)
    return len(keys)


async def _do_delete_user_data(uid) -> dict:
    """Ek user ka data SAARE stores se hatao (DB + in-memory caches).

    Audit AV-005: pehle deletion ke baad user ko turant "delete ho gaya" bol
    diya jata tha, jabki Mongo me likhna 5 second baad hota tha. Us window me
    crash hua to record wapas aa jata — matlab jhooth. Ab:
      1. memory se delete,
      2. TURANT durable commit (Mongo, warna local disk),
      3. verify ki record sach me gaya,
      4. tabhi user ko batate hain.
    """
    res = delete_user_data(uid)          # storage.py: users/referrals/bans/...
    res["memory"] = _forget_memory(uid)
    _ai_hits.pop(uid, None)
    for key in [k for k in list(_member_cache.keys()) if k[0] == int(uid)]:
        _member_cache.pop(key, None)
    _post_flow.pop(uid, None)
    _pending_post.pop(uid, None)
    _gate_msg.pop(uid, None)
    _check_cd.pop(uid, None)
    # Is user ki har chat ki "last text" entry bhi hatao (DM aur group dono).
    for _k in [k for k in list(_last_text.keys()) if k[1] == int(uid)]:
        _last_text.pop(_k, None)

    ok, where = await commit_now()
    res["committed"] = where if ok else "failed"
    res["verified"] = not user_present(uid)
    return res


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/privacy — kaun sa data rakha hai, kahan jaata hai, kaise delete ho."""
    text = PRIVACY_TEXT or DEFAULT_PRIVACY_TEXT
    community = next((b["url"] for b in LINK_BLOCKS
                      if "community" in str(b.get("name", "")).lower()), "")
    text = (text.replace("{cache_days}", str(CACHE_RETENTION_DAYS))
                .replace("{community}", community)
                .replace("{privacy_url}", PRIVACY_URL or "")
                .replace("{bot}", BOT_USERNAME))
    if PRIVACY_URL:
        text += f"\n\nFull policy: {PRIVACY_URL}"
    # Command ka naam yahan NAHI likhte (user ne kaha: kahin advertise na ho).
    # Poora policy + deletion ka tareeka PRIVACY.md (GitHub) par hai.
    if not PRIVACY_URL:
        log.info("PRIVACY_URL set nahi hai — policy link ke bina /privacy chal raha hai")
    await _send_long(update.effective_message, text)


async def cmd_delete_my_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delete_my_data — pehle confirm, phir delete (galti se na ho)."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "For safety this only works in a private chat with me: @"
            f"{context.bot.username}")
        return
    kb = InlineKeyboardMarkup([[
        _btn("Delete my data", callback_data="deldata:yes", style="danger"),
        _btn("Cancel", callback_data="deldata:no", style="primary"),
    ]])
    await update.effective_message.reply_text(
        "This deletes:\n"
        "• your account record (id, name, username, dates)\n"
        "• your invite count, and your id from other people's invite lists\n"
        "• my memory of your conversations\n\n"
        "It cannot be undone. Delete?",
        reply_markup=kb)


def _user_record(uid) -> dict:
    """Ek user ka record. db_all_users() LIST deta hai — usme se id se dhoondo."""
    try:
        want = int(uid)
    except (TypeError, ValueError):
        return {}
    for u in (db_all_users() or []):
        try:
            if int(u.get("id")) == want:
                return u
        except (TypeError, ValueError):
            continue
    return {}


def _target_user(update, context):
    """Reply ya pehle argument se (uid, name) nikalo."""
    msg = update.effective_message
    r = getattr(msg, "reply_to_message", None)
    if r is not None and getattr(r, "from_user", None) is not None:
        u = r.from_user
        return int(u.id), (u.full_name or str(u.id))
    args = context.args or []
    if args:
        try:
            return int(args[0]), str(args[0])
        except ValueError:
            return 0, ""
    return 0, ""


def _group_only(update) -> bool:
    return str(getattr(update.effective_chat, "type", "")) in ("group", "supergroup")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: member ko bolne se roko (default 60 min). Reply ya id se."""
    if not await _need_owner(update):
        return
    bd = fonts.bold
    msg = update.effective_message
    if not _group_only(update):
        await msg.reply_text("This works only inside a group.")
        return
    uid, name = _target_user(update, context)
    if not uid:
        await msg.reply_text("Reply to a message, or give a user id: /mute <id> [minutes]")
        return
    mins = 60
    _a = context.args or []
    if len(_a) > 1:
        try:
            mins = max(1, min(int(_a[1]), 10080))     # max 7 din
        except ValueError:
            pass
    until = datetime.now(timezone.utc) + timedelta(minutes=mins)
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=uid,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until)
        await msg.reply_text(f"{bd('muted')} : {name} — {mins} min")
    except Exception as e:
        await msg.reply_text(f"{bd('failed')} — {_err_hint(e)}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: mute hatao."""
    if not await _need_owner(update):
        return
    bd = fonts.bold
    msg = update.effective_message
    if not _group_only(update):
        await msg.reply_text("This works only inside a group.")
        return
    uid, name = _target_user(update, context)
    if not uid:
        await msg.reply_text("Reply to a message, or give a user id: /unmute <id>")
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=uid,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        await msg.reply_text(f"{bd('unmuted')} : {name}")
    except Exception as e:
        await msg.reply_text(f"{bd('failed')} — {_err_hint(e)}")


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: warning do. 3 par khud kick."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    msg = update.effective_message
    if not _group_only(update):
        await msg.reply_text("This works only inside a group.")
        return
    uid, name = _target_user(update, context)
    if not uid:
        await msg.reply_text("Reply to a message, or give a user id: /warn <id>")
        return
    n = db_warn_add(update.effective_chat.id, uid)
    if n >= 3:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, uid)
            await context.bot.unban_chat_member(update.effective_chat.id, uid)
        except Exception:
            pass
        db_warn_clear(update.effective_chat.id, uid)
        await msg.reply_text(f"{bd('3/3')} — {name} {sc('was removed')}")
        return
    await msg.reply_text(f"{bd(f'{n}/3')} — {name} {sc('warned')}")


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: warnings saaf karo."""
    if not await _need_owner(update):
        return
    bd = fonts.bold
    msg = update.effective_message
    uid, name = _target_user(update, context)
    if not uid:
        await msg.reply_text("Reply to a message, or give a user id: /unwarn <id>")
        return
    db_warn_clear(update.effective_chat.id, uid)
    await msg.reply_text(f"{bd('warnings cleared')} : {name}")


async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: is group me kis-kis ke kitne warnings hain."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    w = db_warns_all(update.effective_chat.id)
    if not w:
        await update.effective_message.reply_text(sc("no warnings in this group"))
        return
    out = [bd("WARNINGS"), ""]
    for uid, n in sorted(w.items(), key=lambda kv: -int(kv[1])):
        out.append(f"• {uid} — {n}/3")
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: reply wale message se le kar ab tak sab delete (max 50)."""
    if not await _need_owner(update):
        return
    msg = update.effective_message
    if not _group_only(update):
        await msg.reply_text("This works only inside a group.")
        return
    r = getattr(msg, "reply_to_message", None)
    if r is None:
        await msg.reply_text("Reply to the first message you want deleted.")
        return
    start, end = r.message_id, msg.message_id
    if end - start > 50:
        start = end - 50
    gone = 0
    for mid in range(start, end + 1):
        try:
            await context.bot.delete_message(msg.chat_id, mid)
            gone += 1
        except Exception:
            pass
    try:
        await msg.delete()
    except Exception:
        pass
    log.info("Purge: %s messages in %s", gone, msg.chat_id)


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: group freeze — sirf admins bol sakte hain."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    if not _group_only(update):
        await update.effective_message.reply_text("This works only inside a group.")
        return
    if not (context.args or []):
        _on = db_lock_get(update.effective_chat.id)
        await update.effective_message.reply_text(
            f"{sc('lock')} : {bd('ON' if _on else 'OFF')}",
            reply_markup=_onoff_kb("lck", _on))
        return
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(can_send_messages=False))
        db_lock_set(update.effective_chat.id, True)
        await update.effective_message.reply_text(f"{bd('locked')} — admins only")
    except Exception as e:
        await update.effective_message.reply_text(f"{bd('failed')} — {_err_hint(e)}")


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: group wapas khol do."""
    if not await _need_owner(update):
        return
    bd = fonts.bold
    if not _group_only(update):
        await update.effective_message.reply_text("This works only inside a group.")
        return
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        db_lock_set(update.effective_chat.id, False)
        await update.effective_message.reply_text(f"{bd('unlocked')}")
    except Exception as e:
        await update.effective_message.reply_text(f"{bd('failed')} — {_err_hint(e)}")


def _onoff_kb(prefix, on_now=None) -> InlineKeyboardMarkup:
    """[ON] [OFF] buttons — 'on' type karne ki zaroorat nahi."""
    return InlineKeyboardMarkup([[
        _btn("ON", callback_data=f"{prefix}:on",
             style="success" if on_now else "primary"),
        _btn("OFF", callback_data=f"{prefix}:off",
             style="primary" if on_now else "danger"),
    ]])


async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: poora AI band / chalu — sirf owner commands chalte rahenge."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    args = context.args or []
    if not args:
        _on = db_maintenance_get()
        await update.effective_message.reply_text(
            f"{sc('maintenance')} : {bd('ON' if _on else 'OFF')}",
            reply_markup=_onoff_kb("mtn", _on))
        return
    on = str(args[0]).lower() in ("on", "1", "yes", "true")
    db_maintenance(on)
    await update.effective_message.reply_text(
        f"{sc('maintenance')} : {bd('ON' if on else 'OFF')}")


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: ek user ka poora card — ban, warns, referrals, source."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    uid, name = _target_user(update, context)
    if not uid:
        await update.effective_message.reply_text(
            "Reply to a message, or give an id: /user <id>")
        return
    u = _user_record(uid)
    s_ = db_stats()
    out = [bd("USER"), "",
           f"{sc('id')}      : {uid}",
           f"{sc('name')}    : {u.get('first_name') or name}",
           f"{sc('username')} : @{u.get('username')}" if u.get("username") else f"{sc('username')} : -",
           f"{sc('joined')}  : {u.get('joined') or u.get('first_seen') or '-'}",
           f"{sc('last')}    : {u.get('last_seen') or '-'}",
           f"{sc('source')}  : {u.get('src') or 'direct'}",
           f"{sc('invites')} : {u.get('ref_count') or 0}",
           f"{sc('banned')}  : {bd('yes') if db_is_banned(uid) else 'no'}",
           f"{sc('warns')}   : {db_warn_get(update.effective_chat.id, uid) if _group_only(update) else '-'}",
           f"{sc('total users')} : {s_['total_users']}"]
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: top referrers."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    # db_ref_rank(uid) ek user ki rank deta hai — yahan poora leaderboard chahiye,
    # isliye referrals se seedha ginte hain.
    rows = sorted(((int(k), int((v or {}).get("count") or 0))
                   for k, v in (db_ref_all() or {}).items()),
                  key=lambda kv: -kv[1])[:10]
    rows = [(u, n) for u, n in rows if n > 0]
    if not rows:
        await update.effective_message.reply_text(sc("no referrals yet"))
        return
    out = [bd("TOP REFERRERS"), ""]
    for i, (uid, n) in enumerate(rows, 1):
        nm = _user_record(uid).get("first_name") or str(uid)
        out.append(f"{i}. {nm} — {bd(str(n))}")
    await _send_long(update.effective_message, "\n".join(out))


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: poora database file ke roop me apne DM me."""
    if not await _need_owner(update):
        return
    sc = fonts.small_caps
    path = getattr(storage, "DB_PATH", None)
    try:
        if not path or not os.path.exists(path):
            await update.effective_message.reply_text(sc("no local database file yet"))
            return
        with open(path, "rb") as f:
            await context.bot.send_document(
                update.effective_chat.id, document=f,
                filename=f"mahiru-backup-{datetime.now(_IST).strftime('%Y%m%d-%H%M')}.json",
                caption=sc("full database backup"))
    except Exception as e:
        await update.effective_message.reply_text(f"failed — {_err_hint(e)}")


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: reply wala message unpin."""
    if not await _need_owner(update):
        return
    bd = fonts.bold
    msg = update.effective_message
    r = getattr(msg, "reply_to_message", None)
    if r is None:
        await msg.reply_text("Reply to the pinned message.")
        return
    try:
        await context.bot.unpin_chat_message(msg.chat_id, r.message_id)
        await msg.reply_text(f"{bd('unpinned')}")
    except Exception as e:
        await msg.reply_text(f"{bd('failed')} — {_err_hint(e)}")


async def cmd_antispam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OWNER: is group me link/forward spam khud delete karo."""
    if not await _need_owner(update):
        return
    sc, bd = fonts.small_caps, fonts.bold
    if not _group_only(update):
        await update.effective_message.reply_text("This works only inside a group.")
        return
    args = context.args or []
    if not args:
        _on = db_antispam_get(update.effective_chat.id)
        await update.effective_message.reply_text(
            f"{sc('antispam')} : {bd('ON' if _on else 'OFF')}",
            reply_markup=_onoff_kb("asp", _on))
        return
    on = str(args[0]).lower() in ("on", "1", "yes", "true")
    db_antispam_set(update.effective_chat.id, on)
    await update.effective_message.reply_text(
        f"{sc('antispam')} : {bd('ON' if on else 'OFF')}")


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner: haal ke admin actions — kaun, kya, kisko, kahan."""
    if not await _need_owner(update):
        return
    entries = audit_tail(25)
    sc, bd = fonts.small_caps, fonts.bold
    if not entries:
        await update.effective_message.reply_text(
            "No admin actions recorded yet.")
        return
    out = [bd("ADMIN ACTION LOG"), ""]
    for a in reversed(entries):
        out.append(f"{a.get('ts', '-')}  {bd(str(a.get('action', '?')))}")
        out.append(f"   {sc('by')} {a.get('actor', '-')}   {sc('on')} {a.get('target') or '-'}")
        if a.get("chat"):
            out.append(f"   {sc('chat')} {a['chat']}")
        if a.get("detail"):
            out.append(f"   {sc('note')} {a['detail']}")
    out.append("")
    out.append(f"{sc('kept')}: last {AUDIT_KEEP} actions ({sc('AUDIT_KEEP')})")
    await _send_long(update.effective_message, "\n".join(out))


async def _job_growth(context: ContextTypes.DEFAULT_TYPE):
    """Roz: har chat ka member count snapshot (growth dikhane ke liye)."""
    if not GROWTH_SNAPSHOT:
        return
    for blk in LINK_BLOCKS:
        cid = blk.get("id") or _learned_ids().get(blk["name"])
        if not cid:
            continue
        try:
            n = await context.bot.get_chat_member_count(cid)
            db_save_member_count(cid, n, GROWTH_KEEP_DAYS)
        except Exception as e:
            log.info("Growth snapshot skip (%s): %s", blk["name"], e)


async def _job_purge(context: ContextTypes.DEFAULT_TYPE):
    """Retention: purane caches saaf karo (roz ek baar)."""
    try:
        res = purge_stale(CACHE_RETENTION_DAYS, USER_RETENTION_DAYS)
        if any(res.values()):
            log.info("Retention purge: %s", res)
    except Exception as e:
        log.warning("Retention purge fail: %s", e)


def _schedule_maintenance(app: Application):
    """Daily retention job — job_queue ho to hi (extra missing ho sakta hai)."""
    jq = getattr(app, "job_queue", None)
    if jq is None:
        return
    try:
        jq.run_repeating(_job_purge, interval=86400, first=120, name="purge")
        jq.run_repeating(_job_growth, interval=86400, first=300, name="growth")
    except Exception as e:
        log.warning("Retention job set nahi hui: %s", e)


# =====================================================================
#  BUILD APPLICATION
# =====================================================================
def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())  # flood-limit se bachne ke liye
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("owner", cmd_owner))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ref", cmd_ref))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("getid", cmd_getid))
    app.add_handler(CommandHandler("setid", cmd_setid))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("mongo", cmd_mongo))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("audience", cmd_audience))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("members", cmd_members))
    app.add_handler(CommandHandler("autopost", cmd_autopost))
    app.add_handler(CommandHandler("admin", cmd_admin_panel))
    app.add_handler(CommandHandler("claim", cmd_claim))
    app.add_handler(CommandHandler("gate", cmd_gate))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("maintenance", cmd_maintenance))
    app.add_handler(CommandHandler("user", cmd_user))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CommandHandler("warns", cmd_warns))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("antispam", cmd_antispam))
    app.add_handler(CommandHandler("unpin", cmd_unpin))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("perms", cmd_perms))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("settitle", cmd_settitle))
    app.add_handler(CommandHandler("invitelink", cmd_invitelink))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("unkick", cmd_unkick))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler(POST_COMMAND, cmd_post))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("posts", cmd_posts))
    app.add_handler(CommandHandler("btnstyle", cmd_btnstyle))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("delete_my_data", cmd_delete_my_data))
    app.add_handler(CommandHandler("audit", cmd_audit))

    app.add_handler(CallbackQueryHandler(on_callback))
    # INLINE MODE — kisi bhi chat me bina add hue
    app.add_handler(InlineQueryHandler(on_inline_query))
    # Reaction -> kaun kaun hai (channel/group ka asli audience signal)
    app.add_handler(MessageReactionHandler(on_reaction))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # Kisi bhi member ka join/leave — leave par request record hata do.
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_chat_seen))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member))
    # Group -> supergroup migration (chat id badal jaati hai)
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    # GUEST MODE — group jahan bot member nahi hai, wahan @mention par jawab.
    # Group 2 me (sabse aakhir) — baaki handlers ko disturb na kare.
    app.add_handler(TypeHandler(Update, on_guest_message), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, on_group_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_private_message))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_private_photo))
    # Photo ke alawa sticker / voice / file / video — in par bhi ek saaf jawab,
    # warna user ko lagta tha bot ne suna hi nahi.
    app.add_handler(MessageHandler(
        # PTB me STICKER/DOCUMENT ke uppercase alias hote hi nahi — sirf
        # filters.Sticker.ALL / filters.Document.ALL. (Yahi wajah thi ki deploy
        # build_application() par AttributeError se gir gaya tha.)
        filters.ChatType.PRIVATE & (filters.VOICE | filters.Sticker.ALL
                                    | filters.Document.ALL | filters.VIDEO
                                    | filters.AUDIO | filters.VIDEO_NOTE),
        on_private_media))

    # Group 1: jo bhi group update group 0 me handle na ho (photo, sticker,
    # command, koi bhi cheez) — sirf chat yaad rakhne ke liye. Isse bot ko
    # us group ka pata chal jata hai, chahe koi bhi tarah ka message aaye.
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, _learn_only), group=1)

    app.add_error_handler(on_error)
    app.post_shutdown = _on_shutdown
    return app


# =====================================================================
#  RUN  —  WEBHOOK (Render)  ya  POLLING (local)
# =====================================================================
def run_webhook(app: Application):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import JSONResponse
    import uvicorn

    # RANDOM per-boot secret. BOT_TOKEN se derive NAHI karte — warna token
    # leak hone par webhook bhi forge ho sakta hai. Har boot par set_webhook
    # dobara hota hai, isliye random secret theek chalta hai.
    secret = WEBHOOK_SECRET or secrets.token_urlsafe(32)
    webhook_path = f"/tg/{secret}"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await app.initialize()
        await _start_mongo(app)
        await verify_blocks_on_startup(app)
        await _replay_pending_requests(app)
        await _rearm_posts(app)
        await _rearm_autopost(app)
        _schedule_maintenance(app)
        await app.bot.set_webhook(
            url=f"{WEBHOOK_URL}{webhook_path}",
            allowed_updates=Update.ALL_TYPES,
            # Pehle ye HAMESHA True tha — restart ke waqt Telegram ke pending
            # updates (messages, join requests) chup-chaap delete ho jaate the
            # (audit TG-02). Ab config se, aur default False = kuch na phenko.
            drop_pending_updates=DROP_PENDING_UPDATES,
            secret_token=secret,
        )
        await app.start()
        log.info("Webhook set: %s/tg/*** (path chhupa diya)", WEBHOOK_URL)
        yield
        await app.stop()
        await app.shutdown()

    api = FastAPI(lifespan=lifespan)

    # ---- Update delivery (TG-01) ------------------------------------
    # Pehle route seedha `await app.process_update(update)` karta tha, aur
    # FastAPI ek hi waqt kai requests serve karta hai. Matlab ek hi chat ke
    # do updates saath-saath chal sakte the — aur wo dono wahi shared state
    # badalte hain (referral count, gate, /post flow). Isliye ab:
    #   • ek hi chat ke updates ek line me (per-chat lock)
    #   • alag-alag chats saath chal sakte hain (bounded semaphore)
    #   • ek hi update_id dobara aaye to kaam dobara nahi hota (dedupe)
    _chat_locks: dict = {}
    _sem = asyncio.Semaphore(max(1, WEBHOOK_CONCURRENCY))

    def _lock_for(key):
        lock = _chat_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _chat_locks[key] = lock
            if len(_chat_locks) > 2000:      # bound — memory leak nahi
                for k in list(_chat_locks.keys())[: len(_chat_locks) - 2000]:
                    if not _chat_locks[k].locked():
                        _chat_locks.pop(k, None)
        return lock

    def _update_key(update):
        """Serialization ki kunji: chat id. Chat na ho to update_id,
        taaki do bilkul alag updates ek doosre ko roken na."""
        chat = getattr(update, "effective_chat", None)
        return f"c{chat.id}" if chat is not None else f"u{update.update_id}"

    @api.post(webhook_path)
    async def telegram_webhook(request: Request):
        # Telegram har request me ye header bhejta hai — match na ho to reject.
        # hmac.compare_digest = timing-safe (simple == timing attack ke liye
        # thoda exposed hota hai; secret random hai par phir bhi theek karte hain).
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(got, secret):
            return Response(status_code=403)
        try:
            payload = await request.json()
        except Exception:
            return Response(status_code=400)
        update = Update.de_json(payload, app.bot)

        # Telegram wahi update DOBARA bhejta hai jab jawab late ho ya fail ho.
        # Bina iske ek hi referral do baar count ho sakta tha.
        if update_seen(update.update_id):
            log.info("Duplicate update %s — skip.", update.update_id)
            return Response(status_code=200)

        failed = False
        async with _sem:
            async with _lock_for(_update_key(update)):
                # Dedupe ko lock ke ANDAR dobara check karo (audit F-03).
                # Upar wala check race hai: do overlapping deliveries
                # (Telegram ka retry jab pehla response late ho) dono
                # "not seen" dekh kar dono process_update chala deti thi —
                # matlab /broadcast do baar, duplicate post.
                if update_seen(update.update_id):
                    log.info("Duplicate update %s (in-lock) — skip.",
                             update.update_id)
                    return Response(status_code=200)
                try:
                    await app.process_update(update)
                except Exception:
                    failed = True
                    log.exception("Update %s process fail", update.update_id)

        if not failed:
            # Sirf SAFAL hone par mark — fail hua to Telegram ka retry
            # dobara process hoga (at-least-once).
            mark_update_seen(update.update_id)
            _update_fails.pop(update.update_id, None)
            return Response(status_code=200)

        # Audit AV-006: pehle yahan hamesha 200 jaata tha, matlab ek exception
        # wala update Telegram ke liye "successfully delivered" ban jata aur
        # retry kabhi nahi hota — update chup-chaap kho jata.
        # Ab pehli baar 500 -> Telegram dobara bhejega (idempotency upar hai).
        n = _update_fails.get(update.update_id, 0) + 1
        _update_fails[update.update_id] = n
        _prune(_update_fails, 2000)
        if n < max(1, UPDATE_MAX_ATTEMPTS):
            log.warning("Update %s fail (%s/%s) — Telegram se retry mangwa raha hoon.",
                        update.update_id, n, UPDATE_MAX_ATTEMPTS)
            return Response(status_code=500)
        # Baar-baar fail = permanently kharab update. Ab chhod do, warna
        # Telegram hamesha retry karta rahega (infinite loop).
        log.error("Update %s %s koshishon ke baad bhi fail — dead letter.",
                  update.update_id, n)
        mark_update_seen(update.update_id)
        try:
            audit_add(0, "update_failed", None, None, f"update {update.update_id}")
        except Exception:
            pass
        return Response(status_code=200)

    _START = time.time()

    @api.api_route("/", methods=["GET", "HEAD"])
    @api.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        """Render ka health check. Hamesha 200 — warna degraded mode me
        Render service ko restart karta rehta hai (aur wo theek nahi hai:
        degraded bot kaam kar raha hai, bas local disk par)."""
        h = storage_health()
        return JSONResponse({
            "status": "ok" if h["durable"] else "degraded",
            "bot": "running",
            "uptime_s": int(time.time() - _START),
            "storage": h,
            "ai_today": int(ai_usage().get("total", 0)),
        })

    @api.get("/ready")
    async def ready():
        """Strict check: 503 jab data kisi DURABLE store par nahi hai.
        Isko apne uptime monitor me lagao, /health ko nahi.

        Audit AV-003: pehle local file par likh dena hi "durable" maan liya
        jata tha. Render Free ka disk ephemeral hai — wahan 200 dena jhoot
        tha. Ab EPHEMERAL par 503, aur reason bhi saaf likha jata hai."""
        h = storage_health()
        ok = bool(h["durable"])
        reason = {
            "MONGO": "data is in MongoDB",
            "PERSISTENT_DISK": "data is on a persistent local disk",
            "EPHEMERAL": "only an ephemeral local file — lost on redeploy; "
                         "set LOCAL_DISK_PERSISTENT=True only if the disk really persists",
            "VOLATILE": "nothing is on disk — data lives only in memory",
        }.get(h.get("durability"), "unknown")
        return JSONResponse({"ready": ok, "reason": reason, "storage": h},
                            status_code=200 if ok else 503)

    # access_log=False -> webhook ka secret path logs me print nahi hota.
    uvicorn.run(api, host="0.0.0.0", port=PORT, log_level="info", access_log=False)


_mongo_task = None


async def _start_mongo(app: Application):
    """State load karo + background loop chalao (reconnect + flush + health).

    Audit DB-01 ka asli fix: pehle yahan ek hi koshish hoti thi aur fail hone
    par reconnection kabhi nahi hota tha. Ab storage_loop() khud backoff ke
    saath dobara connect karta hai, isliye process restart ki zaroorat nahi."""
    global _mongo_task
    # storage ko owner ko alert bhejne ka raasta do (storage me telegram nahi).
    set_alert_hook(lambda text: _notify_owner(app.bot, text))
    ok = await storage_connect()
    if MONGO_ENABLED and not ok:
        await _notify_owner(
            app.bot,
            "⚠️ MongoDB connect nahi hua — bot DEGRADED mode me hai.\n"
            "Har change local file par (fsync ke saath) likha ja raha hai, aur "
            "bot khud backoff ke saath dobara connect karta rahega — restart "
            "ki zaroorat nahi.\n\n"
            f"{_mongo_error_hint(storage_health().get('last_error') or '')}\n\n"
            "Live test: /mongo")
    if MONGO_ENABLED and _mongo_task is None:
        _mongo_task = asyncio.create_task(storage_loop())
        log.info("Storage loop chalu (flush har %ss, reconnect backoff max %ss).",
                 STORAGE_FLUSH_EVERY, 60)


async def _on_shutdown(app: Application):
    """Bot band ho raha hai — aakhri flush, phir loop band."""
    global _mongo_task
    if _mongo_task is not None:
        _mongo_task.cancel()
        _mongo_task = None
    await storage_close()
    try:
        await roster.close()      # MTProto client (agar chalu tha)
    except Exception:
        pass


async def _polling_post_init(app: Application):
    await _start_mongo(app)
    await verify_blocks_on_startup(app)
    await _replay_pending_requests(app)
    await _rearm_posts(app)
    await _rearm_autopost(app)
    _schedule_maintenance(app)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("BOT_TOKEN is not set. Check config.py / your Environment Variables.")
    if not AI_API_KEY or AI_API_KEY == "PASTE_YOUR_AI_API_KEY_HERE":
        log.warning("AI_API_KEY is not set — the bot will start, but every AI reply "
                    "will be the 'busy' message. Set AI_API_KEY in your environment.")
    app = build_application()
    if WEBHOOK_URL:
        log.info("Starting in WEBHOOK mode (Render).")
        run_webhook(app)
    else:
        log.info("Starting in POLLING mode (local).")
        app.post_init = _polling_post_init
        app.run_polling(allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=DROP_PENDING_UPDATES)


if __name__ == "__main__":
    main()
