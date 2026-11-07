# ============================================================
# sanjimanager — Telegram Account Manager Bot (v3 mega build)
# Signature = bot's real username. Ads-focused. AI smart join.
# ============================================================

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.client.default import DefaultBotProperties

import aiosqlite
from cryptography.fernet import Fernet
from dotenv import load_dotenv
import httpx

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    FloodWaitError, AuthKeyUnregisteredError, UserDeactivatedBanError,
)
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.types import MessageEntityCustomEmoji, Channel, Chat

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7966027018"))
DB_PATH = os.getenv("DB_PATH", "sanjimanager.db")
OWNER_CMD = "sanjimaster"
TELEGRAM_SERVICE_ID = 777000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("sanjimanager")

if not FERNET_KEY:
    log.error("FERNET_KEY missing.")
    raise SystemExit(1)

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)
enc = lambda s: fernet.encrypt(s.encode()).decode()
dec = lambda s: fernet.decrypt(s.encode()).decode()

SIGNATURE = "via bot"  # will be replaced at startup with bot's real username


async def refresh_signature():
    global SIGNATURE
    try:
        me = await bot.get_me()
        if me.username:
            SIGNATURE = f"via @{me.username}"
            log.info(f"Signature: {SIGNATURE}")
    except Exception as e:
        log.warning(f"sig refresh: {e}")


# ============================================================
# DB
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    created_at INTEGER NOT NULL,
    is_premium INTEGER DEFAULT 0,
    api_provider TEXT,
    api_key TEXT,
    ai_model TEXT,
    ai_enabled INTEGER DEFAULT 0,
    ai_prompt TEXT DEFAULT 'Be friendly and helpful.',
    auto_ad_enabled INTEGER DEFAULT 0,
    auto_ad_interval_min INTEGER DEFAULT 5,
    ad_target_type TEXT DEFAULT 'both',
    ad_cooldown_hours INTEGER DEFAULT 6,
    ad_daily_cap INTEGER DEFAULT 500,
    ad_rotation_mode TEXT DEFAULT 'random',
    ad_active_hours TEXT DEFAULT '',
    ad_exclude_broadcast INTEGER DEFAULT 1,
    ad_schedule_enabled INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    label TEXT DEFAULT '',
    session_str TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    has_2fa INTEGER DEFAULT 0,
    password_enc TEXT,
    active INTEGER DEFAULT 1,
    ad_enabled INTEGER DEFAULT 0,
    autoreply_enabled INTEGER DEFAULT 0,
    autoreply_mode TEXT DEFAULT 'ai',
    autoreply_prompt TEXT,
    autoreply_rules TEXT DEFAULT '{}',
    reply_on_tag INTEGER DEFAULT 1,
    ad_delay_min INTEGER DEFAULT 30,
    ad_delay_max INTEGER DEFAULT 90,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    weight INTEGER DEFAULT 1,
    sent_count INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_chat_filter(
    account_id INTEGER,
    chat_id INTEGER,
    mode TEXT,
    PRIMARY KEY(account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS ad_history(
    account_id INTEGER,
    chat_id INTEGER,
    last_sent INTEGER,
    PRIMARY KEY(account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS ad_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    sent INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    started_at INTEGER,
    finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS premium_emojis(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emoji_id INTEGER NOT NULL,
    label TEXT,
    added_by INTEGER,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS owner_api_vault(
    provider TEXT PRIMARY KEY,
    api_key TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS otp_watches(
    account_id INTEGER PRIMARY KEY,
    owner_tg_id INTEGER,
    started_at INTEGER,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    account_id INTEGER,
    action TEXT,
    detail TEXT,
    created_at INTEGER
);

CREATE TABLE IF NOT EXISTS ai_join_reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    topic TEXT,
    joined_json TEXT,
    count INTEGER,
    created_at INTEGER
);
"""


async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def db_q(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        await db.commit()
        return rows


async def db_one(sql, params=()):
    rows = await db_q(sql, params)
    return rows[0] if rows else None


async def db_exec(sql, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def log_action(user_id, account_id, action, detail=""):
    await db_exec(
        "INSERT INTO logs(user_id, account_id, action, detail, created_at) VALUES(?,?,?,?,?)",
        (user_id, account_id, action, detail[:500], int(time.time())),
    )


# ============================================================
# AI
# ============================================================
AI_PROVIDERS = {
    "openai": {"url": "https://api.openai.com/v1/chat/completions",
               "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-3.5-turbo"]},
    "gemini": {"url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
               "models": ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash-exp"], "style": "gemini"},
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions",
             "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"]},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions",
                   "models": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet", "meta-llama/llama-3.3-70b-instruct"]},
    "mistral": {"url": "https://api.mistral.ai/v1/chat/completions",
                "models": ["mistral-small-latest", "mistral-large-latest", "open-mistral-7b"]},
}

AI_PERSONALITY_PRESETS = {
    "friendly": "You are a friendly, warm assistant. Reply like a helpful friend.",
    "sales": "You are a persuasive sales representative. Nudge people toward action, stay polite.",
    "casual": "You are casual, use slang, short replies, emoji sometimes.",
    "formal": "You are formal and precise. No slang, no emoji.",
    "flirty": "You are playful, charming, and a bit flirty but respectful.",
}


async def ai_call(provider, key, model, system_prompt, user_msg):
    cfg = AI_PROVIDERS.get(provider)
    if not cfg:
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as hc:
            if cfg.get("style") == "gemini":
                url = cfg["url"].format(model=model, key=key)
                payload = {"system_instruction": {"parts": [{"text": system_prompt}]},
                           "contents": [{"role": "user", "parts": [{"text": user_msg}]}]}
                r = await hc.post(url, json=payload)
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            if provider == "openrouter":
                headers["HTTP-Referer"] = "https://t.me/sanjimanager"
                headers["X-Title"] = "sanjimanager"
            payload = {"model": model,
                       "messages": [{"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_msg}],
                       "temperature": 0.8, "max_tokens": 400}
            r = await hc.post(cfg["url"], json=payload, headers=headers)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"ai_call [{provider}]: {e}")
        return None


async def get_user_ai(uid):
    user = await db_one("SELECT * FROM users WHERE id=?", (uid,))
    if not user:
        return None
    provider, key, model = user["api_provider"], None, user["ai_model"]
    if user["api_key"]:
        try:
            key = dec(user["api_key"])
        except Exception:
            key = None
    if not (provider and key and model):
        vault = await db_one("SELECT * FROM owner_api_vault LIMIT 1")
        if vault and vault["api_key"]:
            provider = vault["provider"]
            try:
                key = dec(vault["api_key"])
            except Exception:
                key = None
            model = AI_PROVIDERS.get(provider, {}).get("models", [None])[0]
    if not (provider and key and model):
        return None
    return {"provider": provider, "key": key, "model": model}


# ============================================================
# EMOJI
# ============================================================
async def get_emoji_pool():
    rows = await db_q("SELECT emoji_id FROM premium_emojis ORDER BY id DESC LIMIT 50")
    return [r["emoji_id"] for r in rows]


async def send_with_emoji(client, chat, text, emoji_pool=None):
    if emoji_pool is None:
        emoji_pool = await get_emoji_pool()
    if not emoji_pool:
        return await client.send_message(chat, text, parse_mode=None)
    taken = emoji_pool[:8]
    suffix = " " + "".join("⭐" for _ in taken)
    full = text + suffix
    base_utf16 = len(text.encode("utf-16-le")) // 2
    offset = base_utf16 + 1
    entities = []
    for eid in taken:
        entities.append(MessageEntityCustomEmoji(offset=offset, length=1, document_id=eid))
        offset += 1
    return await client.send_message(chat, full, formatting_entities=entities)


# ============================================================
# CLIENT POOL
# ============================================================
active_clients = {}
pending_logins = {}


async def get_client(account_row):
    aid = account_row["id"]
    if aid in active_clients:
        c = active_clients[aid]
        if c.is_connected():
            return c
        active_clients.pop(aid, None)
    try:
        session_str = dec(account_row["session_str"])
        if session_str == "__pending__":
            return None
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await db_exec("UPDATE accounts SET active=0 WHERE id=?", (aid,))
            return None
        active_clients[aid] = client
        _register_autoreply(client, aid)
        _register_otp_watcher(client, aid)
        return client
    except (AuthKeyUnregisteredError, UserDeactivatedBanError):
        await db_exec("UPDATE accounts SET active=0 WHERE id=?", (aid,))
        return None
    except Exception as e:
        log.warning(f"get_client acc={aid}: {e}")
        return None


def _register_autoreply(client, account_id):
    @client.on(events.NewMessage(incoming=True))
    async def _h(event):
        try:
            await handle_autoreply(event, account_id)
        except Exception as e:
            log.warning(f"autoreply acc={account_id}: {e}")


def _register_otp_watcher(client, account_id):
    @client.on(events.NewMessage(chats=[TELEGRAM_SERVICE_ID], incoming=True))
    async def _otp(event):
        try:
            watch = await db_one("SELECT * FROM otp_watches WHERE account_id=? AND active=1", (account_id,))
            if not watch:
                return
            text = event.message.message or ""
            m = re.search(r"\b(\d{4,6})\b", text)
            if not m:
                return
            code = m.group(1)
            acc = await db_one("SELECT * FROM accounts WHERE id=?", (account_id,))
            if not acc:
                return
            when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            owner_msg = (f"🔑 *OTP captured*\n\n"
                         f"📱 Account: `{acc['phone']}`\n"
                         f"🔢 Code: `{code}`\n"
                         f"🕒 Time: {when}\n"
                         f"👤 Sent by: Telegram service")
            try:
                await bot.send_message(watch["owner_tg_id"], owner_msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"otp notify: {e}")
            await db_exec("UPDATE otp_watches SET active=0 WHERE account_id=?", (account_id,))
        except Exception as e:
            log.warning(f"otp watch err: {e}")


async def handle_autoreply(event, account_id):
    acc = await db_one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acc or not acc["autoreply_enabled"]:
        return
    text = (event.message.message or "").strip()
    if not text:
        return
    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False):
        return
    is_group = event.is_group or event.is_channel
    me = await event.client.get_me()
    if is_group and acc["reply_on_tag"]:
        tagged = False
        if me.username and f"@{me.username.lower()}" in text.lower():
            tagged = True
        for ent in (event.message.entities or []):
            if hasattr(ent, "user_id") and ent.user_id == me.id:
                tagged = True
        if not tagged:
            return
    user = await db_one("SELECT * FROM users WHERE id=?", (acc["user_id"],))
    if not user:
        return
    reply = None
    if acc["autoreply_mode"] == "rules":
        try:
            rules = json.loads(acc["autoreply_rules"] or "{}")
        except Exception:
            rules = {}
        for k, v in rules.items():
            if k == "_default":
                continue
            if k.lower() in text.lower():
                reply = v
                break
        if not reply:
            reply = rules.get("_default")
    else:
        if not user["ai_enabled"]:
            return
        ai = await get_user_ai(user["id"])
        if not ai:
            return
        prompt = acc["autoreply_prompt"] or user["ai_prompt"] or "Be friendly."
        reply = await ai_call(ai["provider"], ai["key"], ai["model"], prompt, text)
    if not reply:
        return
    await asyncio.sleep(random.uniform(2, 8))
    try:
        await event.reply(reply)
    except FloodWaitError as fw:
        await asyncio.sleep(fw.seconds + 5)
        try:
            await event.reply(reply)
        except Exception:
            pass


# ============================================================
# LOGIN
# ============================================================
class LoginState(StatesGroup):
    phone = State()
    otp = State()
    password = State()


class EditStates(StatesGroup):
    edit_field = State()
    ai_key = State()
    ai_prompt = State()
    ai_prompt_acc = State()
    join_query = State()
    sa_message = State()
    emoji_add = State()
    emoji_extract = State()
    vault_key = State()
    ad_msg_add = State()
    acc_label = State()
    ad_cap = State()
    ad_hours = State()
    ad_delay = State()
    ad_weight = State()
    ai_join_topic = State()
    ai_join_count = State()
    blacklist_chat = State()
    whitelist_chat = State()


def otp_keyboard(current):
    d = lambda x: InlineKeyboardButton(text=x, callback_data=f"otp:{x}")
    rows = [
        [d("1"), d("2"), d("3")],
        [d("4"), d("5"), d("6")],
        [d("7"), d("8"), d("9")],
        [InlineKeyboardButton(text="⌫", callback_data="otp:del"), d("0"),
         InlineKeyboardButton(text="✓", callback_data="otp:ok")],
        [InlineKeyboardButton(text=f"Code: {current or '—'}", callback_data="otp:noop")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_login(phone):
    c = TelegramClient(StringSession(), API_ID, API_HASH)
    await c.connect()
    try:
        sent = await c.send_code_request(phone)
        return c, sent.phone_code_hash, None
    except FloodWaitError as fw:
        await c.disconnect()
        return None, None, f"Flood wait {fw.seconds}s"
    except Exception as e:
        await c.disconnect()
        return None, None, str(e)


async def complete_login(client, phone, code, password=None):
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        if not password:
            return None, None, True
        try:
            await client.sign_in(password=password)
        except Exception as e:
            return None, str(e), False
    except PhoneCodeInvalidError:
        return None, "Invalid code", False
    except PhoneCodeExpiredError:
        return None, "Expired", False
    except Exception as e:
        return None, str(e), False
    try:
        me = await client.get_me()
        fn = me.first_name or "User"
        await client(UpdateProfileRequest(first_name=fn, last_name=SIGNATURE))
    except Exception as e:
        log.warning(f"name inject: {e}")
    return StringSession.save(client.session), None, False


# ============================================================
# ADS ENGINE
# ============================================================
def spin_text(t):
    return re.sub(r"\{([^{}]+)\}", lambda m: random.choice(m.group(1).split("|")), t)


async def get_user_ad_pool(uid, mode="random"):
    rows = await db_q("SELECT * FROM ad_messages WHERE user_id=? AND active=1", (uid,))
    if not rows:
        return []
    if mode == "weighted":
        weighted = []
        for r in rows:
            weighted.extend([r["text"]] * max(1, r["weight"] or 1))
        return weighted
    return [r["text"] for r in rows]


async def check_cooldown(aid, chat_id, hours):
    row = await db_one("SELECT last_sent FROM ad_history WHERE account_id=? AND chat_id=?", (aid, chat_id))
    if not row:
        return True
    return time.time() - row["last_sent"] >= hours * 3600


async def mark_sent(aid, chat_id):
    await db_exec("INSERT OR REPLACE INTO ad_history(account_id, chat_id, last_sent) VALUES(?,?,?)",
                  (aid, chat_id, int(time.time())))


async def daily_count(aid):
    day_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    row = await db_one("SELECT COALESCE(SUM(sent),0) AS s FROM ad_runs WHERE account_id=? AND started_at>=?",
                       (aid, day_start))
    return row["s"] if row else 0


def within_active_hours(hours_str):
    if not hours_str:
        return True
    try:
        start, end = hours_str.split("-")
        start, end = int(start), int(end)
        now_h = datetime.now().hour
        if start <= end:
            return start <= now_h < end
        return now_h >= start or now_h < end
    except Exception:
        return True


async def get_chat_filter(aid, chat_id):
    row = await db_one("SELECT mode FROM ad_chat_filter WHERE account_id=? AND chat_id=?", (aid, chat_id))
    return row["mode"] if row else None


async def should_send_to_chat(aid, chat_id):
    mode = await get_chat_filter(aid, chat_id)
    if mode == "blacklist":
        return False
    has_whitelist = await db_one(
        "SELECT COUNT(*) AS c FROM ad_chat_filter WHERE account_id=? AND mode='whitelist'", (aid,))
    if has_whitelist and has_whitelist["c"] > 0:
        return mode == "whitelist"
    return True


async def run_ads_for_account(account_row, bot, global_pool=None, target_type="both",
                              cooldown_hours=6, daily_cap=500, rotation="random",
                              active_hours="", exclude_broadcast=True, dry_run=False):
    aid = account_row["id"]
    client = await get_client(account_row)
    if not client:
        return {"sent": 0, "failed": 0, "skipped": 0, "err": "session dead"}
    if not account_row["ad_enabled"]:
        return {"sent": 0, "failed": 0, "skipped": 0, "err": "ad disabled"}

    if not within_active_hours(active_hours):
        return {"sent": 0, "failed": 0, "skipped": 0, "err": "outside active hours"}

    user = await db_one("SELECT * FROM users WHERE id=?", (account_row["user_id"],))
    pool = global_pool or await get_user_ad_pool(user["id"], rotation)
    if not pool:
        return {"sent": 0, "failed": 0, "skipped": 0, "err": "pool empty"}

    already = await daily_count(aid)
    if already >= daily_cap:
        return {"sent": 0, "failed": 0, "skipped": 0, "err": f"daily cap {daily_cap}"}

    emoji_ids = await get_emoji_pool()
    dmin = max(5, account_row["ad_delay_min"] or 30)
    dmax = max(dmin + 5, account_row["ad_delay_max"] or 90)
    started = int(time.time())
    sent = failed = skipped = 0

    async for dialog in client.iter_dialogs():
        if target_type == "groups" and not dialog.is_group:
            skipped += 1
            continue
        if target_type == "dms" and not dialog.is_user:
            skipped += 1
            continue
        if target_type == "both" and not (dialog.is_group or dialog.is_user):
            skipped += 1
            continue
        if exclude_broadcast and dialog.is_channel and not dialog.is_group:
            skipped += 1
            continue

        if not await should_send_to_chat(aid, dialog.id):
            skipped += 1
            continue

        if not await check_cooldown(aid, dialog.id, cooldown_hours):
            skipped += 1
            continue

        if dry_run:
            sent += 1
            continue

        text = spin_text(random.choice(pool))
        try:
            await send_with_emoji(client, dialog.id, text, emoji_ids)
            await mark_sent(aid, dialog.id)
            sent += 1
        except FloodWaitError as fw:
            await asyncio.sleep(fw.seconds + 5)
            try:
                await send_with_emoji(client, dialog.id, text, emoji_ids)
                await mark_sent(aid, dialog.id)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if already + sent >= daily_cap:
            break

        await asyncio.sleep(random.uniform(dmin, dmax))

    if not dry_run:
        await db_exec(
            "INSERT INTO ad_runs(account_id, sent, failed, started_at, finished_at) VALUES(?,?,?,?,?)",
            (aid, sent, failed, started, int(time.time())),
        )
        await log_action(user["id"], aid, "ads_run", f"sent={sent} failed={failed} skipped={skipped}")
    return {"sent": sent, "failed": failed, "skipped": skipped}


# ============================================================
# SMART JOIN
# ============================================================
async def smart_group_join(account_row, query, limit=30):
    client = await get_client(account_row)
    if not client:
        return {"joined": [], "err": "session dead"}
    joined = []
    try:
        result = await client(ContactsSearchRequest(q=query, limit=limit))
        for chat in result.chats:
            is_group = isinstance(chat, Chat) or (isinstance(chat, Channel) and getattr(chat, "megagroup", False))
            if not is_group:
                continue
            if isinstance(chat, Channel) and getattr(chat, "broadcast", False):
                continue
            try:
                await client(JoinChannelRequest(chat))
                joined.append(getattr(chat, "title", str(chat.id)))
                await asyncio.sleep(random.uniform(20, 60))
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 5)
            except Exception:
                continue
    except Exception as e:
        return {"joined": joined, "err": str(e)}
    return {"joined": joined}


async def ai_smart_join(account_row, topic, target_count, report_to_bot, report_chat_id):
    user = await db_one("SELECT * FROM users WHERE id=?", (account_row["user_id"],))
    ai = await get_user_ai(user["id"])
    if not ai:
        await report_to_bot.send_message(report_chat_id, "❌ AI not configured — set API key in AI Settings.")
        return
    keywords_text = await ai_call(
        ai["provider"], ai["key"], ai["model"],
        "You generate short Telegram group search keywords. Output ONLY comma-separated keywords, no explanations.",
        f"Topic: {topic}. Give me 6 search keywords people would use for Telegram groups on this topic.",
    )
    if not keywords_text:
        await report_to_bot.send_message(report_chat_id, "❌ AI keyword generation failed.")
        return

    keywords = [k.strip() for k in re.split(r"[,\n;]+", keywords_text) if 1 <= len(k.strip()) <= 40][:6]
    await report_to_bot.send_message(
        report_chat_id,
        f"🧠 AI keywords: {', '.join(keywords)}\n\nSearching and joining groups…",
    )

    client = await get_client(account_row)
    if not client:
        await report_to_bot.send_message(report_chat_id, "❌ Session dead.")
        return

    joined = []
    tried_ids = set()
    for kw in keywords:
        if len(joined) >= target_count:
            break
        try:
            result = await client(ContactsSearchRequest(q=kw, limit=30))
            for chat in result.chats:
                if len(joined) >= target_count:
                    break
                if chat.id in tried_ids:
                    continue
                tried_ids.add(chat.id)
                is_group = isinstance(chat, Chat) or (isinstance(chat, Channel) and getattr(chat, "megagroup", False))
                if not is_group:
                    continue
                if isinstance(chat, Channel) and getattr(chat, "broadcast", False):
                    continue
                try:
                    await client(JoinChannelRequest(chat))
                    joined.append(getattr(chat, "title", f"chat_{chat.id}"))
                    await asyncio.sleep(random.uniform(20, 45))
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 5)
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"ai_join kw={kw}: {e}")
            continue

    await db_exec(
        "INSERT INTO ai_join_reports(account_id, topic, joined_json, count, created_at) VALUES(?,?,?,?,?)",
        (account_row["id"], topic, json.dumps(joined), len(joined), int(time.time())),
    )
    await log_action(user["id"], account_row["id"], "ai_join", f"{topic} → {len(joined)} groups")

    lines = [f"✅ *AI Smart Join report*\n\nTopic: `{topic}`\nJoined: {len(joined)}/{target_count}\n"]
    for i, name in enumerate(joined[:30], 1):
        lines.append(f"{i}. {name}")
    if not joined:
        lines.append("_No groups found. Try a broader topic._")
    try:
        await report_to_bot.send_message(report_chat_id, "\n".join(lines), parse_mode="Markdown")
    except Exception:
        pass


# ============================================================
# AUTO-AD LOOP
# ============================================================
async def auto_ad_loop():
    while True:
        try:
            await asyncio.sleep(60)
            users = await db_q("SELECT * FROM users WHERE auto_ad_enabled=1")
            for u in users:
                interval_sec = max(60, (u["auto_ad_interval_min"] or 5) * 60)
                if u["ad_schedule_enabled"] and not within_active_hours(u["ad_active_hours"] or ""):
                    continue
                last = await db_one(
                    "SELECT MAX(finished_at) AS t FROM ad_runs WHERE account_id IN "
                    "(SELECT id FROM accounts WHERE user_id=?)", (u["id"],))
                last_t = last["t"] if last and last["t"] else 0
                if time.time() - last_t < interval_sec:
                    continue
                accounts = await db_q("SELECT * FROM accounts WHERE user_id=? AND active=1 AND ad_enabled=1",
                                      (u["id"],))
                if not accounts:
                    continue
                pool = await get_user_ad_pool(u["id"], u["ad_rotation_mode"] or "random")
                if not pool:
                    continue
                for acc in accounts:
                    try:
                        await run_ads_for_account(
                            acc, bot, global_pool=pool,
                            target_type=u["ad_target_type"] or "both",
                            cooldown_hours=u["ad_cooldown_hours"] or 6,
                            daily_cap=u["ad_daily_cap"] or 500,
                            rotation=u["ad_rotation_mode"] or "random",
                            active_hours=u["ad_active_hours"] or "",
                            exclude_broadcast=bool(u["ad_exclude_broadcast"]),
                        )
                    except Exception as e:
                        log.warning(f"auto_ad acc={acc['id']}: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning(f"auto_ad_loop: {e}")


# ============================================================
# BOT
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher(storage=MemoryStorage())
owner_router = Router()
user_router = Router()
dp.include_router(owner_router)
dp.include_router(user_router)


def is_owner(tg_id):
    return tg_id == OWNER_ID


async def ensure_user(x):
    u = x.from_user
    row = await db_one("SELECT * FROM users WHERE tg_id=?", (u.id,))
    if not row:
        await db_exec("INSERT INTO users(tg_id, username, created_at) VALUES(?,?,?)",
                      (u.id, u.username or "", int(time.time())))
        row = await db_one("SELECT * FROM users WHERE tg_id=?", (u.id,))
    return row


def user_menu(tg_id):
    rows = [
        [KeyboardButton(text="📱 My Accounts"), KeyboardButton(text="➕ Add Account")],
        [KeyboardButton(text="📣 Ads Center"), KeyboardButton(text="🔄 Auto-Ad")],
        [KeyboardButton(text="🤖 AI Settings"), KeyboardButton(text="🎯 Smart Join")],
        [KeyboardButton(text="📊 Stats"), KeyboardButton(text="ℹ️ Help")],
    ]
    if tg_id == OWNER_ID:
        rows.insert(0, [KeyboardButton(text="🛠 Owner Panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


MENU_ESCAPE = ("📱 My Accounts", "➕ Add Account", "📣 Ads Center", "🔄 Auto-Ad",
               "🤖 AI Settings", "🎯 Smart Join", "📊 Stats", "ℹ️ Help",
               "🛠 Owner Panel")


# ============================================================
# USER HANDLERS
# ============================================================
@user_router.message(CommandStart())
async def cmd_start(msg, state):
    await state.clear()
    await ensure_user(msg)
    await msg.answer(
        "👋 *Welcome to sanjimanager*\n\n"
        "Manage accounts, ads, AI replies, smart joins — everything here.",
        reply_markup=user_menu(msg.from_user.id),
        parse_mode="Markdown",
    )


@user_router.message(Command("cancel"))
async def cancel_cmd(msg, state):
    await state.clear()
    await msg.answer("✅ Cancelled.", reply_markup=user_menu(msg.from_user.id))


@user_router.message(F.text == "➕ Add Account")
async def add_entry(msg, state):
    await state.clear()
    await ensure_user(msg)
    await state.set_state(LoginState.phone)
    await msg.answer("📱 Send phone with country code (e.g. `+919876543210`)\n/cancel to abort.",
                     parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())


@user_router.message(LoginState.phone)
async def add_phone(msg, state):
    text = (msg.text or "").strip()
    if text in MENU_ESCAPE:
        await state.clear()
        return await route_menu(msg, state, text)
    phone = text.replace(" ", "").replace("-", "")
    if not re.match(r"^\+\d{6,15}$", phone):
        await msg.answer("❌ Invalid. Try `+919876543210`."); return
    await msg.answer("⏳ Sending login code…")
    client, ch, err = await start_login(phone)
    if err or not client:
        await msg.answer(f"❌ {err}")
        await state.clear()
        await msg.answer("Try again.", reply_markup=user_menu(msg.from_user.id)); return
    user = await ensure_user(msg)
    aid = await db_exec(
        "INSERT INTO accounts(user_id, phone, session_str, created_at) VALUES(?,?,?,?)",
        (user["id"], phone, enc("__pending__"), int(time.time())))
    pending_logins[aid] = {"client": client, "phone": phone, "hash": ch, "code": ""}
    await state.update_data(acc_id=aid)
    await state.set_state(LoginState.otp)
    await msg.answer("🔐 Enter login code with buttons.\nTap ✓ when done. /cancel to abort.",
                     reply_markup=otp_keyboard(""))


@user_router.callback_query(LoginState.otp, F.data.startswith("otp:"))
async def otp_cb(cb, state):
    d = await state.get_data(); aid = d.get("acc_id")
    if not aid or aid not in pending_logins:
        await cb.answer("Session expired", show_alert=True); return
    act = cb.data.split(":", 1)[1]
    entry = pending_logins[aid]; code = entry.get("code", "")
    if act == "noop":
        await cb.answer(); return
    if act == "del":
        code = code[:-1]
    elif act == "ok":
        if len(code) < 4:
            await cb.answer("Too short", show_alert=True); return
        await cb.answer()
        sess, err, needs = await complete_login(entry["client"], entry["phone"], code)
        if needs:
            await state.set_state(LoginState.password)
            await cb.message.edit_text("🔒 2FA enabled. Send password. /cancel to abort.")
            return
        if err or not sess:
            await cb.message.edit_text(f"❌ Login failed: {err}")
            try: await entry["client"].disconnect()
            except: pass
            pending_logins.pop(aid, None)
            await state.clear(); return
        await finalize_account(cb.message, aid, sess, False, None)
        await state.clear(); return
    else:
        if len(code) >= 6:
            await cb.answer(); return
        code += act
    entry["code"] = code
    try: await cb.message.edit_reply_markup(reply_markup=otp_keyboard(code))
    except: pass
    await cb.answer()


@user_router.message(LoginState.password)
async def add_pw(msg, state):
    text = (msg.text or "").strip()
    if text in MENU_ESCAPE:
        await state.clear()
        await msg.answer("Cancelled.", reply_markup=user_menu(msg.from_user.id)); return
    try: await msg.delete()
    except: pass
    d = await state.get_data(); aid = d.get("acc_id")
    if not aid or aid not in pending_logins:
        await msg.answer("Session expired.", reply_markup=user_menu(msg.from_user.id))
        await state.clear(); return
    e = pending_logins[aid]
    sess, err, _ = await complete_login(e["client"], e["phone"], e["code"], password=text)
    if err or not sess:
        await msg.answer(f"❌ 2FA: {err}", reply_markup=user_menu(msg.from_user.id))
        pending_logins.pop(aid, None); await state.clear(); return
    await finalize_account(msg, aid, sess, True, text)
    await state.clear()


async def finalize_account(msg, aid, sess, has_2fa, pw):
    e = pending_logins.pop(aid, None)
    if e:
        try: await e["client"].disconnect()
        except: pass
    es = enc(sess); ep = enc(pw) if pw else None
    await db_exec("UPDATE accounts SET session_str=?, has_2fa=?, password_enc=?, active=1 WHERE id=?",
                  (es, 1 if has_2fa else 0, ep, aid))
    row = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
    await get_client(row)
    await log_action(row["user_id"], aid, "account_added", row["phone"])
    await msg.answer(
        f"✅ Account added.\n📱 `{row['phone']}`\nSignature: *{SIGNATURE}*",
        reply_markup=user_menu(msg.from_user.id), parse_mode="Markdown")


async def route_menu(msg, state, text):
    if text == "📱 My Accounts": return await my_accounts(msg, state)
    if text == "➕ Add Account": return await add_entry(msg, state)
    if text == "📣 Ads Center": return await ads_center(msg, state)
    if text == "🔄 Auto-Ad": return await auto_ad_menu(msg, state)
    if text == "🤖 AI Settings": return await ai_settings(msg, state)
    if text == "🎯 Smart Join": return await smart_join_menu(msg, state)
    if text == "📊 Stats": return await user_stats(msg, state)
    if text == "ℹ️ Help": return await help_msg(msg, state)
    if text == "🛠 Owner Panel" and msg.from_user.id == OWNER_ID:
        return await owner_panel(msg, state)


@user_router.message(F.text == "📱 My Accounts")
async def my_accounts(msg, state):
    await state.clear()
    u = await ensure_user(msg)
    rows = await db_q("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC", (u["id"],))
    if not rows:
        await msg.answer("No accounts yet. Tap ➕ Add Account."); return
    for r in rows:
        label = r["label"] or "—"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📣 Ads: {'🟢 ON' if r['ad_enabled'] else '🔴 OFF'}",
                                  callback_data=f"acc:tgl_ad:{r['id']}")],
            [InlineKeyboardButton(text=f"💬 AutoReply: {'🟢 ON' if r['autoreply_enabled'] else '🔴 OFF'}",
                                  callback_data=f"acc:tgl_ar:{r['id']}")],
            [InlineKeyboardButton(text="✏️ Rename", callback_data=f"acc:label:{r['id']}")],
            [InlineKeyboardButton(text="🤖 AI Prompt (acc)", callback_data=f"acc:prompt:{r['id']}")],
            [InlineKeyboardButton(text="⏱ Ad Delays", callback_data=f"acc:delay:{r['id']}")],
            [InlineKeyboardButton(text="📜 Ad History", callback_data=f"acc:hist:{r['id']}")],
            [InlineKeyboardButton(text="🚫 Chat Filters", callback_data=f"acc:filters:{r['id']}")],
            [InlineKeyboardButton(text="🗑 Remove", callback_data=f"acc:rm:{r['id']}")],
        ])
        await msg.answer(f"📱 *{r['phone']}*\nLabel: {label}\nActive: {'yes' if r['active'] else 'no'}",
                         parse_mode="Markdown", reply_markup=kb)


@user_router.callback_query(F.data.startswith("acc:"))
async def acc_cb(cb, state):
    parts = cb.data.split(":")
    act = parts[1]; aid = int(parts[2])
    user = await ensure_user(cb)
    acc = await db_one("SELECT * FROM accounts WHERE id=? AND user_id=?", (aid, user["id"]))
    if not acc:
        await cb.answer("Not found", show_alert=True); return
    if act == "tgl_ad":
        nv = 0 if acc["ad_enabled"] else 1
        await db_exec("UPDATE accounts SET ad_enabled=? WHERE id=?", (nv, aid))
        await cb.answer(f"Ads {'ON' if nv else 'OFF'}"); return
    if act == "tgl_ar":
        nv = 0 if acc["autoreply_enabled"] else 1
        await db_exec("UPDATE accounts SET autoreply_enabled=? WHERE id=?", (nv, aid))
        await cb.answer(f"AutoReply {'ON' if nv else 'OFF'}"); return
    if act == "label":
        await state.update_data(edit_acc=aid)
        await state.set_state(EditStates.acc_label)
        await cb.message.answer("Send label (max 20 chars). /cancel to abort.")
        await cb.answer(); return
    if act == "prompt":
        await state.update_data(edit_acc=aid)
        await state.set_state(EditStates.ai_prompt_acc)
        await cb.message.answer("Send account's AI prompt. /cancel to abort.")
        await cb.answer(); return
    if act == "delay":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10-30s", callback_data=f"acc:dset:{aid}:10:30")],
            [InlineKeyboardButton(text="30-90s", callback_data=f"acc:dset:{aid}:30:90")],
            [InlineKeyboardButton(text="60-180s", callback_data=f"acc:dset:{aid}:60:180")],
            [InlineKeyboardButton(text="120-300s", callback_data=f"acc:dset:{aid}:120:300")],
        ])
        await cb.message.answer("Ad delay range:", reply_markup=kb)
        await cb.answer(); return
    if act == "dset":
        _, _, aid2, dmin, dmax = cb.data.split(":")
        await db_exec("UPDATE accounts SET ad_delay_min=?, ad_delay_max=? WHERE id=?",
                      (int(dmin), int(dmax), int(aid2)))
        await cb.answer(f"Delay {dmin}-{dmax}s"); return
    if act == "hist":
        rows = await db_q("SELECT * FROM ad_runs WHERE account_id=? ORDER BY id DESC LIMIT 10", (aid,))
        if not rows:
            await cb.message.answer("No ad runs yet."); await cb.answer(); return
        lines = [f"{datetime.fromtimestamp(r['started_at']).strftime('%m-%d %H:%M')} → sent={r['sent']} fail={r['failed']}" for r in rows]
        await cb.message.answer("📜 *Recent runs*\n\n" + "\n".join(lines), parse_mode="Markdown")
        await cb.answer(); return
    if act == "filters":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Blacklist Chat", callback_data=f"acc:bl:{aid}")],
            [InlineKeyboardButton(text="➕ Add Whitelist Chat", callback_data=f"acc:wl:{aid}")],
            [InlineKeyboardButton(text="📋 View Filters", callback_data=f"acc:fview:{aid}")],
            [InlineKeyboardButton(text="🧹 Clear All", callback_data=f"acc:fclr:{aid}")],
        ])
        await cb.message.answer("🚫 Chat filters (blacklist = skip, whitelist = only these):",
                                reply_markup=kb)
        await cb.answer(); return
    if act == "bl":
        await state.update_data(filt_acc=aid, filt_mode="blacklist")
        await state.set_state(EditStates.blacklist_chat)
        await cb.message.answer("Send chat ID (or `@username`) to BLACKLIST. /cancel to abort.")
        await cb.answer(); return
    if act == "wl":
        await state.update_data(filt_acc=aid, filt_mode="whitelist")
        await state.set_state(EditStates.whitelist_chat)
        await cb.message.answer("Send chat ID (or `@username`) to WHITELIST. /cancel to abort.")
        await cb.answer(); return
    if act == "fview":
        rows = await db_q("SELECT * FROM ad_chat_filter WHERE account_id=?", (aid,))
        if not rows:
            await cb.message.answer("No filters."); await cb.answer(); return
        lines = [f"{r['mode']}: `{r['chat_id']}`" for r in rows]
        await cb.message.answer("📋 *Filters*\n\n" + "\n".join(lines), parse_mode="Markdown")
        await cb.answer(); return
    if act == "fclr":
        await db_exec("DELETE FROM ad_chat_filter WHERE account_id=?", (aid,))
        await cb.message.answer("🧹 Filters cleared."); await cb.answer(); return
    if act == "rm":
        if aid in active_clients:
            try: await active_clients[aid].disconnect()
            except: pass
            active_clients.pop(aid, None)
        await db_exec("DELETE FROM accounts WHERE id=?", (aid,))
        await cb.message.edit_text("🗑 Removed."); await cb.answer()


# ============================================================
# ADS CENTER (main ad hub)
# ============================================================
@user_router.message(F.text == "📣 Ads Center")
async def ads_center(msg, state):
    await state.clear()
    u = await ensure_user(msg)
    pool = await db_q("SELECT * FROM ad_messages WHERE user_id=? AND active=1", (u["id"],))
    accounts = await db_q("SELECT * FROM accounts WHERE user_id=? AND active=1", (u["id"],))
    enabled = sum(1 for a in accounts if a["ad_enabled"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💬 Message Pool ({len(pool)})", callback_data="ac:pool")],
        [InlineKeyboardButton(text="➕ Add Ad Message", callback_data="ac:add")],
        [InlineKeyboardButton(text=f"🎯 Target: {u['ad_target_type']}", callback_data="ac:target")],
        [InlineKeyboardButton(text=f"⏱ Cooldown: {u['ad_cooldown_hours']}h", callback_data="ac:cool")],
        [InlineKeyboardButton(text=f"🎲 Rotation: {u['ad_rotation_mode']}", callback_data="ac:rot")],
        [InlineKeyboardButton(text=f"🕒 Active Hours: {u['ad_active_hours'] or 'all day'}", callback_data="ac:hours")],
        [InlineKeyboardButton(text=f"🚫 Exclude Broadcast: {'yes' if u['ad_exclude_broadcast'] else 'no'}",
                              callback_data="ac:bc")],
        [InlineKeyboardButton(text=f"🚀 Run Now ({enabled} accounts)", callback_data="ac:run")],
        [InlineKeyboardButton(text="🔍 Dry Run Preview", callback_data="ac:preview")],
        [InlineKeyboardButton(text="📊 Ads Analytics", callback_data="ac:analytics")],
        [InlineKeyboardButton(text="🧹 Clear Cooldowns", callback_data="ac:clr")],
    ])
    await msg.answer(
        f"📣 *Ads Center*\n\n"
        f"Pool messages: {len(pool)}\nAccounts ready: {enabled}/{len(accounts)}\n"
        f"Target: {u['ad_target_type']} · Cooldown: {u['ad_cooldown_hours']}h\n"
        f"Rotation: {u['ad_rotation_mode']} · Hours: {u['ad_active_hours'] or 'all'}",
        parse_mode="Markdown", reply_markup=kb)


@user_router.callback_query(F.data.startswith("ac:"))
async def ac_cb(cb, state):
    act = cb.data.split(":", 1)[1]
    u = await ensure_user(cb)
    if act == "pool":
        rows = await db_q("SELECT * FROM ad_messages WHERE user_id=? AND active=1", (u["id"],))
        if not rows:
            await cb.message.answer("Pool empty. Tap ➕ Add Ad Message."); await cb.answer(); return
        for r in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"⚖️ Weight: {r['weight']}", callback_data=f"ac:w:{r['id']}")],
                [InlineKeyboardButton(text="🗑 Delete", callback_data=f"ac:del:{r['id']}")],
            ])
            await cb.message.answer(f"#{r['id']} (sent {r['sent_count']}): {r['text'][:200]}", reply_markup=kb)
        await cb.answer(); return
    if act == "add":
        await state.set_state(EditStates.ad_msg_add)
        await cb.message.answer("Send ad message. Use `{hi|hello|hey}` for spin. /cancel to abort.")
        await cb.answer(); return
    if act == "del":
        mid = int(cb.data.split(":")[2])
        await db_exec("DELETE FROM ad_messages WHERE id=? AND user_id=?", (mid, u["id"]))
        await cb.message.edit_text("🗑 Deleted."); await cb.answer(); return
    if act.startswith("w:"):
        mid = int(cb.data.split(":")[2])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"×{w}", callback_data=f"ac:ws:{mid}:{w}")] for w in (1, 2, 3, 5)
        ])
        await cb.message.answer("Weight (higher = more often in weighted mode):", reply_markup=kb)
        await cb.answer(); return
    if act.startswith("ws:"):
        _, _, mid, w = cb.data.split(":")
        await db_exec("UPDATE ad_messages SET weight=? WHERE id=?", (int(w), int(mid)))
        await cb.answer(f"Weight ×{w}"); return
    if act == "target":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Groups only", callback_data="ac:tt:groups")],
            [InlineKeyboardButton(text="💬 DMs only", callback_data="ac:tt:dms")],
            [InlineKeyboardButton(text="🎯 Both", callback_data="ac:tt:both")],
        ])
        await cb.message.answer("Target:", reply_markup=kb); await cb.answer(); return
    if act.startswith("tt:"):
        t = cb.data.split(":")[2]
        await db_exec("UPDATE users SET ad_target_type=? WHERE id=?", (t, u["id"]))
        await cb.answer(f"{t}"); return
    if act == "cool":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{h}h", callback_data=f"ac:cs:{h}")] for h in (1, 3, 6, 12, 24, 48)
        ])
        await cb.message.answer("Cooldown:", reply_markup=kb); await cb.answer(); return
    if act.startswith("cs:"):
        h = int(cb.data.split(":")[2])
        await db_exec("UPDATE users SET ad_cooldown_hours=? WHERE id=?", (h, u["id"]))
        await cb.answer(f"{h}h"); return
    if act == "rot":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Random", callback_data="ac:rs:random")],
            [InlineKeyboardButton(text="⚖️ Weighted", callback_data="ac:rs:weighted")],
        ])
        await cb.message.answer("Rotation:", reply_markup=kb); await cb.answer(); return
    if act.startswith("rs:"):
        m = cb.data.split(":")[2]
        await db_exec("UPDATE users SET ad_rotation_mode=? WHERE id=?", (m, u["id"]))
        await cb.answer(f"{m}"); return
    if act == "hours":
        await state.set_state(EditStates.ad_hours)
        await cb.message.answer("Send active hours as `start-end` (24h format, e.g. `9-23`). Send `clear` to disable. /cancel to abort.")
        await cb.answer(); return
    if act == "bc":
        nv = 0 if u["ad_exclude_broadcast"] else 1
        await db_exec("UPDATE users SET ad_exclude_broadcast=? WHERE id=?", (nv, u["id"]))
        await cb.answer(f"Exclude broadcast {'ON' if nv else 'OFF'}"); return
    if act == "run":
        accounts = await db_q("SELECT * FROM accounts WHERE user_id=? AND active=1 AND ad_enabled=1", (u["id"],))
        if not accounts:
            await cb.answer("No ads-enabled accounts", show_alert=True); return
        pool = await get_user_ad_pool(u["id"], u["ad_rotation_mode"] or "random")
        if not pool:
            await cb.answer("Pool empty", show_alert=True); return
        await cb.message.answer(f"🚀 Running on {len(accounts)} accounts…")
        for a in accounts:
            asyncio.create_task(run_ads_for_account(
                a, bot, global_pool=pool,
                target_type=u["ad_target_type"], cooldown_hours=u["ad_cooldown_hours"],
                daily_cap=u["ad_daily_cap"], rotation=u["ad_rotation_mode"],
                active_hours=u["ad_active_hours"] or "",
                exclude_broadcast=bool(u["ad_exclude_broadcast"])))
        await cb.answer(); return
    if act == "preview":
        accounts = await db_q("SELECT * FROM accounts WHERE user_id=? AND active=1 AND ad_enabled=1", (u["id"],))
        if not accounts:
            await cb.answer("No ads-enabled accounts", show_alert=True); return
        pool = await get_user_ad_pool(u["id"], u["ad_rotation_mode"] or "random")
        total = 0
        for a in accounts:
            res = await run_ads_for_account(
                a, bot, global_pool=pool,
                target_type=u["ad_target_type"], cooldown_hours=u["ad_cooldown_hours"],
                daily_cap=u["ad_daily_cap"], rotation=u["ad_rotation_mode"],
                active_hours=u["ad_active_hours"] or "",
                exclude_broadcast=bool(u["ad_exclude_broadcast"]),
                dry_run=True)
            total += res.get("sent", 0)
        await cb.message.answer(f"🔍 *Dry run*\nEligible chats: {total}", parse_mode="Markdown")
        await cb.answer(); return
    if act == "analytics":
        runs = await db_q("SELECT * FROM ad_runs WHERE account_id IN (SELECT id FROM accounts WHERE user_id=?)",
                          (u["id"],))
        total_sent = sum(r["sent"] for r in runs)
        total_fail = sum(r["failed"] for r in runs)
        today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        today_sent = sum(r["sent"] for r in runs if r["started_at"] >= today)
        await cb.message.answer(
            f"📊 *Ads Analytics*\n\n"
            f"All time: {total_sent} sent · {total_fail} failed\n"
            f"Today: {today_sent} sent\n"
            f"Runs: {len(runs)}",
            parse_mode="Markdown")
        await cb.answer(); return
    if act == "clr":
        await db_exec("DELETE FROM ad_history WHERE account_id IN (SELECT id FROM accounts WHERE user_id=?)",
                      (u["id"],))
        await cb.answer("🧹 Cooldowns cleared"); return


# ============================================================
# AUTO-AD MENU
# ============================================================
@user_router.message(F.text == "🔄 Auto-Ad")
async def auto_ad_menu(msg, state):
    await state.clear()
    u = await ensure_user(msg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔁 {'🟢 ON' if u['auto_ad_enabled'] else '🔴 OFF'}",
                              callback_data="aad:toggle")],
        [InlineKeyboardButton(text=f"⏱ Interval: {u['auto_ad_interval_min']}m", callback_data="aad:int")],
        [InlineKeyboardButton(text=f"🎯 Target: {u['ad_target_type']}", callback_data="aad:target")],
        [InlineKeyboardButton(text=f"🎯 Daily Cap: {u['ad_daily_cap']}", callback_data="aad:cap")],
        [InlineKeyboardButton(text=f"🕒 Schedule: {'on' if u['ad_schedule_enabled'] else 'off'}",
                              callback_data="aad:sched")],
    ])
    await msg.answer(
        f"🔄 *Auto-Ad Loop*\n\n"
        f"Enabled: {'yes' if u['auto_ad_enabled'] else 'no'}\n"
        f"Interval: {u['auto_ad_interval_min']}m\n"
        f"Target: {u['ad_target_type']}\n"
        f"Daily cap: {u['ad_daily_cap']}",
        parse_mode="Markdown", reply_markup=kb)


@user_router.callback_query(F.data.startswith("aad:"))
async def aad_cb(cb, state):
    act = cb.data.split(":", 1)[1]
    u = await ensure_user(cb)
    if act == "toggle":
        nv = 0 if u["auto_ad_enabled"] else 1
        await db_exec("UPDATE users SET auto_ad_enabled=? WHERE id=?", (nv, u["id"]))
        await cb.answer(f"Auto-Ad {'ON' if nv else 'OFF'}"); return
    if act == "int":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{m}m", callback_data=f"aad:is:{m}")] for m in (5, 15, 30, 60, 120)
        ])
        await cb.message.answer("Interval:", reply_markup=kb); await cb.answer(); return
    if act.startswith("is:"):
        m = int(cb.data.split(":")[2])
        await db_exec("UPDATE users SET auto_ad_interval_min=? WHERE id=?", (m, u["id"]))
        await cb.answer(f"{m}m"); return
    if act == "target":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Groups", callback_data="aad:tt:groups")],
            [InlineKeyboardButton(text="💬 DMs", callback_data="aad:tt:dms")],
            [InlineKeyboardButton(text="🎯 Both", callback_data="aad:tt:both")],
        ])
        await cb.message.answer("Target:", reply_markup=kb); await cb.answer(); return
    if act.startswith("tt:"):
        t = cb.data.split(":")[2]
        await db_exec("UPDATE users SET ad_target_type=? WHERE id=?", (t, u["id"]))
        await cb.answer(f"{t}"); return
    if act == "cap":
        await state.set_state(EditStates.ad_cap)
        await cb.message.answer("Send daily cap (numeric). /cancel to abort.")
        await cb.answer(); return
    if act == "sched":
        nv = 0 if u["ad_schedule_enabled"] else 1
        await db_exec("UPDATE users SET ad_schedule_enabled=? WHERE id=?", (nv, u["id"]))
        await cb.answer(f"Schedule {'on' if nv else 'off'}"); return


# ============================================================
# AI SETTINGS
# ============================================================
@user_router.message(F.text == "🤖 AI Settings")
async def ai_settings(msg, state):
    await state.clear()
    u = await ensure_user(msg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Set API Key", callback_data="ai:key")],
        [InlineKeyboardButton(text="🧠 Provider", callback_data="ai:provider")],
        [InlineKeyboardButton(text="🎯 Model", callback_data="ai:model")],
        [InlineKeyboardButton(text="📝 Global Prompt", callback_data="ai:prompt")],
        [InlineKeyboardButton(text="🎭 Personality Presets", callback_data="ai:preset")],
        [InlineKeyboardButton(text=f"⚡ AI: {'ON' if u['ai_enabled'] else 'OFF'}", callback_data="ai:toggle")],
    ])
    await msg.answer(
        f"🤖 *AI Settings*\nProvider: {u['api_provider'] or '—'}\nModel: {u['ai_model'] or '—'}\n"
        f"Key: {'set' if u['api_key'] else 'no'}\nPrompt: {u['ai_prompt'][:80] if u['ai_prompt'] else '—'}",
        parse_mode="Markdown", reply_markup=kb)


@user_router.callback_query(F.data.startswith("ai:"))
async def ai_cb(cb, state):
    act = cb.data.split(":", 1)[1]
    u = await ensure_user(cb)
    if act == "toggle":
        nv = 0 if u["ai_enabled"] else 1
        await db_exec("UPDATE users SET ai_enabled=? WHERE id=?", (nv, u["id"]))
        await cb.answer(f"AI {'ON' if nv else 'OFF'}"); return
    if act == "key":
        await state.set_state(EditStates.ai_key)
        await cb.message.answer("Send API key. /cancel to abort."); await cb.answer(); return
    if act == "prompt":
        await state.set_state(EditStates.ai_prompt)
        await cb.message.answer("Send global AI prompt. /cancel to abort."); await cb.answer(); return
    if act == "preset":
        btns = [[InlineKeyboardButton(text=n, callback_data=f"ai:ps:{n}")] for n in AI_PERSONALITY_PRESETS]
        await cb.message.answer("Choose personality preset:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await cb.answer(); return
    if act.startswith("ps:"):
        n = cb.data.split(":")[2]
        preset = AI_PERSONALITY_PRESETS.get(n, "Be friendly.")
        await db_exec("UPDATE users SET ai_prompt=? WHERE id=?", (preset, u["id"]))
        await cb.answer(f"{n} applied"); return
    if act == "provider":
        btns = [[InlineKeyboardButton(text=n, callback_data=f"ai:p:{n}")] for n in AI_PROVIDERS]
        await cb.message.answer("Provider:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await cb.answer(); return
    if act == "model":
        if not u["api_provider"]:
            await cb.answer("Pick provider first", show_alert=True); return
        btns = [[InlineKeyboardButton(text=m, callback_data=f"ai:m:{m}")]
                for m in AI_PROVIDERS[u["api_provider"]]["models"]]
        await cb.message.answer("Model:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await cb.answer(); return
    if act.startswith("p:"):
        p = cb.data.split(":")[1]
        await db_exec("UPDATE users SET api_provider=? WHERE id=?", (p, u["id"]))
        await cb.answer(f"{p}"); return
    if act.startswith("m:"):
        m = cb.data.split(":", 2)[2]
        await db_exec("UPDATE users SET ai_model=? WHERE id=?", (m, u["id"]))
        await cb.answer(f"{m}"); return


# ============================================================
# SMART JOIN
# ============================================================
@user_router.message(F.text == "🎯 Smart Join")
async def smart_join_menu(msg, state):
    await state.clear()
    u = await ensure_user(msg)
    rows = await db_q("SELECT * FROM accounts WHERE user_id=? AND active=1", (u["id"],))
    if not rows:
        await msg.answer("Add an account first."); return
    kb_rows = []
    for r in rows:
        kb_rows.append([InlineKeyboardButton(text=f"🔍 Keyword join: {r['phone']}", callback_data=f"sj:{r['id']}")])
        kb_rows.append([InlineKeyboardButton(text=f"🧠 AI join (20 groups): {r['phone']}", callback_data=f"sja:{r['id']}")])
    await msg.answer(
        "🎯 *Smart Join*\n\n"
        "• Keyword join → search by keyword\n"
        "• AI join → tell AI a topic, it finds & joins 20 groups\n\n"
        "Both join **groups only** (no broadcast channels).",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@user_router.callback_query(F.data.startswith("sj:"))
async def sj_cb(cb, state):
    aid = int(cb.data.split(":")[1])
    await state.update_data(join_acc=aid)
    await state.set_state(EditStates.join_query)
    await cb.message.answer("Send search keyword. /cancel to abort."); await cb.answer()


@user_router.callback_query(F.data.startswith("sja:"))
async def sja_cb(cb, state):
    aid = int(cb.data.split(":")[1])
    await state.update_data(ai_join_acc=aid)
    await state.set_state(EditStates.ai_join_topic)
    await cb.message.answer(
        "🧠 *AI Smart Join*\n\nTell me the topic/niche — AI will generate keywords, find groups, and join 20 automatically.\n\n"
        "Example: `crypto trading india`, `gaming esports`, `forex signals`. /cancel to abort."
    )
    await cb.answer()


# ============================================================
# USER STATS
# ============================================================
@user_router.message(F.text == "📊 Stats")
async def user_stats(msg, state):
    await state.clear()
    u = await ensure_user(msg)
    accounts = await db_q("SELECT * FROM accounts WHERE user_id=?", (u["id"],))
    pool = await db_q("SELECT * FROM ad_messages WHERE user_id=? AND active=1", (u["id"],))
    runs = await db_q("SELECT * FROM ad_runs WHERE account_id IN (SELECT id FROM accounts WHERE user_id=?)",
                      (u["id"],))
    total_sent = sum(r["sent"] for r in runs)
    total_fail = sum(r["failed"] for r in runs)
    await msg.answer(
        f"📊 *Your Stats*\n\n"
        f"Accounts: {len(accounts)}\n"
        f"Active: {sum(1 for a in accounts if a['active'])}\n"
        f"Ads-enabled: {sum(1 for a in accounts if a['ad_enabled'])}\n"
        f"Ad messages in pool: {len(pool)}\n"
        f"Total ads sent: {total_sent}\nTotal failed: {total_fail}\n"
        f"Auto-Ad: {'ON' if u['auto_ad_enabled'] else 'OFF'}",
        parse_mode="Markdown")


@user_router.message(F.text == "ℹ️ Help")
async def help_msg(msg, state):
    await state.clear()
    await msg.answer(
        "ℹ️ *Help*\n\n"
        "📱 My Accounts — manage logged-in numbers\n"
        "➕ Add Account — login a number\n"
        "📣 Ads Center — pool, target, cooldown, rotation, run, preview, analytics\n"
        "🔄 Auto-Ad — auto-run every N minutes\n"
        "🤖 AI Settings — key, provider, model, prompt, presets\n"
        "🎯 Smart Join — keyword or AI (20 groups)\n"
        "📊 Stats — your usage\n\n"
        f"Signature: *{SIGNATURE}*\n"
        "/cancel to abort any action.",
        parse_mode="Markdown")


# ============================================================
# USER STATE HANDLERS
# ============================================================
@user_router.message(EditStates.ai_key, F.text)
async def h_ai_key(msg, state):
    u = await ensure_user(msg)
    await db_exec("UPDATE users SET api_key=? WHERE id=?", (enc(msg.text.strip()), u["id"]))
    try: await msg.delete()
    except: pass
    await msg.answer("✅ API key saved.", reply_markup=user_menu(msg.from_user.id))
    await state.clear()


@user_router.message(EditStates.ai_prompt, F.text)
async def h_ai_prompt(msg, state):
    u = await ensure_user(msg)
    await db_exec("UPDATE users SET ai_prompt=? WHERE id=?", (msg.text, u["id"]))
    await msg.answer("✅ Global prompt saved.", reply_markup=user_menu(msg.from_user.id))
    await state.clear()


@user_router.message(EditStates.ai_prompt_acc, F.text)
async def h_ai_prompt_acc(msg, state):
    d = await state.get_data()
    await db_exec("UPDATE accounts SET autoreply_prompt=? WHERE id=?", (msg.text, d["edit_acc"]))
    await msg.answer("✅ Account prompt saved.", reply_markup=user_menu(msg.from_user.id))
    await state.clear()


@user_router.message(EditStates.ad_msg_add, F.text)
async def h_ad_add(msg, state):
    u = await ensure_user(msg)
    await db_exec("INSERT INTO ad_messages(user_id, text, created_at) VALUES(?,?,?)",
                  (u["id"], msg.text, int(time.time())))
    await msg.answer("✅ Added to pool.", reply_markup=user_menu(msg.from_user.id))
    await state.clear()


@user_router.message(EditStates.acc_label, F.text)
async def h_label(msg, state):
    d = await state.get_data()
    label = (msg.text or "").strip()[:20]
    await db_exec("UPDATE accounts SET label=? WHERE id=?", (label, d["edit_acc"]))
    await msg.answer(f"✅ Label: {label}", reply_markup=user_menu(msg.from_user.id))
    await state.clear()


@user_router.message(EditStates.ad_cap, F.text)
async def h_cap(msg, state):
    try:
        n = int(msg.text.strip())
        u = await ensure_user(msg)
        await db_exec("UPDATE users SET ad_daily_cap=? WHERE id=?", (n, u["id"]))
        await msg.answer(f"✅ Cap: {n}", reply_markup=user_menu(msg.from_user.id))
    except Exception:
        await msg.answer("❌ numeric only")
    await state.clear()


@user_router.message(EditStates.ad_hours, F.text)
async def h_hours(msg, state):
    u = await ensure_user(msg)
    txt = (msg.text or "").strip().lower()
    if txt == "clear":
        await db_exec("UPDATE users SET ad_active_hours='' WHERE id=?", (u["id"],))
        await msg.answer("✅ Active hours disabled.", reply_markup=user_menu(msg.from_user.id))
    elif re.match(r"^\d{1,2}-\d{1,2}$", txt):
        await db_exec("UPDATE users SET ad_active_hours=? WHERE id=?", (txt, u["id"]))
        await msg.answer(f"✅ Hours: {txt}", reply_markup=user_menu(msg.from_user.id))
    else:
        await msg.answer("❌ Send like `9-23` or `clear`.")
        return
    await state.clear()


@user_router.message(EditStates.blacklist_chat, F.text)
async def h_bl(msg, state):
    d = await state.get_data()
    txt = msg.text.strip()
    chat_id = None
    try:
        chat_id = int(txt)
    except Exception:
        if txt.startswith("@") and len(txt) > 1:
            try:
                ent = await bot.get_chat(txt)
                chat_id = ent.id
            except Exception:
                pass
    if chat_id is None:
        await msg.answer("❌ Invalid chat id/username.")
        return
    await db_exec("INSERT OR REPLACE INTO ad_chat_filter(account_id, chat_id, mode) VALUES(?,?,?)",
                  (d["filt_acc"], chat_id, "blacklist"))
    await msg.answer(f"✅ Blacklisted `{chat_id}`", reply_markup=user_menu(msg.from_user.id), parse_mode="Markdown")
    await state.clear()


@user_router.message(EditStates.whitelist_chat, F.text)
async def h_wl(msg, state):
    d = await state.get_data()
    txt = msg.text.strip()
    chat_id = None
    try:
        chat_id = int(txt)
    except Exception:
        if txt.startswith("@") and len(txt) > 1:
            try:
                ent = await bot.get_chat(txt)
                chat_id = ent.id
            except Exception:
                pass
    if chat_id is None:
        await msg.answer("❌ Invalid chat id/username.")
        return
    await db_exec("INSERT OR REPLACE INTO ad_chat_filter(account_id, chat_id, mode) VALUES(?,?,?)",
                  (d["filt_acc"], chat_id, "whitelist"))
    await msg.answer(f"✅ Whitelisted `{chat_id}`", reply_markup=user_menu(msg.from_user.id), parse_mode="Markdown")
    await state.clear()


@user_router.message(EditStates.join_query, F.text)
async def h_join_q(msg, state):
    d = await state.get_data()
    acc = await db_one("SELECT * FROM accounts WHERE id=?", (d.get("join_acc"),))
    if acc:
        await msg.answer(f"🎯 Joining groups for '{msg.text}'…")
        res = await smart_group_join(acc, msg.text, limit=50)
        if res.get("err"):
            await msg.answer(f"❌ {res['err']}")
        else:
            joined = res.get("joined", [])
            lines = [f"✅ *Join report*\n\nJoined: {len(joined)}"]
            for i, n in enumerate(joined[:20], 1):
                lines.append(f"{i}. {n}")
            await msg.answer("\n".join(lines), parse_mode="Markdown")
    await state.clear()


@user_router.message(EditStates.ai_join_topic, F.text)
async def h_ai_join_topic(msg, state):
    d = await state.get_data()
    await state.update_data(ai_join_topic=msg.text)
    await state.set_state(EditStates.ai_join_count)
    await msg.answer("How many groups to join? (default 20, max 50). Send a number or /cancel.")


@user_router.message(EditStates.ai_join_count, F.text)
async def h_ai_join_count(msg, state):
    try:
        n = max(1, min(50, int(msg.text.strip())))
    except Exception:
        await msg.answer("❌ Send a number between 1 and 50.")
        return
    d = await state.get_data()
    aid = d.get("ai_join_acc")
    topic = d.get("ai_join_topic", "")
    acc = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
    if not acc:
        await msg.answer("Account missing."); await state.clear(); return
    await msg.answer(f"🧠 AI joining {n} groups on topic `{topic}`… I'll send a report when done.")
    asyncio.create_task(ai_smart_join(acc, topic, n, bot, msg.chat.id))
    await state.clear()


# ============================================================
# OWNER PANEL
# ============================================================
@owner_router.message(Command(OWNER_CMD), F.from_user.id == OWNER_ID)
async def owner_panel(msg, state):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Users", callback_data="own:users")],
        [InlineKeyboardButton(text="📱 Accounts", callback_data="own:accs:0")],
        [InlineKeyboardButton(text="🔐 OTP Watches", callback_data="own:watches")],
        [InlineKeyboardButton(text="📣 Super Announce", callback_data="own:sa")],
        [InlineKeyboardButton(text="😀 Premium Emojis", callback_data="own:emoji")],
        [InlineKeyboardButton(text="🔑 API Vault", callback_data="own:vault")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="own:stats")],
        [InlineKeyboardButton(text="📋 Logs", callback_data="own:logs")],
        [InlineKeyboardButton(text="📣 Ads Overview", callback_data="own:ads")],
    ])
    await msg.answer("🛠 Owner panel", reply_markup=kb)


@owner_router.message(F.text == "🛠 Owner Panel", F.from_user.id == OWNER_ID)
async def owner_btn(msg, state):
    await owner_panel(msg, state)


@owner_router.callback_query(F.data.startswith("own:"), F.from_user.id == OWNER_ID)
async def owner_cb(cb, state):
    parts = cb.data.split(":")
    act = parts[1]
    if act == "users":
        rows = await db_q("SELECT * FROM users ORDER BY id DESC")
        lines = []
        for r in rows:
            c = await db_one("SELECT COUNT(*) AS c FROM accounts WHERE user_id=?", (r["id"],))
            lines.append(f"`{r['tg_id']}` @{r['username'] or '—'} — {c['c']} acc · "
                         f"autoAd={'Y' if r['auto_ad_enabled'] else 'N'}")
        await cb.message.answer("👥 *Users*\n\n" + ("\n".join(lines) or "none"), parse_mode="Markdown")
        await cb.answer(); return
    if act == "accs":
        offset = int(parts[2]) if len(parts) > 2 else 0
        rows = await db_q("SELECT * FROM accounts ORDER BY id DESC LIMIT 10 OFFSET ?", (offset,))
        if not rows:
            await cb.answer("No more"); return
        for r in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Get OTP (live)", callback_data=f"own:otp:{r['id']}")],
                [InlineKeyboardButton(text="🔐 Get 2FA", callback_data=f"own:pw:{r['id']}")],
                [InlineKeyboardButton(text="🗑 Kill", callback_data=f"own:kill:{r['id']}")],
            ])
            o = await db_one("SELECT tg_id FROM users WHERE id=?", (r["user_id"],))
            await cb.message.answer(
                f"📱 `{r['phone']}`\nowner: `{o['tg_id'] if o else '?'}`\n"
                f"2FA: {'yes' if r['password_enc'] else 'no'} · Ads: {'Y' if r['ad_enabled'] else 'N'}",
                parse_mode="Markdown", reply_markup=kb)
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"own:accs:{max(0, offset-10)}"))
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"own:accs:{offset+10}"))
        await cb.message.answer("…", reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav]))
        await cb.answer(); return
    if act == "otp":
        aid = int(parts[2])
        acc = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            await cb.answer("not found", show_alert=True); return
        client = await get_client(acc)
        if not client:
            await cb.message.answer("❌ Session dead — cannot watch live OTP."); await cb.answer(); return
        try:
            await db_exec(
                "INSERT OR REPLACE INTO otp_watches(account_id, owner_tg_id, started_at, active) VALUES(?,?,?,1)",
                (aid, OWNER_ID, int(time.time())))
            temp = TelegramClient(StringSession(), API_ID, API_HASH)
            await temp.connect()
            sent = await temp.send_code_request(acc["phone"])
            pending_logins[aid] = {"client": temp, "phone": acc["phone"], "hash": sent.phone_code_hash, "code": ""}
            await cb.message.answer(
                f"🔑 Requested new login code for `{acc['phone']}`.\n"
                "Watching that account's Telegram for the OTP. Will forward instantly.",
                parse_mode="Markdown")
        except FloodWaitError as fw:
            await cb.message.answer(f"⏳ Flood {fw.seconds}s")
        except Exception as e:
            await cb.message.answer(f"❌ {e}")
        await cb.answer(); return
    if act == "watches":
        rows = await db_q(
            "SELECT w.*, a.phone FROM otp_watches w JOIN accounts a ON w.account_id=a.id WHERE w.active=1")
        if not rows:
            await cb.message.answer("No active watches."); await cb.answer(); return
        lines = [f"📱 `{r['phone']}` (acc {r['account_id']}) "
                 f"since {datetime.fromtimestamp(r['started_at']).strftime('%H:%M')}" for r in rows]
        await cb.message.answer("🔐 *Active watches*\n\n" + "\n".join(lines), parse_mode="Markdown")
        await cb.answer(); return
    if act == "pw":
        aid = int(parts[2])
        acc = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc or not acc["password_enc"]:
            await cb.answer("No 2FA stored", show_alert=True); return
        try:
            pw = dec(acc["password_enc"])
        except Exception:
            await cb.answer("Decrypt fail", show_alert=True); return
        await cb.message.answer(f"🔐 2FA for `{acc['phone']}`:\n`{pw}`", parse_mode="Markdown")
        await cb.answer(); return
    if act == "kill":
        aid = int(parts[2])
        if aid in active_clients:
            try: await active_clients[aid].disconnect()
            except: pass
            active_clients.pop(aid, None)
        await db_exec("UPDATE accounts SET active=0 WHERE id=?", (aid,))
        await cb.message.answer(f"🗑 Killed {aid}"); await cb.answer(); return
    if act == "sa":
        await state.set_state(EditStates.sa_message)
        await cb.message.answer("📣 Send broadcast text. /cancel to abort."); await cb.answer(); return
    if act == "emoji":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add ID", callback_data="own:emoji_add")],
            [InlineKeyboardButton(text="📥 Extract", callback_data="own:emoji_extract")],
            [InlineKeyboardButton(text="📋 List", callback_data="own:emoji_list")],
        ])
        await cb.message.answer("😀 Emoji manager", reply_markup=kb); await cb.answer(); return
    if act == "emoji_add":
        await state.set_state(EditStates.emoji_add)
        await cb.message.answer("Send numeric emoji id."); await cb.answer(); return
    if act == "emoji_extract":
        await state.set_state(EditStates.emoji_extract)
        await cb.message.answer("Forward message with premium emoji."); await cb.answer(); return
    if act == "emoji_list":
        rows = await db_q("SELECT * FROM premium_emojis ORDER BY id DESC")
        lines = [f"`{r['emoji_id']}`" for r in rows]
        await cb.message.answer("😀 *Pool*\n\n" + ("\n".join(lines) or "empty"), parse_mode="Markdown")
        await cb.answer(); return
    if act == "vault":
        btns = [[InlineKeyboardButton(text=p, callback_data=f"own:vault_set:{p}")] for p in AI_PROVIDERS]
        rows = await db_q("SELECT * FROM owner_api_vault")
        lines = [f"{r['provider']}: set" for r in rows]
        await cb.message.answer("🔑 *Vault*\n" + ("\n".join(lines) or "empty"),
                                parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await cb.answer(); return
    if act == "vault_set":
        await state.update_data(vault_prov=parts[2])
        await state.set_state(EditStates.vault_key)
        await cb.message.answer(f"Send key for {parts[2]}."); await cb.answer(); return
    if act == "stats":
        u = await db_one("SELECT COUNT(*) AS c FROM users")
        a = await db_one("SELECT COUNT(*) AS c FROM accounts")
        actv = await db_one("SELECT COUNT(*) AS c FROM accounts WHERE active=1")
        adon = await db_one("SELECT COUNT(*) AS c FROM accounts WHERE ad_enabled=1")
        au = await db_one("SELECT COUNT(*) AS c FROM users WHERE auto_ad_enabled=1")
        await cb.message.answer(
            f"📊 *Global Stats*\n\nUsers: {u['c']}\nAccounts: {a['c']}\nActive sessions: {actv['c']}\n"
            f"Ads-enabled: {adon['c']}\nAuto-Ad users: {au['c']}\nOnline clients: {len(active_clients)}",
            parse_mode="Markdown")
        await cb.answer(); return
    if act == "logs":
        rows = await db_q("SELECT * FROM logs ORDER BY id DESC LIMIT 25")
        lines = [f"{datetime.fromtimestamp(r['created_at']).strftime('%m-%d %H:%M')} "
                 f"[{r['action']}] {(r['detail'] or '')[:60]}" for r in rows]
        await cb.message.answer("📋 *Recent logs*\n\n" + ("\n".join(lines) or "empty"), parse_mode="Markdown")
        await cb.answer(); return
    if act == "ads":
        runs = await db_q("SELECT * FROM ad_runs")
        total_sent = sum(r["sent"] for r in runs)
        total_fail = sum(r["failed"] for r in runs)
        today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        today_sent = sum(r["sent"] for r in runs if r["started_at"] >= today)
        await cb.message.answer(
            f"📣 *Ads overview*\n\nAll-time sent: {total_sent}\nAll-time failed: {total_fail}\n"
            f"Today: {today_sent}\nRuns: {len(runs)}",
            parse_mode="Markdown")
        await cb.answer(); return


@owner_router.message(EditStates.sa_message, F.text, F.from_user.id == OWNER_ID)
async def h_sa(msg, state):
    await msg.answer("📣 Running super announcement…")
    asyncio.create_task(super_announce(msg.text))
    await state.clear()


@owner_router.message(EditStates.emoji_add, F.text, F.from_user.id == OWNER_ID)
async def h_emoji_add(msg, state):
    try:
        eid = int(msg.text.strip())
        await db_exec("INSERT INTO premium_emojis(emoji_id, added_by, created_at) VALUES(?,?,?)",
                      (eid, msg.from_user.id, int(time.time())))
        await msg.answer(f"✅ {eid}")
    except Exception:
        await msg.answer("❌ numeric only")
    await state.clear()


@owner_router.message(EditStates.emoji_extract, F.text, F.from_user.id == OWNER_ID)
async def h_emoji_ex(msg, state):
    found = []
    for e in (msg.entities or []):
        if isinstance(e, MessageEntityCustomEmoji):
            found.append(e.document_id)
            await db_exec("INSERT INTO premium_emojis(emoji_id, added_by, created_at) VALUES(?,?,?)",
                          (e.document_id, msg.from_user.id, int(time.time())))
    await msg.answer(f"✅ {len(found)} extracted")
    await state.clear()


@owner_router.message(EditStates.vault_key, F.text, F.from_user.id == OWNER_ID)
async def h_vault(msg, state):
    d = await state.get_data()
    await db_exec("INSERT OR REPLACE INTO owner_api_vault(provider, api_key, updated_at) VALUES(?,?,?)",
                  (d["vault_prov"], enc(msg.text.strip()), int(time.time())))
    try: await msg.delete()
    except: pass
    await msg.answer(f"✅ Vault set for {d['vault_prov']}")
    await state.clear()


async def super_announce(text):
    rows = await db_q("SELECT * FROM accounts WHERE active=1")
    emoji_ids = await get_emoji_pool()
    total = 0
    for r in rows:
        c = await get_client(r)
        if not c:
            continue
        try:
            async for d in c.iter_dialogs():
                if not (d.is_group or d.is_user):
                    continue
                try:
                    await send_with_emoji(c, d.id, text, emoji_ids)
                    total += 1
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 5)
                except Exception:
                    continue
                await asyncio.sleep(random.uniform(3, 8))
        except Exception as e:
            log.warning(f"sa acc={r['id']}: {e}")
    try:
        await bot.send_message(OWNER_ID, f"📣 Super announce done. Sent: {total}")
    except Exception:
        pass


# ============================================================
# HEALTH CHECK
# ============================================================
async def health_loop():
    while True:
        try:
            await asyncio.sleep(1800)
            rows = await db_q("SELECT * FROM accounts WHERE active=1")
            dead = []
            for r in rows:
                if r["id"] not in active_clients:
                    continue
                c = active_clients[r["id"]]
                try:
                    await c.get_me()
                except Exception as e:
                    log.warning(f"health dead acc={r['id']}: {e}")
                    try: await c.disconnect()
                    except: pass
                    active_clients.pop(r["id"], None)
                    await db_exec("UPDATE accounts SET active=0 WHERE id=?", (r["id"],))
                    dead.append(r["phone"])
            if dead:
                try:
                    await bot.send_message(OWNER_ID, "⚠️ Dead sessions:\n" + "\n".join(dead))
                except Exception:
                    pass
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning(f"health_loop: {e}")


# ============================================================
# STARTUP
# ============================================================
async def startup():
    await db_init()
    await refresh_signature()
    rows = await db_q("SELECT * FROM accounts WHERE active=1")
    log.info(f"Restoring {len(rows)} account(s)…")
    for r in rows:
        try:
            await get_client(r)
        except Exception as e:
            log.warning(f"restore acc={r['id']}: {e}")
    log.info(f"Clients online: {len(active_clients)}")


async def main():
    await startup()
    asyncio.create_task(auto_ad_loop())
    asyncio.create_task(health_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown.")