import asyncio
import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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


# =====================================================================
# CONFIG
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured. Set BOT_TOKEN as an environment variable."
    )

if not BASEROW_TOKEN:
    raise RuntimeError(
        "BASEROW_TOKEN is not configured. Set BASEROW_TOKEN as an environment variable."
    )

WEBSITE_URL = "https://articular.uz"
WEBSITE_IMAGE = "LOGO ARTICULARUZ (2).png"


# =====================================================================
# BASEROW TABLES
# =====================================================================

BASEROW_TABLE_IDS = {
    "Users": "1237476",
    "Tashkent": "1241776",
    "Bukhara": "1241780",
    "Samarkand": "1241781",
}


# =====================================================================
# EVENT LOCATIONS
# =====================================================================

LOCATIONS = {
    "Tashkent": "https://t.me/+your_tashkent_group_invite",
    "Samarkand": "https://t.me/+your_samarkand_group_invite",
    "Bukhara": "https://t.me/+your_bukhara_group_invite",
}

VALID_CITIES = (
    "Tashkent",
    "Bukhara",
    "Samarkand",
)


# =====================================================================
# LOGGING
# =====================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =====================================================================
# CONVERSATION STATES
# =====================================================================

(
    MENU_SECTION,
    FULL_NAME,
    AGE,
    PHONE,
    USERNAME,
    LOCATION,
    EDIT_INFO_MENU,
) = range(7)


# =====================================================================
# BASEROW HELPERS
# =====================================================================

def _baserow_headers():
    return {
        "Authorization": f"Token {BASEROW_TOKEN}",
        "Content-Type": "application/json",
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

    if registered_at:
        payload["Registered At"] = registered_at

    return payload


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# =====================================================================
# TELEGRAM USERNAME
# =====================================================================

def get_telegram_username(user) -> str:
    """
    Always get the current Telegram username directly from Telegram.
    Returns @username or an empty string.
    """

    if not user:
        return ""

    if user.username:
        return f"@{user.username}"

    return ""


# =====================================================================
# BASEROW: FIND ROWS
# =====================================================================

def _get_baserow_rows_sync(
    table_name: str,
    user_id: int,
):
    table_id = BASEROW_TABLE_IDS[table_name]

    query = urllib.parse.urlencode({
        "user_field_names": "true",
        "size": "100",
        "filter__Telegram ID__equal": str(user_id),
    })

    url = (
        "https://api.baserow.io/api/database/rows/table/"
        f"{table_id}/?{query}"
    )

    request = urllib.request.Request(
        url,
        method="GET",
        headers=_baserow_headers(),
    )

    with urllib.request.urlopen(
        request,
        timeout=15,
    ) as response:

        data = json.loads(
            response.read().decode("utf-8")
        )

    return data.get("results", [])


# =====================================================================
# BASEROW: DELETE USER FROM CITY TABLES
# =====================================================================

def _remove_user_from_city_tables_sync(
    user_id: int,
):
    if not BASEROW_TOKEN:
        return

    for city in VALID_CITIES:

        try:

            rows = _get_baserow_rows_sync(
                city,
                user_id,
            )

            table_id = BASEROW_TABLE_IDS[city]

            for row in rows:

                row_id = row["id"]

                delete_url = (
                    "https://api.baserow.io/api/database/rows/table/"
                    f"{table_id}/{row_id}/"
                )

                delete_request = urllib.request.Request(
                    delete_url,
                    method="DELETE",
                    headers=_baserow_headers(),
                )

                with urllib.request.urlopen(
                    delete_request,
                    timeout=15,
                ) as response:

                    if not (200 <= response.status < 300):
                        raise RuntimeError(
                            f"Failed deleting row {row_id} "
                            f"from {city}: HTTP {response.status}"
                        )

                logger.info(
                    "Removed Telegram ID %s from %s",
                    user_id,
                    city,
                )

        except urllib.error.HTTPError as exc:

            body = exc.read().decode(
                "utf-8",
                errors="replace",
            )

            logger.error(
                "Baserow delete error in %s: HTTP %s: %s",
                city,
                exc.code,
                body,
            )

        except Exception:

            logger.exception(
                "Unexpected error removing user from %s",
                city,
            )


async def remove_user_from_city_tables(
    user_id: int,
):
    await asyncio.to_thread(
        _remove_user_from_city_tables_sync,
        user_id,
    )


# =====================================================================
# BASEROW: UPDATE USERS TABLE
# =====================================================================

def _sync_users_table_sync(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):
    table_id = BASEROW_TABLE_IDS["Users"]

    rows = _get_baserow_rows_sync(
        "Users",
        user_id,
    )

    if rows:

        row = rows[0]
        row_id = row["id"]

        payload = _baserow_payload(
            user_id=user_id,
            username=username,
            full_name=full_name,
            age=age,
            phone=phone,
            location=location,
        )

        existing_registered_at = row.get(
            "Registered At"
        )

        if existing_registered_at:
            payload["Registered At"] = existing_registered_at
        else:
            payload["Registered At"] = _utc_now()

        update_url = (
            "https://api.baserow.io/api/database/rows/table/"
            f"{table_id}/{row_id}/"
            "?user_field_names=true"
        )

        request = urllib.request.Request(
            update_url,
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
            headers=_baserow_headers(),
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            if not (200 <= response.status < 300):
                raise RuntimeError(
                    f"Users update failed: HTTP {response.status}"
                )

        logger.info(
            "Updated Users row %s for Telegram ID %s",
            row_id,
            user_id,
        )

    else:

        payload = _baserow_payload(
            user_id=user_id,
            username=username,
            full_name=full_name,
            age=age,
            phone=phone,
            location=location,
            registered_at=_utc_now(),
        )

        create_url = (
            "https://api.baserow.io/api/database/rows/table/"
            f"{table_id}/?user_field_names=true"
        )

        request = urllib.request.Request(
            create_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=_baserow_headers(),
        )

        with urllib.request.urlopen(
            request,
            timeout=15,
        ) as response:

            if not (200 <= response.status < 300):
                raise RuntimeError(
                    f"Users creation failed: HTTP {response.status}"
                )

        logger.info(
            "Created Users row for Telegram ID %s",
            user_id,
        )


# =====================================================================
# BASEROW: ADD USER TO CITY
# =====================================================================

def _add_user_to_city_sync(
    city: str,
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
):
    if city not in VALID_CITIES:
        return

    table_id = BASEROW_TABLE_IDS[city]

    existing_rows = _get_baserow_rows_sync(
        city,
        user_id,
    )

    if existing_rows:

        logger.info(
            "Telegram ID %s already exists in %s",
            user_id,
            city,
        )

        return

    payload = _baserow_payload(
        user_id=user_id,
        username=username,
        full_name=full_name,
        age=age,
        phone=phone,
        location=city,
        registered_at=_utc_now(),
    )

    create_url = (
        "https://api.baserow.io/api/database/rows/table/"
        f"{table_id}/?user_field_names=true"
    )

    request = urllib.request.Request(
        create_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=_baserow_headers(),
    )

    with urllib.request.urlopen(
        request,
        timeout=15,
    ) as response:

        if not (200 <= response.status < 300):
            raise RuntimeError(
                f"{city} creation failed: HTTP {response.status}"
            )

    logger.info(
        "Added Telegram ID %s to %s",
        user_id,
        city,
    )


# =====================================================================
# BASEROW: FULL SYNC
# =====================================================================

def _sync_user_to_baserow_sync(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):
    if not BASEROW_TOKEN:

        logger.warning(
            "BASEROW_TOKEN is not configured."
        )

        return

    try:

        # -------------------------------------------------------------
        # 1. Users table
        # -------------------------------------------------------------

        _sync_users_table_sync(
            user_id=user_id,
            username=username,
            full_name=full_name,
            age=age,
            phone=phone,
            location=location,
        )

        # -------------------------------------------------------------
        # 2. Remove old city rows
        # -------------------------------------------------------------

        _remove_user_from_city_tables_sync(
            user_id
        )

        # -------------------------------------------------------------
        # 3. Add user to selected city
        # -------------------------------------------------------------

        if location in VALID_CITIES:

            _add_user_to_city_sync(
                city=location,
                user_id=user_id,
                username=username,
                full_name=full_name,
                age=age,
                phone=phone,
            )

    except urllib.error.HTTPError as exc:

        body = exc.read().decode(
            "utf-8",
            errors="replace",
        )

        logger.error(
            "Baserow HTTP error %s: %s",
            exc.code,
            body,
        )

    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:

        logger.error(
            "Baserow connection error: %s",
            exc,
        )

    except Exception:

        logger.exception(
            "Unexpected Baserow synchronization error"
        )


async def sync_user_to_baserow(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: int | str,
    location: str,
):
    await asyncio.to_thread(
        _sync_user_to_baserow_sync,
        user_id,
        username,
        full_name,
        age,
        phone,
        location,
    )


# =====================================================================
# SQLITE DATABASE
# =====================================================================

DB_PATH = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
    ),
    "registrations.db",
)


def get_db_connection():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

    conn.execute(
        "PRAGMA foreign_keys=ON"
    )

    return conn


def init_db():

    conn = get_db_connection()

    try:

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
                registered_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS event_registrations (
                registration_id INTEGER
                    PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                location TEXT NOT NULL,
                registered_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,
                cancelled_at TIMESTAMP NULL,
                FOREIGN KEY (user_id)
                    REFERENCES users(user_id)
                    ON DELETE CASCADE
            )
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
            CREATE INDEX IF NOT EXISTS idx_event_user
            ON event_registrations(user_id)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_event_location
            ON event_registrations(location)
            """
        )

        conn.commit()

    finally:

        conn.close()


# =====================================================================
# USER DATABASE
# =====================================================================

def save_user(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):

    conn = get_db_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO users (
                user_id,
                username,
                full_name,
                age,
                phone,
                location
            )
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

    finally:

        conn.close()


def get_user(user_id: int):

    conn = get_db_connection()

    try:

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

        return cursor.fetchone()

    finally:

        conn.close()


def delete_user(user_id: int):

    conn = get_db_connection()

    try:

        conn.execute(
            "DELETE FROM users WHERE user_id = ?",
            (user_id,),
        )

        conn.commit()

    finally:

        conn.close()


# =====================================================================
# EVENT DATABASE
# =====================================================================

def get_active_event_registration(
    user_id: int,
):

    conn = get_db_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                registration_id,
                location
            FROM event_registrations
            WHERE
                user_id = ?
                AND cancelled_at IS NULL
            ORDER BY registration_id DESC
            LIMIT 1
            """,
            (user_id,),
        )

        return cursor.fetchone()

    finally:

        conn.close()


def create_event_registration(
    user_id: int,
    location: str,
):

    conn = get_db_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT registration_id
            FROM event_registrations
            WHERE
                user_id = ?
                AND cancelled_at IS NULL
            LIMIT 1
            """,
            (user_id,),
        )

        existing = cursor.fetchone()

        if existing:
            return None

        cursor.execute(
            """
            INSERT INTO event_registrations (
                user_id,
                location
            )
            VALUES (?, ?)
            """,
            (
                user_id,
                location,
            ),
        )

        registration_id = cursor.lastrowid

        conn.commit()

        return registration_id

    finally:

        conn.close()


def cancel_latest_event_registration(
    user_id: int,
):

    conn = get_db_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT registration_id
            FROM event_registrations
            WHERE
                user_id = ?
                AND cancelled_at IS NULL
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


# =====================================================================
# DATABASE BACKUP
# =====================================================================

def backup_database():

    if not os.path.exists(DB_PATH):
        return

    backup_dir = os.path.join(
        os.path.dirname(
            os.path.abspath(__file__)
        ),
        "db_backups",
    )

    os.makedirs(
        backup_dir,
        exist_ok=True,
    )

    backup_name = datetime.now().strftime(
        "registrations_%Y%m%d_%H%M%S.db"
    )

    backup_path = os.path.join(
        backup_dir,
        backup_name,
    )

    source = get_db_connection()

    destination = sqlite3.connect(
        backup_path
    )

    try:

        source.backup(
            destination
        )

    finally:

        destination.close()
        source.close()

    backups = sorted(
        Path(backup_dir).glob(
            "registrations_*.db"
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for old_backup in backups[7:]:

        try:
            old_backup.unlink()
        except OSError:
            pass


# =====================================================================
# KEYBOARDS
# =====================================================================

def get_main_menu_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("👤 Profile")
            ],
            [
                KeyboardButton("🎯 Choose an event"),
                KeyboardButton("🌐 Our website"),
            ],
        ],
        resize_keyboard=True,
    )


def get_back_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🔙 Back")
            ]
        ],
        resize_keyboard=True,
    )


def get_profile_register_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📝 Register"),
                KeyboardButton("🔙 Back"),
            ]
        ],
        resize_keyboard=True,
    )


def get_profile_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("✏️ Change profile info"),
                KeyboardButton("🔙 Back"),
            ]
        ],
        resize_keyboard=True,
    )


def get_profile_edit_keyboard():

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


def get_start_keyboard():

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


def get_event_location_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📍 Tashkent",
                    callback_data="Tashkent",
                )
            ],
            [
                InlineKeyboardButton(
                    "📍 Samarkand",
                    callback_data="Samarkand",
                )
            ],
            [
                InlineKeyboardButton(
                    "📍 Bukhara",
                    callback_data="Bukhara",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="event_back",
                )
            ],
        ]
    )


def get_register_confirmation_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Register",
                    callback_data="register_event",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="cancel_event_preview",
                )
            ],
        ]
    )


def get_registered_event_keyboard():

    return InlineKeyboardMarkup(
        [



            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="event_back",
                )
            ],
        ]
    )


# =====================================================================
# PROFILE DISPLAY
# =====================================================================

def format_user_info_message(
    user_data: dict,
    telegram_user=None,
):

    name = user_data.get(
        "full_name",
        "N/A",
    ) or "N/A"

    age = user_data.get(
        "age",
        "N/A",
    ) or "N/A"

    phone = user_data.get(
        "phone",
        "N/A",
    ) or "N/A"

    # Always read username directly from Telegram.
    username = get_telegram_username(
        telegram_user
    )

    if not username:

        username = user_data.get(
            "telegram_username",
            "",
        ) or "N/A"

    location = user_data.get(
        "location",
        "N/A",
    ) or "N/A"

    return (
        "Here is your personal information:\n\n"
        f"👤 Name: {name}\n"
        f"🎂 Age: {age}\n"
        f"📞 Phone: {phone}\n"
        f"📱 Username: {username}\n"
        f"📍 Event location: {location}\n\n"
        "Use the buttons below to change your profile "
        "or return to the menu."
    )


# =====================================================================
# START
# =====================================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data.clear()

    await update.message.reply_text(
        "Welcome to ArticularUz! 🚀\n\n"
        "Where young innovators across Uzbekistan present, "
        "defend, and connect their scientific ideas with "
        "the global community!\n\n"
        "Press Start to continue.",
        reply_markup=get_start_keyboard(),
    )

    return MENU_SECTION


async def restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await start(
        update,
        context,
    )


# =====================================================================
# LOAD USER INTO CONTEXT
# =====================================================================

def load_user_into_context(
    user,
    context,
):

    (
        _user_id,
        db_username,
        db_full_name,
        db_age,
        db_phone,
        db_location,
    ) = user

    context.user_data.update({
        "full_name": db_full_name or "",
        "age": db_age,
        "phone": db_phone or "",
        "telegram_username": db_username or "",
        "location": db_location or "",
    })


# =====================================================================
# MENU COMMAND
# =====================================================================

async def menu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    user_id = update.effective_user.id

    existing_user = get_user(
        user_id
    )

    if not existing_user:

        await update.message.reply_text(
            "You are not registered.\n\n"
            "Please press /start to register."
        )

        return ConversationHandler.END

    load_user_into_context(
        existing_user,
        context,
    )

    # IMPORTANT:
    # /menu only reads SQLite.
    # No Baserow synchronization here.

    await update.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# START BUTTON
# =====================================================================

async def start_button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    existing_user = get_user(
        user_id
    )

    await query.edit_message_reply_markup(
        reply_markup=None
    )

    if existing_user:

        load_user_into_context(
            existing_user,
            context,
        )

        # IMPORTANT:
        # Do NOT call save_current_user().
        # Existing users are simply loaded from SQLite.

        await query.message.reply_text(
            "Welcome back! 👋\n\n"
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION

    context.user_data.clear()

    await query.message.reply_text(
        "Let's get you registered! 📝\n\n"
        "What's your full name?",
        reply_markup=ReplyKeyboardRemove(),
    )

    return FULL_NAME


# =====================================================================
# FULL NAME
# =====================================================================

async def full_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    name = update.message.text.strip()

    if len(name.split()) < 2:

        await update.message.reply_text(
            "Please enter your full name "
            "(first and last name)."
        )

        return FULL_NAME

    context.user_data[
        "full_name"
    ] = name

    if context.user_data.get(
        "editing_single_field"
    ):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        # Actual profile change -> sync Baserow.
        await save_current_user(
            update,
            context,
        )

        await update.message.reply_text(
            "Name updated successfully! ✅"
        )

        await update.message.reply_text(
            format_user_info_message(
                context.user_data,
                update.effective_user,
            ),
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    await update.message.reply_text(
        f"Welcome, {name}! 👋\n\n"
        "How old are you?"
    )

    return AGE


# =====================================================================
# AGE
# =====================================================================

async def age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    text = update.message.text.strip()

    if (
        not text.isdigit()
        or not (5 <= int(text) <= 100)
    ):

        await update.message.reply_text(
            "Please enter a valid age as a number "
            "(e.g. 17)."
        )

        return AGE

    context.user_data[
        "age"
    ] = int(text)

    if context.user_data.get(
        "editing_single_field"
    ):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        # Actual profile change -> sync Baserow.
        await save_current_user(
            update,
            context,
        )

        await update.message.reply_text(
            "Age updated successfully! ✅"
        )

        await update.message.reply_text(
            format_user_info_message(
                context.user_data,
                update.effective_user,
            ),
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
        "Please share your phone number "
        "using the button below:",
        reply_markup=contact_keyboard,
    )

    return PHONE


# =====================================================================
# PHONE
# =====================================================================

async def phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    phone_number = None

    if update.message.contact:

        phone_number = (
            update.message.contact.phone_number
        )

    elif update.message.text:

        text = update.message.text.strip()

        if re.match(
            r"^\+?\d{7,15}$",
            text,
        ):

            phone_number = text

    if not phone_number:

        await update.message.reply_text(
            "Please use the button below or type "
            "a valid phone number "
            "(e.g. +998901234567)."
        )

        return PHONE

    if not phone_number.startswith("+"):

        phone_number = "+" + phone_number

    context.user_data[
        "phone"
    ] = phone_number

    if context.user_data.get(
        "editing_single_field"
    ):

        context.user_data.pop(
            "editing_single_field",
            None,
        )

        # Actual profile change -> sync Baserow.
        await save_current_user(
            update,
            context,
        )

        await update.message.reply_text(
            "Phone number updated successfully! ✅",
            reply_markup=ReplyKeyboardRemove(),
        )

        await update.message.reply_text(
            format_user_info_message(
                context.user_data,
                update.effective_user,
            ),
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    # New registration.
    username_value = get_telegram_username(
        update.effective_user
    )

    context.user_data[
        "telegram_username"
    ] = username_value

    # New registration -> sync Baserow.
    await save_current_user(
        update,
        context,
    )

    await update.message.reply_text(
        "Registration completed successfully! ✅",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# USERNAME
# =====================================================================

async def username(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    username_value = get_telegram_username(
        update.effective_user
    )

    context.user_data[
        "telegram_username"
    ] = username_value

    # Actual profile change -> sync Baserow.
    await save_current_user(
        update,
        context,
    )

    await update.message.reply_text(
        "Telegram username updated automatically. ✅"
    )

    await update.message.reply_text(
        format_user_info_message(
            context.user_data,
            update.effective_user,
        ),
        reply_markup=get_profile_edit_keyboard(),
    )

    return EDIT_INFO_MENU


# =====================================================================
# SAVE CURRENT USER
# =====================================================================

async def save_current_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    telegram_username = get_telegram_username(
        update.effective_user
    )

    if telegram_username:

        username_value = telegram_username

        context.user_data[
            "telegram_username"
        ] = username_value

    else:

        username_value = context.user_data.get(
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

    # Save locally first.
    save_user(
        user_id=user_id,
        username=username_value,
        full_name=full_name,
        age=age_value,
        phone=phone_number,
        location=location,
    )

    # Synchronize with Baserow only when this function
    # is explicitly called because data changed.
    await sync_user_to_baserow(
        user_id=user_id,
        username=username_value,
        full_name=full_name,
        age=age_value,
        phone=phone_number,
        location=location,
    )

    logger.info(
        "Profile synchronized for Telegram ID %s "
        "with username %s",
        user_id,
        username_value,
    )


# =====================================================================
# PROFILE EDIT BACK
# =====================================================================

async def profile_edit_back(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    user = get_user(
        update.effective_user.id
    )

    if not user:

        await update.message.reply_text(
            "You don't have a profile.",
            reply_markup=get_profile_register_keyboard(),
        )

        return MENU_SECTION

    load_user_into_context(
        user,
        context,
    )

    username_value = get_telegram_username(
        update.effective_user
    )

    if username_value:

        context.user_data[
            "telegram_username"
        ] = username_value

    await update.message.reply_text(
        format_user_info_message(
            context.user_data,
            update.effective_user,
        ),
        reply_markup=get_profile_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# MAIN MENU
# =====================================================================

async def menu_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    text = update.message.text.strip()

    # ---------------------------------------------------------------
    # PROFILE
    # ---------------------------------------------------------------

    if text == "👤 Profile":

        user_id = update.effective_user.id

        existing_user = get_user(
            user_id
        )

        if not existing_user:

            context.user_data.clear()

            await update.message.reply_text(
                "Let's get you registered. 📝\n\n"
                "What's your full name?",
                reply_markup=ReplyKeyboardRemove(),
            )

            return FULL_NAME

        # -----------------------------------------------------------
        # IMPORTANT:
        # Only load from SQLite.
        # NO Baserow synchronization.
        # -----------------------------------------------------------

        load_user_into_context(
            existing_user,
            context,
        )

        # Read username directly from Telegram for display.
        username_value = get_telegram_username(
            update.effective_user
        )

        if username_value:

            context.user_data[
                "telegram_username"
            ] = username_value

        # Show profile immediately.
        await update.message.reply_text(
            format_user_info_message(
                context.user_data,
                update.effective_user,
            ),
            reply_markup=get_profile_keyboard(),
        )

        return MENU_SECTION

    # ---------------------------------------------------------------
    # CHANGE PROFILE
    # ---------------------------------------------------------------

    if text == "✏️ Change profile info":

        existing_user = get_user(
            update.effective_user.id
        )

        if not existing_user:

            await update.message.reply_text(
                "You don't have a profile yet.",
                reply_markup=get_profile_register_keyboard(),
            )

            return MENU_SECTION

        load_user_into_context(
            existing_user,
            context,
        )

        await update.message.reply_text(
            "Select an option below:",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    # ---------------------------------------------------------------
    # REGISTER
    # ---------------------------------------------------------------

    if text == "📝 Register":

        if get_user(
            update.effective_user.id
        ):

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
    # BACK
    # ---------------------------------------------------------------

    if text == "🔙 Back":

        await update.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION

    # ---------------------------------------------------------------
    # WEBSITE
    # ---------------------------------------------------------------

    if text == "🌐 Our website":

        website_keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🌐 Visit our website",
                        url=WEBSITE_URL,
                    )
                ]
            ]
        )

        image_path = os.path.join(
            os.path.dirname(
                os.path.abspath(__file__)
            ),
            WEBSITE_IMAGE,
        )

        if os.path.exists(image_path):

            with open(
                image_path,
                "rb",
            ) as website_photo:

                await update.message.reply_photo(
                    photo=website_photo,
                    caption="🌐 Our Website",
                    reply_markup=website_keyboard,
                )

        else:

            await update.message.reply_text(
                "🌐 Our Website\n\n"
                "Please use the button below.",
                reply_markup=website_keyboard,
            )

        await update.message.reply_text(
            "Use the button below to return "
            "to the main menu.",
            reply_markup=get_back_keyboard(),
        )

        return MENU_SECTION

    # ---------------------------------------------------------------
    # CHOOSE EVENT
    # ---------------------------------------------------------------

    if text == "🎯 Choose an event":

        user_id = update.effective_user.id

        active_registration = (
            get_active_event_registration(
                user_id
            )
        )

        if active_registration:

            (
                _registration_id,
                registered_location,
            ) = active_registration

            context.user_data[
                "location"
            ] = registered_location

            await update.message.reply_text(
                "You are already registered! ✅\n\n"
                f"📍 Event location: "
                f"{registered_location}\n\n"
                "You cannot register for another "
                "location while this registration "
                "is active.",
                reply_markup=get_back_keyboard(),
            )


            return LOCATION

        await update.message.reply_text(
            "Select your preferred event location:",
            reply_markup=get_event_location_keyboard(),
        )

        return LOCATION

    # ---------------------------------------------------------------
    # UNKNOWN
    # ---------------------------------------------------------------

    await update.message.reply_text(
        "Please select an option using "
        "the keyboard below.",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# EVENT LOCATION CHOSEN
# =====================================================================

async def location_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    chosen = query.data
    user_id = query.from_user.id

    if chosen not in VALID_CITIES:

        await query.edit_message_text(
            "Unknown location."
        )

        return MENU_SECTION

    active_registration = (
        get_active_event_registration(
            user_id
        )
    )

    if active_registration:
        (
            _registration_id,
            registered_location,
        ) = active_registration

        context.user_data[
            "location"
        ] = registered_location

        await query.edit_message_text(
            f"📍 You are already registered in {registered_location}.",
            reply_markup=get_registered_event_keyboard(),
        )

        return LOCATION

    existing_user = get_user(
        user_id
    )

    if existing_user:

        load_user_into_context(
            existing_user,
            context,
        )

    username_value = get_telegram_username(
        query.from_user
    )

    if username_value:

        context.user_data[
            "telegram_username"
        ] = username_value

    required = (
        "full_name",
        "age",
        "phone",
    )

    if not all(
        context.user_data.get(key)
        for key in required
    ):

        await query.edit_message_text(
            "Your profile is incomplete."
        )

        await query.message.reply_text(
            "Please complete your profile first.",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION

    context.user_data[
        "selected_event_location"
    ] = chosen

    username_display = (
        context.user_data.get(
            "telegram_username"
        )
        or "No username"
    )

    preview = (
        "Please confirm your registration:\n\n"
        f"👤 Name: "
        f"{context.user_data.get('full_name')}\n"
        f"🎂 Age: "
        f"{context.user_data.get('age')}\n"
        f"📞 Phone: "
        f"{context.user_data.get('phone')}\n"
        f"📱 Username: "
        f"{username_display}\n"
        f"📍 Location: {chosen}"
    )

    await query.edit_message_text(
        preview,
        reply_markup=get_register_confirmation_keyboard(),
    )

    return LOCATION


# =====================================================================
# REGISTER EVENT
# =====================================================================

async def register_event_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    chosen = context.user_data.get(
        "selected_event_location"
    )

    if chosen not in VALID_CITIES:

        await query.edit_message_text(
            "No valid event location selected."
        )

        return MENU_SECTION

    active_registration = (
        get_active_event_registration(
            user_id
        )
    )

    if active_registration:

        (
            _registration_id,
            registered_location,
        ) = active_registration

        context.user_data.pop(
            "selected_event_location",
            None,
        )

        await query.edit_message_text(
            "You are already registered! ✅\n\n"
            f"📍 Event location: "
            f"{registered_location}",
            reply_markup=get_registered_event_keyboard(),
        )

        return LOCATION

    username_value = get_telegram_username(
        query.from_user
    )

    if username_value:

        context.user_data[
            "telegram_username"
        ] = username_value

    full_name_value = context.user_data.get(
        "full_name",
        "",
    )

    age_value = context.user_data.get(
        "age",
        0,
    )

    phone_value = context.user_data.get(
        "phone",
        "",
    )

    context.user_data[
        "location"
    ] = chosen

    # ---------------------------------------------------------------
    # Save SQLite
    # ---------------------------------------------------------------

    save_user(
        user_id=user_id,
        username=username_value,
        full_name=full_name_value,
        age=age_value,
        phone=phone_value,
        location=chosen,
    )

    # ---------------------------------------------------------------
    # Create event registration
    # ---------------------------------------------------------------

    registration_id = create_event_registration(
        user_id,
        chosen,
    )

    if registration_id is None:

        await query.edit_message_text(
            "You are already registered for an event. "
            "Please check your current registration."
        )

        return MENU_SECTION

    # ---------------------------------------------------------------
    # Sync Baserow
    # ---------------------------------------------------------------

    await sync_user_to_baserow(
        user_id=user_id,
        username=username_value,
        full_name=full_name_value,
        age=age_value,
        phone=phone_value,
        location=chosen,
    )

    summary = (
        "You're registered! ✅\n\n"
        f"👤 Name: {full_name_value}\n"
        f"🎂 Age: {age_value}\n"
        f"📞 Phone: {phone_value}\n"
        f"📱 Username: "
        f"{username_value or 'No username'}\n"
        f"📍 Location: {chosen}\n\n"
        f"Join the {chosen} group here:\n"
        f"{LOCATIONS[chosen]}"
    )

    cancel_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel Registration",
                    callback_data="cancel_registration",
                )
            ]
        ]
    )

    await query.edit_message_text(
        summary,
        reply_markup=cancel_keyboard,
    )

    context.user_data.pop(
        "selected_event_location",
        None,
    )

    await query.message.reply_text(
        "What would you like to do next?",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# EVENT PREVIEW CANCEL
# =====================================================================

async def cancel_event_preview_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    context.user_data.pop(
        "selected_event_location",
        None,
    )

    await query.edit_message_text(
        "Event registration cancelled. ❌"
    )

    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# EVENT BACK
# =====================================================================

async def event_back_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    context.user_data.pop(
        "selected_event_location",
        None,
    )

    await query.edit_message_text(
        "Returning to the main menu..."
    )

    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# CANCEL REGISTRATION
# =====================================================================

async def cancel_registration_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    cancelled = cancel_latest_event_registration(
        user_id
    )

    if not cancelled:

        await query.edit_message_text(
            "You do not have an active event registration."
        )

        await query.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION

    conn = get_db_connection()

    try:

        conn.execute(
            """
            UPDATE users
            SET location = ''
            WHERE user_id = ?
            """,
            (user_id,),
        )

        conn.commit()

    finally:

        conn.close()

    context.user_data[
        "location"
    ] = ""

    context.user_data.pop(
        "selected_event_location",
        None,
    )

    username_value = get_telegram_username(
        query.from_user
    )

    if username_value:

        context.user_data[
            "telegram_username"
        ] = username_value

    # Cancellation changes the location,
    # so Baserow MUST be synchronized.
    await sync_user_to_baserow(
        user_id=user_id,
        username=username_value or context.user_data.get(
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
        location="",
    )

    await query.edit_message_text(
        "Your event registration has been cancelled. ❌\n\n"
        "Your profile is still saved. ✅"
    )

    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# CANCEL LOCATION FROM PROFILE
# =====================================================================

async def cancel_location_from_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    user_id = update.effective_user.id

    active_registration = (
        get_active_event_registration(
            user_id
        )
    )

    if not active_registration:

        await update.message.reply_text(
            "You are not registered for any "
            "event location. 📍",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    (
        _registration_id,
        registered_location,
    ) = active_registration

    cancelled = cancel_latest_event_registration(
        user_id
    )

    if not cancelled:

        await update.message.reply_text(
            "Your event registration could not "
            "be cancelled.",
            reply_markup=get_profile_edit_keyboard(),
        )

        return EDIT_INFO_MENU

    conn = get_db_connection()

    try:

        conn.execute(
            """
            UPDATE users
            SET location = ''
            WHERE user_id = ?
            """,
            (user_id,),
        )

        conn.commit()

    finally:

        conn.close()

    context.user_data[
        "location"
    ] = ""

    context.user_data.pop(
        "selected_event_location",
        None,
    )

    username_value = get_telegram_username(
        update.effective_user
    )

    if username_value:

        context.user_data[
            "telegram_username"
        ] = username_value

    # Cancellation changes the location,
    # so Baserow MUST be synchronized.
    await sync_user_to_baserow(
        user_id=user_id,
        username=username_value or context.user_data.get(
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
        location="",
    )

    await update.message.reply_text(
        f"Your {registered_location} event "
        "registration has been cancelled. ❌\n\n"
        "Your profile information is still saved. ✅\n"
        "You can register for another location anytime.",
        reply_markup=get_profile_edit_keyboard(),
    )

    return EDIT_INFO_MENU


# =====================================================================
# PROFILE EDITING
# =====================================================================

async def cmd_edit_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data[
        "editing_single_field"
    ] = True

    await update.message.reply_text(
        "Please enter your updated Full Name:",
        reply_markup=ReplyKeyboardRemove(),
    )

    return FULL_NAME


async def cmd_edit_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data[
        "editing_single_field"
    ] = True

    await update.message.reply_text(
        "Please enter your updated Age:",
        reply_markup=ReplyKeyboardRemove(),
    )

    return AGE


async def cmd_edit_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    context.user_data[
        "editing_single_field"
    ] = True

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
        "Please share or enter your updated "
        "Phone Number:",
        reply_markup=contact_keyboard,
    )

    return PHONE


async def cmd_edit_username(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    username_value = get_telegram_username(
        update.effective_user
    )

    context.user_data[
        "telegram_username"
    ] = username_value

    # Actual username update -> sync Baserow.
    await save_current_user(
        update,
        context,
    )

    if username_value:

        message = (
            f"Your Telegram username is {username_value}. ✅"
        )

    else:

        message = (
            "You do not currently have a Telegram username. "
            "You can set one in Telegram and then press "
            "\"4️⃣ Change Username\" again."
        )

    await update.message.reply_text(
        message
    )

    await update.message.reply_text(
        format_user_info_message(
            context.user_data,
            update.effective_user,
        ),
        reply_markup=get_profile_edit_keyboard(),
    )

    return EDIT_INFO_MENU


async def profile_edit_name_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await cmd_edit_name(
        update,
        context,
    )


async def profile_edit_age_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await cmd_edit_age(
        update,
        context,
    )


async def profile_edit_contact_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await cmd_edit_phone(
        update,
        context,
    )


async def profile_edit_username_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    return await cmd_edit_username(
        update,
        context,
    )


# =====================================================================
# PROFILE BACK
# =====================================================================

async def profile_back_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:

    user = get_user(
        update.effective_user.id
    )

    if not user:

        await update.message.reply_text(
            "You don't have a profile.",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION

    load_user_into_context(
        user,
        context,
    )

    # Only read username for display.
    # Do NOT save/sync here.
    username_value = get_telegram_username(
        update.effective_user
    )

    if username_value:

        context.user_data[
            "telegram_username"
        ] = username_value

    await update.message.reply_text(
        format_user_info_message(
            context.user_data,
            update.effective_user,
        ),
        reply_markup=get_profile_keyboard(),
    )

    return MENU_SECTION


# =====================================================================
# GENERAL CANCEL COMMAND
# =====================================================================

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


# =====================================================================
# BOT COMMANDS
# =====================================================================

async def set_bot_commands(
    application: Application,
):

    await application.bot.set_my_commands(
        [
            (
                "start",
                "Start the bot",
            ),
            (
                "restart",
                "Restart the bot",
            ),
            (
                "menu",
                "Open menu",
            ),
        ]
    )


# =====================================================================
# MAIN
# =====================================================================

def main():

    if (
        not BOT_TOKEN
        or BOT_TOKEN == "YOUR_NEW_TELEGRAM_BOT_TOKEN"
    ):

        raise RuntimeError(
            "BOT_TOKEN is not configured. "
            "Set BOT_TOKEN as an environment variable."
        )

    if (
        not BASEROW_TOKEN
        or BASEROW_TOKEN == "YOUR_NEW_BASEROW_TOKEN"
    ):

        raise RuntimeError(
            "BASEROW_TOKEN is not configured. "
            "Set BASEROW_TOKEN as an environment variable."
        )

    # ---------------------------------------------------------------
    # Database
    # ---------------------------------------------------------------

    init_db()

    backup_database()

    # ---------------------------------------------------------------
    # Telegram application
    # ---------------------------------------------------------------

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(set_bot_commands)
        .build()
    )

    # ---------------------------------------------------------------
    # Conversation handler
    # ---------------------------------------------------------------

    conv_handler = ConversationHandler(

        entry_points=[
            CommandHandler(
                "start",
                start,
            ),
            CommandHandler(
                "restart",
                restart,
            ),
            CommandHandler(
                "menu",
                menu_command,
            ),
        ],

        states={

            # =========================================================
            # MAIN MENU
            # =========================================================

            MENU_SECTION: [

                CallbackQueryHandler(
                    start_button_callback,
                    pattern=r"^start_registration$",
                ),

                CallbackQueryHandler(
                    cancel_registration_callback,
                    pattern=r"^cancel_registration$",
                ),

                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    menu_handler,
                ),
            ],

            # =========================================================
            # FULL NAME
            # =========================================================

            FULL_NAME: [

                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    full_name,
                ),
            ],

            # =========================================================
            # AGE
            # =========================================================

            AGE: [

                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    age,
                ),
            ],

            # =========================================================
            # PHONE
            # =========================================================

            PHONE: [

                MessageHandler(
                    (
                        filters.CONTACT
                        | filters.TEXT
                    )
                    & ~filters.COMMAND,
                    phone,
                ),
            ],

            # =========================================================
            # USERNAME
            # =========================================================

            USERNAME: [

                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    username,
                ),
            ],

            # =========================================================
            # LOCATION / EVENT
            # =========================================================

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
                    cancel_registration_callback,
                    pattern=r"^cancel_registration$",
                ),

                CallbackQueryHandler(
                    event_back_callback,
                    pattern=r"^event_back$",
                ),

                MessageHandler(
                    filters.Regex(r"^🔙 Back$"),
                    menu_handler,
                ),
            ],

            # =========================================================
            # PROFILE EDIT
            # =========================================================

            EDIT_INFO_MENU: [

                MessageHandler(
                    filters.Regex(
                        r"^1️⃣ Change Name$"
                    ),
                    profile_edit_name_button,
                ),

                MessageHandler(
                    filters.Regex(
                        r"^2️⃣ Change Age$"
                    ),
                    profile_edit_age_button,
                ),

                MessageHandler(
                    filters.Regex(
                        r"^3️⃣ Change Contact$"
                    ),
                    profile_edit_contact_button,
                ),

                MessageHandler(
                    filters.Regex(
                        r"^4️⃣ Change Username$"
                    ),
                    profile_edit_username_button,
                ),

                MessageHandler(
                    filters.Regex(
                        r"^❌ Cancel Location$"
                    ),
                    cancel_location_from_profile,
                ),

                MessageHandler(
                    filters.Regex(
                        r"^🔙 Back$"
                    ),
                    profile_back_button,
                ),

                CommandHandler(
                    "editname",
                    cmd_edit_name,
                ),

                CommandHandler(
                    "editage",
                    cmd_edit_age,
                ),

                CommandHandler(
                    "editphone",
                    cmd_edit_phone,
                ),

                CommandHandler(
                    "editusername",
                    cmd_edit_username,
                ),
            ],
        },

        fallbacks=[

            CommandHandler(
                "restart",
                restart,
            ),

            CommandHandler(
                "menu",
                menu_command,
            ),

            CommandHandler(
                "cancel",
                cancel,
            ),

            CommandHandler(
                "editname",
                cmd_edit_name,
            ),

            CommandHandler(
                "editage",
                cmd_edit_age,
            ),

            CommandHandler(
                "editphone",
                cmd_edit_phone,
            ),

            CommandHandler(
                "editusername",
                cmd_edit_username,
            ),

            CallbackQueryHandler(
                cancel_registration_callback,
                pattern=r"^cancel_registration$",
            ),
        ],

        per_chat=True,
        per_user=True,
        per_message=False,
    )

    app.add_handler(
        conv_handler
    )

    logger.info(
        "Bot started. Press Ctrl+C to stop."
    )

    # ---------------------------------------------------------------
    # Python 3.14 event loop compatibility
    # ---------------------------------------------------------------

    try:

        asyncio.get_event_loop()

    except RuntimeError:

        asyncio.set_event_loop(
            asyncio.new_event_loop()
        )

    # ---------------------------------------------------------------
    # Start bot
    # ---------------------------------------------------------------

    app.run_polling(
        drop_pending_updates=True
    )


# =====================================================================
# RUN
# =====================================================================

if __name__ == "__main__":
    main()