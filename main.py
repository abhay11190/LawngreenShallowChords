import asyncio
import html
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "filestore.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("file-store-bot")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_owner_id() -> int | None:
    try:
        return int(OWNER_ID_RAW)
    except (TypeError, ValueError):
        return None


OWNER_ID = parse_owner_id()


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.create_tables()

    def create_tables(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            source_chat_id TEXT NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_name TEXT NOT NULL,
            caption TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            title TEXT NOT NULL,
            invite_link TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS global_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            invite_link TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS statistics (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        """
        with self.lock:
            self.conn.executescript(schema)
            for key in (
                "total_uploads",
                "link_opens",
                "successful_verifications",
                "failed_verifications",
            ):
                self.conn.execute(
                    "INSERT OR IGNORE INTO statistics(key, value) VALUES(?, 0)",
                    (key,),
                )
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('maintenance_mode', '0')"
            )
            self.conn.commit()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    def stat(self, key: str, amount: int = 1) -> None:
        self.execute(
            "INSERT INTO statistics(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
            (key, amount),
        )


db: Database | None = None
bot_username = ""
rate_lock = threading.Lock()
rate_events: dict[int, list[float]] = {}


def is_owner(user_id: int) -> bool:
    return OWNER_ID is not None and user_id == OWNER_ID


def allowed_rate(user_id: int, limit: int = 15, window: float = 10.0) -> bool:
    now = time.monotonic()
    with rate_lock:
        recent = [stamp for stamp in rate_events.get(user_id, []) if now - stamp < window]
        if len(recent) >= limit:
            rate_events[user_id] = recent
            return False
        recent.append(now)
        rate_events[user_id] = recent
        if len(rate_events) > 2000:
            for uid in list(rate_events)[:500]:
                if not rate_events[uid]:
                    del rate_events[uid]
        return True


def user_is_blocked(user_id: int) -> bool:
    assert db
    return db.one("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,)) is not None


def maintenance_enabled() -> bool:
    assert db
    return db.scalar("SELECT value FROM settings WHERE key='maintenance_mode'") == "1"


def upsert_user(user: Any) -> None:
    assert db
    now = utc_now()
    db.execute(
        """
        INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, first_name=excluded.first_name,
          last_name=excluded.last_name, last_seen=excluded.last_seen
        """,
        (
            user.id,
            user.username,
            user.first_name or "",
            user.last_name or "",
            now,
            now,
        ),
    )


def file_label(row: sqlite3.Row) -> str:
    name = row["file_name"] or row["file_type"]
    return str(name)[:70]


def channel_link(row: sqlite3.Row) -> str:
    link = str(row["invite_link"] or "")
    return link if link.startswith(("http://", "https://", "tg://")) else ""


def owner_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Statistics", callback_data="owner:stats"),
                InlineKeyboardButton("👥 Users", callback_data="owner:users"),
            ],
            [
                InlineKeyboardButton("📂 User Uploads", callback_data="owner:uploads"),
                InlineKeyboardButton("📁 All Files", callback_data="owner:files"),
            ],
            [
                InlineKeyboardButton("🌐 Global Force Sub", callback_data="owner:global"),
                InlineKeyboardButton("📢 Broadcast", callback_data="owner:broadcast"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="owner:settings"),
                InlineKeyboardButton("🛠 Maintenance", callback_data="owner:maintenance"),
            ],
            [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
        ]
    )


def user_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 Upload File", callback_data="upload")],
            [
                InlineKeyboardButton("📁 My Files", callback_data="myfiles:0"),
                InlineKeyboardButton("📢 My Channels", callback_data="mychannels"),
            ],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
    )


async def send_menu(message: Any, user_id: int) -> None:
    text = "📂 FILE STORE\n\nUpload files and share them with a secure link."
    markup = user_menu_keyboard()
    if is_owner(user_id):
        markup = InlineKeyboardMarkup(
            list(markup.inline_keyboard)
            + [[InlineKeyboardButton("👑 Owner Panel", callback_data="owner:menu")]]
        )
    await message.reply_text(text, reply_markup=markup)


async def blocked_or_maintenance(message: Any, user_id: int) -> bool:
    if user_is_blocked(user_id):
        await message.reply_text("Your access to this bot has been blocked.")
        return True
    if maintenance_enabled() and not is_owner(user_id):
        await message.reply_text("🛠 The bot is temporarily under maintenance. Please try again later.")
        return True
    return False


def media_details(message: Any) -> tuple[str, str, str] | None:
    if message.document:
        return "document", message.document.file_id, message.document.file_name or f"document_{message.message_id}"
    if message.video:
        return "video", message.video.file_id, message.video.file_name or f"video_{message.message_id}.mp4"
    if message.audio:
        return "audio", message.audio.file_id, message.audio.file_name or f"audio_{message.message_id}"
    if message.photo:
        photo = message.photo[-1]
        return "photo", photo.file_id, f"photo_{message.message_id}.jpg"
    if message.animation:
        return "animation", message.animation.file_id, message.animation.file_name or f"animation_{message.message_id}.gif"
    if message.voice:
        return "voice", message.voice.file_id, f"voice_{message.message_id}.ogg"
    return None


def new_file_token() -> str:
    assert db
    while True:
        token = secrets.token_urlsafe(18)
        if db.one("SELECT 1 FROM files WHERE token=?", (token,)) is None:
            return token


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not allowed_rate(user.id):
        await message.reply_text("Please slow down for a moment.")
        return
    upsert_user(user)
    if await blocked_or_maintenance(message, user.id):
        return
    details = media_details(message)
    if not details:
        return
    file_type, file_id, file_name = details
    assert db
    token = new_file_token()
    db.execute(
        """
        INSERT INTO files(owner_user_id, token, source_chat_id, telegram_message_id,
                          file_id, file_type, file_name, caption, enabled, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            user.id,
            token,
            str(message.chat_id),
            message.message_id,
            file_id,
            file_type,
            file_name,
            message.caption or "",
            utc_now(),
        ),
    )
    db.stat("total_uploads")
    link = f"https://t.me/{bot_username}?start=file_{token}"
    context.user_data.pop("awaiting_upload", None)
    await message.reply_text(
        f"✅ File saved.\n\n📄 {file_name}\n🔗 {link}",
        disable_web_page_preview=True,
    )


def required_channels(user_id: int) -> list[sqlite3.Row]:
    assert db
    rows = db.all(
        "SELECT * FROM global_channels WHERE enabled=1 ORDER BY id"
    ) + db.all(
        "SELECT * FROM user_channels WHERE user_id=? AND enabled=1 ORDER BY id",
        (user_id,),
    )
    seen: set[str] = set()
    unique: list[sqlite3.Row] = []
    for row in rows:
        if str(row["chat_id"]) not in seen:
            seen.add(str(row["chat_id"]))
            unique.append(row)
    return unique


async def member_of(bot: Bot, channel: sqlite3.Row, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel["chat_id"], user_id=user_id)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return True
        return member.status == ChatMemberStatus.RESTRICTED and bool(member.is_member)
    except (BadRequest, Forbidden, NetworkError, TelegramError):
        return False


def join_markup(channels: list[sqlite3.Row], token: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        link = channel_link(channel)
        if link:
            rows.append([InlineKeyboardButton(f"📢 {str(channel['title'])[:40]}", url=link)])
        else:
            rows.append([InlineKeyboardButton(f"📢 {str(channel['title'])[:40]}", callback_data="noop")])
    rows.append([InlineKeyboardButton("✅ Check Membership", callback_data=f"check:{token}")])
    return InlineKeyboardMarkup(rows)


async def copy_stored_file(bot: Bot, row: sqlite3.Row, user_id: int) -> bool:
    try:
        await bot.copy_message(
            chat_id=user_id,
            from_chat_id=row["source_chat_id"],
            message_id=row["telegram_message_id"],
        )
        return True
    except (BadRequest, Forbidden, NetworkError, TelegramError):
        try:
            kwargs = {"chat_id": user_id, "caption": row["caption"] or ""}
            methods = {
                "document": bot.send_document,
                "video": bot.send_video,
                "audio": bot.send_audio,
                "photo": bot.send_photo,
                "animation": bot.send_animation,
                "voice": bot.send_voice,
            }
            sender = methods.get(row["file_type"])
            if sender is None:
                return False
            field = "photo" if row["file_type"] == "photo" else row["file_type"]
            kwargs[field] = row["file_id"]
            await sender(**kwargs)
            return True
        except (BadRequest, Forbidden, NetworkError, TelegramError):
            return False


async def open_file(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    upsert_user(user)
    assert db
    row = db.one("SELECT * FROM files WHERE token=? AND enabled=1", (token,))
    if not row:
        await message.reply_text("That file link is invalid, disabled, or expired.")
        return
    if user_is_blocked(user.id):
        await message.reply_text("Your access to this bot has been blocked.")
        return
    if maintenance_enabled() and not is_owner(user.id):
        await message.reply_text("🛠 The bot is temporarily under maintenance. Please try again later.")
        return
    db.stat("link_opens")
    missing: list[sqlite3.Row] = []
    for channel in required_channels(row["owner_user_id"]):
        if not await member_of(context.bot, channel, user.id):
            missing.append(channel)
    if missing:
        db.stat("failed_verifications")
        names = "\n".join(f"• {html.escape(str(channel['title']))}" for channel in missing)
        await message.reply_text(
            "🔒 This file is locked.\n\nJoin every required channel, then press Check Membership.\n\n"
            f"Missing channels:\n{names}",
            parse_mode="HTML",
            reply_markup=join_markup(missing, token),
        )
        return
    db.stat("successful_verifications")
    if not await copy_stored_file(context.bot, row, user.id):
        await message.reply_text("The original Telegram file is no longer available.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not allowed_rate(user.id):
        await message.reply_text("Please slow down for a moment.")
        return
    payload = context.args[0] if context.args else ""
    if payload.startswith("file_") and len(payload) > 5:
        await open_file(update, context, payload[5:])
        return
    upsert_user(user)
    if await blocked_or_maintenance(message, user.id):
        return
    await send_menu(message, user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    upsert_user(user)
    if await blocked_or_maintenance(message, user.id):
        return
    await message.reply_text(
        "ℹ️ Help\n\n"
        "Send a video, document, audio, photo, animation, or voice message to save it.\n"
        "The bot returns a secure deep link that you can share.\n\n"
        "Use My Files to manage your own uploads. Force-Subscribe channels are checked "
        "with Telegram before a file is delivered."
    )


async def owner_only_callback(query: Any) -> bool:
    user = query.from_user
    if not is_owner(user.id):
        await query.answer("Owner access only.", show_alert=True)
        return False
    return True


async def show_my_files(query: Any, user_id: int, page: int = 0) -> None:
    assert db
    per_page = 5
    total = int(db.scalar("SELECT COUNT(*) FROM files WHERE owner_user_id=?", (user_id,)) or 0)
    rows = db.all(
        "SELECT * FROM files WHERE owner_user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, per_page, page * per_page),
    )
    text = f"📁 My Files ({total})\n\n"
    if not rows:
        text += "No files yet. Send a supported file to save it."
    else:
        for row in rows:
            status = "enabled" if row["enabled"] else "disabled"
            text += f"#{row['id']} · {file_label(row)} · {status}\n{row['created_at']}\n"
            text += f"https://t.me/{bot_username}?start=file_{row['token']}\n\n"
    buttons = [
        [InlineKeyboardButton(f"📄 {file_label(row)}", callback_data=f"myfile:{row['id']}")]
        for row in rows
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"myfiles:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"myfiles:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


async def show_my_file(query: Any, user_id: int, file_id: int) -> None:
    assert db
    row = db.one("SELECT * FROM files WHERE id=? AND owner_user_id=?", (file_id, user_id))
    if not row:
        await query.answer("File not found.", show_alert=True)
        return
    link = f"https://t.me/{bot_username}?start=file_{row['token']}"
    status = "enabled" if row["enabled"] else "disabled"
    await query.edit_message_text(
        f"📄 {file_label(row)}\nType: {row['file_type']}\nStatus: {status}\n"
        f"Uploaded: {row['created_at']}\n\n{link}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗑 Delete", callback_data=f"delask:{file_id}")],
                [InlineKeyboardButton("🔁 Enable/Disable", callback_data=f"toggle:{file_id}")],
                [InlineKeyboardButton("⬅️ My Files", callback_data="myfiles:0")],
            ]
        ),
    )


async def show_user_channels(query: Any, user_id: int) -> None:
    assert db
    rows = db.all("SELECT * FROM user_channels WHERE user_id=? ORDER BY id", (user_id,))
    text = f"📢 My Channels ({len(rows)}/6)\n\n"
    if rows:
        for row in rows:
            state = "enabled" if row["enabled"] else "disabled"
            text += f"#{row['id']} · {row['title']} · {state}\n"
    else:
        text += "No personal channels configured."
    buttons = [
        [
            InlineKeyboardButton(
                f"{'🔴' if row['enabled'] else '🟢'} {str(row['title'])[:30]}",
                callback_data=f"uchannel:{row['id']}",
            )
        ]
        for row in rows
    ]
    if len(rows) < 6:
        buttons.append([InlineKeyboardButton("➕ Add Channel", callback_data="uchannel:add")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="menu")])
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


def parse_channel_input(value: str) -> tuple[str, str]:
    value = value.strip()
    parts = value.split("|", 1)
    target = parts[0].strip()
    invite = parts[1].strip() if len(parts) == 2 else target
    if target.startswith("https://t.me/") or target.startswith("http://t.me/"):
        target = target.rsplit("/", 1)[-1]
        if target.startswith("+"):
            raise ValueError("Private invite links need the numeric chat ID before |.")
        target = "@" + target
    if target and re.fullmatch(r"[A-Za-z0-9_]{4,}", target) and not target.startswith("@"):
        target = "@" + target
    if not target or target == "@":
        raise ValueError("Enter a public @username or numeric chat ID.")
    if not invite.startswith(("http://", "https://", "tg://")):
        if invite.startswith("@"):
            invite = f"https://t.me/{invite[1:]}"
        elif invite.startswith("-100") or invite.isdigit():
            invite = ""
    return target, invite


async def validate_channel(bot: Bot, value: str) -> tuple[str, str, str]:
    target, invite = parse_channel_input(value)
    chat = await bot.get_chat(target)
    if chat.type not in ("channel", "supergroup"):
        raise ValueError("That chat is not a channel or supergroup.")
    me = await bot.get_me()
    member = await bot.get_chat_member(chat.id, me.id)
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise ValueError("The bot must be an administrator in that channel.")
    title = chat.title or str(target)
    if not invite:
        invite = getattr(chat, "invite_link", None) or ""
        if not invite and getattr(chat, "username", None):
            invite = f"https://t.me/{chat.username}"
    return str(chat.id), title, invite


async def show_global_channels(query: Any) -> None:
    assert db
    rows = db.all("SELECT * FROM global_channels ORDER BY id")
    text = f"🌐 Global Force Sub ({len(rows)}/6)\n\n"
    text += "\n".join(
        f"#{row['id']} · {row['title']} · {'enabled' if row['enabled'] else 'disabled'}"
        for row in rows
    ) or "No global channels configured."
    buttons = [
        [
            InlineKeyboardButton(
                f"{'🔴' if row['enabled'] else '🟢'} {str(row['title'])[:30]}",
                callback_data=f"gchannel:{row['id']}",
            )
        ]
        for row in rows
    ]
    if len(rows) < 6:
        buttons.append([InlineKeyboardButton("➕ Add Global Channel", callback_data="gchannel:add")])
    buttons.append([InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")])
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


async def show_stats(query: Any) -> None:
    assert db
    def count(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(db.scalar(sql, params) or 0)
    def stat(key: str) -> int:
        return count("SELECT value FROM statistics WHERE key=?", (key,))
    text = (
        "📊 Statistics\n\n"
        f"Total users: {count('SELECT COUNT(*) FROM users')}\n"
        f"Total files: {count('SELECT COUNT(*) FROM files')}\n"
        f"Total uploads: {stat('total_uploads')}\n"
        f"Link opens: {stat('link_opens')}\n"
        f"Successful verifications: {stat('successful_verifications')}\n"
        f"Failed verifications: {stat('failed_verifications')}\n"
        f"Global channels: {count('SELECT COUNT(*) FROM global_channels')}\n"
        f"User channels: {count('SELECT COUNT(*) FROM user_channels')}"
    )
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")]])
    )


def format_owner_file(row: sqlite3.Row) -> str:
    username = f"@{row['username']}" if row["username"] else "(no username)"
    link = f"https://t.me/{bot_username}?start=file_{row['token']}"
    return (
        f"#{row['id']} · {file_label(row)}\n"
        f"User: {username} ({row['owner_user_id']})\n"
        f"Type: {row['file_type']} · {row['created_at']}\n{link}"
    )


async def show_owner_files(query: Any, search: str = "", page: int = 0) -> None:
    assert db
    per_page = 6
    pattern = f"%{search}%"
    where = (
        "WHERE f.file_name LIKE ? OR u.username LIKE ? OR CAST(f.owner_user_id AS TEXT) LIKE ?"
        if search else ""
    )
    params: tuple[Any, ...] = ((pattern, pattern, pattern) if search else ())
    total = int(db.scalar(
        f"SELECT COUNT(*) FROM files f JOIN users u ON u.user_id=f.owner_user_id {where}", params
    ) or 0)
    rows = db.all(
        f"""
        SELECT f.*, u.username FROM files f JOIN users u ON u.user_id=f.owner_user_id
        {where} ORDER BY f.id DESC LIMIT ? OFFSET ?
        """,
        params + (per_page, page * per_page),
    )
    title = "📂 User Uploads" if not search else f"🔎 Upload Search: {search}"
    text = title + f" ({total})\n\n" + (
        "\n\n".join(format_owner_file(row) for row in rows) or "No files found."
    )
    buttons = [
        [InlineKeyboardButton(f"Manage #{row['id']}", callback_data=f"adminfile:{row['id']}")]
        for row in rows
    ]
    nav: list[InlineKeyboardButton] = []
    if page:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"ownerfiles:{page - 1}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"ownerfiles:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔎 Search", callback_data="owner:searchfiles")])
    buttons.append([InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")])
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


async def show_users(query: Any, search: str = "") -> None:
    assert db
    pattern = f"%{search}%"
    where = (
        "WHERE CAST(u.user_id AS TEXT) LIKE ? OR u.username LIKE ? OR u.first_name LIKE ?"
        if search else ""
    )
    params: tuple[Any, ...] = ((pattern, pattern, pattern) if search else ())
    rows = db.all(
        f"""
        SELECT u.*, COUNT(f.id) AS file_count
        FROM users u LEFT JOIN files f ON f.owner_user_id=u.user_id
        {where} GROUP BY u.user_id ORDER BY u.last_seen DESC LIMIT 30
        """,
        params,
    )
    text = ("🔎 User Search\n\n" if search else "👥 Users\n\n") + (
        "\n".join(
            f"{'🚫 ' if user_is_blocked(row['user_id']) else ''}"
            f"{row['first_name'] or '(unnamed)'} "
            f"@{row['username'] or '-'} · {row['user_id']} · {row['file_count']} files"
            for row in rows
        )
        or "No users found."
    )
    buttons = [
        [InlineKeyboardButton(f"{str(row['first_name'] or row['user_id'])[:28]}", callback_data=f"user:{row['user_id']}")]
        for row in rows
    ]
    buttons += [
        [InlineKeyboardButton("🔎 Search", callback_data="owner:searchusers")],
        [InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")],
    ]
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


async def show_user_detail(query: Any, target_id: int) -> None:
    assert db
    row = db.one("SELECT * FROM users WHERE user_id=?", (target_id,))
    if not row:
        await query.answer("User not found.", show_alert=True)
        return
    files = int(db.scalar("SELECT COUNT(*) FROM files WHERE owner_user_id=?", (target_id,)) or 0)
    channels = db.all("SELECT * FROM user_channels WHERE user_id=? ORDER BY id", (target_id,))
    state = "blocked" if user_is_blocked(target_id) else "active"
    text = (
        f"👤 {row['first_name'] or '(unnamed)'}\nUsername: @{row['username'] or '-'}\n"
        f"Telegram ID: {target_id}\nStatus: {state}\nFiles: {files}\n\n"
        "Channels:\n" + ("\n".join(f"• {c['title']}" for c in channels) or "None")
    )
    action = "unblock" if state == "blocked" else "block"
    buttons = [
        [InlineKeyboardButton(f"{'✅ Unblock' if action == 'unblock' else '🚫 Block'}", callback_data=f"user:{action}:{target_id}")],
        [InlineKeyboardButton("⬅️ Users", callback_data="owner:users")],
    ]
    await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(buttons))


async def show_admin_file(query: Any, file_id: int) -> None:
    assert db
    row = db.one(
        "SELECT f.*, u.username FROM files f JOIN users u ON u.user_id=f.owner_user_id WHERE f.id=?",
        (file_id,),
    )
    if not row:
        await query.answer("File not found.", show_alert=True)
        return
    await query.edit_message_text(
        "📄 File details\n\n" + format_owner_file(row) + f"\nStatus: {'enabled' if row['enabled'] else 'disabled'}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗑 Delete", callback_data=f"adminfiledelask:{file_id}")],
                [InlineKeyboardButton("🔁 Enable/Disable", callback_data=f"adminfiletoggle:{file_id}")],
                [InlineKeyboardButton("⬅️ All Files", callback_data="owner:files")],
            ]
        ),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return
    if not allowed_rate(user.id, limit=10):
        await message.reply_text("Please slow down for a moment.")
        return
    upsert_user(user)
    if await blocked_or_maintenance(message, user.id):
        return
    state = context.user_data.get("state")
    value = message.text.strip()
    if state == "add_user_channel":
        context.user_data.pop("state", None)
        try:
            chat_id, title, invite = await validate_channel(context.bot, value)
            assert db
            if db.scalar("SELECT COUNT(*) FROM user_channels WHERE user_id=?", (user.id,)) >= 6:
                raise ValueError("You already have six personal channels.")
            db.execute(
                "INSERT INTO user_channels(user_id, chat_id, title, invite_link, enabled, created_at) VALUES(?, ?, ?, ?, 1, ?)",
                (user.id, chat_id, title, invite, utc_now()),
            )
            await message.reply_text("✅ Personal channel added.", reply_markup=user_menu_keyboard())
        except sqlite3.IntegrityError:
            await message.reply_text("That channel is already configured.")
        except (ValueError, BadRequest, Forbidden, NetworkError, TelegramError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "Telegram could not validate it."
            await message.reply_text(f"Could not add channel: {reason}")
        return
    if state == "add_global_channel" and is_owner(user.id):
        context.user_data.pop("state", None)
        try:
            chat_id, title, invite = await validate_channel(context.bot, value)
            assert db
            if db.scalar("SELECT COUNT(*) FROM global_channels") >= 6:
                raise ValueError("There are already six global channels.")
            db.execute(
                "INSERT INTO global_channels(chat_id, title, invite_link, enabled, created_at) VALUES(?, ?, ?, 1, ?)",
                (chat_id, title, invite, utc_now()),
            )
            await message.reply_text("✅ Global channel added.", reply_markup=owner_panel_keyboard())
        except sqlite3.IntegrityError:
            await message.reply_text("That channel is already configured.")
        except (ValueError, BadRequest, Forbidden, NetworkError, TelegramError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "Telegram could not validate it."
            await message.reply_text(f"Could not add channel: {reason}")
        return
    if state == "owner_search_users" and is_owner(user.id):
        context.user_data.pop("state", None)
        await show_users_as_message(message, value)
        return
    if state == "owner_search_files" and is_owner(user.id):
        context.user_data.pop("state", None)
        await show_files_as_message(message, value)
        return
    if state == "broadcast" and is_owner(user.id):
        context.user_data.pop("state", None)
        context.user_data["broadcast_text"] = value
        await message.reply_text(
            f"Broadcast preview:\n\n{value[:3500]}\n\nSend it?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Send Broadcast", callback_data="broadcast:confirm")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="owner:menu")],
                ]
            ),
        )
        return
    await send_menu(message, user.id)


async def show_users_as_message(message: Any, search: str) -> None:
    assert db
    pattern = f"%{search}%"
    rows = db.all(
        """
        SELECT u.*, COUNT(f.id) AS file_count FROM users u
        LEFT JOIN files f ON f.owner_user_id=u.user_id
        WHERE CAST(u.user_id AS TEXT) LIKE ? OR u.username LIKE ? OR u.first_name LIKE ?
        GROUP BY u.user_id ORDER BY u.last_seen DESC LIMIT 30
        """,
        (pattern, pattern, pattern),
    )
    buttons = [
        [InlineKeyboardButton(f"{str(row['first_name'] or row['user_id'])[:28]}", callback_data=f"user:{row['user_id']}")]
        for row in rows
    ]
    buttons.append([InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")])
    await message.reply_text(
        "👥 User results\n\n" + ("\n".join(
            f"{row['first_name'] or '(unnamed)'} @{row['username'] or '-'} · {row['user_id']} · {row['file_count']} files"
            for row in rows
        ) or "No users found."),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_files_as_message(message: Any, search: str) -> None:
    assert db
    pattern = f"%{search}%"
    rows = db.all(
        """
        SELECT f.*, u.username FROM files f JOIN users u ON u.user_id=f.owner_user_id
        WHERE f.file_name LIKE ? OR u.username LIKE ? OR CAST(f.owner_user_id AS TEXT) LIKE ?
        ORDER BY f.id DESC LIMIT 20
        """,
        (pattern, pattern, pattern),
    )
    buttons = [
        [InlineKeyboardButton(f"Manage #{row['id']}", callback_data=f"adminfile:{row['id']}")]
        for row in rows
    ]
    buttons.append([InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")])
    await message.reply_text(
        "📂 File results\n\n" + ("\n\n".join(format_owner_file(row) for row in rows) or "No files found."),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def broadcast(context: ContextTypes.DEFAULT_TYPE, text: str) -> tuple[int, int]:
    assert db
    sent = failed = 0
    users = db.all("SELECT user_id FROM users")
    for row in users:
        target = int(row["user_id"])
        if user_is_blocked(target):
            continue
        try:
            await context.bot.send_message(target, text)
            sent += 1
            await asyncio.sleep(0.04)
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await context.bot.send_message(target, text)
                sent += 1
            except (TelegramError, NetworkError):
                failed += 1
        except (Forbidden, BadRequest, NetworkError, TelegramError):
            failed += 1
    return sent, failed


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    if not allowed_rate(user.id):
        await query.answer("Please slow down.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    if data.startswith("owner:") or data.startswith(
        ("gchannel:", "gtoggle:", "gdel:", "user:", "adminfile", "ownerfiles:", "broadcast:")
    ):
        if not await owner_only_callback(query):
            return
    elif await blocked_or_maintenance(query.message, user.id):
        return
    if data == "noop":
        return
    if data == "menu":
        if await blocked_or_maintenance(query.message, user.id):
            return
        await send_menu(query.message, user.id)
    elif data == "upload":
        if await blocked_or_maintenance(query.message, user.id):
            return
        context.user_data["awaiting_upload"] = True
        await query.edit_message_text(
            "📤 Send a video, document, audio, photo, animation, or voice message now.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]),
        )
    elif data.startswith("myfiles:"):
        if await blocked_or_maintenance(query.message, user.id):
            return
        await show_my_files(query, user.id, int(data.split(":")[1]))
    elif data.startswith("myfile:"):
        if await blocked_or_maintenance(query.message, user.id):
            return
        await show_my_file(query, user.id, int(data.split(":")[1]))
    elif data.startswith("delask:"):
        assert db
        file_id = int(data.split(":")[1])
        row = db.one("SELECT id FROM files WHERE id=? AND owner_user_id=?", (file_id, user.id))
        if not row:
            await query.answer("File not found.", show_alert=True)
            return
        await query.edit_message_text(
            "Delete this file and invalidate its link?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🗑 Yes, delete", callback_data=f"delete:{file_id}")],
                    [InlineKeyboardButton("Cancel", callback_data=f"myfile:{file_id}")],
                ]
            ),
        )
    elif data.startswith("delete:"):
        assert db
        file_id = int(data.split(":")[1])
        db.execute("DELETE FROM files WHERE id=? AND owner_user_id=?", (file_id, user.id))
        await query.edit_message_text("✅ File deleted.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ My Files", callback_data="myfiles:0")]]
        ))
    elif data.startswith("toggle:"):
        assert db
        file_id = int(data.split(":")[1])
        db.execute(
            "UPDATE files SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND owner_user_id=?",
            (file_id, user.id),
        )
        await show_my_file(query, user.id, file_id)
    elif data == "mychannels":
        if await blocked_or_maintenance(query.message, user.id):
            return
        await show_user_channels(query, user.id)
    elif data == "uchannel:add":
        if await blocked_or_maintenance(query.message, user.id):
            return
        context.user_data["state"] = "add_user_channel"
        await query.edit_message_text(
            "Send a public @channel username or numeric chat ID.\n"
            "For a private channel, send: numeric_chat_id|invite_link\n\n"
            "The bot must be an administrator there.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="mychannels")]]),
        )
    elif data.startswith("uchannel:"):
        assert db
        arg = data.split(":")[1]
        if arg == "add":
            return
        channel_id = int(arg)
        row = db.one("SELECT * FROM user_channels WHERE id=? AND user_id=?", (channel_id, user.id))
        if not row:
            await query.answer("Channel not found.", show_alert=True)
            return
        await query.edit_message_text(
            f"📢 {row['title']}\nStatus: {'enabled' if row['enabled'] else 'disabled'}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Enable/Disable", callback_data=f"utoggle:{channel_id}")],
                    [InlineKeyboardButton("🗑 Remove", callback_data=f"udel:{channel_id}")],
                    [InlineKeyboardButton("⬅️ My Channels", callback_data="mychannels")],
                ]
            ),
        )
    elif data.startswith("utoggle:"):
        assert db
        channel_id = int(data.split(":")[1])
        db.execute(
            "UPDATE user_channels SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND user_id=?",
            (channel_id, user.id),
        )
        await show_user_channels(query, user.id)
    elif data.startswith("udel:"):
        assert db
        channel_id = int(data.split(":")[1])
        db.execute("DELETE FROM user_channels WHERE id=? AND user_id=?", (channel_id, user.id))
        await show_user_channels(query, user.id)
    elif data == "help":
        if await blocked_or_maintenance(query.message, user.id):
            return
        await query.edit_message_text(
            "ℹ️ Send any supported media to save it and receive a shareable link.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu")]]),
        )
    elif data.startswith("check:"):
        token = data.split(":", 1)[1]
        await open_file(update, context, token)
    elif data == "owner:menu":
        await query.edit_message_text("👑 OWNER PANEL", reply_markup=owner_panel_keyboard())
    elif data == "owner:stats":
        await show_stats(query)
    elif data == "owner:users":
        await show_users(query)
    elif data == "owner:uploads" or data == "owner:files":
        await show_owner_files(query)
    elif data.startswith("ownerfiles:"):
        await show_owner_files(query, page=int(data.split(":")[1]))
    elif data == "owner:searchusers":
        context.user_data["state"] = "owner_search_users"
        await query.edit_message_text("Send a Telegram ID, username, or name to search.")
    elif data == "owner:searchfiles":
        context.user_data["state"] = "owner_search_files"
        await query.edit_message_text("Send a file name, username, or Telegram ID to search.")
    elif data.startswith("user:"):
        parts = data.split(":")
        if len(parts) == 2:
            await show_user_detail(query, int(parts[1]))
        else:
            target_id = int(parts[2])
            if parts[1] == "block":
                db.execute("INSERT OR IGNORE INTO blocked_users(user_id, created_at) VALUES(?, ?)", (target_id, utc_now()))
            else:
                db.execute("DELETE FROM blocked_users WHERE user_id=?", (target_id,))
            await show_user_detail(query, target_id)
    elif data.startswith("adminfile:"):
        await show_admin_file(query, int(data.split(":")[1]))
    elif data.startswith("adminfiledelask:"):
        file_id = int(data.split(":")[1])
        await query.edit_message_text(
            "Delete this file for its owner and invalidate its link?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🗑 Yes, delete", callback_data=f"adminfiledelete:{file_id}")],
                    [InlineKeyboardButton("Cancel", callback_data=f"adminfile:{file_id}")],
                ]
            ),
        )
    elif data.startswith("adminfiledelete:"):
        db.execute("DELETE FROM files WHERE id=?", (int(data.split(":")[1]),))
        await query.edit_message_text("✅ File deleted.", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ All Files", callback_data="owner:files")]]
        ))
    elif data.startswith("adminfiletoggle:"):
        file_id = int(data.split(":")[1])
        db.execute("UPDATE files SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (file_id,))
        await show_admin_file(query, file_id)
    elif data == "owner:global":
        await show_global_channels(query)
    elif data == "gchannel:add":
        context.user_data["state"] = "add_global_channel"
        await query.edit_message_text(
            "Send @channel, numeric chat ID, or numeric_chat_id|invite_link.\n"
            "The bot must be an administrator there.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="owner:global")]]),
        )
    elif data.startswith("gchannel:"):
        assert db
        channel_id = int(data.split(":")[1])
        row = db.one("SELECT * FROM global_channels WHERE id=?", (channel_id,))
        if not row:
            await query.answer("Channel not found.", show_alert=True)
            return
        await query.edit_message_text(
            f"🌐 {row['title']}\nStatus: {'enabled' if row['enabled'] else 'disabled'}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔁 Enable/Disable", callback_data=f"gtoggle:{channel_id}")],
                    [InlineKeyboardButton("🗑 Remove", callback_data=f"gdel:{channel_id}")],
                    [InlineKeyboardButton("⬅️ Global Channels", callback_data="owner:global")],
                ]
            ),
        )
    elif data.startswith("gtoggle:"):
        db.execute(
            "UPDATE global_channels SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
            (int(data.split(":")[1]),),
        )
        await show_global_channels(query)
    elif data.startswith("gdel:"):
        db.execute("DELETE FROM global_channels WHERE id=?", (int(data.split(":")[1]),))
        await show_global_channels(query)
    elif data == "owner:broadcast":
        context.user_data["state"] = "broadcast"
        await query.edit_message_text(
            "Send the message to broadcast to registered users.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="owner:menu")]]),
        )
    elif data == "broadcast:confirm":
        text = context.user_data.pop("broadcast_text", "")
        if not text:
            await query.edit_message_text("Broadcast cancelled.", reply_markup=owner_panel_keyboard())
            return
        await query.edit_message_text("Broadcasting safely; this can take a moment...")
        sent, failed = await broadcast(context, text)
        await query.message.reply_text(
            f"📢 Broadcast complete.\nSent: {sent}\nFailed: {failed}",
            reply_markup=owner_panel_keyboard(),
        )
    elif data == "owner:settings":
        await query.edit_message_text(
            f"⚙️ Settings\n\nBot username: @{bot_username}\nDatabase: SQLite\nPolling: enabled\n"
            f"Maintenance: {'enabled' if maintenance_enabled() else 'disabled'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Owner Panel", callback_data="owner:menu")]]),
        )
    elif data == "owner:maintenance":
        new_value = "0" if maintenance_enabled() else "1"
        db.execute(
            "INSERT INTO settings(key, value) VALUES('maintenance_mode', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (new_value,),
        )
        await query.edit_message_text(
            f"🛠 Maintenance mode is now {'enabled' if new_value == '1' else 'disabled'}.",
            reply_markup=owner_panel_keyboard(),
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, RetryAfter):
        logger.warning("Telegram requested a retry after %s seconds", error.retry_after)
    else:
        logger.error("Unhandled bot error: %s", type(error).__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/", "/health", "/healthz"):
            body = b"ok\n"
            self.send_response(200)
        else:
            body = b"not found\n"
            self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_health_server() -> None:
    try:
        port = int(os.getenv("PORT", "8080"))
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        threading.Thread(target=server.serve_forever, daemon=True, name="health-server").start()
        logger.info("Health server listening on port %s", port)
    except (OSError, ValueError):
        logger.warning("Health server could not start; continuing with polling")


async def post_init(application: Application) -> None:
    global bot_username
    me = await application.bot.get_me()
    bot_username = me.username or bot_username
    assert db
    db.execute(
        "INSERT INTO settings(key, value) VALUES('bot_username', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (bot_username,),
    )
    await application.bot.delete_webhook(drop_pending_updates=False)
    logger.info("Bot started as @%s", bot_username)


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    media_filter = (
        filters.Document.ALL
        | filters.VIDEO
        | filters.AUDIO
        | filters.PHOTO
        | filters.ANIMATION
        | filters.VOICE
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(media_filter, receive_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    global db
    if not BOT_TOKEN or OWNER_ID is None:
        print("Setup required: add BOT_TOKEN and numeric OWNER_ID as environment variables or Replit Secrets.")
        return
    db = Database(DB_PATH)
    start_health_server()
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()