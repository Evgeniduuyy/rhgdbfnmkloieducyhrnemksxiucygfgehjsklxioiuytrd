import aiosqlite
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "bot_database.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_admin BOOLEAN DEFAULT 0,
                subscription_end TIMESTAMP NULL,
                subscription_lifetime BOOLEAN DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_data TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                duration_days INTEGER,
                status TEXT DEFAULT 'pending',
                crypto_invoice_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS report_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id TEXT,
                message_id TEXT,
                report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS peer_report_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                peer_username TEXT,
                peer_type TEXT,
                report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS backup_bot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backup_bot_token TEXT,
                is_active BOOLEAN DEFAULT 0,
                last_sync TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS force_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_username TEXT NOT NULL,
                channel_id INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                days INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                uses INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS whitelist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target TEXT NOT NULL UNIQUE,
                target_type TEXT NOT NULL DEFAULT 'unknown',
                note TEXT DEFAULT '',
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                days INTEGER NOT NULL,
                price REAL NOT NULL,
                label TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Добавляем дефолтные тарифы если таблица пустая
        async with db.execute("SELECT COUNT(*) FROM subscription_plans") as cur:
            count = (await cur.fetchone())[0]
        if count == 0:
            await db.executemany(
                "INSERT INTO subscription_plans (days, price, label) VALUES (?, ?, ?)",
                [
                    (30,  10.0,  "📅 30 дней"),
                    (0,  100.0, "♾️ Навсегда"),
                ]
            )
        await db.commit()


# ─── Пользователи ──────────────────────────────────────────

async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def upsert_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user_id, username, first_name))
        await db.commit()


async def is_admin(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user.get("is_admin", False)) if user else False


async def has_active_subscription(user_id: int) -> bool:
    user = await get_user(user_id)
    if user is None:
        return False
    if bool(user.get("subscription_lifetime", False)):
        return True
    sub_end = user.get("subscription_end")
    if sub_end:
        try:
            end_dt = datetime.fromisoformat(sub_end)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) <= end_dt
        except Exception:
            return False
    return False


async def set_admin(user_id: int, value: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        if value:
            await db.execute("""
                INSERT INTO users (user_id, is_admin, subscription_lifetime)
                VALUES (?, 1, 1)
                ON CONFLICT(user_id) DO UPDATE SET is_admin=1, subscription_lifetime=1
            """, (user_id,))
        else:
            await db.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def activate_subscription(user_id: int, days: int):
    async with aiosqlite.connect(DB_PATH) as db:
        if days == 0:
            await db.execute(
                "UPDATE users SET subscription_lifetime=1, subscription_end=NULL WHERE user_id=?",
                (user_id,)
            )
        else:
            end_dt = datetime.now(timezone.utc) + timedelta(days=days)
            await db.execute(
                "UPDATE users SET subscription_end=?, subscription_lifetime=0 WHERE user_id=?",
                (end_dt.isoformat(), user_id)
            )
        await db.commit()


async def revoke_subscription(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET subscription_end=NULL, subscription_lifetime=0 WHERE user_id=?",
            (user_id,)
        )
        await db.commit()


async def grant_subscription(user_id: int, days: int):
    user = await get_user(user_id)
    now = datetime.now(timezone.utc)
    if user and user.get("subscription_lifetime"):
        return None
    current_end_str = user.get("subscription_end") if user else None
    if current_end_str:
        try:
            end_dt = datetime.fromisoformat(current_end_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            base = end_dt if end_dt > now else now
        except Exception:
            base = now
    else:
        base = now
    new_end = base + timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, first_name, subscription_end, subscription_lifetime)
            VALUES (?, '', '', ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                subscription_end=excluded.subscription_end,
                subscription_lifetime=0
        """, (user_id, new_end.isoformat()))
        await db.commit()
    return new_end


async def get_all_admins() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE is_admin=1") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_all_subscribers() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM users
            WHERE subscription_lifetime=1
               OR (subscription_end IS NOT NULL AND subscription_end > datetime('now'))
            ORDER BY subscription_end ASC
        """) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_all_users() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            return [row[0] for row in await cursor.fetchall()]


# ─── Сессии ────────────────────────────────────────────────

async def add_session(session_data: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO sessions (session_data, is_active) VALUES (?, 1)", (session_data,)
        )
        await db.commit()
        return cursor.lastrowid


async def get_all_sessions() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM sessions WHERE is_active=1") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def delete_session(session_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE sessions SET is_active=0 WHERE id=?", (session_id,))
        await db.commit()


# ─── Логи обращений ────────────────────────────────────────

async def has_reported_before(user_id: int, chat_id: str, message_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM report_logs WHERE user_id=? AND chat_id=? AND message_id=?",
            (user_id, chat_id, message_id)
        ) as cursor:
            return await cursor.fetchone() is not None


async def has_peer_reported_before(user_id: int, peer_username: str) -> bool:
    normalized = peer_username.lower().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM peer_report_logs WHERE user_id=? AND LOWER(peer_username)=?",
            (user_id, normalized)
        ) as cursor:
            return await cursor.fetchone() is not None


async def add_report_log(user_id: int, chat_id: str, message_id: str, success: int, total: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO report_logs (user_id,chat_id,message_id,success_count,total_count) VALUES (?,?,?,?,?)",
            (user_id, chat_id, message_id, success, total)
        )
        await db.commit()


async def add_peer_report_log(user_id: int, peer_username: str, peer_type: str, success: int, total: int):
    normalized = peer_username.lower().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO peer_report_logs (user_id,peer_username,peer_type,success_count,total_count) VALUES (?,?,?,?,?)",
            (user_id, normalized, peer_type, success, total)
        )
        await db.commit()


# ─── Платежи ───────────────────────────────────────────────

async def add_payment(user_id: int, amount: float, days: int, invoice_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO payments (user_id,amount,duration_days,status,crypto_invoice_id) VALUES (?,?,?,'pending',?)",
            (user_id, amount, days, invoice_id)
        )
        await db.commit()
        return cursor.lastrowid


async def get_payment_by_invoice(invoice_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payments WHERE crypto_invoice_id=?", (invoice_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_payment_status(invoice_id: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE payments SET status=? WHERE crypto_invoice_id=?", (status, invoice_id))
        await db.commit()


async def get_pending_payments() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM payments WHERE status='pending'") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ─── Каналы ────────────────────────────────────────────────

async def get_force_channels() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM force_channels") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def add_force_channel(username: str, channel_id: Optional[int] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO force_channels (channel_username, channel_id) VALUES (?,?)",
            (username, channel_id)
        )
        await db.commit()


async def delete_force_channel(channel_id_db: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM force_channels WHERE id=?", (channel_id_db,))
        await db.commit()


# ─── Настройки ─────────────────────────────────────────────

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        await db.commit()


# ─── Резервный бот ─────────────────────────────────────────

async def get_backup_bot() -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM backup_bot ORDER BY id DESC LIMIT 1") as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_backup_bot(token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM backup_bot")
        await db.execute("INSERT INTO backup_bot (backup_bot_token, is_active) VALUES (?,0)", (token,))
        await db.commit()


async def set_backup_bot_active(active: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE backup_bot SET is_active=?", (1 if active else 0,))
        await db.commit()


# ─── Логи админа ───────────────────────────────────────────

async def log_admin_action(admin_id: int, action: str, details: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_logs (admin_id,action,details) VALUES (?,?,?)",
            (admin_id, action, details)
        )
        await db.commit()


async def get_admin_logs(limit: int = 50) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ─── Промокоды ─────────────────────────────────────────────

async def create_promo_code(code: str, days: int, max_uses: int, created_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO promo_codes (code,days,max_uses,created_by) VALUES (?,?,?,?)",
            (code.upper(), days, max_uses, created_by)
        )
        await db.commit()


async def get_promo_code(code: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes WHERE code=?", (code.upper(),)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def use_promo_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code.upper(),))
        await db.commit()


async def get_all_promo_codes() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def delete_promo_code(code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promo_codes WHERE code=?", (code.upper(),))
        await db.commit()


# ─── Белый список ──────────────────────────────────────────

def _normalize_target(target: str) -> str:
    return target.strip().lower().lstrip("@")


async def add_to_whitelist(target: str, target_type: str, added_by: int, note: str = "") -> bool:
    normalized = _normalize_target(target)
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO whitelist (target,target_type,note,added_by) VALUES (?,?,?,?)",
                (normalized, target_type, note, added_by)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_from_whitelist(whitelist_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM whitelist WHERE id=?", (whitelist_id,))
        await db.commit()


async def get_whitelist() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM whitelist ORDER BY added_at DESC") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def is_whitelisted(target: str) -> Optional[dict]:
    normalized = _normalize_target(target)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM whitelist WHERE target=?", (normalized,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ─── Тарифы подписки ───────────────────────────────────────

async def get_subscription_plans() -> list:
    """Возвращает все тарифы, отсортированные по цене."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM subscription_plans ORDER BY price ASC") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def add_subscription_plan(days: int, price: float, label: str) -> int:
    """Добавляет новый тариф. Возвращает id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO subscription_plans (days,price,label) VALUES (?,?,?)",
            (days, price, label)
        )
        await db.commit()
        return cursor.lastrowid


async def delete_subscription_plan(plan_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscription_plans WHERE id=?", (plan_id,))
        await db.commit()
