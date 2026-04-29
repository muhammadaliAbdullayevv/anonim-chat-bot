import json
import logging
import os
import random
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
import psycopg
from psycopg.types.json import Jsonb
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionType,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageReactionHandler,
    MessageHandler,
    filters,
)

LEGACY_USERS_PATH = Path("users.json")
LEGACY_MESSAGES_PATH = Path("messages.json")
DATABASE_URL = None
BOT_USERNAME = None
MAX_MAP_SIZE = 30
REACTION_UPDATE_TYPES = ("message_reaction", "message_reaction_count")
DAILY_QUESTION_INTERVAL_SECONDS = 3600
LOGGER = logging.getLogger(__name__)

DAILY_QUESTION_POOL = [
    "If you could change one thing about me, what would it be?",
    "What do you admire in me but rarely say out loud?",
    "When did I surprise you the most?",
    "What is one habit of mine you secretly like?",
    "What should I stop overthinking about?",
    "What should I start doing more often?",
    "If I disappeared for a year, what would you miss most?",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_date_iso(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def format_db_dt(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")
    return now_iso()


def require_db_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return DATABASE_URL


def get_db_connection():
    return psycopg.connect(require_db_url(), autocommit=True)


def ensure_db_schema() -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL DEFAULT '',
                    owner_id BIGINT NULL,
                    reply_partner_id BIGINT NULL,
                    reply_owner_msg_id BIGINT NULL,
                    guess_target_id BIGINT NULL,
                    guess_source_msg_id BIGINT NULL,
                    daily_q_last_sent DATE NULL,
                    blocked BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS owner_id BIGINT NULL")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reply_partner_id BIGINT NULL")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reply_owner_msg_id BIGINT NULL")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS guess_target_id BIGINT NULL")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS guess_source_msg_id BIGINT NULL")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_q_last_sent DATE NULL")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN NOT NULL DEFAULT FALSE")

            # Hard cleanup of deprecated random mode schema.
            cur.execute("DROP INDEX IF EXISTS idx_users_random_waiting")
            cur.execute("DROP INDEX IF EXISTS idx_users_random_partner")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_waiting")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_partner_id")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_language")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_gender")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_profile_language")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_profile_gender")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_waiting_since")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_last_partner_id")
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS random_last_match_at")

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_token
                ON users(token)
                WHERE token <> ''
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_users_owner_id
                ON users(owner_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS message_pairs (
                    pair_key TEXT PRIMARY KEY,
                    a_chat_id BIGINT NOT NULL,
                    b_chat_id BIGINT NOT NULL,
                    a_to_b JSONB NOT NULL DEFAULT '{}'::jsonb,
                    b_to_a JSONB NOT NULL DEFAULT '{}'::jsonb,
                    a_to_b_order JSONB NOT NULL DEFAULT '[]'::jsonb,
                    b_to_a_order JSONB NOT NULL DEFAULT '[]'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_pairs_chat_a
                ON message_pairs(a_chat_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_message_pairs_chat_b
                ON message_pairs(b_chat_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS owner_blocks (
                    owner_id BIGINT NOT NULL,
                    blocked_user_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (owner_id, blocked_user_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_owner_blocks_owner
                ON owner_blocks(owner_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_owner_blocks_blocked_user
                ON owner_blocks(blocked_user_id)
                """
            )
            cur.execute("DROP TABLE IF EXISTS random_reports")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_daily_stats (
                    user_id BIGINT NOT NULL,
                    stat_date DATE NOT NULL,
                    anon_received INTEGER NOT NULL DEFAULT 0,
                    replies_sent INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, stat_date)
                )
                """
            )
            cur.execute("ALTER TABLE user_daily_stats ADD COLUMN IF NOT EXISTS anon_received INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE user_daily_stats ADD COLUMN IF NOT EXISTS replies_sent INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE user_daily_stats DROP COLUMN IF EXISTS random_messages")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_daily_stats_user_date
                ON user_daily_stats(user_id, stat_date)
                """
            )


def load_legacy_users_json() -> dict:
    if not LEGACY_USERS_PATH.exists():
        return {}
    try:
        with LEGACY_USERS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_legacy_messages_json() -> dict:
    if not LEGACY_MESSAGES_PATH.exists():
        return {"pairs": {}}
    try:
        with LEGACY_MESSAGES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"pairs": {}}
    if not isinstance(data, dict):
        return {"pairs": {}}
    if "pairs" not in data or not isinstance(data["pairs"], dict):
        data["pairs"] = {}
    for record in data["pairs"].values():
        if not isinstance(record, dict):
            continue
        if "a_to_b" in record and "a_to_b_order" not in record:
            record["a_to_b_order"] = list(record.get("a_to_b", {}).keys())
        if "b_to_a" in record and "b_to_a_order" not in record:
            record["b_to_a_order"] = list(record.get("b_to_a", {}).keys())
    return data


def migrate_legacy_json_if_needed() -> None:
    migrate_flag = os.getenv("MIGRATE_JSON_ON_START", "1").strip().lower()
    if migrate_flag in {"0", "false", "no"}:
        return

    legacy_users = load_legacy_users_json()
    legacy_messages = load_legacy_messages_json()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            users_count = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM message_pairs")
            pairs_count = int(cur.fetchone()[0])

    if users_count == 0 and legacy_users:
        save_users(legacy_users)
        print(f"Migrated {len(legacy_users)} users from users.json to PostgreSQL.")

    legacy_pairs = legacy_messages.get("pairs", {})
    if pairs_count == 0 and legacy_pairs:
        save_messages(legacy_messages)
        print(f"Migrated {len(legacy_pairs)} message pairs from messages.json to PostgreSQL.")


def load_users() -> dict:
    users = {}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    user_id,
                    chat_id,
                    username,
                    first_name,
                    last_name,
                    token,
                    owner_id,
                    reply_partner_id,
                    reply_owner_msg_id,
                    guess_target_id,
                    guess_source_msg_id,
                    daily_q_last_sent,
                    blocked,
                    created_at,
                    updated_at
                FROM users
                """
            )
            for row in cur.fetchall():
                (
                    user_id,
                    chat_id,
                    username,
                    first_name,
                    last_name,
                    token,
                    owner_id,
                    reply_partner_id,
                    reply_owner_msg_id,
                    guess_target_id,
                    guess_source_msg_id,
                    daily_q_last_sent,
                    blocked,
                    created_at,
                    updated_at,
                ) = row
                users[str(user_id)] = {
                    "user_id": int(user_id),
                    "chat_id": int(chat_id),
                    "username": username or "",
                    "first_name": first_name or "",
                    "last_name": last_name or "",
                    "token": token or "",
                    "owner_id": int(owner_id) if owner_id is not None else None,
                    "reply_partner_id": int(reply_partner_id) if reply_partner_id is not None else None,
                    "reply_owner_msg_id": int(reply_owner_msg_id) if reply_owner_msg_id is not None else None,
                    "guess_target_id": int(guess_target_id) if guess_target_id is not None else None,
                    "guess_source_msg_id": int(guess_source_msg_id) if guess_source_msg_id is not None else None,
                    "daily_q_last_sent": daily_q_last_sent.isoformat() if isinstance(daily_q_last_sent, date) else None,
                    "blocked": bool(blocked),
                    "created_at": format_db_dt(created_at),
                    "updated_at": format_db_dt(updated_at),
                }
    return users


def save_users(users: dict) -> None:
    if not isinstance(users, dict):
        return

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for uid, data in users.items():
                if not isinstance(data, dict):
                    continue

                user_id = int(data.get("user_id") or uid)
                chat_id = int(data.get("chat_id") or user_id)
                username = data.get("username") or ""
                first_name = data.get("first_name") or ""
                last_name = data.get("last_name") or ""
                token = data.get("token") or ""
                owner_id = data.get("owner_id")
                reply_partner_id = data.get("reply_partner_id")
                reply_owner_msg_id = data.get("reply_owner_msg_id")
                guess_target_id = data.get("guess_target_id")
                guess_source_msg_id = data.get("guess_source_msg_id")
                daily_q_last_sent = parse_date_iso(data.get("daily_q_last_sent"))
                blocked = bool(data.get("blocked", False))
                created_at = parse_iso(data.get("created_at"))
                updated_at = parse_iso(data.get("updated_at"))

                cur.execute(
                    """
                    INSERT INTO users (
                        user_id,
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        token,
                        owner_id,
                        reply_partner_id,
                        reply_owner_msg_id,
                        guess_target_id,
                        guess_source_msg_id,
                        daily_q_last_sent,
                        blocked,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        chat_id = EXCLUDED.chat_id,
                        username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name,
                        last_name = EXCLUDED.last_name,
                        token = EXCLUDED.token,
                        owner_id = EXCLUDED.owner_id,
                        reply_partner_id = EXCLUDED.reply_partner_id,
                        reply_owner_msg_id = EXCLUDED.reply_owner_msg_id,
                        guess_target_id = EXCLUDED.guess_target_id,
                        guess_source_msg_id = EXCLUDED.guess_source_msg_id,
                        daily_q_last_sent = EXCLUDED.daily_q_last_sent,
                        blocked = EXCLUDED.blocked,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        user_id,
                        chat_id,
                        username,
                        first_name,
                        last_name,
                        token,
                        int(owner_id) if owner_id is not None else None,
                        int(reply_partner_id) if reply_partner_id is not None else None,
                        int(reply_owner_msg_id) if reply_owner_msg_id is not None else None,
                        int(guess_target_id) if guess_target_id is not None else None,
                        int(guess_source_msg_id) if guess_source_msg_id is not None else None,
                        daily_q_last_sent,
                        blocked,
                        created_at,
                        updated_at,
                    ),
                )


def load_messages() -> dict:
    messages = {"pairs": {}}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pair_key,
                    a_chat_id,
                    b_chat_id,
                    a_to_b,
                    b_to_a,
                    a_to_b_order,
                    b_to_a_order
                FROM message_pairs
                """
            )
            for row in cur.fetchall():
                pair_id, a_chat_id, b_chat_id, a_to_b, b_to_a, a_to_b_order, b_to_a_order = row
                record = {
                    "a_chat_id": int(a_chat_id),
                    "b_chat_id": int(b_chat_id),
                    "a_to_b": dict(a_to_b) if isinstance(a_to_b, dict) else {},
                    "b_to_a": dict(b_to_a) if isinstance(b_to_a, dict) else {},
                    "a_to_b_order": list(a_to_b_order) if isinstance(a_to_b_order, list) else [],
                    "b_to_a_order": list(b_to_a_order) if isinstance(b_to_a_order, list) else [],
                }
                if record["a_to_b"] and not record["a_to_b_order"]:
                    record["a_to_b_order"] = list(record["a_to_b"].keys())
                if record["b_to_a"] and not record["b_to_a_order"]:
                    record["b_to_a_order"] = list(record["b_to_a"].keys())
                messages["pairs"][pair_id] = record
    return messages


def save_messages(messages: dict) -> None:
    pairs = messages.get("pairs", {}) if isinstance(messages, dict) else {}
    if not isinstance(pairs, dict):
        pairs = {}

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pair_key FROM message_pairs")
            existing = {row[0] for row in cur.fetchall()}
            current = set()

            for pair_id, record in pairs.items():
                if not isinstance(record, dict):
                    continue
                pair_key_value = str(pair_id)
                try:
                    a_chat_id = int(record.get("a_chat_id"))
                    b_chat_id = int(record.get("b_chat_id"))
                except (TypeError, ValueError):
                    continue

                current.add(pair_key_value)
                a_to_b = record.get("a_to_b", {})
                b_to_a = record.get("b_to_a", {})
                a_to_b_order = record.get("a_to_b_order", [])
                b_to_a_order = record.get("b_to_a_order", [])
                if not isinstance(a_to_b, dict):
                    a_to_b = {}
                if not isinstance(b_to_a, dict):
                    b_to_a = {}
                if not isinstance(a_to_b_order, list):
                    a_to_b_order = []
                if not isinstance(b_to_a_order, list):
                    b_to_a_order = []

                cur.execute(
                    """
                    INSERT INTO message_pairs (
                        pair_key,
                        a_chat_id,
                        b_chat_id,
                        a_to_b,
                        b_to_a,
                        a_to_b_order,
                        b_to_a_order,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (pair_key) DO UPDATE SET
                        a_chat_id = EXCLUDED.a_chat_id,
                        b_chat_id = EXCLUDED.b_chat_id,
                        a_to_b = EXCLUDED.a_to_b,
                        b_to_a = EXCLUDED.b_to_a,
                        a_to_b_order = EXCLUDED.a_to_b_order,
                        b_to_a_order = EXCLUDED.b_to_a_order,
                        updated_at = NOW()
                    """,
                    (
                        pair_key_value,
                        a_chat_id,
                        b_chat_id,
                        Jsonb(a_to_b),
                        Jsonb(b_to_a),
                        Jsonb(a_to_b_order),
                        Jsonb(b_to_a_order),
                    ),
                )

            stale = existing - current
            if stale:
                cur.execute(
                    "DELETE FROM message_pairs WHERE pair_key = ANY(%s)",
                    (list(stale),),
                )


def is_owner_blocked(owner_id: int, blocked_user_id: int) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM owner_blocks
                WHERE owner_id = %s AND blocked_user_id = %s
                """,
                (int(owner_id), int(blocked_user_id)),
            )
            return cur.fetchone() is not None


def add_owner_block(owner_id: int, blocked_user_id: int) -> None:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO owner_blocks (owner_id, blocked_user_id, created_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (owner_id, blocked_user_id) DO NOTHING
                """,
                (int(owner_id), int(blocked_user_id)),
            )


def increment_daily_stat(user_id: int, stat_key: str, amount: int = 1) -> None:
    if amount <= 0:
        return
    allowed_keys = {"anon_received", "replies_sent"}
    if stat_key not in allowed_keys:
        return

    stat_values = {"anon_received": 0, "replies_sent": 0}
    stat_values[stat_key] = int(amount)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_daily_stats (
                    user_id,
                    stat_date,
                    anon_received,
                    replies_sent
                )
                VALUES (%s, CURRENT_DATE, %s, %s)
                ON CONFLICT (user_id, stat_date) DO UPDATE SET
                    anon_received = user_daily_stats.anon_received + EXCLUDED.anon_received,
                    replies_sent = user_daily_stats.replies_sent + EXCLUDED.replies_sent
                """,
                (
                    int(user_id),
                    stat_values["anon_received"],
                    stat_values["replies_sent"],
                ),
            )


def fetch_user_stats(user_id: int) -> dict:
    result = {
        "anon_received": 0,
        "replies_sent": 0,
        "most_active_day": None,
        "weekly_growth": 0,
    }

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(SUM(anon_received), 0),
                    COALESCE(SUM(replies_sent), 0)
                FROM user_daily_stats
                WHERE user_id = %s
                """,
                (int(user_id),),
            )
            totals = cur.fetchone()
            if totals:
                result["anon_received"] = int(totals[0] or 0)
                result["replies_sent"] = int(totals[1] or 0)

            cur.execute(
                """
                SELECT
                    EXTRACT(DOW FROM stat_date)::INT AS dow,
                    SUM(anon_received + replies_sent) AS total
                FROM user_daily_stats
                WHERE user_id = %s
                GROUP BY dow
                ORDER BY total DESC, dow ASC
                LIMIT 1
                """,
                (int(user_id),),
            )
            row = cur.fetchone()
            if row:
                result["most_active_day"] = int(row[0])

            cur.execute(
                """
                SELECT COALESCE(SUM(anon_received), 0)
                FROM user_daily_stats
                WHERE user_id = %s
                  AND stat_date >= (CURRENT_DATE - INTERVAL '6 days')::DATE
                """,
                (int(user_id),),
            )
            current_week = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT COALESCE(SUM(anon_received), 0)
                FROM user_daily_stats
                WHERE user_id = %s
                  AND stat_date BETWEEN
                      (CURRENT_DATE - INTERVAL '13 days')::DATE
                      AND (CURRENT_DATE - INTERVAL '7 days')::DATE
                """,
                (int(user_id),),
            )
            previous_week = int(cur.fetchone()[0] or 0)
            result["weekly_growth"] = current_week - previous_week

    return result


def pair_key(chat_id_a: int, chat_id_b: int) -> str:
    a, b = sorted([int(chat_id_a), int(chat_id_b)])
    return f"{a}:{b}"


def get_pair_record(messages: dict, chat_id_a: int, chat_id_b: int, create: bool = False) -> dict | None:
    key = pair_key(chat_id_a, chat_id_b)
    pairs = messages.setdefault("pairs", {})
    record = pairs.get(key)
    if not record and create:
        a, b = sorted([int(chat_id_a), int(chat_id_b)])
        record = {
            "a_chat_id": a,
            "b_chat_id": b,
            "a_to_b": {},
            "b_to_a": {},
            "a_to_b_order": [],
            "b_to_a_order": [],
        }
        pairs[key] = record
    return record


def map_reply_id(messages: dict, from_chat_id: int, to_chat_id: int, reply_to_id: int | None) -> int | None:
    if not reply_to_id:
        return None
    record = get_pair_record(messages, from_chat_id, to_chat_id, create=False)
    if not record:
        return None
    if int(from_chat_id) == int(record["a_chat_id"]):
        mapping = record["a_to_b"]
    else:
        mapping = record["b_to_a"]
    mapped = mapping.get(str(reply_to_id))
    return int(mapped) if mapped else None


def store_message_mapping(messages: dict, from_chat_id: int, to_chat_id: int, from_msg_id: int, to_msg_id: int) -> None:
    record = get_pair_record(messages, from_chat_id, to_chat_id, create=True)
    if int(from_chat_id) == int(record["a_chat_id"]):
        record.setdefault("a_to_b_order", [])
        record.setdefault("b_to_a_order", [])
        record["a_to_b"][str(from_msg_id)] = str(to_msg_id)
        record["b_to_a"][str(to_msg_id)] = str(from_msg_id)
        record["a_to_b_order"].append(str(from_msg_id))
        record["b_to_a_order"].append(str(to_msg_id))
        trim_message_mapping(record, "a_to_b", "a_to_b_order")
        trim_message_mapping(record, "b_to_a", "b_to_a_order")
    else:
        record.setdefault("a_to_b_order", [])
        record.setdefault("b_to_a_order", [])
        record["b_to_a"][str(from_msg_id)] = str(to_msg_id)
        record["a_to_b"][str(to_msg_id)] = str(from_msg_id)
        record["b_to_a_order"].append(str(from_msg_id))
        record["a_to_b_order"].append(str(to_msg_id))
        trim_message_mapping(record, "b_to_a", "b_to_a_order")
        trim_message_mapping(record, "a_to_b", "a_to_b_order")


def trim_message_mapping(record: dict, map_key: str, order_key: str) -> None:
    order = record.get(order_key, [])
    if len(order) <= MAX_MAP_SIZE:
        return
    to_remove = order[:-MAX_MAP_SIZE]
    record[order_key] = order[-MAX_MAP_SIZE:]
    mapping = record.get(map_key, {})
    for msg_id in to_remove:
        mapping.pop(str(msg_id), None)


def clear_pair_messages(chat_id_a: int, chat_id_b: int) -> None:
    key = pair_key(chat_id_a, chat_id_b)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM message_pairs WHERE pair_key = %s", (key,))


def find_mirrored_message(messages: dict, from_chat_id: int, from_msg_id: int) -> tuple[int, int] | None:
    pairs = messages.get("pairs", {}) if isinstance(messages, dict) else {}
    if not isinstance(pairs, dict):
        return None

    source_chat_id = int(from_chat_id)
    source_msg_key = str(from_msg_id)

    for record in pairs.values():
        if not isinstance(record, dict):
            continue
        try:
            a_chat_id = int(record.get("a_chat_id"))
            b_chat_id = int(record.get("b_chat_id"))
        except (TypeError, ValueError):
            continue

        if source_chat_id == a_chat_id:
            mapping = record.get("a_to_b", {})
            if not isinstance(mapping, dict):
                continue
            mirrored_msg_id = mapping.get(source_msg_key)
            if mirrored_msg_id is None:
                continue
            try:
                return b_chat_id, int(mirrored_msg_id)
            except (TypeError, ValueError):
                continue

        if source_chat_id == b_chat_id:
            mapping = record.get("b_to_a", {})
            if not isinstance(mapping, dict):
                continue
            mirrored_msg_id = mapping.get(source_msg_key)
            if mirrored_msg_id is None:
                continue
            try:
                return a_chat_id, int(mirrored_msg_id)
            except (TypeError, ValueError):
                continue

    return None


def normalize_reaction_payload(raw_reactions) -> list[ReactionType]:
    if not raw_reactions:
        return []

    normalized: list[ReactionType] = []
    for reaction in raw_reactions:
        if isinstance(reaction, ReactionTypeEmoji):
            normalized.append(ReactionTypeEmoji(reaction.emoji))
        elif isinstance(reaction, ReactionTypeCustomEmoji):
            normalized.append(ReactionTypeCustomEmoji(reaction.custom_emoji_id))
        elif isinstance(reaction, ReactionTypePaid):
            normalized.append(ReactionTypePaid())

    return normalized


def build_reaction_notice_text(reactions: list[ReactionType]) -> str:
    if not reactions:
        return "Suhbatdosh reaksiyani olib tashladi."

    parts: list[str] = []
    for reaction in reactions:
        if isinstance(reaction, ReactionTypeEmoji):
            parts.append(reaction.emoji)
        elif isinstance(reaction, ReactionTypeCustomEmoji):
            parts.append("maxsus emoji")
        elif isinstance(reaction, ReactionTypePaid):
            parts.append("yulduzli reaksiya")

    if not parts:
        return "Suhbatdosh reaksiya yubordi."
    return f"Suhbatdosh reaksiyasi: {' '.join(parts)}"


def parse_bool_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def build_allowed_updates(include_reactions: bool) -> list[str]:
    updates = list(Update.ALL_TYPES)
    if include_reactions:
        return updates

    for update_name in REACTION_UPDATE_TYPES:
        while update_name in updates:
            updates.remove(update_name)
    return updates


async def send_reaction_notice_fallback(
    app,
    chat_id: int,
    message_id: int,
    reactions: list[ReactionType],
) -> None:
    notice_text = build_reaction_notice_text(reactions)
    kwargs = {"chat_id": chat_id, "text": notice_text}
    if int(message_id) > 0:
        kwargs["reply_to_message_id"] = int(message_id)
    try:
        await app.bot.send_message(**kwargs)
    except Exception as exc:
        LOGGER.warning(
            "Reaction fallback message failed for chat_id=%s message_id=%s: %s",
            chat_id,
            message_id,
            exc,
        )


def generate_unique_token(users: dict) -> str:
    existing = {u.get("token") for u in users.values() if u.get("token")}
    while True:
        token = secrets.token_urlsafe(8)
        if token not in existing:
            return token


def find_user_by_token(users: dict, token: str):
    for uid, data in users.items():
        if data.get("token") == token:
            return uid, data
    return None, None


def ensure_user_record(users: dict, tg_user, chat_id: int) -> dict:
    uid = str(tg_user.id)
    now = now_iso()
    record = users.get(uid)
    if not record:
        record = {
            "user_id": tg_user.id,
            "chat_id": chat_id,
            "username": tg_user.username or "",
            "first_name": tg_user.first_name or "",
            "last_name": tg_user.last_name or "",
            "token": "",
            "owner_id": None,
            "reply_partner_id": None,
            "reply_owner_msg_id": None,
            "guess_target_id": None,
            "guess_source_msg_id": None,
            "daily_q_last_sent": None,
            "blocked": False,
            "created_at": now,
            "updated_at": now,
        }
        users[uid] = record
    else:
        record["chat_id"] = chat_id
        record["username"] = tg_user.username or ""
        record["first_name"] = tg_user.first_name or ""
        record["last_name"] = tg_user.last_name or ""
        if "owner_id" not in record:
            if record.get("partner_id"):
                record["owner_id"] = record.get("partner_id")
            else:
                record["owner_id"] = None
        if "reply_partner_id" not in record:
            record["reply_partner_id"] = None
        if "reply_owner_msg_id" not in record:
            record["reply_owner_msg_id"] = None
        if "guess_target_id" not in record:
            record["guess_target_id"] = None
        if "guess_source_msg_id" not in record:
            record["guess_source_msg_id"] = None
        if "daily_q_last_sent" not in record:
            record["daily_q_last_sent"] = None
        record.pop("partner_id", None)
        record["updated_at"] = now
    return record


def build_share_keyboard(link: str) -> InlineKeyboardMarkup:
    share_text = "Xavfsiz va tez muloqot uchun ushbu havoladan foydalaning 🙂"
    share_url = f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Havolani ulashish 📤", url=share_url)]]
    )


def build_reply_keyboard(partner_id: int, owner_msg_id: int) -> InlineKeyboardMarkup:
    data = f"reply:{partner_id}:{owner_msg_id}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Javob berish ↩️", callback_data=data)]]
    )


def build_confession_keyboard(partner_id: int, owner_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Reply anonymously", callback_data=f"reply:{partner_id}:{owner_msg_id}")],
            [
                InlineKeyboardButton("🚫 Block sender", callback_data=f"block:{partner_id}"),
                InlineKeyboardButton("🔍 Guess who", callback_data=f"guess:{partner_id}:{owner_msg_id}"),
            ],
        ]
    )


def build_guess_name(record: dict) -> str:
    username = (record.get("username") or "").strip()
    if username:
        return f"@{username}"
    first_name = (record.get("first_name") or "").strip()
    if first_name:
        return first_name
    user_id = int(record.get("user_id") or 0)
    return f"Anonim-{abs(user_id) % 10000:04d}"


def build_guess_keyboard(users: dict, correct_user_id: int, owner_user_id: int) -> InlineKeyboardMarkup | None:
    candidate_ids = []
    for user in users.values():
        user_id = user.get("user_id")
        if not isinstance(user_id, int):
            continue
        if user_id == owner_user_id:
            continue
        if user.get("owner_id") == owner_user_id or user_id == correct_user_id:
            candidate_ids.append(user_id)

    candidate_ids = list(dict.fromkeys(candidate_ids))
    if correct_user_id not in candidate_ids:
        candidate_ids.append(correct_user_id)

    if len(candidate_ids) > 4:
        others = [x for x in candidate_ids if x != correct_user_id]
        sampled = random.sample(others, k=3)
        candidate_ids = sampled + [correct_user_id]

    random.shuffle(candidate_ids)
    if not candidate_ids:
        return None

    rows = []
    for candidate_id in candidate_ids:
        candidate = users.get(str(candidate_id))
        if not candidate:
            continue
        label = build_guess_name(candidate)
        data = f"guesspick:{correct_user_id}:{candidate_id}"
        rows.append([InlineKeyboardButton(label, callback_data=data)])

    return InlineKeyboardMarkup(rows) if rows else None


def weekday_name_from_index(index: int | None) -> str:
    names = {
        0: "Sunday",
        1: "Monday",
        2: "Tuesday",
        3: "Wednesday",
        4: "Thursday",
        5: "Friday",
        6: "Saturday",
    }
    return names.get(index, "N/A")


def get_daily_question_text(target_date: date | None = None) -> str:
    day = target_date or datetime.now(timezone.utc).date()
    return DAILY_QUESTION_POOL[day.toordinal() % len(DAILY_QUESTION_POOL)]


def has_connected_clients(users: dict, owner_user_id: int) -> bool:
    for user in users.values():
        if user.get("owner_id") == owner_user_id:
            return True
    return False


async def get_bot_username(app) -> str:
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await app.bot.get_me()
    BOT_USERNAME = me.username
    return BOT_USERNAME


async def disconnect_client(
    users: dict,
    client_id: str,
    app,
    notify_client_text: str | None = None,
    notify_owner_text: str | None = None,
):
    client = users.get(client_id)
    if not client:
        return
    owner_id = client.get("owner_id")
    if not owner_id:
        return
    owner = users.get(str(owner_id))
    client_chat_id = client.get("chat_id")
    owner_chat_id = owner.get("chat_id") if owner else None
    client["owner_id"] = None
    client["updated_at"] = now_iso()
    if client_chat_id and owner_chat_id:
        clear_pair_messages(client_chat_id, owner_chat_id)
    if notify_client_text and client_chat_id:
        try:
            await app.bot.send_message(chat_id=client_chat_id, text=notify_client_text)
        except Exception:
            pass
    if notify_owner_text and owner_chat_id:
        try:
            await app.bot.send_message(chat_id=owner_chat_id, text=notify_owner_text)
        except Exception:
            pass


def clear_guess_state(record: dict) -> None:
    record["guess_target_id"] = None
    record["guess_source_msg_id"] = None


def ensure_user_link(record: dict, users: dict) -> str:
    if not record.get("token"):
        record["token"] = generate_unique_token(users)
        record["updated_at"] = now_iso()
    return record["token"]


def mark_daily_sent(record: dict) -> None:
    record["daily_q_last_sent"] = datetime.now(timezone.utc).date().isoformat()
    record["updated_at"] = now_iso()


async def deliver_daily_question_for_user(users: dict, record: dict, app) -> bool:
    chat_id = record.get("chat_id")
    user_id = record.get("user_id")
    if not chat_id or not user_id or record.get("blocked") is True:
        return False

    today = datetime.now(timezone.utc).date().isoformat()
    if record.get("daily_q_last_sent") == today:
        return False

    username = await get_bot_username(app)
    token = ensure_user_link(record, users)
    link = f"https://t.me/{username}?start={token}"
    question = get_daily_question_text()
    text = (
        "🔥 Question of the Day\n\n"
        f"{question}\n\n"
        "Share your anonymous link again:\n"
        f"{link}"
    )
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=build_share_keyboard(link),
        )
    except Exception:
        return False

    mark_daily_sent(record)
    return True


async def daily_question_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = load_users()
    changed = False
    for record in users.values():
        sent = await deliver_daily_question_for_user(users, record, context.application)
        if sent:
            changed = True
    if changed:
        save_users(users)


async def configure_bot_commands(app: Application) -> None:
    commands = [
        BotCommand("start", "Open your anonymous link/inbox"),
        BotCommand("newlink", "Generate a new personal link"),
        BotCommand("stats", "Show your profile statistics"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception as exc:
        LOGGER.warning("Failed to set bot commands: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    users = load_users()
    record = ensure_user_record(users, update.effective_user, update.effective_chat.id)

    if record.get("blocked") is True:
        await update.effective_chat.send_message("Siz ushbu botdan foydalanish uchun bloklangansiz.")
        return

    if context.args:
        token = context.args[0]
        owner_id, owner = find_user_by_token(users, token)
        if not owner_id:
            await update.effective_chat.send_message("Noto'g'ri havola. Yangi havola so'rang.")
            save_users(users)
            return
        if owner_id == str(update.effective_user.id):
            await update.effective_chat.send_message("Bu sizning havolangiz. Uni boshqa odamga yuboring.")
            save_users(users)
            return
        if owner.get("blocked") is True:
            await update.effective_chat.send_message("Bu foydalanuvchi mavjud emas.")
            save_users(users)
            return
        if is_owner_blocked(int(owner_id), int(update.effective_user.id)):
            await update.effective_chat.send_message("Bu foydalanuvchi sizni qabul qilmaydi.")
            save_users(users)
            return

        old_owner_id = record.get("owner_id")
        if old_owner_id and str(old_owner_id) != owner_id:
            await disconnect_client(
                users,
                str(update.effective_user.id),
                context.application,
                notify_client_text=None,
                notify_owner_text="Suhbatdosh uzildi.",
            )

        record["owner_id"] = int(owner_id)
        record["updated_at"] = now_iso()
        save_users(users)

        await update.effective_chat.send_message(
            "Ulanildi. Endi yozishishingiz mumkin.",
            reply_markup=build_reply_keyboard(int(owner_id), 0),
        )
        try:
            await context.application.bot.send_message(
                chat_id=owner["chat_id"],
                text="Yangi suhbatdosh ulandi. Javob berish uchun xabardagi tugmadan foydalaning.",
                reply_markup=build_reply_keyboard(int(update.effective_user.id), 0),
            )
        except Exception:
            pass
        return

    # No payload: provide permanent link
    if not record.get("token"):
        record["token"] = generate_unique_token(users)
        record["updated_at"] = now_iso()
    username = await get_bot_username(context.application)
    link = f"https://t.me/{username}?start={record['token']}"
    keyboard = build_share_keyboard(link)

    save_users(users)
    await update.effective_chat.send_message(
        "Xavfsiz va tez muloqot uchun ushbu havoladan foydalaning 🙂\n"
        f"{link}",
        reply_markup=keyboard,
    )


async def newlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return

    users = load_users()
    record = ensure_user_record(users, update.effective_user, update.effective_chat.id)

    if record.get("blocked") is True:
        await update.effective_chat.send_message("Siz ushbu botdan foydalanish uchun bloklangansiz.")
        return

    record["token"] = generate_unique_token(users)
    record["updated_at"] = now_iso()

    username = await get_bot_username(context.application)
    link = f"https://t.me/{username}?start={record['token']}"
    keyboard = build_share_keyboard(link)

    save_users(users)
    await update.effective_chat.send_message(
        "Yangi havola tayyor. Uni kim bilandir ulashing 🙂\n"
        f"{link}",
        reply_markup=keyboard,
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    stats_data = fetch_user_stats(int(update.effective_user.id))
    growth = int(stats_data.get("weekly_growth", 0))
    growth_label = f"+{growth}" if growth >= 0 else str(growth)
    message = (
        f"👤 Anonymous messages received: {int(stats_data.get('anon_received', 0))}\n"
        f"💬 Replies sent: {int(stats_data.get('replies_sent', 0))}\n"
        f"🔥 Most active day: {weekday_name_from_index(stats_data.get('most_active_day'))}\n"
        f"📈 Weekly growth: {growth_label} messages"
    )
    await update.effective_chat.send_message(message)


async def handle_block_sender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user or not update.effective_chat:
        return
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await query.answer()
        return

    blocked_user_id = int(parts[1])
    users = load_users()
    owner_record = ensure_user_record(users, update.effective_user, update.effective_chat.id)
    blocked_record = users.get(str(blocked_user_id))
    if not blocked_record:
        await query.answer("Foydalanuvchi topilmadi.", show_alert=True)
        return
    if blocked_record.get("owner_id") != owner_record.get("user_id"):
        await query.answer("Bu foydalanuvchini bloklay olmaysiz.", show_alert=True)
        return

    add_owner_block(int(owner_record["user_id"]), blocked_user_id)
    await disconnect_client(
        users,
        str(blocked_user_id),
        context.application,
        notify_client_text="Siz ushbu suhbatdan uzildingiz.",
        notify_owner_text=None,
    )
    save_users(users)
    await query.answer("Sender blocked.", show_alert=True)
    try:
        await context.application.bot.send_message(
            chat_id=owner_record["chat_id"],
            text="Foydalanuvchi bloklandi va chat uzildi.",
        )
    except Exception:
        pass


async def handle_guess_sender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user or not update.effective_chat:
        return
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await query.answer()
        return

    guessed_target_id = int(parts[1])
    owner_msg_id = int(parts[2])
    users = load_users()
    owner_record = ensure_user_record(users, update.effective_user, update.effective_chat.id)
    partner_record = users.get(str(guessed_target_id))
    if not partner_record or partner_record.get("owner_id") != owner_record.get("user_id"):
        await query.answer("Bu sender endi mavjud emas.", show_alert=True)
        return

    keyboard = build_guess_keyboard(users, guessed_target_id, int(owner_record["user_id"]))
    if not keyboard:
        await query.answer("Taxmin uchun ma'lumot yetarli emas.", show_alert=True)
        return

    owner_record["guess_target_id"] = guessed_target_id
    owner_record["guess_source_msg_id"] = owner_msg_id
    owner_record["updated_at"] = now_iso()
    save_users(users)
    await query.answer("Taxmin qiling 👀")
    await update.effective_chat.send_message(
        "Kim deb o'ylaysiz?",
        reply_markup=keyboard,
    )


async def handle_guess_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user or not update.effective_chat:
        return
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await query.answer()
        return

    correct_id = int(parts[1])
    picked_id = int(parts[2])
    users = load_users()
    owner_record = ensure_user_record(users, update.effective_user, update.effective_chat.id)
    active_target = owner_record.get("guess_target_id")
    if active_target is None:
        await query.answer("Taxmin sessiyasi tugagan.", show_alert=True)
        return

    is_correct = int(active_target) == picked_id and int(correct_id) == picked_id
    clear_guess_state(owner_record)
    owner_record["updated_at"] = now_iso()
    save_users(users)

    if is_correct:
        await query.answer("To'g'ri taxmin! 😮", show_alert=True)
    else:
        await query.answer("Noto'g'ri taxmin 😅", show_alert=True)


async def set_reply_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user or not update.effective_chat:
        return
    query = update.callback_query
    data = query.data or ""
    if not data.startswith("reply:"):
        return

    parts = data.split(":")
    if len(parts) != 3:
        await query.answer()
        return

    partner_id = parts[1]
    owner_msg_id = parts[2]
    if not (partner_id.isdigit() and owner_msg_id.isdigit()):
        await query.answer()
        return

    users = load_users()
    record = ensure_user_record(users, update.effective_user, update.effective_chat.id)

    partner = users.get(str(partner_id))
    if not partner:
        await query.answer("Suhbatdosh endi mavjud emas.", show_alert=True)
        return

    is_owner_replying = partner.get("owner_id") == record.get("user_id")
    is_client_replying = record.get("owner_id") == partner.get("user_id")
    if not (is_owner_replying or is_client_replying):
        await query.answer("Suhbatdosh endi mavjud emas.", show_alert=True)
        return

    record["reply_partner_id"] = int(partner_id)
    record["reply_owner_msg_id"] = int(owner_msg_id)
    record["updated_at"] = now_iso()
    save_users(users)

    await query.answer("Javob yo'nalishi tanlandi.")
    try:
        await context.application.bot.send_chat_action(
            chat_id=partner.get("chat_id"),
            action=ChatAction.TYPING,
        )
    except Exception:
        pass
    try:
        prompt_kwargs = {}
        if int(owner_msg_id) > 0:
            prompt_kwargs["reply_to_message_id"] = int(owner_msg_id)
        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Yubormoqchi bo'lgan xabaringizni yozing:...",
            **prompt_kwargs,
        )
    except Exception:
        await context.application.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Yubormoqchi bo'lgan xabaringizni yozing:...",
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.effective_message:
        return

    users = load_users()
    record = ensure_user_record(users, update.effective_user, update.effective_chat.id)

    if record.get("blocked") is True:
        save_users(users)
        await update.effective_chat.send_message("Siz ushbu botdan foydalanish uchun bloklangansiz.")
        return

    reply_partner_id = record.get("reply_partner_id")
    reply_owner_msg_id = record.get("reply_owner_msg_id")
    if reply_partner_id is not None and reply_owner_msg_id is not None:
        partner = users.get(str(reply_partner_id))
        if not partner:
            record["reply_partner_id"] = None
            record["reply_owner_msg_id"] = None
            record["updated_at"] = now_iso()
            save_users(users)
            await update.effective_chat.send_message("Suhbatdosh endi mavjud emas.")
            return

        is_owner_replying = partner.get("owner_id") == record.get("user_id")
        is_client_replying = record.get("owner_id") == partner.get("user_id")
        if not (is_owner_replying or is_client_replying):
            record["reply_partner_id"] = None
            record["reply_owner_msg_id"] = None
            record["updated_at"] = now_iso()
            save_users(users)
            await update.effective_chat.send_message("Suhbatdosh endi mavjud emas.")
            return

        if partner.get("blocked") is True:
            record["reply_partner_id"] = None
            record["reply_owner_msg_id"] = None
            record["updated_at"] = now_iso()
            save_users(users)
            await update.effective_chat.send_message("Suhbatdosh mavjud emas.")
            return

        messages = load_messages()
        reply_to_id = map_reply_id(
            messages,
            record["chat_id"],
            partner["chat_id"],
            int(reply_owner_msg_id),
        )

        try:
            copy_kwargs = {}
            if reply_to_id:
                copy_kwargs["reply_to_message_id"] = reply_to_id
            copied = await context.application.bot.copy_message(
                chat_id=partner["chat_id"],
                from_chat_id=update.effective_chat.id,
                message_id=update.effective_message.message_id,
                **copy_kwargs,
            )
            store_message_mapping(
                messages,
                update.effective_chat.id,
                partner["chat_id"],
                update.effective_message.message_id,
                copied.message_id,
            )
            save_messages(messages)
            try:
                if is_client_replying:
                    markup = build_confession_keyboard(record["user_id"], copied.message_id)
                else:
                    markup = build_reply_keyboard(record["user_id"], copied.message_id)
                await context.application.bot.edit_message_reply_markup(
                    chat_id=partner["chat_id"],
                    message_id=copied.message_id,
                    reply_markup=markup,
                )
            except Exception:
                pass
            if update.effective_message.voice:
                try:
                    await context.application.bot.send_message(
                        chat_id=partner["chat_id"],
                        text="Someone sent you a voice message 👀",
                    )
                except Exception:
                    pass

            increment_daily_stat(int(partner["user_id"]), "anon_received", 1)
            increment_daily_stat(int(record["user_id"]), "replies_sent", 1)
        except Exception:
            if is_client_replying:
                record["owner_id"] = None
            elif is_owner_replying:
                partner["owner_id"] = None
            record["reply_partner_id"] = None
            record["reply_owner_msg_id"] = None
            record["updated_at"] = now_iso()
            save_users(users)
            if record.get("chat_id") and partner.get("chat_id"):
                clear_pair_messages(record["chat_id"], partner["chat_id"])
            try:
                await update.effective_chat.send_message("Suhbatdosh mavjud emas. Ulanish uchun havolangizdan foydalaning.")
            except Exception:
                pass
            return

        record["reply_partner_id"] = None
        record["reply_owner_msg_id"] = None
        record["updated_at"] = now_iso()
        save_users(users)
        try:
            await update.effective_chat.send_message("Xabaringiz yuborildi.")
        except Exception:
            pass
        return

    if record.get("owner_id") or has_connected_clients(users, record["user_id"]):
        await update.effective_chat.send_message("Javob berish uchun xabardagi 'Javob berish' tugmasini bosing.")
        return

    await update.effective_chat.send_message("Suhbatdosh ulanmagan. Ulanish uchun havolangizdan foydalaning.")


async def handle_message_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reaction_update = update.message_reaction
    if not reaction_update:
        return

    try:
        source_chat_id = int(reaction_update.chat.id)
        source_message_id = int(reaction_update.message_id)
    except (TypeError, ValueError):
        return

    reactions = normalize_reaction_payload(reaction_update.new_reaction)
    messages = load_messages()
    mirrored = find_mirrored_message(messages, source_chat_id, source_message_id)
    if not mirrored:
        return

    target_chat_id, target_message_id = mirrored
    try:
        await context.application.bot.set_message_reaction(
            chat_id=target_chat_id,
            message_id=target_message_id,
            reaction=(reactions or None),
        )
    except Exception as exc:
        LOGGER.warning(
            "set_message_reaction failed for chat_id=%s message_id=%s: %s",
            target_chat_id,
            target_message_id,
            exc,
        )
        await send_reaction_notice_fallback(
            context.application,
            target_chat_id,
            target_message_id,
            reactions,
        )


def main() -> None:
    global DATABASE_URL
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN environment variable is required.")
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL environment variable is required.")
    telegram_base_url = os.getenv("TELEGRAM_BASE_URL", "").strip()
    telegram_base_file_url = os.getenv("TELEGRAM_BASE_FILE_URL", "").strip()
    telegram_local_mode = parse_bool_env("TELEGRAM_LOCAL_MODE", "0")
    reaction_updates_enabled = parse_bool_env("ENABLE_REACTION_UPDATES", "1")
    daily_question_enabled = parse_bool_env("ENABLE_DAILY_QUESTION", "1")

    ensure_db_schema()
    migrate_legacy_json_if_needed()

    app_builder = Application.builder().token(token).post_init(configure_bot_commands)
    if telegram_base_url:
        app_builder = app_builder.base_url(telegram_base_url)
    if telegram_base_file_url:
        app_builder = app_builder.base_file_url(telegram_base_file_url)
    if telegram_local_mode:
        app_builder = app_builder.local_mode(True)
    app = app_builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newlink", newlink))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(set_reply_target, pattern=r"^reply:"))
    app.add_handler(CallbackQueryHandler(handle_block_sender_callback, pattern=r"^block:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_guess_pick_callback, pattern=r"^guesspick:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_guess_sender_callback, pattern=r"^guess:\d+:\d+$"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(
        MessageReactionHandler(
            handle_message_reaction,
            message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED,
        )
    )
    if daily_question_enabled and app.job_queue is not None:
        app.job_queue.run_repeating(
            daily_question_job,
            interval=DAILY_QUESTION_INTERVAL_SECONDS,
            first=15,
            name="daily-question-job",
        )

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=build_allowed_updates(reaction_updates_enabled),
    )


if __name__ == "__main__":
    main()
