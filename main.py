import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor
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
from telegram.error import InvalidToken
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    ApplicationHandlerStop,
    filters,
)


# =====================================================================
# CONFIG
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip() or None
BASEROW_TOKEN = os.getenv("BASEROW_TOKEN", "").strip() or None

# Optional channel whose posts should be forwarded to all users who have started the bot.
# Set ANNOUNCEMENT_CHANNEL_ID to the channel numeric ID (usually starts with -100).
_announcement_channel_id = os.getenv("ANNOUNCEMENT_CHANNEL_ID", "").strip()
try:
    ANNOUNCEMENT_CHANNEL_ID = int(_announcement_channel_id) if _announcement_channel_id else None
except ValueError:
    raise RuntimeError("ANNOUNCEMENT_CHANNEL_ID must be a numeric Telegram channel ID.")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured. Set BOT_TOKEN as an environment variable."
    )

if not BASEROW_TOKEN:
    # Keep the bot process alive so the platform smoke-test can complete.
    # Baserow-backed registration/profile syncing requires BASEROW_TOKEN to be
    # configured in the hosting platform's environment settings.
    logging.warning(
        "BASEROW_TOKEN is not configured. The bot can start, but Baserow "
        "features will not work until BASEROW_TOKEN is set."
    )

WEBSITE_URL = "https://articular.uz"
WEBSITE_IMAGE = "LOGO ARTICULARUZ (2).png"

SUBSCRIPTION_CHANNEL = "@articularuz"
SUBSCRIPTION_CHANNEL_URL = "https://t.me/articularuz"


# =====================================================================
# BASEROW TABLES
# =====================================================================

# Keep each key aligned with its corresponding Baserow table ID.
BASEROW_TABLE_IDS = {
    "Users": "1237476",
    "Tashkent": "1241776",
    "Tashkent Region": "1241955",
    "Bukhara": "1241780",
    "Sirdaryo": "1241781",
    "Andijan": "1241953",
    "Fergana": "1275284",
    # Navoiy table ID from the current Baserow setup.
    "Navoiy": "1274288",
}


# =====================================================================
# EVENT LOCATIONS
# =====================================================================

LOCATIONS = {
    # Tashkent and Tashkent Region use the same Telegram group.
    "Tashkent Region": "https://t.me/articularuztoshkent",
    # These links must point to the official group for each event location.
    "Sirdaryo": "https://t.me/articularuzsyrdarya",
    "Fergana": "https://t.me/articularuzfargona",
    "Andijan": "https://t.me/articularuzandijon",
    "Bukhara": "https://t.me/articularuzbuxoro",
    "Navoiy": "https://t.me/articularuznavoiy",
}

# Only these locations can be selected for a new registration.
# Tashkent and Tashkent Region are represented by one shared location.
VALID_CITIES = (
    "Tashkent Region",
    "Sirdaryo",
    "Fergana",
    "Andijan",
    "Bukhara",
    "Navoiy",
)

# Canonical location names used in buttons, messages, SQLite, and Baserow.
# These aliases let older records continue to work without displaying mixed names.
LOCATION_ALIASES = {
    "Tashkent": "Tashkent Region",
    "Tashkent / Tashkent Region": "Tashkent Region",
    "Tashkent/Tashkent Region": "Tashkent Region",
    "Tashkent Region": "Tashkent Region",
    # Accept historical/alternate spellings, but always display/store Sirdaryo.
    "Sirdaryo": "Sirdaryo",
    "Sirdarya": "Sirdaryo",
    "Syrdarya": "Sirdaryo",
    "Syrdaryo": "Sirdaryo",
    "sirdaryo": "Sirdaryo",
    "sirdarya": "Sirdaryo",
    "syrdarya": "Sirdaryo",
    "syrdaryo": "Sirdaryo",
    "Fergana": "Fergana",
    "Andijan": "Andijan",
    "Buhoro": "Bukhara",
    "Bukhara": "Bukhara",
    "Navoiy": "Navoiy",
}

def normalize_location(location: str | None) -> str:
    """Return the single canonical display/storage name for a location."""
    if not location:
        return ""
    cleaned = str(location).strip()
    return LOCATION_ALIASES.get(
        cleaned,
        LOCATION_ALIASES.get(cleaned.casefold(), cleaned),
    )


# Retain old city tables for safe cleanup of legacy registrations.
ALL_CITY_TABLES = (
    "Tashkent",
    "Tashkent Region",
    "Bukhara",
    "Sirdaryo",
    "Andijan",
    "Fergana",
    "Navoiy",
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


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ArticularUz bot is running")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))

    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    logging.info("Health server listening on port %s", port)

# =====================================================================
# BASEROW HELPERS
# =====================================================================

BASEROW_API_BASE = "https://api.baserow.io/api/database/rows/table"


def _baserow_url(table_name: str, row_id: int | None = None) -> str:
    table_id = BASEROW_TABLE_IDS[table_name]
    if row_id is None:
        return f"{BASEROW_API_BASE}/{table_id}/?user_field_names=true"
    return f"{BASEROW_API_BASE}/{table_id}/{row_id}/?user_field_names=true"


def _baserow_headers():
    return {
        "Authorization": f"Token {BASEROW_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "ArticularUZBot/1.0",
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
    location = normalize_location(location)
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
    """Always get the current Telegram username directly from Telegram."""
    if not user:
        return ""
    if user.username:
        return f"@{user.username}"
    return ""


# =====================================================================
# BASEROW: FIND ONE USER ROW
# =====================================================================

def _get_baserow_rows_sync(table_name: str, user_id: int):
    """
    Find this user's rows in one specific table.

    IMPORTANT: This function intentionally checks only the table supplied.
    We no longer scan every city table for every profile change.
    """
    query = urllib.parse.urlencode({
        "user_field_names": "true",
        "size": "200",
        "filter__Telegram ID__equal": str(user_id),
    })

    table_id = BASEROW_TABLE_IDS[table_name]
    url = f"{BASEROW_API_BASE}/{table_id}/?{query}"

    request = urllib.request.Request(
        url,
        method="GET",
        headers=_baserow_headers(),
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))

    return data.get("results", [])


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
    rows = _get_baserow_rows_sync("Users", user_id)

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
            registered_at=row.get("Registered At") or _utc_now(),
        )

        request = urllib.request.Request(
            _baserow_url("Users", row_id),
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
            headers=_baserow_headers(),
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Users update failed: HTTP {response.status}")

        for duplicate in rows[1:]:
            _delete_baserow_row_sync("Users", duplicate["id"])
        logger.info("Updated Users row %s for Telegram ID %s", row_id, user_id)
        return

    payload = _baserow_payload(
        user_id=user_id,
        username=username,
        full_name=full_name,
        age=age,
        phone=phone,
        location=location,
        registered_at=_utc_now(),
    )

    request = urllib.request.Request(
        _baserow_url("Users"),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=_baserow_headers(),
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Users creation failed: HTTP {response.status}")

    logger.info("Created Users row for Telegram ID %s", user_id)


# =====================================================================
# BASEROW: UPDATE ONE CITY ROW
# =====================================================================

def _sync_city_row_sync(
    city: str,
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
):
    """
    Update/create the user in the KNOWN city table only.

    The old implementation searched all six city tables and then deleted
    rows before adding the user again. That was the main source of delay.
    """
    if city not in VALID_CITIES:
        return

    rows = _get_baserow_rows_sync(city, user_id)

    if rows:
        row = rows[0]
        payload = _baserow_payload(
            user_id=user_id,
            username=username,
            full_name=full_name,
            age=age,
            phone=phone,
            location=city,
            registered_at=row.get("Registered At") or _utc_now(),
        )

        request = urllib.request.Request(
            _baserow_url(city, row["id"]),
            data=json.dumps(payload).encode("utf-8"),
            method="PATCH",
            headers=_baserow_headers(),
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"{city} update failed: HTTP {response.status}"
                )

        for duplicate in rows[1:]:
            _delete_baserow_row_sync(city, duplicate["id"])
        logger.info("Updated Telegram ID %s in %s", user_id, city)
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

    request = urllib.request.Request(
        _baserow_url(city),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=_baserow_headers(),
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"{city} creation failed: HTTP {response.status}")

    logger.info("Added Telegram ID %s to %s", user_id, city)


def _delete_baserow_row_sync(table_name: str, row_id: int):
    """Delete one row by its Baserow row ID."""
    request = urllib.request.Request(
        _baserow_url(table_name, row_id),
        method="DELETE",
        headers=_baserow_headers(),
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"Failed deleting row {row_id} from {table_name}: HTTP {response.status}"
            )


# =====================================================================
# BASEROW: DELETE FROM ONE KNOWN CITY
# =====================================================================

def _delete_user_from_city_sync(city: str, user_id: int):
    """Delete the user from one known city table only."""
    if city not in ALL_CITY_TABLES:
        return

    rows = _get_baserow_rows_sync(city, user_id)
    table_id = BASEROW_TABLE_IDS[city]

    for row in rows:
        request = urllib.request.Request(
            f"{BASEROW_API_BASE}/{table_id}/{row['id']}/",
            method="DELETE",
            headers=_baserow_headers(),
        )

        with urllib.request.urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"Failed deleting row {row['id']} from {city}: HTTP {response.status}"
                )

        logger.info("Removed Telegram ID %s from %s", user_id, city)


# =====================================================================
# BASEROW: LEGACY FULL CITY CLEANUP
# =====================================================================

async def remove_user_from_city_tables(user_id: int, known_city: str | None = None):
    """
    Fast path: if the city is known, delete only from that city.

    The all-city fallback is kept for compatibility, but normal bot actions
    should always provide known_city so only one Baserow table is queried.
    """
    if known_city in VALID_CITIES:
        await asyncio.to_thread(
            _delete_user_from_city_sync,
            known_city,
            user_id,
        )
        return

    # Fallback only for old/unrecoverable data where the location is unknown.
    # Run the six lookups concurrently instead of sequentially.
    await asyncio.gather(*[
        asyncio.to_thread(_delete_user_from_city_sync, city, user_id)
        for city in ALL_CITY_TABLES
    ], return_exceptions=True)


# =====================================================================
# BASEROW: FAST USER SYNCHRONIZATION
# =====================================================================

def _sync_user_to_baserow_sync(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    location: str,
):
    """
    Synchronize Users + the known city in parallel.

    Before:
        Users lookup/update
        + six city lookups
        + possible deletes
        + city lookup/create

    Now:
        Users lookup/update
        + one known-city lookup/update

    The two operations are executed concurrently.
    """
    if not BASEROW_TOKEN:
        logger.warning("BASEROW_TOKEN is not configured.")
        return

    def sync_users():
        _sync_users_table_sync(
            user_id=user_id,
            username=username,
            full_name=full_name,
            age=age,
            phone=phone,
            location=location,
        )

    def sync_city():
        if location in VALID_CITIES:
            _sync_city_row_sync(
                city=location,
                user_id=user_id,
                username=username,
                full_name=full_name,
                age=age,
                phone=phone,
            )

    try:
        # Users and city are independent Baserow requests, so run them together.
        with ThreadPoolExecutor(max_workers=2) as executor:
            users_future = executor.submit(sync_users)
            city_future = executor.submit(sync_city)
            users_future.result()
            city_future.result()

        logger.info("Baserow sync completed for Telegram ID %s", user_id)
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("Baserow HTTP error %s: %s", exc.code, body)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("Baserow connection error: %s", exc)
    except Exception:
        logger.exception("Unexpected Baserow synchronization error")
    return False


async def sync_user_to_baserow(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: int | str,
    location: str,
):
    return await asyncio.to_thread(
        _sync_user_to_baserow_sync,
        user_id,
        username,
        full_name,
        age,
        phone,
        location,
    )


# =====================================================================
# BASEROW: FAST CANCELLATION SYNC
# =====================================================================

async def cancel_user_in_baserow(
    user_id: int,
    username: str,
    full_name: str,
    age: int,
    phone: str,
    registered_city: str,
):
    """Clear the Users location and remove all city rows for this user.

    This deliberately checks every city only on cancellation, because stale
    SQLite state or historical duplicate rows must not leave an event active
    in Baserow after the user cancels.
    """
    if not BASEROW_TOKEN:
        logger.error("Cannot cancel Baserow registration: BASEROW_TOKEN is missing")
        return False

    def clear_users():
        _sync_users_table_sync(
            user_id=user_id,
            username=username,
            full_name=full_name,
            age=age,
            phone=phone,
            location="",
        )

    tasks = [
        asyncio.to_thread(_delete_user_from_city_sync, city, user_id)
        for city in ALL_CITY_TABLES
    ]
    tasks.append(asyncio.to_thread(clear_users))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException) or result is False]
    if errors:
        for error in errors:
            logger.error(
                "Baserow cancellation step failed for Telegram ID %s: %s",
                user_id,
                error,
            )
        return False

    logger.info("Baserow cancellation completed for Telegram ID %s (previous city: %s)", user_id, registered_city)
    return True


# SQLITE DATABASE
# =====================================================================

# All persistent files must live inside DATA_DIR. Use the hosting platform's configured
# directory, defaulting to /data rather than writing beside the source code.
DATA_DIR = os.environ.get("DATA_DIR", "/data").strip() or "/data"
DATA_DIR = os.path.abspath(DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "registrations.db")


def get_db_connection():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    # WAL mode is configured once in init_db(), not on every short-lived
    # connection; repeatedly setting it adds avoidable locking/latency.
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")

    return conn


def init_db():
    conn = get_db_connection()

    try:
        conn.execute("PRAGMA journal_mode=WAL")
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

        # Track everyone who has started the bot, including users who have not
        # completed registration. This avoids creating incomplete profile rows in Baserow.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Normalize older location spellings so the database uses one name per location.
        location_migrations = {
            "Tashkent": "Tashkent Region",
            "Tashkent / Tashkent Region": "Tashkent Region",
            "Tashkent/Tashkent Region": "Tashkent Region",
            # Consolidate all legacy spellings into the canonical name.
            "Sirdarya": "Sirdaryo",
            "Syrdarya": "Sirdaryo",
            "Syrdaryo": "Sirdaryo",
            "sirdarya": "Sirdaryo",
            "syrdarya": "Sirdaryo",
            "syrdaryo": "Sirdaryo",
            "Buhoro": "Bukhara",
        }
        for old_name, canonical_name in location_migrations.items():
            cursor.execute(
                "UPDATE users SET location = ? WHERE LOWER(TRIM(location)) = LOWER(?)",
                (canonical_name, old_name),
            )
            cursor.execute(
                "UPDATE event_registrations SET location = ? "
                "WHERE LOWER(TRIM(location)) = LOWER(?)",
                (canonical_name, old_name),
            )

        # Keep the newest active registration if old data contains duplicates.
        cursor.execute(
            """
            UPDATE event_registrations
            SET cancelled_at = CURRENT_TIMESTAMP
            WHERE cancelled_at IS NULL
              AND registration_id NOT IN (
                  SELECT MAX(registration_id)
                  FROM event_registrations
                  WHERE cancelled_at IS NULL
                  GROUP BY user_id
              )
            """
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_event_per_user
            ON event_registrations(user_id)
            WHERE cancelled_at IS NULL
            """
        )

        conn.commit()

    finally:
        conn.close()


# =====================================================================
# BROADCAST RECIPIENTS
# =====================================================================

def remember_broadcast_recipient(user) -> None:
    """Remember every person who starts/restarts the bot for future broadcasts."""
    if not user or getattr(user, "is_bot", False):
        return
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO broadcast_recipients (user_id, username, first_seen, last_seen)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                last_seen = CURRENT_TIMESTAMP
            """,
            (int(user.id), get_telegram_username(user)),
        )
        conn.commit()
    finally:
        conn.close()


def get_broadcast_recipient_ids() -> list[int]:
    """Merge locally tracked bot starters with Telegram IDs in Baserow Users.

    Local tracking includes people who started the bot but did not finish profile
    registration. Baserow Users adds existing registered users after a redeploy.
    """
    recipient_ids: set[int] = set()

    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT user_id FROM broadcast_recipients
            UNION
            SELECT user_id FROM users
            """
        ).fetchall()
        recipient_ids.update(int(row[0]) for row in rows if row and row[0] is not None)
    finally:
        conn.close()

    # Also load registered recipients from Baserow, including users from before
    # the current Railway SQLite database was created. Follow pagination safely.
    if BASEROW_TOKEN:
        try:
            url = f"{BASEROW_API_BASE}/{BASEROW_TABLE_IDS['Users']}/?user_field_names=true&size=200"
            while url:
                request = urllib.request.Request(url, method="GET", headers=_baserow_headers())
                with urllib.request.urlopen(request, timeout=15) as response:
                    data = json.loads(response.read().decode("utf-8"))
                for row in data.get("results", []):
                    value = row.get("Telegram ID")
                    if value is not None:
                        try:
                            recipient_ids.add(int(value))
                        except (TypeError, ValueError):
                            logger.warning("Ignoring invalid Telegram ID in Baserow: %r", value)
                url = data.get("next")
        except Exception:
            logger.exception("Could not load broadcast recipients from Baserow; using local recipients")

    return sorted(recipient_ids)


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
    location = normalize_location(location)
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

        row = cursor.fetchone()
        if row:
            canonical_location = normalize_location(row[5])
            if canonical_location != (row[5] or ""):
                conn.execute(
                    "UPDATE users SET location = ? WHERE user_id = ?",
                    (canonical_location, user_id),
                )
                conn.commit()
                row = (*row[:5], canonical_location)
        return row

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

def get_active_event_registration(user_id: int):
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


def create_event_registration(user_id: int, location: str):
    """Atomically create at most one active event registration per user."""
    location = normalize_location(location)
    if location not in VALID_CITIES:
        raise ValueError(f"Invalid event location: {location!r}")

    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT registration_id FROM event_registrations
               WHERE user_id = ? AND cancelled_at IS NULL
               ORDER BY registration_id DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
        if existing:
            conn.rollback()
            return None

        cursor = conn.execute(
            "INSERT INTO event_registrations (user_id, location) VALUES (?, ?)",
            (user_id, location),
        )
        registration_id = cursor.lastrowid
        conn.commit()
        return registration_id
    except sqlite3.IntegrityError:
        conn.rollback()
        # A concurrent request may have created the registration first.
        return None
    finally:
        conn.close()


def cancel_latest_event_registration(user_id: int):
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


async def get_user_with_baserow_fallback(user_id: int):
    """Return a local profile, recovering it from Baserow when SQLite is empty.

    When the remote profile records an event location but SQLite has no active
    registration, reconstruct the local active registration as well. This is
    important after an ephemeral-host restart or database replacement.
    """
    local_user = get_user(user_id)
    active = get_active_event_registration(user_id)

    if local_user and active:
        if local_user[5] != active[1]:
            save_user(user_id, local_user[1] or "", local_user[2] or "", local_user[3] or 0,
                      local_user[4] or "", active[1])
            local_user = get_user(user_id)
        return local_user

    if local_user and local_user[5] in VALID_CITIES and not active:
        registration_id = create_event_registration(user_id, local_user[5])
        if registration_id is not None:
            return local_user
        active = get_active_event_registration(user_id)
        if active:
            save_user(user_id, local_user[1] or "", local_user[2] or "", local_user[3] or 0,
                      local_user[4] or "", active[1])
            return get_user(user_id)

    # SQLite is authoritative once a local profile exists. In particular, an
    # empty location means the user canceled or has not registered for an
    # event; do not query Baserow and accidentally restore stale remote state.
    # This also avoids a slow Baserow request on routine menu/profile actions.
    if local_user:
        return local_user

    # Only query Baserow when this deployment has no local profile to recover.
    try:
        rows = await asyncio.to_thread(_get_baserow_rows_sync, "Users", user_id)
    except Exception:
        logger.exception("Could not recover Telegram ID %s from Baserow", user_id)
        return local_user

    if not rows:
        return local_user

    row = rows[0]
    username = (row.get("Telegram Username") or "").lstrip("@")
    full_name = row.get("Full Name") or ""
    raw_age = row.get("Age")
    try:
        age_value = int(raw_age) if raw_age not in (None, "") else 0
    except (TypeError, ValueError):
        age_value = 0
    phone_value = str(row.get("Phone") or "")
    remote_location = normalize_location(row.get("Event Location"))

    # Preserve locally edited fields if SQLite already has a profile; otherwise
    # use Baserow as the recovery source.
    if local_user:
        username = local_user[1] or username
        full_name = local_user[2] or full_name
        age_value = local_user[3] or age_value
        phone_value = local_user[4] or phone_value
        # Local SQLite is authoritative when a local profile exists. In
        # particular, a blank local location means the user canceled; do not
        # reactivate a stale remote location if a previous Baserow sync failed.
        remote_location = local_user[5] or ""

    save_user(user_id, username, full_name, age_value, phone_value, remote_location)
    if remote_location in VALID_CITIES and not get_active_event_registration(user_id):
        create_event_registration(user_id, remote_location)
    active = get_active_event_registration(user_id)
    if active:
        save_user(user_id, username, full_name, age_value, phone_value, active[1])
    return get_user(user_id)


# =====================================================================
# DATABASE BACKUP
# =====================================================================

def backup_database():
    if not os.path.exists(DB_PATH):
        return

    backup_dir = os.path.join(DATA_DIR, "db_backups")

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
    destination = sqlite3.connect(backup_path)

    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    backups = sorted(
        Path(backup_dir).glob("registrations_*.db"),
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
            [KeyboardButton("👤 Profile")],
            [KeyboardButton("🎯 Choose an event")],
        ],
        resize_keyboard=True,
    )


def get_back_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("❌ Cancel Location")],
            [KeyboardButton("🔙 Back")],
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
                KeyboardButton("🔙 Back"),
            ],
        ],
        resize_keyboard=True,
    )


def get_start_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Start", callback_data="start_registration")]]
    )


def get_subscription_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join our channel", url=SUBSCRIPTION_CHANNEL_URL)],
            [InlineKeyboardButton("✅ Check subscription", callback_data="check_subscription")],
        ]
    )


SUBSCRIPTION_CACHE_TTL_SECONDS = 5
_subscription_cache: dict[int, tuple[float, bool]] = {}


async def is_subscribed(bot, user_id: int, *, force_refresh: bool = False) -> bool:
    """Check membership with a short cache to avoid a network request on every message.

    The explicit subscription button always forces a live check, so a user can
    continue immediately after joining. Cache failures are not stored.
    """
    now = time.monotonic()
    cached = _subscription_cache.get(user_id)
    if not force_refresh and cached and now - cached[0] < SUBSCRIPTION_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        member = await bot.get_chat_member(
            chat_id=SUBSCRIPTION_CHANNEL,
            user_id=user_id,
            read_timeout=4,
            write_timeout=4,
            connect_timeout=4,
            pool_timeout=4,
        )
        subscribed = member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and bool(getattr(member, "is_member", False))
        )
        _subscription_cache[user_id] = (time.monotonic(), subscribed)
        return subscribed
    except Exception as exc:
        logger.warning(
            "Subscription check failed for Telegram ID %s (check bot admin permissions for %s): %s",
            user_id,
            SUBSCRIPTION_CHANNEL,
            exc,
        )
        return False


async def send_subscription_prompt(message, *, edit=False):
    text = (
        "Welcome to ArticularUZ! 🚀\n\n"
        "Please subscribe to our Telegram channel to use the bot.\n"
        "After subscribing, press ‘Check subscription’."
    )
    if edit:
        await message.edit_text(text, reply_markup=get_subscription_keyboard())
    else:
        await message.reply_text(text, reply_markup=get_subscription_keyboard())


def get_event_location_keyboard():
    # Tashkent and Tashkent Region are separate visible buttons but share the
    # canonical internal location, database record, and Telegram group.
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
                    "📍 Tashkent Region",
                    callback_data="Tashkent Region",
                )
            ],
            [
                InlineKeyboardButton(
                    "📍 Sirdaryo",
                    callback_data="Sirdaryo",
                )
            ],
            [
                InlineKeyboardButton(
                    "📍 Fergana",
                    callback_data="Fergana",
                )
            ],
            [
                InlineKeyboardButton(
                    "📍 Andijan",
                    callback_data="Andijan",
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
                    "📍 Navoiy",
                    callback_data="Navoiy",
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
            ]
        ]
    )


def get_registered_event_keyboard():
    # Show both actions when a user already has an active event registration.
    # The cancellation handler is registered in the LOCATION conversation state.
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("❌ Cancel Location")],
            [KeyboardButton("🔙 Back")],
        ],
        resize_keyboard=True,
    )


def get_join_group_keyboard(location):
    canonical_location = normalize_location(location)
    group_link = LOCATIONS.get(canonical_location, "").strip()
    if not group_link or "your_" in group_link:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔗 Join {canonical_location} Group", url=group_link)]]
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

    username = get_telegram_username(telegram_user)

    if not username:
        username = user_data.get(
            "telegram_username",
            "",
        ) or "N/A"

    location = normalize_location(user_data.get("location")) or "N/A"

    return (
        "Here is your profile information:\n\n"
        f"👤 Name: {name}\n"
        f"🎂 Age: {age}\n"
        f"📞 Phone: {phone}\n"
        f"📱 Username: {username}\n"
        f"📍 Event location: {location}\n\n"
        "Use the buttons below to update your profile "
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
    user_id = update.effective_user.id
    remember_broadcast_recipient(update.effective_user)

    if not await is_subscribed(context.bot, user_id, force_refresh=True):
        await send_subscription_prompt(update.effective_message)
        return MENU_SECTION

    existing_user = await get_user_with_baserow_fallback(user_id)
    if existing_user:
        load_user_into_context(existing_user, context)
        await update.effective_message.reply_text(
            "Welcome back to ArticularUZ! 👋\n\n"
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    await update.effective_message.reply_text(
        "Welcome to ArticularUZ! 🚀\n\n"
        "You're subscribed! Press Start to continue.",
        reply_markup=get_start_keyboard(),
    )
    return MENU_SECTION


async def restart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    context.user_data.clear()
    user_id = update.effective_user.id
    remember_broadcast_recipient(update.effective_user)

    if not await is_subscribed(context.bot, user_id, force_refresh=True):
        await send_subscription_prompt(update.effective_message)
        return MENU_SECTION

    existing_user = await get_user_with_baserow_fallback(user_id)
    if existing_user:
        load_user_into_context(existing_user, context)
        await update.effective_message.reply_text(
            "Welcome back to ArticularUZ! 👋\n\n"
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    await update.effective_message.reply_text(
        "Welcome to ArticularUZ! 🚀\n\n"
        "You're subscribed! Press Start to continue.",
        reply_markup=get_start_keyboard(),
    )
    return MENU_SECTION


async def check_subscription_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    query = update.callback_query
    user_id = query.from_user.id

    if not await is_subscribed(context.bot, user_id, force_refresh=True):
        await query.answer(
            "I couldn't confirm your subscription. Join the channel first, then try again.",
            show_alert=True,
        )
        return MENU_SECTION

    await query.answer("Subscription confirmed!")

    context.user_data.clear()
    existing_user = await get_user_with_baserow_fallback(user_id)

    if existing_user:
        load_user_into_context(existing_user, context)
        await query.edit_message_text(
            "Welcome back to ArticularUZ! 👋\n\n"
            "Subscription confirmed!"
        )
        await query.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )
        return MENU_SECTION

    await query.edit_message_text(
        "Welcome to ArticularUZ! 🚀\n\n"
        "Subscription confirmed! Let's get you registered. 📝"
    )
    await query.message.reply_text(
        "What's your full name?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return FULL_NAME


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
        "location": normalize_location(db_location),
    })


# =====================================================================
# MENU COMMAND
# =====================================================================

async def menu_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    user_id = update.effective_user.id

    if not await is_subscribed(context.bot, user_id):
        await send_subscription_prompt(update.effective_message)
        return MENU_SECTION

    existing_user = await get_user_with_baserow_fallback(user_id)

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

    if not await is_subscribed(context.bot, user_id):
        await query.edit_message_text(
            "Welcome to ArticularUZ! 🚀\n\n"
            "Please subscribe to our Telegram channel to use the bot.",
            reply_markup=get_subscription_keyboard(),
        )
        return MENU_SECTION

    existing_user = await get_user_with_baserow_fallback(user_id)

    await query.edit_message_reply_markup(
        reply_markup=None
    )

    if existing_user:
        load_user_into_context(
            existing_user,
            context,
        )

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

    context.user_data["full_name"] = name

    if context.user_data.get("editing_single_field"):
        context.user_data.pop(
            "editing_single_field",
            None,
        )

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

    context.user_data["age"] = int(text)

    if context.user_data.get("editing_single_field"):
        context.user_data.pop(
            "editing_single_field",
            None,
        )

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
        contact = update.message.contact
        if contact.user_id is not None and contact.user_id != update.effective_user.id:
            await update.message.reply_text(
                "Please share your own contact using the button below, or type your phone number."
            )
            return PHONE
        phone_number = contact.phone_number

    elif update.message.text:
        text = update.message.text.strip()

        if re.match(r"^\+?\d{7,15}$", text):
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

    context.user_data["phone"] = phone_number

    if context.user_data.get("editing_single_field"):
        context.user_data.pop(
            "editing_single_field",
            None,
        )

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

    username_value = get_telegram_username(
        update.effective_user
    )

    context.user_data["telegram_username"] = username_value

    await save_current_user(
        update,
        context,
    )


    await update.message.reply_text(
        "Registration completed successfully! ✅\n\n"
        "Now, let's register you for an event! 🎯",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update.message.reply_text(
        "Select your preferred event location:",
        reply_markup=get_event_location_keyboard(),
    )

    await update.message.reply_text(
        "Use the buttons below to go back or cancel.",
        reply_markup=get_back_keyboard(),
    )

    return LOCATION


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

    context.user_data["telegram_username"] = username_value

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
        context.user_data["telegram_username"] = username_value
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

    save_user(
        user_id=user_id,
        username=username_value,
        full_name=full_name,
        age=age_value,
        phone=phone_number,
        location=location,
    )

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
        context.user_data["telegram_username"] = username_value

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

    if text == "👤 Profile":
        user_id = update.effective_user.id
        existing_user = await get_user_with_baserow_fallback(user_id)

        if not existing_user:
            try:
                rows = await asyncio.to_thread(_get_baserow_rows_sync, "Users", user_id)
                if rows:
                    row = rows[0]
                    existing_user = (
                        user_id,
                        (row.get("Telegram Username") or "").lstrip("@"),
                        row.get("Full Name") or "",
                        row.get("Age"),
                        row.get("Phone") or "",
                        row.get("Event Location") or "",
                    )
                    # Rehydrate local SQLite so later menu actions find them too.
                    try:
                        save_user(
                            user_id=user_id,
                            username=existing_user[1],
                            full_name=existing_user[2],
                            age=existing_user[3],
                            phone=existing_user[4],
                            location=existing_user[5],
                        )
                    except Exception:
                        logger.exception("Could not cache Baserow profile for %s", user_id)
            except Exception:
                logger.exception("Could not look up profile in Baserow for %s", user_id)

        if not existing_user:
            context.user_data.clear()
            await update.message.reply_text(
                "You don't have a profile yet. Please complete registration first.\n\n"
                "Use /start to begin registration.",
                reply_markup=get_profile_register_keyboard(),
            )
            return MENU_SECTION

        load_user_into_context(
            existing_user,
            context,
        )

        username_value = get_telegram_username(
            update.effective_user
        )

        if username_value:
            context.user_data["telegram_username"] = username_value

        await update.message.reply_text(
            format_user_info_message(
                context.user_data,
                update.effective_user,
            ),
            reply_markup=get_profile_keyboard(),
        )

        return MENU_SECTION

    if text == "✏️ Change profile info":
        existing_user = await get_user_with_baserow_fallback(
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

    if text == "📝 Register":
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

    # This Back is only for normal ReplyKeyboard menus.
    # The event-location Back button is an INLINE callback.
    if text == "🔙 Back":
        await update.message.reply_text(
            "Choose an option from the menu below:",
            reply_markup=get_main_menu_keyboard(),
        )

        return MENU_SECTION


    if text == "🎯 Choose an event":
        user_id = update.effective_user.id

        await get_user_with_baserow_fallback(user_id)
        active_registration = get_active_event_registration(user_id)

        if active_registration:
            (
                _registration_id,
                registered_location,
            ) = active_registration

            registered_location = normalize_location(registered_location)
            context.user_data["location"] = registered_location

            # IMPORTANT:
            # Back is now an inline button, not a separate
            # ReplyKeyboard button/message.
            await update.message.reply_text(
                "You are already registered! ✅\n\n"
                f"📍 Event location: {registered_location}\n\n"
                "You cannot register for another "
                "location while this registration is active.",
                reply_markup=get_registered_event_keyboard(),
            )

            return LOCATION

        await update.message.reply_text(
            "Select your preferred event location:",
            reply_markup=get_event_location_keyboard(),
        )

        await update.message.reply_text(
            "Use the button below to go back.",
            reply_markup=get_back_keyboard(),
        )
        return LOCATION

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

    chosen = normalize_location(query.data)
    user_id = query.from_user.id

    if chosen not in VALID_CITIES:
        await query.edit_message_text(
            "That event location is not recognized. Please choose an available location."
        )

        return MENU_SECTION

    await get_user_with_baserow_fallback(user_id)
    active_registration = get_active_event_registration(user_id)

    if active_registration:
        (
            _registration_id,
            registered_location,
        ) = active_registration

        registered_location = normalize_location(registered_location)
        context.user_data["location"] = registered_location

        await query.edit_message_text(
            f"📍 You are already registered in {registered_location}."
        )

        await query.message.reply_text(
            "Use the button below to go back.",
            reply_markup=get_registered_event_keyboard(),
        )

        return LOCATION

    existing_user = get_user(user_id)

    if existing_user:
        load_user_into_context(
            existing_user,
            context,
        )

    username_value = get_telegram_username(
        query.from_user
    )

    if username_value:
        context.user_data["telegram_username"] = username_value

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

    context.user_data["selected_event_location"] = chosen

    username_display = (
        context.user_data.get(
            "telegram_username"
        )
        or "No username"
    )

    preview = (
        "Please confirm your registration:\n\n"
        f"👤 Name: {context.user_data.get('full_name')}\n"
        f"🎂 Age: {context.user_data.get('age')}\n"
        f"📞 Phone: {context.user_data.get('phone')}\n"
        f"📱 Username: {username_display}\n"
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
    chosen = normalize_location(context.user_data.get("selected_event_location"))

    if chosen not in VALID_CITIES:
        await query.edit_message_text("No valid event location selected. Please choose a location again.")
        await query.message.reply_text("Choose an option from the menu below:", reply_markup=get_main_menu_keyboard())
        return MENU_SECTION

    existing_user = await get_user_with_baserow_fallback(user_id)
    active_registration = get_active_event_registration(user_id)
    if active_registration:
        registered_location = normalize_location(active_registration[1])
        context.user_data["location"] = registered_location
        context.user_data.pop("selected_event_location", None)
        await query.edit_message_text(
            "You are already registered! ✅\n\n"
            f"📍 Event location: {registered_location}\n\n"
            "You cannot register for another location while this registration is active."
        )
        await query.message.reply_text(
            "You can cancel your current location or go back.",
            reply_markup=get_registered_event_keyboard(),
        )
        return LOCATION

    if not existing_user:
        await query.edit_message_text("Your profile could not be found. Please use /start to register first.")
        await query.message.reply_text("Choose an option from the menu below:", reply_markup=get_main_menu_keyboard())
        return MENU_SECTION

    username_value = get_telegram_username(query.from_user) or context.user_data.get("telegram_username", "")
    full_name_value = context.user_data.get("full_name") or existing_user[2] or ""
    age_value = context.user_data.get("age") or existing_user[3] or 0
    phone_value = context.user_data.get("phone") or existing_user[4] or ""

    if not full_name_value or not age_value or not phone_value:
        await query.edit_message_text("Your profile is incomplete. Please update your profile before registering for an event.")
        await query.message.reply_text("Choose an option from the menu below:", reply_markup=get_main_menu_keyboard())
        return MENU_SECTION

    # The profile row already exists. Do not write its location until the
    # atomic event-registration insert succeeds; a second rapid click must not
    # clear the location written by the first successful request.
    registration_id = create_event_registration(user_id, chosen)
    if registration_id is None:
        active_registration = get_active_event_registration(user_id)
        registered_location = normalize_location(active_registration[1]) if active_registration else "another location"
        await query.edit_message_text(
            "You already have an active event registration.\n"
            f"📍 Event location: {registered_location}"
        )
        await query.message.reply_text(
            "You can cancel your current location or go back.",
            reply_markup=get_registered_event_keyboard(),
        )
        return LOCATION

    # Update profile location only after the active registration was created.
    save_user(user_id, username_value, full_name_value, int(age_value), phone_value, chosen)
    context.user_data.update({
        "telegram_username": username_value,
        "full_name": full_name_value,
        "age": int(age_value),
        "phone": phone_value,
        "location": chosen,
    })

    synced = await sync_user_to_baserow(
        user_id=user_id,
        username=username_value,
        full_name=full_name_value,
        age=int(age_value),
        phone=phone_value,
        location=chosen,
    )

    group_keyboard = get_join_group_keyboard(chosen)
    group_note = (
        f"Press the button below to join the {chosen} group."
        if group_keyboard
        else f"The {chosen} group link has not been configured yet. Please contact an ArticularUZ administrator."
    )
    sync_note = "" if synced else "\n\n⚠️ Your registration is saved, but the online database sync failed temporarily. Please contact an administrator if this persists."
    summary = (
        "You're registered! ✅\n\n"
        f"👤 Name: {full_name_value}\n"
        f"🎂 Age: {age_value}\n"
        f"📞 Phone: {phone_value}\n"
        f"📱 Username: {username_value or 'No username'}\n"
        f"📍 Location: {chosen}\n\n"
        f"{group_note}{sync_note}"
    )
    await query.edit_message_text(summary, reply_markup=group_keyboard)
    context.user_data.pop("selected_event_location", None)
    await query.message.reply_text("What would you like to do next?", reply_markup=get_main_menu_keyboard())
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
        "Event registration canceled. ❌"
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

    # Edit the existing event message instead of sending
    # a separate "Back" message.
    try:
        await query.edit_message_text("Returned to the main menu.")
    except Exception:
        logger.debug("Could not edit event message while going back", exc_info=True)

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
    await get_user_with_baserow_fallback(user_id)
    active_registration = get_active_event_registration(user_id)
    registered_location = normalize_location(active_registration[1]) if active_registration else context.user_data.get("location", "")

    cancelled = cancel_latest_event_registration(user_id)
    if cancelled:
        conn = get_db_connection()
        try:
            conn.execute("UPDATE users SET location = '' WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()
        context.user_data["location"] = ""
        context.user_data.pop("selected_event_location", None)

    username_value = get_telegram_username(query.from_user) or context.user_data.get("telegram_username", "")
    synced = await cancel_user_in_baserow(
        user_id=user_id,
        username=username_value,
        full_name=context.user_data.get("full_name", ""),
        age=context.user_data.get("age", 0),
        phone=context.user_data.get("phone", ""),
        registered_city=registered_location,
    )

    if not cancelled:
        message = "No active event registration was found. I checked the online city tables for stale rows."
    else:
        message = (
            "Your event registration has been canceled. ❌\n\n"
            "Your profile is still saved. ✅\n"
            "You can register for another location anytime."
        )

    await query.edit_message_text(message)

    await query.message.reply_text(
        "Choose an option from the menu below:",
        reply_markup=get_main_menu_keyboard()
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
    await get_user_with_baserow_fallback(user_id)
    active_registration = get_active_event_registration(user_id)

    if not active_registration:
        # Also repair historical stale Baserow city rows when SQLite says none.
        profile = await get_user_with_baserow_fallback(user_id)
        if profile:
            ok = await cancel_user_in_baserow(
                user_id=user_id,
                username=get_telegram_username(update.effective_user) or profile[1] or "",
                full_name=profile[2] or "",
                age=profile[3] or 0,
                phone=profile[4] or "",
                registered_city=profile[5] or "",
            )
            if not ok:
                await update.message.reply_text(
                    "No active local registration was found, but the online database could not be fully checked. Please try again later.",
                    reply_markup=get_back_keyboard(),
                )
                return LOCATION
        await update.message.reply_text(
            "You are not registered for any event location. 📍",
            reply_markup=get_back_keyboard(),
        )
        return LOCATION

    registered_location = normalize_location(active_registration[1])
    cancelled = cancel_latest_event_registration(user_id)
    if not cancelled:
        await update.message.reply_text(
            "Your event registration could not be canceled. Please try again.",
            reply_markup=get_back_keyboard(),
        )
        return LOCATION

    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET location = '' WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()

    context.user_data["location"] = ""
    context.user_data.pop("selected_event_location", None)
    username_value = get_telegram_username(update.effective_user) or context.user_data.get("telegram_username", "")
    context.user_data["telegram_username"] = username_value

    synced = await cancel_user_in_baserow(
        user_id=user_id,
        username=username_value,
        full_name=context.user_data.get("full_name", ""),
        age=context.user_data.get("age", 0),
        phone=context.user_data.get("phone", ""),
        registered_city=registered_location,
    )

    await update.message.reply_text(
        f"Your {registered_location} event registration has been canceled. ❌\n\n"
        "Your profile information is still saved. ✅\n"
        "You can register for another location anytime."
    )

    await update.message.reply_text(
        "📍 Choose a new event location:",
        reply_markup=get_event_location_keyboard(),
    )

    return LOCATION


# =====================================================================
# PROFILE EDITING
# =====================================================================

async def cmd_edit_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    context.user_data["editing_single_field"] = True

    await update.message.reply_text(
        "Please enter your updated Full Name:",
        reply_markup=ReplyKeyboardRemove(),
    )

    return FULL_NAME


async def cmd_edit_age(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    context.user_data["editing_single_field"] = True

    await update.message.reply_text(
        "Please enter your updated Age:",
        reply_markup=ReplyKeyboardRemove(),
    )

    return AGE


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

    context.user_data["telegram_username"] = username_value

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

    await update.message.reply_text(message)

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

    username_value = get_telegram_username(
        update.effective_user
    )

    if username_value:
        context.user_data["telegram_username"] = username_value

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
        "Registration canceled.\n\n"
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
# GLOBAL SUBSCRIPTION GUARD
# =====================================================================

async def subscription_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Require channel membership for bot interactions."""
    message = update.effective_message
    user = update.effective_user
    if not user:
        return

    # Let these handlers run their own subscription flow / allow the user to retry.
    if message and message.text and message.text.split()[0].split("@")[0] in ("/start", "/restart", "/menu"):
        return
    if update.callback_query and update.callback_query.data == "check_subscription":
        return

    if not await is_subscribed(context.bot, user.id):
        if update.callback_query:
            await update.callback_query.answer(
                "Please subscribe to our channel to continue.", show_alert=True
            )
        if message:
            await send_subscription_prompt(message)
        raise ApplicationHandlerStop


# =====================================================================
# CHANNEL ANNOUNCEMENT BROADCAST
# =====================================================================

async def broadcast_from_announcement_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Copy posts from the configured announcements channel to every known bot user."""
    message = update.channel_post
    if not message or not ANNOUNCEMENT_CHANNEL_ID or message.chat_id != ANNOUNCEMENT_CHANNEL_ID:
        return
    if not (message.text is not None or message.photo or message.video or message.document
            or message.animation or message.audio or message.voice or message.video_note):
        return

    recipient_ids = await asyncio.to_thread(get_broadcast_recipient_ids)
    sent = blocked = failed = 0
    for recipient_id in recipient_ids:
        try:
            await context.bot.copy_message(chat_id=recipient_id, from_chat_id=message.chat_id, message_id=message.message_id)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as exc:
            error_text = str(exc).lower()
            if ("forbidden" in type(exc).__name__.lower() or "bot was blocked" in error_text
                    or "user is deactivated" in error_text or "chat not found" in error_text):
                blocked += 1
            elif getattr(exc, "retry_after", None):
                try:
                    await asyncio.sleep(float(exc.retry_after) + 1)
                    await context.bot.copy_message(chat_id=recipient_id, from_chat_id=message.chat_id, message_id=message.message_id)
                    sent += 1
                except Exception as retry_exc:
                    if "forbidden" in type(retry_exc).__name__.lower() or "blocked" in str(retry_exc).lower():
                        blocked += 1
                    else:
                        failed += 1
            else:
                failed += 1
                logger.warning("Channel announcement delivery failed for Telegram user %s: %s", recipient_id, exc)
    logger.info("Channel announcement broadcast complete: sent=%s blocked=%s failed=%s", sent, blocked, failed)


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

    if not BASEROW_TOKEN or BASEROW_TOKEN == "YOUR_NEW_BASEROW_TOKEN":
        logger.warning(
            "BASEROW_TOKEN is missing. Bot startup will continue, but "
            "Baserow-backed features require BASEROW_TOKEN in environment settings."
        )

    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    backup_database()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(set_bot_commands)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                check_subscription_callback,
                pattern=r"^check_subscription$",
            ),
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
                    check_subscription_callback,
                    pattern=r"^check_subscription$",
                ),
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
                # All enabled event locations.
                CallbackQueryHandler(
                    location_chosen,
                    pattern=r"^(Tashkent|Tashkent Region|Sirdaryo|Fergana|Andijan|Bukhara|Navoiy)$",
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

                # Inline Back button from the event keyboard.
                CallbackQueryHandler(
                    event_back_callback,
                    pattern=r"^event_back$",
                ),

                MessageHandler(
                    filters.Regex(r"^❌ Cancel Location$"),
                    cancel_location_from_profile,
                ),

                # Kept only for compatibility if an old ReplyKeyboard
                # Back button is still visible on a user's device.
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

    # Gate all bot interactions when the user has left the required channel.
    app.add_handler(TypeHandler(Update, subscription_guard), group=-1)

    app.add_handler(conv_handler)

    if ANNOUNCEMENT_CHANNEL_ID:
        app.add_handler(
            MessageHandler(
                filters.UpdateType.CHANNEL_POST
                & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL
                   | filters.ANIMATION | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE),
                broadcast_from_announcement_channel,
            ),
            group=1,
        )
        logger.info("Channel announcement forwarding enabled for channel ID %s", ANNOUNCEMENT_CHANNEL_ID)
    else:
        logger.warning("Channel announcement forwarding is disabled. Set ANNOUNCEMENT_CHANNEL_ID to enable it.")

    logger.info(
        "Bot started. Press Ctrl+C to stop."
    )

    # Python 3.14 no longer implicitly creates a current event loop.
    # PTB's run_polling() expects one to exist when it calls get_event_loop().
    # Create and register it explicitly before handing control to PTB.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        app.run_polling(drop_pending_updates=False)
    except InvalidToken:
        # python-telegram-bot's InvalidToken exception may include the rejected
        # token in its message. Do not print the exception or its token.
        logger.critical(
            "Telegram rejected BOT_TOKEN. Create a new token with BotFather "
            "and update BOT_TOKEN in your hosting platform environment settings."
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    start_health_server()
    main()
