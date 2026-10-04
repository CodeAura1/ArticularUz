import asyncio
import json
import logging
from datetime import datetime, timezone
import os
import re
import sqlite3
from os import remove
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

# Recommended: fetch token from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Set it as an environment variable before running the bot."
    )

WEBSITE_URL = "https://articular.uz"
WEBSITE_IMAGE = "LOGO ARTICULARUZ (2).png"

# Baserow connection
# Keep the secret token in an environment variable named BASEROW_TOKEN.
BASEROW_TABLE_ID = "1237476"
BASEROW_API_URL = (
    f"https://api.baserow.io/api/database/rows/table/"
    f"{BASEROW_TABLE_ID}/?user_field_names=true"
)
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN", "").strip()


LOCATIONS = {
    "Tashkent": "https://t.me/+your_tashkent_group_invite",
    "Samarkand": "https://t.me/+your_samarkand_group_invite",
    "Bukhara": "https://t.me/+your_bukhara_group_invite",
}

# ---------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# CONVERSATION STATES
# ---------------------------------------------------------------------

(
    MENU_SECTION,
    FULL_NAME,
    AGE,
    PHONE,
    USERNAME,
    LOCATION,
    EDIT_INFO_MENU,
) = range(7)


# ---------------------------------------------------------------------
# BASEROW INTEGRATION
# ---------------------------------------------------------------------

def _baserow_headers():
    return {
        "Authorization": f"Token {BASEROW_TOKEN}",
        "Content-Type": "application/json",
        # Helps avoid Cloudflare HTTP 403 / error code 1010.
        "User-Agent": "ArticularUzBot/1.0",
        "Accept": "application/json",
    }


def _baserow_payload(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
    registered_at: str | None = None,
):
    payload = {
        "Full Name": full_name,
        "Age": age,
        "Phone": phone,
        "Telegram Username": username,
        "Telegram ID": user_id,
        "Event Location": location,
    }

    # Only set the registration timestamp when creating a new row (or
    # when repairing an older row that has no timestamp). This prevents
    # profile edits from changing the original registration date.
    if registered_at:
        payload["Registered At"] = registered_at

    return payload


def _sync_user_to_baserow_sync(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):
    """
    Create the user's Baserow row if it does not exist.
    cg update the existing row.

    This keeps exactly one Baserow row per Telegram user and means
    profile edits are reflected online.
    """
    if not BASEROW_TOKEN:
        logger.warning(
            "BASEROW_TOKEN is not configured. "
            "Telegram registration was saved locally only."
        )
        return

    base_payload = _baserow_payload(
        user_id=user_id,
        username=username,
        full_name=full_name,
        age=age,
        phone=phone,
        location=location,
    )
    headers = _baserow_headers()

    try:
        # Find the existing row by Telegram ID.
        query = urllib_parse.urlencode(
            {
                "user_field_names": "true",
                "size": "1",
                "filter__Telegram ID__equal": str(user_id),
            }
        )
        lookup_url = (
            f"https://api.baserow.io/api/database/rows/table/"
            f"{BASEROW_TABLE_ID}/?{query}"
        )

        lookup_req = urllib_request.Request(
            lookup_url,
            method="GET",
            headers=headers,
        )

        with urllib_request.urlopen(lookup_req, timeout=15) as response:
            lookup_body = response.read().decode("utf-8")
            lookup_data = json.loads(lookup_body)

        results = lookup_data.get("results", [])

        if results:
            # Existing row: update it. Preserve the original registration
            # date so profile edits do not change it.
            existing_row = results[0]
            row_id = existing_row["id"]

            payload = dict(base_payload)
            if not existing_row.get("Registered At"):
                payload["Registered At"] = datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

            update_url = (
                f"https://api.baserow.io/api/database/rows/table/"
                f"{BASEROW_TABLE_ID}/{row_id}/?user_field_names=true"
            )

            update_req = urllib_request.Request(
                update_url,
                data=json.dumps(payload).encode("utf-8"),
                method="PATCH",
                headers=headers,
            )

            with urllib_request.urlopen(update_req, timeout=15) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(
                        f"Baserow returned HTTP {response.status}"
                    )

            logger.info(
                "Baserow user updated successfully: Telegram ID %s, row %s",
                user_id,
                row_id,
            )
            return

        # No existing row: create it with the registration timestamp.
        payload = dict(base_payload)
        payload["Registered At"] = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        create_req = urllib_request.Request(
            BASEROW_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )

        with urllib_request.urlopen(create_req, timeout=15) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Baserow returned HTTP {response.status}"
                )

        logger.info(
            "Baserow user created successfully: Telegram ID %s",
            user_id,
        )

    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "Baserow sync failed with HTTP %s: %s",
            exc.code,
            body,
        )
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        logger.error("Baserow sync failed: %s", exc)
    except Exception:
        logger.exception("Unexpected Baserow integration error")


async def sync_user_to_baserow(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):
    # Keep HTTP work outside the Telegram event loop.
    await asyncio.to_thread(
        _sync_user_to_baserow_sync,
        user_id,
        username,
        full_name,
        age,
        phone,
        location,
    )


# ---------------------------------------------------------------------
# DATABASE FUNCTIONS
# ---------------------------------------------------------------------

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "registrations.db",
)


def get_db_connection():
    # SQLite is more reliable under concurrent bot activity when WAL mode
    # and a busy timeout are enabled.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            age INTEGER,
            phone TEXT,
            location TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Add columns if an older database doesn't have them
    for col in [("phone", "TEXT"), ("location", "TEXT")]:
        try:
            cursor.execute(
                f"ALTER TABLE users ADD COLUMN {col[0]} {col[1]}"
            )
        except sqlite3.OperationalError:
            pass

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_registered_at
        ON users(registered_at)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_location
        ON users(location)
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS event_registrations (
            registration_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cancelled_at TIMESTAMP NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_registrations_user_id
        ON event_registrations(user_id)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_registrations_location
        ON event_registrations(location)
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_registrations_registered_at
        ON event_registrations(registered_at)
        """
    )

    conn.commit()
    conn.close()


def save_user(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO users
        (user_id, username, full_name, age, phone, location)
        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            age = excluded.age,
            phone = excluded.phone,
            location = excluded.location
        """,
        (
            user_id,
            username,
            full_name,
            age,
            phone,
            location,
        ),
    )

    conn.commit()
    conn.close()


def get_active_event_registration(user_id: int):
    """
    Return the user's current active event registration as:
    (registration_id, location), or None if the user is not registered.

    Cancelled registrations are ignored.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT registration_id, location
            FROM event_registrations
            WHERE user_id = ? AND cancelled_at IS NULL
            ORDER BY registration_id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        return cursor.fetchone()
    finally:
        conn.close()


def create_event_registration(user_id: int, location: str) -> int:
    """
    Store a historical event registration without changing the existing
    profile table semantics. Multiple registrations by the same user are
    allowed and are preserved.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO event_registrations (user_id, location)
            VALUES (?, ?)
            """,
            (user_id, location),
        )

        registration_id = cursor.lastrowid
        conn.commit()
        return registration_id
    finally:
        conn.close()


def cancel_latest_event_registration(user_id: int) -> bool:
    """
    Cancel the user's most recent active event registration.
    Returns True if an active registration was cancelled.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT registration_id
            FROM event_registrations
            WHERE user_id = ? AND cancelled_at IS NULL
            ORDER BY registration_id DESC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cursor.fetchone()

        if not row:
            return False

        cursor.execute(
            """
            UPDATE event_registrations
            SET cancelled_at = CURRENT_TIMESTAMP
            WHERE registration_id = ?
            """,
            (row[0],),
        )

        conn.commit()
        return True
    finally:
        conn.close()


def delete_user(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM users WHERE user_id = ?",
        (user_id,),
    )

    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            user_id,
            username,
            full_name,
            age,
            phone,
            location
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )

    user = cursor.fetchone()

    conn.close()

    return user


def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            user_id,
            username,
            full_name,
            age,
            phone,
            location,
            registered_at
        FROM users
        """
    )

    rows = cursor.fetchall()

    conn.close()

    return rows


def backup_database():
    """
    Create a lightweight SQLite backup before the bot starts.
    Keeps the latest few backups so accidental corruption or bad edits
    are easier to recover from.
    """
    if not os.path.exists(DB_PATH):
        return

    backup_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "db_backups",
    )
    os.makedirs(backup_dir, exist_ok=True)

    from datetime import datetime
    backup_name = datetime.now().strftime("registrations_%Y%m%d_%H%M%S.db")
    backup_path = os.path.join(backup_dir, backup_name)

    source = get_db_connection()
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    # Keep the newest 7 backups.
    backups = sorted(
        Path(backup_dir).glob("registrations_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[7:]:
        try:
            old_backup.unlink()
        except OSError:
            logger.warning("Could not remove old database backup: %s", old_backup)


# ---------------------------------------------------------------------
# KEYBOARDS
# ---------------------------------------------------------------------

def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("👤 Profile")],
            [
                KeyboardButton("🎯 Choose an event"),
                KeyboardButton("🌐 Our website"),
            ],
        ],
        resize_keyboard=True,
    )


def get_back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 Back")]],
        resize_keyboard=True,
    )


def get_choose_event_back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🔙 Back")]],
        resize_keyboard=True,
    )


def get_profile_register_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📝 Register"),
                KeyboardButton("🔙 Back"),
            ]
        ],
        resize_keyboard=True,
    )


def get_profile_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("✏️ Change profile info"),
                KeyboardButton("🔙 Back"),
            ]
        ],
        resize_keyboard=True,
    )


def get_profile_edit_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("1️⃣ Change Name"),
                KeyboardButton("2️⃣ Change Age"),
            ],
            [
                KeyboardButton("3️⃣ Change Contact"),
                KeyboardButton("4️⃣ Change Username"),
            ],
            [
                KeyboardButton("❌ Cancel Location"),
            ],
            [
                KeyboardButton("🔙 Back"),
            ],
        ],
        resize_keyboard=True,
    )


def get_go_back_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 Go back",
                    callback_data="go_back_to_menu",
                )
            ]
        ]
    )


def get_event_registration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Register", callback_data="register_event")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_event_preview")],
        ]
    )


def get_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Start",
                    callback_data="start_registration",
                )
            ]
        ]
    )


# ---------------------------------------------------------------------
# PERSONAL INFORMATION DISPLAY
# ---------------------------------------------------------------------

def format_user_info_message(user_data: dict) -> str:
    name = user_data.get("full_name", "N/A")
    age = user_data.get("age", "N/A")
    phone = user_data.get("phone", "N/A")
    username = user_data.get("telegram_username", "N/A")
    location = user_data.get("location", "N/A") or "N/A"

    return (
        "Here is your personal information:\n\n"
        f"👤 **Name:** {name}\n"
        f"🎂 **Age:** {age}\n"
        f"📞 **Phone:** {phone}\n"
        f"📱 **Username:** {username}\n"
        f"📍 **Event location:** {location}\n\n"
        "Use the buttons below to change your profile or return to the menu."
    )


# ---------------------------------------------------------------------
# /START
# ---------------------------------------------------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    # Clear temporary conversation data
    context.user_data.clear()

    await update.message.reply_text(
        "Welcome to ArticularUz! 🚀\n\n"
        "Where young innovators across Uzbekistan present, defend, "
        "and connect their scientific ideas with the global community!\n\n"
        "Press Start to continue.",
        reply_markup=get_start_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# /RESTART
# ---------------------------------------------------------------------

async def restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    # /restart shows exactly the same welcome screen as /start.
    return await start(update, context)


# ---------------------------------------------------------------------
# /MENU
# ---------------------------------------------------------------------

async def menu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user_id = update.effective_user.id
    existing_user = get_user(user_id)

    if not existing_user:
        # /menu is only available to registered users.
        await update.message.reply_text(
            "You are not registered.\n\n"
            "Please press /start to register."
        )
        return ConversationHandler.END

    (
        _,
        db_username,
        db_full_name,
        db_age,
        db_phone,
        db_location,
    ) = existing_user

    # Keep the same automatic Telegram username behavior used by Start.
    telegram_username = update.effective_user.username
    telegram_username = (
        f"@{telegram_username}" if telegram_username else ""
    )

    context.user_data["full_name"] = db_full_name
    context.user_data["age"] = db_age
    context.user_data["phone"] = db_phone
    context.user_data["telegram_username"] = (
        telegram_username or db_username or ""
    )
    context.user_data["location"] = db_location

    await update.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# START BUTTON
# ---------------------------------------------------------------------

async def start_button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    existing_user = get_user(user_id)

    # Remove the Start button from the original message.
    await query.edit_message_reply_markup(reply_markup=None)

    if existing_user:
        (
            _,
            db_username,
            db_full_name,
            db_age,
            db_phone,
            db_location,
        ) = existing_user

        # Always take the current Telegram username automatically.
        telegram_username = query.from_user.username
        telegram_username = (
            f"@{telegram_username}" if telegram_username else ""
        )

        context.user_data["full_name"] = db_full_name
        context.user_data["age"] = db_age
        context.user_data["phone"] = db_phone
        context.user_data["telegram_username"] = (
            telegram_username or db_username or ""
        )
        context.user_data["location"] = db_location

        await query.message.reply_text(
            "Welcome back! 👋\n\n"
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    # New user: start the existing registration flow immediately.
    context.user_data.clear()

    await query.message.reply_text(
        "Let's get you registered! 📝\n\n"
        "What's your full name?",
        reply_markup=ReplyKeyboardRemove(),
    )

    return FULL_NAME


# ---------------------------------------------------------------------
# FULL NAME
# ---------------------------------------------------------------------

async def full_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    name = update.message.text.strip()

    if len(name.split()) < 2:
        await update.message.reply_text(
            "Please enter your full name (first and last name)."
        )
        return FULL_NAME

    context.user_data["full_name"] = name

    # Editing existing information
    if context.user_data.get("editing_single_field"):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        await save_current_user(update, context)

        await update.message.reply_text(
            "Name updated successfully! ✅"
        )

        await update.message.reply_text(
            format_user_info_message(context.user_data),
            parse_mode="Markdown",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    await update.message.reply_text(
        f"Welcome, {name}! 👋\n\nHow old are you?"
    )

    return AGE


# ---------------------------------------------------------------------
# AGE
# ---------------------------------------------------------------------

async def age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    text = update.message.text.strip()

    if not text.isdigit() or not (5 <= int(text) <= 100):
        await update.message.reply_text(
            "Please enter a valid age as a number (e.g. 17)."
        )
        return AGE

    context.user_data["age"] = int(text)

    # Editing existing information
    if context.user_data.get("editing_single_field"):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        await save_current_user(update, context)

        await update.message.reply_text(
            "Age updated successfully! ✅"
        )

        await update.message.reply_text(
            format_user_info_message(context.user_data),
            parse_mode="Markdown",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    contact_keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📞 Share my contact",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "Please share your phone number using the button below:",
        reply_markup=contact_keyboard,
    )

    return PHONE


# ---------------------------------------------------------------------
# PHONE
# ---------------------------------------------------------------------

async def phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    phone_number = None

    if update.message.contact:
        phone_number = update.message.contact.phone_number

    elif update.message.text:
        text = update.message.text.strip()

        if re.match(r"^\+?\d{7,15}$", text):
            phone_number = text

    if not phone_number:
        await update.message.reply_text(
            "Please use the button below or type a valid phone number "
            "(e.g. +998901234567)."
        )
        return PHONE

    if not phone_number.startswith("+"):
        phone_number = "+" + phone_number

    context.user_data["phone"] = phone_number

    # Editing existing information
    if context.user_data.get("editing_single_field"):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        await save_current_user(update, context)

        await update.message.reply_text(
            "Phone number updated successfully! ✅",
            reply_markup=ReplyKeyboardRemove(),
        )

        await update.message.reply_text(
            format_user_info_message(context.user_data),
            parse_mode="Markdown",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    # Telegram provides the username automatically. The user does not
    # need to type it during registration.
    telegram_username = update.effective_user.username
    context.user_data["telegram_username"] = (
        f"@{telegram_username}" if telegram_username else ""
    )

    # Contact is now the final registration step.
    # Save the complete profile to SQLite and Baserow immediately.
    await save_current_user(update, context)

    await update.message.reply_text(
        "Registration completed successfully! ✅",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# USERNAME
# ---------------------------------------------------------------------

async def username(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    text = update.message.text.strip()

    if not text.startswith("@"):
        text = "@" + text

    context.user_data["telegram_username"] = text

    # Editing existing information
    if context.user_data.get("editing_single_field"):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        await save_current_user(update, context)

        await update.message.reply_text(
            "Username updated successfully! ✅"
        )

        await update.message.reply_text(
            format_user_info_message(context.user_data),
            parse_mode="Markdown",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    # Registration is complete here. Save the user first, then return
    # directly to the main menu. Event location is selected later from
    # "🎯 Choose an event".
    await save_current_user(update, context)

    await update.message.reply_text(
        "Registration completed successfully! ✅",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# BACK FROM PROFILE EDIT MENU
# ---------------------------------------------------------------------

async def profile_edit_back(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user = get_user(update.effective_user.id)

    if not user:
        await update.message.reply_text(
            "You don't have a profile.",
            reply_markup=get_profile_register_keyboard(),
        )
        return MENU_SECTION

    (
        _user_id,
        db_username,
        db_full_name,
        db_age,
        db_phone,
        db_location,
    ) = user

    profile_data = {
        "full_name": db_full_name,
        "age": db_age,
        "phone": db_phone,
        "telegram_username": db_username,
        "location": db_location,
    }
    context.user_data.update(profile_data)

    await update.message.reply_text(
        format_user_info_message(profile_data),
        parse_mode="Markdown",
        reply_markup=get_profile_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# MAIN MENU
# ---------------------------------------------------------------------

async def menu_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    text = update.message.text.strip()

    # ---------------------------------------------------------------
    # ---------------------------------------------------------------
    # PROFILE
    # ---------------------------------------------------------------

    if text == "👤 Profile":

        user_id = update.effective_user.id
        existing_user = get_user(user_id)

        if not existing_user:
            # Normally new users are registered from the Start button.
            # Keep this as a safe fallback if Profile is pressed first.
            context.user_data.clear()
            await update.message.reply_text(
                "Let's get you registered. 📝\n\n"
                "What's your full name?",
                reply_markup=ReplyKeyboardRemove(),
            )
            return FULL_NAME

        (
            _user_id,
            db_username,
            db_full_name,
            db_age,
            db_phone,
            db_location,
        ) = existing_user

        # Telegram username is automatic. Refresh it every time the
        # profile is opened so Baserow stays current if the user changes
        # their Telegram username.
        telegram_username = update.effective_user.username
        telegram_username = (
            f"@{telegram_username}" if telegram_username else ""
        )

        profile_data = {
            "full_name": db_full_name,
            "age": db_age,
            "phone": db_phone,
            "telegram_username": telegram_username or db_username or "",
            "location": db_location,
        }

        context.user_data.update(profile_data)

        # Save the current Telegram username to SQLite/Baserow if it
        # changed, without asking the user anything.
        if telegram_username and telegram_username != db_username:
            await save_current_user(update, context)

        await update.message.reply_text(
            format_user_info_message(profile_data),
            parse_mode="Markdown",
            reply_markup=get_profile_keyboard(),
        )

        return MENU_SECTION

    # ---------------------------------------------------------------
    # PROFILE - CHANGE PROFILE
    # ---------------------------------------------------------------

    elif text == "✏️ Change profile info":

        existing_user = get_user(update.effective_user.id)

        if not existing_user:
            await update.message.reply_text(
                "You don't have a profile yet.",
                reply_markup=get_profile_register_keyboard(),
            )
            return MENU_SECTION

        (
            _user_id,
            db_username,
            db_full_name,
            db_age,
            db_phone,
            db_location,
        ) = existing_user

        context.user_data.update({
            "full_name": db_full_name,
            "age": db_age,
            "phone": db_phone,
            "telegram_username": db_username,
            "location": db_location,
        })

        await update.message.reply_text(
            "Select options below:",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    # ---------------------------------------------------------------
    # PROFILE - REGISTER
    # ---------------------------------------------------------------

    elif text == "📝 Register":

        if get_user(update.effective_user.id):
            await update.message.reply_text(
                "You already have a profile. ✅",
                reply_markup=get_main_menu_keyboard(),
            )
            return MENU_SECTION

        context.user_data.clear()

        await update.message.reply_text(
            "Let's get you registered. 📝\n\n"
            "What's your full name?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return FULL_NAME

    # ---------------------------------------------------------------
    # PROFILE - BACK
    # ---------------------------------------------------------------

    elif text == "🔙 Back":

        await update.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    # ---------------------------------------------------------------
    # OUR WEBSITE
    # ---------------------------------------------------------------

    elif text == "🌐 Our website":
        website_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Visit our website", url=WEBSITE_URL)]
        ])


        image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), WEBSITE_IMAGE)

        if os.path.exists(image_path):
            with open(image_path, "rb") as website_photo:
                await update.message.reply_photo(
                    photo=website_photo,
                    caption="🌐 Our Website",
                    reply_markup=website_keyboard,
                )
        else:
            await update.message.reply_text(
                "🌐 **Our Website**\n\n"
                "Please use the button below to visit our website.",
                parse_mode="Markdown",
                reply_markup=website_keyboard,
            )

        await update.message.reply_text(
            "Use the button below to return to the main menu.",
            reply_markup=get_back_keyboard(),
        )



        return MENU_SECTION

    # ---------------------------------------------------------------
    # CHOOSE AN EVENT
    # ---------------------------------------------------------------

    elif "Choose an event" in text:

        # Show the event-location buttons directly.
        # Registration is handled only from Profile -> Register.
        keyboard = [
            [
                InlineKeyboardButton(
                    f"📍 {location}",
                    callback_data=location,
                )
            ]
            for location in LOCATIONS
        ]

        # Keep the previous Back button as a normal reply-keyboard button.
        # The event locations remain inline buttons.
        await update.message.reply_text(
            "Select your preferred event location:\n\n"
            "Use the button below to return to the main menu.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        await update.message.reply_text(
            "Use the button below to return to the main menu.",
            reply_markup=get_choose_event_back_keyboard(),
        )

        return LOCATION

    # ---------------------------------------------------------------
    # UNKNOWN MENU INPUT
    # ---------------------------------------------------------------

    else:

        await update.message.reply_text(
            "Please select an option using the keyboard below.",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION


# ---------------------------------------------------------------------
# SAVE CURRENT USER
# ---------------------------------------------------------------------

async def save_current_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    telegram_username = update.effective_user.username
    if telegram_username:
        username = f"@{telegram_username}"
        context.user_data["telegram_username"] = username
    else:
        username = context.user_data.get(
            "telegram_username",
            "",
        )
    full_name = context.user_data.get(
        "full_name",
        "",
    )
    age_value = context.user_data.get(
        "age",
        0,
    )
    phone_number = context.user_data.get(
        "phone",
        "",
    )
    location = context.user_data.get(
        "location",
        "",
    )

    save_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
        age=age_value,
        phone=phone_number,
        location=location,
    )

    # Keep Baserow synchronized whenever the profile is saved, including
    # name/age/phone/username edits.
    await sync_user_to_baserow(
        user_id=user_id,
        username=username,
        full_name=full_name,
        age=age_value,
        phone=phone_number,
        location=location,
    )
    logger.info(
        "Profile synchronized to Baserow: Telegram ID %s",
        user_id,
    )


# ---------------------------------------------------------------------
# EDIT NAME
# ---------------------------------------------------------------------

async def cmd_edit_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data["editing_single_field"] = True

    await update.message.reply_text(
        "Please enter your updated **Full Name**:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    return FULL_NAME


# ---------------------------------------------------------------------
# EDIT AGE
# ---------------------------------------------------------------------

async def cmd_edit_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data["editing_single_field"] = True

    await update.message.reply_text(
        "Please enter your updated **Age**:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    return AGE


# ---------------------------------------------------------------------
# EDIT PHONE
# ---------------------------------------------------------------------

async def cmd_edit_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data["editing_single_field"] = True

    contact_keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📞 Share my contact",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        "Please share or enter your updated **Phone Number**:",
        reply_markup=contact_keyboard,
        parse_mode="Markdown",
    )

    return PHONE


# ---------------------------------------------------------------------
# EDIT USERNAME
# ---------------------------------------------------------------------

async def cmd_edit_username(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    # Telegram provides the username automatically. There is no need
    # for the user to type it manually.
    telegram_username = update.effective_user.username
    context.user_data["telegram_username"] = (
        f"@{telegram_username}" if telegram_username else ""
    )

    await save_current_user(update, context)

    await update.message.reply_text(
        "Telegram username updated automatically from your Telegram account. ✅"
    )

    await update.message.reply_text(
        format_user_info_message(context.user_data),
        parse_mode="Markdown",
        reply_markup=get_profile_edit_keyboard(),
    )

    return EDIT_INFO_MENU


# ---------------------------------------------------------------------
# PROFILE EDIT BUTTONS
# ---------------------------------------------------------------------

async def profile_edit_name_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    return await cmd_edit_name(update, context)


async def profile_edit_age_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    return await cmd_edit_age(update, context)


async def profile_edit_contact_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    return await cmd_edit_phone(update, context)


async def profile_edit_username_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    return await cmd_edit_username(update, context)


# ---------------------------------------------------------------------
# GO BACK TO MENU
# ---------------------------------------------------------------------

async def go_back_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    if context.user_data.get("full_name"):

        save_user(
            user_id=user_id,
            username=context.user_data.get(
                "telegram_username",
                "",
            ),
            full_name=context.user_data.get(
                "full_name",
                "",
            ),
            age=context.user_data.get(
                "age",
                0,
            ),
            phone=context.user_data.get(
                "phone",
                "",
            ),
            location=context.user_data.get(
                "location",
                "",
            ),
        )

    await query.message.reply_text(
        "Returning to the main menu...",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# BACK FROM CHOOSE EVENT
# ---------------------------------------------------------------------

async def back_from_choose_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()

    await query.edit_message_text("Select your preferred event location:")
    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# ---------------------------------------------------------------------
# LOCATION CHOSEN
# ---------------------------------------------------------------------

async def location_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    chosen = query.data

    if chosen not in LOCATIONS:
        await query.edit_message_text("Unknown location, please try again.")
        return MENU_SECTION

    # A user may have only ONE active event registration.
    active_registration = get_active_event_registration(query.from_user.id)
    if active_registration:
        _registration_id, registered_location = active_registration
        await query.edit_message_text(
            f"You are already registered to {registered_location}. ✅\n\n"
            "You cannot register for another event location while this "
            "registration is active."
        )
        await query.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    # Load saved profile information if this conversation has no cached data.
    existing_user = get_user(query.from_user.id)
    if existing_user:
        (_, db_username, db_full_name, db_age, db_phone, db_location) = existing_user
        context.user_data.update({
            "telegram_username": db_username or "",
            "full_name": db_full_name or "",
            "age": db_age,
            "phone": db_phone or "",
            "location": db_location or "",
        })

    required = ("full_name", "age", "phone", "telegram_username")
    if not all(context.user_data.get(key) for key in required):
        await query.edit_message_text(
            "Please create your profile first, then choose an event location."
        )
        await query.message.reply_text(
            "Open Profile and press Register to create your profile.",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    # Only remember the selected location temporarily.
    # The actual registration is created after the user presses "Register".
    context.user_data["selected_event_location"] = chosen

    preview = (
        f"Name: {context.user_data.get('full_name', 'N/A')}\n"
        f"Age: {context.user_data.get('age', 'N/A')}\n"
        f"Phone: {context.user_data.get('phone', 'N/A')}\n"
        f"Username: {context.user_data.get('telegram_username', 'N/A')}\n"
        f"Location: {chosen}"
    )
    await query.edit_message_text(
        preview,
        reply_markup=get_event_registration_keyboard(),
    )
    return LOCATION


async def register_event_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    chosen = context.user_data.get("selected_event_location")

    if not chosen or chosen not in LOCATIONS:
        await query.edit_message_text(
            "No event location selected. Please choose an event again."
        )
        return MENU_SECTION

    user_id = query.from_user.id

    # Check again immediately before creating the registration.
    # This protects against duplicate/parallel registration attempts.
    active_registration = get_active_event_registration(user_id)
    if active_registration:
        _registration_id, registered_location = active_registration
        context.user_data.pop("selected_event_location", None)
        await query.edit_message_text(
            f"You are already registered to {registered_location}. ✅\n\n"
            "You cannot register for another event location while this "
            "registration is active."
        )
        await query.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    context.user_data["location"] = chosen

    # Preserve the current profile table for the existing UI, while also
    # recording this event registration permanently in its own history table.
    save_user(
        user_id=user_id,
        username=context.user_data.get("telegram_username", ""),
        full_name=context.user_data.get("full_name", ""),
        age=context.user_data.get("age", 0),
        phone=context.user_data.get("phone", ""),
        location=chosen,
    )
    create_event_registration(user_id, chosen)

    # Keep the same Baserow row synchronized with the confirmed event location.
    await sync_user_to_baserow(
        user_id=user_id,
        username=context.user_data.get("telegram_username", ""),
        full_name=context.user_data.get("full_name", ""),
        age=context.user_data.get("age", 0),
        phone=context.user_data.get("phone", ""),
        location=chosen,
    )

    summary = (
        "You're registered! ✅\n\n"
        f"Name: {context.user_data.get('full_name', 'N/A')}\n"
        f"Age: {context.user_data.get('age', 'N/A')}\n"
        f"Phone: {context.user_data.get('phone', 'N/A')}\n"
        f"Username: {context.user_data.get('telegram_username', 'N/A')}\n"
        f"Location: {chosen}\n\n"
        f"Join the {chosen} group here:\n"
        f"{LOCATIONS[chosen]}"
    )
    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Registration", callback_data="cancel_registration")]
    ])
    await query.edit_message_text(summary, reply_markup=cancel_keyboard)
    context.user_data.pop("selected_event_location", None)
    await query.message.reply_text(
        "What would you like to do next?",
        reply_markup=get_main_menu_keyboard(),
    )
    return MENU_SECTION


async def cancel_event_preview_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("selected_event_location", None)
    context.user_data["location"] = ""

    await query.edit_message_text("Event registration cancelled. ❌")
    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )
    return MENU_SECTION


# ---------------------------------------------------------------------
# CANCEL REGISTRATION
# ---------------------------------------------------------------------

async def cancel_registration_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Keep the user's profile; cancel only their latest active event
    # registration in the history table.
    cancelled = cancel_latest_event_registration(user_id)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET location = '' WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()

    context.user_data["location"] = ""
    context.user_data.pop("selected_event_location", None)

    # Reflect the cancellation in Baserow while keeping the user profile.
    await sync_user_to_baserow(
        user_id=user_id,
        username=context.user_data.get("telegram_username", ""),
        full_name=context.user_data.get("full_name", ""),
        age=context.user_data.get("age", 0),
        phone=context.user_data.get("phone", ""),
        location="",
    )

    await query.edit_message_text(
        "Your event registration has been cancelled. ❌\n\n"
        "Your profile is saved, so you can register for an event again anytime."
    )
    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )
    return MENU_SECTION


# ---------------------------------------------------------------------
# CANCEL LOCATION FROM PROFILE EDIT MENU
# ---------------------------------------------------------------------

async def cancel_location_from_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user_id = update.effective_user.id

    active_registration = get_active_event_registration(user_id)

    if not active_registration:
        await update.message.reply_text(
            "You are not registered for any event location. 📍",
            reply_markup=get_profile_edit_keyboard(),
        )
        return EDIT_INFO_MENU

    _, registered_location = active_registration

    # Cancel only the active event registration.
    # The user's profile remains saved.
    cancelled = cancel_latest_event_registration(user_id)

    if not cancelled:
        await update.message.reply_text(
            "Your event location could not be cancelled. Please try again.",
            reply_markup=get_profile_edit_keyboard(),
        )
        return EDIT_INFO_MENU

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET location = '' WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()

    context.user_data["location"] = ""
    context.user_data.pop("selected_event_location", None)

    # Update the existing Baserow profile row, keeping the profile data.
    await sync_user_to_baserow(
        user_id=user_id,
        username=context.user_data.get("telegram_username", ""),
        full_name=context.user_data.get("full_name", ""),
        age=context.user_data.get("age", 0),
        phone=context.user_data.get("phone", ""),
        location="",
    )

    await update.message.reply_text(
        f"Your {registered_location} event registration has been cancelled. ❌\n\n"
        "Your profile information is still saved. ✅\n"
        "You can register for another location anytime.",
        reply_markup=get_profile_edit_keyboard(),
    )

    return EDIT_INFO_MENU


# ---------------------------------------------------------------------
# /CANCEL
# ---------------------------------------------------------------------

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data.clear()

    await update.message.reply_text(
        "Registration cancelled.\n\n"
        "Send /start to begin again.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return ConversationHandler.END


# ---------------------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------------------

async def set_bot_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            ("start", "Start the bot"),
            ("restart", "Restart the bot"),
            ("menu", "Open menu"),
        ]
    )


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

def main() -> None:
    # Initialize database and make a backup before starting the bot.
    init_db()
    backup_database()

    # Create Telegram application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(set_bot_commands)
        .build()
    )

    # ---------------------------------------------------------------
    # CONVERSATION HANDLER
    # ---------------------------------------------------------------

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("restart", restart),
            CommandHandler("menu", menu_command),
        ],
        states={
            MENU_SECTION: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    menu_handler,
                ),
                CallbackQueryHandler(
                    start_button_callback,
                    pattern="^start_registration$",
                ),
                CallbackQueryHandler(
                    cancel_registration_callback,
                    pattern="^cancel_registration$",
                ),
            ],
            FULL_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    full_name,
                )
            ],
            AGE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    age,
                )
            ],
            PHONE: [
                MessageHandler(
                    (filters.CONTACT | filters.TEXT) & ~filters.COMMAND,
                    phone,
                )
            ],
            LOCATION: [
                CallbackQueryHandler(
                    location_chosen,
                    pattern=r"^(Tashkent|Samarkand|Bukhara)$",
                ),
                CallbackQueryHandler(
                    register_event_callback,
                    pattern=r"^register_event$",
                ),
                CallbackQueryHandler(
                    cancel_event_preview_callback,
                    pattern=r"^cancel_event_preview$",
                ),
                CallbackQueryHandler(
                    back_from_choose_event,
                    pattern=r"^back_to_menu$",
                ),
                MessageHandler(
                    filters.Regex(r"^🔙 Back$"),
                    menu_handler,
                ),
                MessageHandler(
                    filters.Regex(r"^Use the button below to return to the main menu\.$"),
                    menu_handler,
                ),
            ],
            EDIT_INFO_MENU: [
                MessageHandler(
                    filters.Regex(r"^1️⃣ Change Name$"),
                    profile_edit_name_button,
                ),
                MessageHandler(
                    filters.Regex(r"^2️⃣ Change Age$"),
                    profile_edit_age_button,
                ),
                MessageHandler(
                    filters.Regex(r"^3️⃣ Change Contact$"),
                    profile_edit_contact_button,
                ),
                MessageHandler(
                    filters.Regex(r"^4️⃣ Change Username$"),
                    profile_edit_username_button,
                ),
                MessageHandler(
                    filters.Regex(r"^🔙 Back$"),
                    profile_edit_back,
                ),
                MessageHandler(
                    filters.Regex(r"^❌ Cancel Location$"),
                    cancel_location_from_profile,
                ),
                CommandHandler("editname", cmd_edit_name),
                CommandHandler("editage", cmd_edit_age),
                CommandHandler("editphone", cmd_edit_phone),
                CommandHandler("editusername", cmd_edit_username),
                CommandHandler("done", go_back_callback),
                CallbackQueryHandler(
                    go_back_callback,
                    pattern="^go_back_to_menu$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("restart", restart),
            CommandHandler("menu", menu_command),
            CommandHandler("cancel", cancel),
            CommandHandler("editname", cmd_edit_name),
            CommandHandler("editage", cmd_edit_age),
            CommandHandler("editphone", cmd_edit_phone),
            CommandHandler("editusername", cmd_edit_username),
            CallbackQueryHandler(
                cancel_registration_callback,
                pattern="^cancel_registration$",
            ),
        ],
        # Fixed PTBUserWarning by aligning per_message tracking
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    # Add conversation handler
    app.add_handler(conv_handler)

    logger.info("Bot started. Press Ctrl+C to stop.")

    # Python 3.14 event loop initialization fix
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Start polling
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()


