"""
Diamond Mines Bot — aiogram 3.x + aiosqlite.
Currency: 💎 crystals.
"""

import asyncio
import csv
import io
import json
import logging
import math
import os
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import DiceEmoji, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

# ============================================================================
# CONFIG
# ============================================================================

BOT_TOKEN   = "8680465230:AAFB-jpZf4xYMOTi4uMUGAI18_tdebqh9CY"
ADMIN_IDS   = {853173723}
DB_PATH     = os.getenv("DB_PATH", "casino.db")

START_BONUS         = 1_000
MIN_BET             = 10
MAX_BET             = 100_000
DAILY_MIN           = 200
DAILY_MAX           = 1_000
REF_BONUS_INVITED   = 500
REF_BONUS_L1        = 250
REF_BONUS_L2        = 100
REF_BONUS_L3        = 50
TRANSFER_MIN        = 50
TRANSFER_FEE        = 0.02
SLOTS_JACKPOT_RATE  = 0.01
FLOOD_INTERVAL      = 30          # сек: окно для подсчёта действий
FLOOD_MAX_ACTIONS   = 10          # макс. действий за окно
FLOOD_BAN_HOURS     = 5           # бан при превышении
COOLDOWN_SECONDS    = 5           # пауза между ставками
MINES_TIMEOUT_MIN   = 30          # минуты до авто-отмены мин

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("casino")

# ============================================================================
# PREMIUM EMOJI IDS  (defaults — overwritten from DB on startup)
# ============================================================================
EMOJI_IDS: Dict[str, str] = {
    "settings": "5870982283724328568",
    "profile": "5870994129244131212",
    "people": "5870772616305839506",
    "checkmark": "5870633910337015697",
    "cross": "5870657884844462243",
    "gift": "6032644646587338669",
    "celebrate": "6041731551845159060",
    "chart": "5870921681735781843",
    "tag": "5886285355279193209",
    "coins": "5904462880941545555",
    "send_money": "5890848474563352982",
    "receive_money": "5879814368572478751",
    "back": "5963103826075456248",
    "download": "6039802767931871481",
    "code": "5940433880585605708",
    "lock": "6037249452824072506",
    "unlock": "6037496202990194718",
    "eye": "6037397706505195857",
    "info": "6028435952299413210",
    "bot": "6030400221232501136",
}

# Human-readable labels for admin UI
EMOJI_LABELS: Dict[str, str] = {
    "settings":      "⚙️ Настройки",
    "profile":       "👤 Профиль",
    "people":        "👥 Люди",
    "checkmark":     "✅ Галочка",
    "cross":         "❌ Крест",
    "gift":          "🎁 Подарок",
    "celebrate":     "🎊 Праздник",
    "chart":         "📊 График",
    "tag":           "🏷 Тег",
    "coins":         "💎 Монеты",
    "send_money":    "📤 Отправить",
    "receive_money": "📥 Получить",
    "back":          "⬅ Назад",
    "download":      "📥 Скачать",
    "code":          "💻 Код",
    "lock":          "🔒 Замок",
    "unlock":        "🔓 Открыт",
    "eye":           "👁 Глаз",
    "info":          "ℹ️ Инфо",
    "bot":           "🤖 Бот",
}

# ============================================================================
# DATABASE SCHEMA
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    balance         REAL    DEFAULT 0,
    level           INTEGER DEFAULT 1,
    prestige        INTEGER DEFAULT 0,
    exp             INTEGER DEFAULT 0,
    total_games     INTEGER DEFAULT 0,
    total_won       INTEGER DEFAULT 0,
    total_lost      INTEGER DEFAULT 0,
    total_profit    REAL    DEFAULT 0,
    current_streak  INTEGER DEFAULT 0,
    best_streak     INTEGER DEFAULT 0,
    register_date   TIMESTAMP,
    last_daily      TIMESTAMP,
    daily_streak    INTEGER DEFAULT 0,
    last_game       TIMESTAMP,
    last_bet_ts     REAL    DEFAULT 0,
    is_blocked      INTEGER DEFAULT 0,
    temp_ban_until  TIMESTAMP,
    flood_count     INTEGER DEFAULT 0,
    flood_win_start REAL    DEFAULT 0,
    is_premium      INTEGER DEFAULT 0,
    premium_until   TIMESTAMP,
    ref_code        TEXT UNIQUE,
    invited_by      INTEGER,
    achievements    TEXT    DEFAULT '[]',
    inventory       TEXT    DEFAULT '[]',
    total_refs      INTEGER DEFAULT 0,
    custom_nickname TEXT,
    language        TEXT    DEFAULT 'ru',
    last_cashback   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code            TEXT PRIMARY KEY,
    reward          REAL    DEFAULT 0,
    reward_type     TEXT    DEFAULT 'fixed',
    discount_percent REAL   DEFAULT 0,
    promo_type      TEXT    DEFAULT 'diamonds',
    uses_left       INTEGER DEFAULT 1,
    uses_max        INTEGER DEFAULT 1,
    created_by      INTEGER,
    created_at      TIMESTAMP,
    expires_at      TIMESTAMP,
    is_active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS promo_activations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    user_id     INTEGER,
    activated_at TIMESTAMP,
    UNIQUE(code, user_id)
);

CREATE TABLE IF NOT EXISTS game_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    game_type   TEXT,
    bet         REAL,
    dice_value  INTEGER,
    multiplier  REAL,
    result      TEXT,
    profit      REAL,
    timestamp   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS referrals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inviter_id  INTEGER,
    invited_id  INTEGER,
    level       INTEGER,
    bonus_given REAL,
    date        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE TABLE IF NOT EXISTS admin_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    INTEGER,
    action      TEXT,
    target_id   INTEGER,
    details     TEXT,
    timestamp   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jackpot (
    game_type   TEXT PRIMARY KEY,
    amount      REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jackpot_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    amount      REAL,
    game_type   TEXT,
    timestamp   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shop_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    description TEXT,
    price       REAL,
    item_type   TEXT,
    is_active   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS user_inventory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    item_id     INTEGER,
    quantity    INTEGER DEFAULT 1,
    used        INTEGER DEFAULT 0,
    purchased_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pending_games (
    user_id     INTEGER PRIMARY KEY,
    message_id  INTEGER,
    chat_id     INTEGER,
    bet         REAL,
    game_data   TEXT,
    created_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_settings (
    game        TEXT,
    key         TEXT,
    value       TEXT,
    PRIMARY KEY (game, key)
);

CREATE TABLE IF NOT EXISTS broadcast_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT,
    filter_type     TEXT,
    filter_param    TEXT,
    scheduled_at    TIMESTAMP,
    sent            INTEGER DEFAULT 0,
    created_by      INTEGER,
    created_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS message_templates (
    key     TEXT PRIMARY KEY,
    text    TEXT
);

CREATE TABLE IF NOT EXISTS required_channels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT UNIQUE,
    channel_title TEXT,
    invite_link TEXT,
    added_at    TIMESTAMP
);
"""

SEED_SQL = """
INSERT OR IGNORE INTO jackpot(game_type, amount) VALUES ('slots', 0);
INSERT OR IGNORE INTO settings(key, value) VALUES
    ('maintenance', '0'),
    ('disabled_games', '[]'),
    ('currency', '<tg-emoji emoji-id=''5904462880941545555''>💎</tg-emoji>'),
    ('start_bonus', '1000'),
    ('transfer_fee', '0.02'),
    ('min_bet', '10'),
    ('max_bet', '100000');
INSERT OR IGNORE INTO message_templates(key, text) VALUES
    ('start', 'Добро пожаловать в <tg-emoji emoji-id=''5904462880941545555''>💎</tg-emoji> Diamond Mines!\nСтартовый бонус: {bonus}'),
    ('help', '📖 Используй кнопки меню или текстовые команды.'),
    ('daily', '<tg-emoji emoji-id=''6032644646587338669''>🎀</tg-emoji> Ежедневный бонус: +{amount} <tg-emoji emoji-id=''5904462880941545555''>💎</tg-emoji>'),
    ('win', '<tg-emoji emoji-id=''6041731551845159060''>🎊</tg-emoji> Победа! +{win} <tg-emoji emoji-id=''5904462880941545555''>💎</tg-emoji>'),
    ('lose', '<tg-emoji emoji-id=''5870657884844462243''>💔</tg-emoji> Проигрыш: -{bet} <tg-emoji emoji-id=''5904462880941545555''>💎</tg-emoji>');
INSERT OR IGNORE INTO shop_items(name,description,price,item_type) VALUES
    ('💎 Премиум 30д','×1.2 к выигрышам, 30 дней',5000,'premium'),
    ('🔰 Никнейм','Смена ника командой /nick',1000,'nickname'),
    ('🎁 Мега-бонус','Бонус ×2 на следующий daily',2000,'mega_daily');
INSERT OR IGNORE INTO settings(key, value) VALUES
    ('emoji.settings',      '5870982283724328568'),
    ('emoji.profile',       '5870994129244131212'),
    ('emoji.people',        '5870772616305839506'),
    ('emoji.checkmark',     '5870633910337015697'),
    ('emoji.cross',         '5870657884844462243'),
    ('emoji.gift',          '6032644646587338669'),
    ('emoji.celebrate',     '6041731551845159060'),
    ('emoji.chart',         '5870921681735781843'),
    ('emoji.tag',           '5886285355279193209'),
    ('emoji.coins',         '5904462880941545555'),
    ('emoji.send_money',    '5890848474563352982'),
    ('emoji.receive_money', '5879814368572478751'),
    ('emoji.back',          '5963103826075456248'),
    ('emoji.download',      '6039802767931871481'),
    ('emoji.code',          '5940433880585605708'),
    ('emoji.lock',          '6037249452824072506'),
    ('emoji.unlock',        '6037496202990194718'),
    ('emoji.eye',           '6037397706505195857'),
    ('emoji.info',          '6028435952299413210'),
    ('emoji.bot',           '6030400221232501136');
"""

# ============================================================================
# DB HELPERS
# ============================================================================

async def db_exec(sql: str, params=()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()

async def db_fetchone(sql: str, params=()) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()

async def db_fetchall(sql: str, params=()) -> List[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN last_bet_ts REAL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN temp_ban_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN flood_count INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN flood_win_start REAL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN custom_nickname TEXT",
    "ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN premium_until TIMESTAMP",
    "ALTER TABLE users ADD COLUMN total_refs INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'ru'",
    "ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN prestige INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN exp INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN total_games INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN total_won INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN total_lost INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN total_profit REAL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN current_streak INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN best_streak INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN daily_streak INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN last_game TIMESTAMP",
    "ALTER TABLE users ADD COLUMN last_daily TIMESTAMP",
    "ALTER TABLE users ADD COLUMN invited_by INTEGER",
    "ALTER TABLE users ADD COLUMN achievements TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN inventory TEXT DEFAULT '[]'",
]

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.executescript(SEED_SQL)
        # Run migrations — ignore errors for columns that already exist
        for sql in MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass
        await db.commit()
    log.info("DB initialised.")

def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")

# ============================================================================
# SETTINGS CACHE
# ============================================================================

async def get_setting(key: str, default: str = "") -> str:
    r = await db_fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default

async def set_setting(key: str, value: str) -> None:
    await db_exec(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

async def get_currency() -> str:
    return await get_setting("currency", "💎")

async def load_emoji_ids() -> None:
    """Load emoji IDs from settings DB into the global EMOJI_IDS dict."""
    rows = await db_fetchall("SELECT key, value FROM settings WHERE key LIKE 'emoji.%'")
    for r in rows:
        key = r["key"][6:]  # strip 'emoji.' prefix
        if r["value"]:
            EMOJI_IDS[key] = r["value"]

async def save_emoji_id(key: str, value: str) -> None:
    """Persist a single emoji ID to the settings table and update EMOJI_IDS."""
    await set_setting(f"emoji.{key}", value)
    EMOJI_IDS[key] = value

# ============================================================================
# GAME REGISTRY
# ============================================================================

GAME_KEYS: Dict[str, str] = {
    "dice":  "🎲 Кости",
    "foot":  "⚽ Футбол",
    "darts": "🎯 Дартс",
    "bowl":  "🎳 Боулинг",
    "slot":  "🎰 Слоты",
    "roul":  "🎡 Рулетка",
    "coin":  "🪙 Монетка",
    "horse": "🏇 Скачки",
    "mines": "💣 Мины",
    "bj":    "🃏 Блэкджек",
}

async def get_disabled_games() -> Set[str]:
    raw = await get_setting("disabled_games", "[]")
    try:
        return set(json.loads(raw))
    except Exception:
        return set()

async def set_disabled_games(disabled: Set[str]) -> None:
    await set_setting("disabled_games", json.dumps(sorted(disabled)))

async def game_enabled(key: str) -> bool:
    return key not in await get_disabled_games()

async def guard_game(m: Message, key: str, bot: Bot = None) -> bool:
    maint = await get_setting("maintenance", "0")
    if maint == "1" and not is_admin(m.from_user.id):
        await m.answer("🔧 Бот на техническом обслуживании. Скоро вернёмся!", parse_mode=ParseMode.HTML)
        return False
    if not await game_enabled(key):
        await m.answer("⛔ Эта игра временно отключена администратором.")
        return False
    if bot and not is_admin(m.from_user.id):
        if not await subscription_wall(m, bot):
            return False
    return True

async def get_game_setting(game: str, key: str, default: str) -> str:
    r = await db_fetchone("SELECT value FROM game_settings WHERE game=? AND key=?", (game, key))
    return r["value"] if r else default

# ============================================================================
# SUBSCRIPTION CHECK
# ============================================================================

async def check_subscriptions(user_id: int, bot: Bot) -> Tuple[bool, List]:
    """Returns (all_ok, list_of_channels_not_subscribed)."""
    channels = await db_fetchall("SELECT * FROM required_channels")
    if not channels:
        return True, []
    not_subbed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subbed.append(ch)
        except Exception:
            not_subbed.append(ch)
    return len(not_subbed) == 0, not_subbed

async def subscription_wall(m: Message, bot: Bot) -> bool:
    """Shows subscription wall if user is not subscribed. Returns True if OK to proceed."""
    ok, missing = await check_subscriptions(m.from_user.id, bot)
    if ok:
        return True
    btns = []
    for ch in missing:
        title = ch["channel_title"] or ch["channel_id"]
        link = ch["invite_link"] or f"https://t.me/{str(ch['channel_id']).lstrip('@')}"
        btns.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
    btns.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")])
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await m.answer(
        "⚠️ <b>Для использования бота нужно подписаться на каналы:</b>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )
    return False

async def ensure_subscribed(user_id: int, bot: Bot) -> bool:
    """Check if user is subscribed. Returns True if OK, False if wall shown."""
    ok, missing = await check_subscriptions(user_id, bot)
    return ok

# ============================================================================
# STARS SHOP PACKAGES
# ============================================================================

STARS_PACKAGES = [
    {"diamonds": 100_000,  "stars": 50,  "label": "100 000 💎"},
    {"diamonds": 200_000,  "stars": 90,  "label": "200 000 💎"},
    {"diamonds": 500_000,  "stars": 222, "label": "500 000 💎"},
    {"diamonds": 1_000_000,"stars": 400, "label": "1 000 000 💎"},
]

# user_id -> {"code": str, "discount": float}
SHOP_DISCOUNT: dict[int, dict] = {}

# user_id -> {"answer": int, "payload": str, "attempts": int}
CAPTCHA_PENDING: dict[int, dict] = {}

CASHBACK_RATE        = 0.05   # 5% от проигрышей
CASHBACK_PERIOD_DAYS = 7      # период расчёта
CASHBACK_MIN_LOSS    = 1_000  # минимальный проигрыш для получения кэшбэка

# ============================================================================
# ANTI-FLOOD
# ============================================================================

async def check_flood(user_id: int) -> Optional[str]:
    u = await db_fetchone(
        "SELECT last_bet_ts, flood_count, flood_win_start, temp_ban_until FROM users WHERE user_id=?",
        (user_id,),
    )
    if not u:
        return None
    now = time.time()
    # Проверка временного бана
    if u["temp_ban_until"]:
        ban_until = datetime.fromisoformat(u["temp_ban_until"])
        if datetime.now(timezone.utc) < ban_until:
            remain = int((ban_until - datetime.now(timezone.utc)).total_seconds() / 60)
            return f"⛔ Вы заблокированы за флуд на {remain} мин."
    # Кулдаун 5 сек
    if now - u["last_bet_ts"] < COOLDOWN_SECONDS:
        wait = COOLDOWN_SECONDS - (now - u["last_bet_ts"])
        return f"⏳ Подождите {wait:.1f} сек перед следующей ставкой."
    # Счётчик флуда
    win_start = u["flood_win_start"] or 0.0
    count = u["flood_count"] or 0
    if now - win_start > FLOOD_INTERVAL:
        count = 1
        win_start = now
    else:
        count += 1
    if count > FLOOD_MAX_ACTIONS:
        ban_until_dt = datetime.now(timezone.utc) + timedelta(hours=FLOOD_BAN_HOURS)
        await db_exec(
            "UPDATE users SET temp_ban_until=?, flood_count=0 WHERE user_id=?",
            (ban_until_dt.isoformat(), user_id),
        )
        return f"🚫 Слишком много ставок! Бан на {FLOOD_BAN_HOURS} часов."
    await db_exec(
        "UPDATE users SET last_bet_ts=?, flood_count=?, flood_win_start=? WHERE user_id=?",
        (now, count, win_start, user_id),
    )
    return None

# ============================================================================
# USER HELPERS
# ============================================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    return await db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))

async def ensure_user(m: Message) -> aiosqlite.Row:
    u = await get_user(m.from_user.id)
    if not u:
        bonus = float(await get_setting("start_bonus", str(START_BONUS)))
        ref_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        await db_exec(
            "INSERT OR IGNORE INTO users(user_id,username,first_name,last_name,balance,ref_code,register_date) "
            "VALUES(?,?,?,?,?,?,?)",
            (m.from_user.id, m.from_user.username or "",
             m.from_user.first_name or "", m.from_user.last_name or "",
             bonus, ref_code, now_ts()),
        )
        u = await get_user(m.from_user.id)
    return u

async def update_balance(user_id: int, delta: float) -> None:
    await db_exec("UPDATE users SET balance=balance+? WHERE user_id=?", (delta, user_id))

async def can_bet(user_id: int, bet: float) -> Tuple[bool, str]:
    min_b = float(await get_setting("min_bet", str(MIN_BET)))
    max_b = float(await get_setting("max_bet", str(MAX_BET)))
    u = await get_user(user_id)
    if not u:
        return False, "Сначала /start"
    if u["is_blocked"]:
        return False, "❌ Аккаунт заблокирован."
    if u["temp_ban_until"]:
        if datetime.now(timezone.utc) < datetime.fromisoformat(u["temp_ban_until"]):
            return False, "⛔ Вы временно заблокированы за флуд."
    if bet < min_b:
        return False, f"Минимальная ставка: {fmt(min_b)} 💎"
    if bet > max_b:
        return False, f"Максимальная ставка: {fmt(max_b)} 💎"
    if u["balance"] < bet:
        return False, f"Недостаточно 💎. Баланс: {fmt(u['balance'])}"
    return True, ""

def parse_bet(s: str) -> Optional[float]:
    try:
        v = float(s.replace(" ", "").replace(",", "."))
        return v if v > 0 else None
    except Exception:
        return None

async def resolve_bet(s: str, user_id: int) -> Optional[float]:
    """Как parse_bet, но «все»/«вб» возвращает весь баланс пользователя."""
    if s.lower() in ("все", "вб", "all"):
        row = await db_fetchone("SELECT balance FROM users WHERE user_id=?", (user_id,))
        if row and row["balance"] > 0:
            return float(row["balance"])
        return None
    return parse_bet(s)

async def record_game(user_id: int, game_type: str, bet: float, dice_val: int,
                      mult: float, res: str, profit: float) -> None:
    await db_exec(
        "INSERT INTO game_stats(user_id,game_type,bet,dice_value,multiplier,result,profit,timestamp) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user_id, game_type, bet, dice_val, mult, res, profit, now_ts()),
    )
    won = 1 if profit > 0 else 0
    lost = 1 if profit < 0 else 0
    streak_sql = (
        "UPDATE users SET total_games=total_games+1, total_won=total_won+?, total_lost=total_lost+?,"
        " total_profit=total_profit+?, last_game=?, "
        " current_streak=CASE WHEN ?=1 THEN current_streak+1 ELSE 0 END,"
        " best_streak=CASE WHEN ?=1 AND current_streak+1>best_streak THEN current_streak+1 ELSE best_streak END"
        " WHERE user_id=?"
    )
    await db_exec(streak_sql, (won, lost, profit, now_ts(), won, won, user_id))

async def add_exp(user_id: int, amount: int = 1) -> None:
    u = await get_user(user_id)
    if not u:
        return
    new_exp = u["exp"] + amount
    new_level = u["level"]
    if new_exp >= 10:
        new_exp -= 10
        new_level += 1
        if new_level > 50:
            new_level = 1
            await db_exec("UPDATE users SET prestige=prestige+1 WHERE user_id=?", (user_id,))
    await db_exec("UPDATE users SET exp=?, level=? WHERE user_id=?", (new_exp, new_level, user_id))

async def premium_multiplier(user_id: int) -> float:
    u = await get_user(user_id)
    if not u or not u["is_premium"]:
        return 1.0
    if u["premium_until"]:
        if datetime.now(timezone.utc) > datetime.fromisoformat(u["premium_until"]):
            await db_exec("UPDATE users SET is_premium=0 WHERE user_id=?", (user_id,))
            return 1.0
    mult = 1.0 + 0.05 * u["prestige"] + 0.2
    return mult

async def add_admin_log(admin_id: int, action: str, target_id: int = 0, details: str = "") -> None:
    await db_exec(
        "INSERT INTO admin_logs(admin_id,action,target_id,details,timestamp) VALUES(?,?,?,?,?)",
        (admin_id, action, target_id, details, now_ts()),
    )

ACHIEVEMENTS: Dict[str, str] = {
    "novice":    "🎲 Новичок",
    "gamer":     "🎮 Геймер",
    "veteran":   "🏆 Ветеран",
    "whale":     "🐋 Кит",
    "lucky":     "🍀 Удачливый",
    "rich":      "💰 Богач",
    "recruiter": "👥 Рекрутёр",
    "partner":   "🤝 Партнёр",
    "jackpot":   "🎰 Джекпот",
    "prestige":  "🌟 Престиж",
}

async def grant_achievement(user_id: int, key: str, bot: Bot) -> None:
    u = await get_user(user_id)
    if not u:
        return
    achs = json.loads(u["achievements"] or "[]")
    if key not in achs:
        achs.append(key)
        await db_exec("UPDATE users SET achievements=? WHERE user_id=?",
                      (json.dumps(achs), user_id))
        try:
            await bot.send_message(user_id,
                                   f"🏅 Получено достижение: {ACHIEVEMENTS.get(key, key)}!")
        except Exception:
            pass

async def check_post_game_achievements(user_id: int, bet: float, game: str, bot: Bot) -> None:
    u = await get_user(user_id)
    if not u:
        return
    if u["total_games"] >= 1:
        await grant_achievement(user_id, "novice", bot)
    if u["total_games"] >= 50:
        await grant_achievement(user_id, "gamer", bot)
    if u["total_games"] >= 500:
        await grant_achievement(user_id, "veteran", bot)
    if u["balance"] >= 100_000:
        await grant_achievement(user_id, "rich", bot)
    if bet >= 10_000:
        await grant_achievement(user_id, "whale", bot)
    if u["total_refs"] >= 5:
        await grant_achievement(user_id, "recruiter", bot)
    if u["total_refs"] >= 20:
        await grant_achievement(user_id, "partner", bot)
    if u["prestige"] >= 1:
        await grant_achievement(user_id, "prestige", bot)

def prestige_badge(prestige: int) -> str:
    return ["", "💫", "🌟", "👑"][min(prestige, 3)]

# ============================================================================
# ROULETTE HELPERS
# ============================================================================

ROULETTE_RED: Set[int] = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
ROULETTE_BLACK: Set[int] = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

def roul_color_emoji(n: int) -> str:
    if n == 0: return "🟢"
    return "🔴" if n in ROULETTE_RED else "⚫"

def roul_color_name(n: int) -> str:
    if n == 0: return "зелёное"
    return "красное" if n in ROULETTE_RED else "чёрное"

def parse_roulette_bet(spec: str):
    s = spec.strip().lower().replace("ё","е").replace(" ","")
    if not s: return None
    if s in ("красное","красн","red","к"): return ROULETTE_RED, 2.0, "красное"
    if s in ("черное","чёрное","черн","black","ч"): return ROULETTE_BLACK, 2.0, "чёрное"
    if s in ("чет","even"): return {n for n in range(1,37) if n%2==0}, 2.0, "чёт"
    if s in ("нечет","odd"): return {n for n in range(1,37) if n%2==1}, 2.0, "нечёт"
    if s in ("малые","малое","low","1-18"): return set(range(1,19)), 2.0, "малые (1-18)"
    if s in ("большие","большое","high","19-36"): return set(range(19,37)), 2.0, "большие (19-36)"
    if s in ("1д","1d"): return set(range(1,13)), 3.0, "1-я дюжина (1-12)"
    if s in ("2д","2d"): return set(range(13,25)), 3.0, "2-я дюжина (13-24)"
    if s in ("3д","3d"): return set(range(25,37)), 3.0, "3-я дюжина (25-36)"
    if "-" in s and "," not in s:
        try:
            a,b = s.split("-",1); ai,bi = int(a),int(b)
            if 0<=ai<=bi<=36:
                nums = set(range(ai,bi+1))
                return nums, round(36.0/len(nums),2), f"{ai}-{bi}"
        except Exception: pass
    if "," in s:
        try:
            nums = {int(x) for x in s.split(",") if x.strip()}
            if nums and all(0<=x<=36 for x in nums):
                return nums, round(36.0/len(nums),2), ",".join(str(x) for x in sorted(nums))
        except Exception: pass
    try:
        n = int(s)
        if 0<=n<=36: return {n}, 36.0, f"число {n}"
    except Exception: pass
    return None

# ============================================================================
# SLOTS (pure random, no dice)
# ============================================================================

SLOT_SYM = {"7":"7️⃣","BAR":"🎰","CHERRY":"🍒","BELL":"🔔","DIAMOND":"💎","STAR":"⭐"}

def spin_slots() -> Tuple[List[str], float, str]:
    """Returns (symbols, multiplier, label)."""
    r = random.randint(0, 999)
    if r < 5:          # 0.5%
        return ["7","7","7"], 100.0, "🎊 ДЖЕКПОТ 777! ✨"
    if r < 25:         # 2%
        return ["BAR","BAR","BAR"], 20.0, "BAR BAR BAR! 💰"
    if r < 75:         # 5%
        return ["CHERRY","CHERRY","CHERRY"], 5.0, "CHERRY CHERRY CHERRY! 🍒"
    # Loss – generate random non-winning combo
    pool = ["7","BAR","CHERRY","BELL","DIAMOND","STAR"]
    while True:
        s = [random.choice(pool) for _ in range(3)]
        if len(set(s)) > 1:  # ensure no triple match
            return s, 0.0, "Нет выигрыша"

def slot_display(syms: List[str]) -> str:
    return " | ".join(SLOT_SYM.get(s, s) for s in syms)

# ============================================================================
# MINES SESSIONS
# ============================================================================

MINES_SESSIONS: Dict[int, Dict] = {}  # user_id → session

def mines_mult(revealed: int, mines_count: int) -> float:
    """Математически верный множитель (комбинаторика, 3% хаус эдж)."""
    total = 25
    safe = total - mines_count
    if revealed <= 0 or safe <= 0 or revealed > safe:
        return 1.0
    return round(math.comb(total, revealed) / math.comb(safe, revealed) * 0.97, 2)

def mines_board_kb(user_id: int) -> InlineKeyboardMarkup:
    sess = MINES_SESSIONS.get(user_id, {})
    revealed = sess.get("revealed", set())
    mine_set = sess.get("mines", set())
    show_mines = sess.get("show_mines", False)
    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if idx in revealed:
                row.append(InlineKeyboardButton(text="💎", callback_data="mines:done"))
            elif show_mines and idx in mine_set:
                row.append(InlineKeyboardButton(text="💥", callback_data="mines:done"))
            else:
                row.append(InlineKeyboardButton(text="⬜", callback_data=f"mines:cell:{idx}"))
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="💰 Забрать выигрыш", callback_data="mines:cashout"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="mines:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def save_mines_session(user_id: int, message_id: int, chat_id: int) -> None:
    sess = MINES_SESSIONS.get(user_id, {})
    data = json.dumps({
        "mines": list(sess.get("mines", set())),
        "revealed": list(sess.get("revealed", set())),
        "bet": sess.get("bet", 0),
        "mines_count": sess.get("mines_count", 3),
    })
    await db_exec(
        "INSERT OR REPLACE INTO pending_games(user_id,message_id,chat_id,bet,game_data,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, message_id, chat_id, sess.get("bet", 0), data, now_ts()),
    )

async def load_mines_sessions() -> None:
    rows = await db_fetchall("SELECT * FROM pending_games")
    for row in rows:
        try:
            data = json.loads(row["game_data"])
            MINES_SESSIONS[row["user_id"]] = {
                "mines": set(data["mines"]),
                "revealed": set(data["revealed"]),
                "bet": data["bet"],
                "mines_count": data["mines_count"],
                "message_id": row["message_id"],
                "chat_id": row["chat_id"],
                "show_mines": False,
            }
        except Exception as e:
            log.warning("Failed to restore mines session %s: %s", row["user_id"], e)
    log.info("Restored %d mines sessions.", len(MINES_SESSIONS))

async def cancel_mines_session(user_id: int, bot: Optional[Bot] = None) -> None:
    sess = MINES_SESSIONS.pop(user_id, None)
    await db_exec("DELETE FROM pending_games WHERE user_id=?", (user_id,))
    if sess:
        await update_balance(user_id, sess["bet"])
        if bot and sess.get("chat_id") and sess.get("message_id"):
            try:
                await bot.edit_message_text(
                    "⏰ Игра в мины отменена (таймаут). Ставка возвращена.",
                    chat_id=sess["chat_id"], message_id=sess["message_id"],
                )
            except Exception:
                pass

# ============================================================================
# BLACKJACK
# ============================================================================

BJ_SESSIONS: Dict[int, Dict] = {}

def bj_deck():
    vals = [2,3,4,5,6,7,8,9,10,10,10,10,11] * 4
    random.shuffle(vals)
    return vals

def bj_total(hand: List[int]) -> int:
    s = sum(hand)
    aces = hand.count(11)
    while s > 21 and aces:
        s -= 10; aces -= 1
    return s

def bj_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Ещё карту", callback_data="bj:hit", icon_custom_emoji_id=EMOJI_IDS["unlock"]),
        InlineKeyboardButton(text="Стоп", callback_data="bj:stand", icon_custom_emoji_id=EMOJI_IDS["lock"]),
    ]])

def bj_msg(sess: Dict) -> str:
    ph = bj_total(sess["player"])
    dh = bj_total(sess["dealer"][:1])
    return (
        f"🃏 <b>Блэкджек</b> | Ставка: {fmt(sess['bet'])} 💎\n"
        f"Ваши карты: {sess['player']} = <b>{ph}</b>\n"
        f"Дилер: [{sess['dealer'][0]}, ?] = {dh}+"
    )

# ============================================================================
# KEYBOARDS
# ============================================================================

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Профиль", callback_data="profile", icon_custom_emoji_id=EMOJI_IDS["profile"]),
         InlineKeyboardButton(text="Баланс",  callback_data="balance", icon_custom_emoji_id=EMOJI_IDS["coins"])],
        [InlineKeyboardButton(text="Бонус",   callback_data="daily", icon_custom_emoji_id=EMOJI_IDS["gift"]),
         InlineKeyboardButton(text="Игры",    callback_data="games", icon_custom_emoji_id=EMOJI_IDS["bot"])],
        [InlineKeyboardButton(text="Топ",     callback_data="top", icon_custom_emoji_id=EMOJI_IDS["chart"]),
         InlineKeyboardButton(text="Магазин", callback_data="shop", icon_custom_emoji_id=EMOJI_IDS["tag"])],
        [InlineKeyboardButton(text="Купить",  callback_data="buy_menu", icon_custom_emoji_id=EMOJI_IDS["coins"]),
         InlineKeyboardButton(text="Реферал", callback_data="ref", icon_custom_emoji_id=EMOJI_IDS["people"])],
        [InlineKeyboardButton(text="Помощь",  callback_data="help", icon_custom_emoji_id=EMOJI_IDS["info"])],
    ])

async def games_menu_kb() -> InlineKeyboardMarkup:
    disabled = await get_disabled_games()
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"g:{key}")
        for key, label in GAME_KEYS.items() if key not in disabled
    ]
    rows: list = []
    for i in range(0, len(buttons), 3):
        rows.append(buttons[i:i+3])
    if not rows:
        rows.append([InlineKeyboardButton(text="(нет активных игр)", callback_data="menu")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu", icon_custom_emoji_id=EMOJI_IDS["back"])])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Игры", web_app=None),    KeyboardButton(text="Профиль"), KeyboardButton(text="Бонус")],
            [KeyboardButton(text="Баланс"),  KeyboardButton(text="Топ"),     KeyboardButton(text="Магазин")],
            [KeyboardButton(text="Реферал"), KeyboardButton(text="Купить"),   KeyboardButton(text="Кэшбэк")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True, is_persistent=True,
    )

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Дашборд",             callback_data="adm:dash")],
        [InlineKeyboardButton(text="👥 Пользователи",        callback_data="adm:users")],
        [InlineKeyboardButton(text="🎮 Игры (вкл/выкл)",     callback_data="adm:games")],
        [InlineKeyboardButton(text="🎁 Промокоды",           callback_data="adm:promo")],
        [InlineKeyboardButton(text="📢 Рассылка",            callback_data="adm:bcast")],
        [InlineKeyboardButton(text="💰 Джекпот",             callback_data="adm:jp")],
        [InlineKeyboardButton(text="📡 Обязательные каналы", callback_data="adm:channels")],
        [InlineKeyboardButton(text="⚙️ Настройки",           callback_data="adm:settings")],
        [InlineKeyboardButton(text="✨ Премиум эмодзи",      callback_data="adm:emoji")],
        [InlineKeyboardButton(text="📜 Логи",                callback_data="adm:logs")],
        [InlineKeyboardButton(text="💾 База данных",         callback_data="adm:db")],
        [InlineKeyboardButton(text="🧹 Сессии мин",          callback_data="adm:sessions")],
        [InlineKeyboardButton(text="📝 Шаблоны",             callback_data="adm:templates")],
        [InlineKeyboardButton(text="🔄 Экспорт/Импорт",      callback_data="adm:export")],
    ])

def back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="adm:back", icon_custom_emoji_id=EMOJI_IDS["back"])]
    ])

# Admin pending state {admin_id: {action, data}}
ADMIN_PENDING: Dict[int, Dict] = {}

# ============================================================================
# ROUTER
# ============================================================================

router = Router()

# ============================================================================
# safe_edit helper
# ============================================================================

async def safe_edit(c: CallbackQuery, text: str, kb=None):
    # Prevent users from clicking buttons sent by other users (only in group chats)
    if c.message and c.message.from_user and c.message.chat.type != "private":
        if c.message.from_user.id != c.from_user.id:
            await c.answer("❌ Ты не можешь использовать чужие кнопки.", show_alert=False)
            return
    try:
        await c.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass

# ============================================================================
# /start
# ============================================================================

def _make_captcha() -> tuple[int, int, int, list[int]]:
    """Returns (a, b, answer, [shuffled options x4])."""
    import random as _rnd
    a = _rnd.randint(2, 12)
    b = _rnd.randint(2, 12)
    answer = a + b
    wrong = set()
    while len(wrong) < 3:
        w = answer + _rnd.randint(-5, 5)
        if w != answer and w > 0:
            wrong.add(w)
    opts = [answer] + list(wrong)
    _rnd.shuffle(opts)
    return a, b, answer, opts

def captcha_kb(opts: list[int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=str(o), callback_data=f"captcha:{o}", icon_custom_emoji_id=EMOJI_IDS["checkmark"])
        for o in opts
    ]])

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message, bot: Bot):
    args = (m.text or "").split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""
    # — Проверяем: новый пользователь? Если да — капча
    existing = await get_user(m.from_user.id)
    if not existing:
        a, b, answer, opts = _make_captcha()
        CAPTCHA_PENDING[m.from_user.id] = {"answer": answer, "payload": payload, "attempts": 0}
        await m.answer(
            "🔒 <b>Проверка безопасности</b>\n\n"
            f"Сколько будет <b>{a} + {b}</b>?\n\n"
            "Нажми правильный ответ:",
            reply_markup=captcha_kb(opts)
        )
        return
    # Уже зарегистрирован — просто показываем меню
    await _finish_start(m, payload, bot)

async def _finish_start(m_or_c, payload: str, bot: Bot):
    """Общий финал /start — вызывается после прохождения капчи или напрямую."""
    from aiogram.types import Message as Msg, CallbackQuery as CQ
    is_cb = isinstance(m_or_c, CQ)
    user_obj = m_or_c.from_user
    msg = m_or_c.message if is_cb else m_or_c

    user = await ensure_user(m_or_c if not is_cb else msg)

    # Check subscription wall before showing welcome menu
    if not await ensure_subscribed(user_obj.id, bot):
        ok, missing = await check_subscriptions(user_obj.id, bot)
        btns = []
        for ch in missing:
            title = ch["channel_title"] or ch["channel_id"]
            link = ch["invite_link"] or f"https://t.me/{str(ch['channel_id']).lstrip('@')}"
            btns.append([InlineKeyboardButton(text=f"📢 {title}", url=link)])
        btns.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="sub:check")])
        kb = InlineKeyboardMarkup(inline_keyboard=btns)
        await msg.answer(
            "⚠️ <b>Для использования бота нужно подписаться на каналы:</b>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        return

    if payload and not user["invited_by"]:
        ref_row = await db_fetchone("SELECT user_id FROM users WHERE ref_code=?", (payload,))
        if ref_row and ref_row["user_id"] != user_obj.id:
            inviter = ref_row["user_id"]
            await db_exec("UPDATE users SET invited_by=?,balance=balance+? WHERE user_id=?",
                          (inviter, REF_BONUS_INVITED, user_obj.id))
            await update_balance(inviter, REF_BONUS_L1)
            await db_exec("UPDATE users SET total_refs=total_refs+1 WHERE user_id=?", (inviter,))
            await db_exec(
                "INSERT INTO referrals(inviter_id,invited_id,level,bonus_given,date) VALUES(?,?,?,?,?)",
                (inviter, user_obj.id, 1, REF_BONUS_L1, now_ts()),
            )
            inv2 = await db_fetchone("SELECT invited_by FROM users WHERE user_id=?", (inviter,))
            if inv2 and inv2["invited_by"]:
                await update_balance(inv2["invited_by"], REF_BONUS_L2)
                await db_exec(
                    "INSERT INTO referrals(inviter_id,invited_id,level,bonus_given,date) VALUES(?,?,?,?,?)",
                    (inv2["invited_by"], user_obj.id, 2, REF_BONUS_L2, now_ts()),
                )
                inv3 = await db_fetchone("SELECT invited_by FROM users WHERE user_id=?", (inv2["invited_by"],))
                if inv3 and inv3["invited_by"]:
                    await update_balance(inv3["invited_by"], REF_BONUS_L3)
            try:
                await bot.send_message(inviter, f"✨ По вашей ссылке зарегистрирован новый игрок! +{REF_BONUS_L1} 💎")
            except Exception:
                pass

    tmpl = await get_setting("start_bonus", str(START_BONUS))
    bonus_val = float(tmpl)
    await msg.answer(
        f"✨ <b>Добро пожаловать в Diamond Mines!</b> ✨\n\n"
        f"💎 Стартовый бонус: <b>{fmt(bonus_val)}</b>\n"
        f"📖 Команды: /help",
        reply_markup=reply_kb(),
        parse_mode=ParseMode.HTML
    )
    await msg.answer("Главное меню:", reply_markup=main_menu_kb())

@router.callback_query(F.data.startswith("captcha:"))
async def cb_captcha(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    pending = CAPTCHA_PENDING.get(uid)
    if not pending:
        await c.answer("Введи /start заново.", show_alert=True); return

    chosen = int(c.data.split(":")[1])
    if chosen == pending["answer"]:
        CAPTCHA_PENDING.pop(uid, None)
        try:
            await c.message.edit_text("✅ <b>Проверка пройдена!</b> Добро пожаловать!")
        except TelegramBadRequest:
            pass
        await _finish_start(c, pending["payload"], bot)
        await c.answer("✅ Верно!", parse_mode=ParseMode.HTML)
    else:
        pending["attempts"] += 1
        if pending["attempts"] >= 5:
            CAPTCHA_PENDING.pop(uid, None)
            await c.message.edit_text("❌ Слишком много попыток. Напиши /start снова.")
            await c.answer("❌ Много ошибок", show_alert=True, parse_mode=ParseMode.HTML)
            return
        a, b, answer, opts = _make_captcha()
        pending["answer"] = answer
        await c.message.edit_text(
            f"❌ Неверно! Попытка {pending['attempts']}/5\n\n"
            f"🔒 <b>Проверка безопасности</b>\n\n"
            f"Сколько будет <b>{a} + {b}</b>?\n\n"
            "Нажми правильный ответ:",
            reply_markup=captcha_kb(opts)
        )
        await c.answer("❌ Неверно, попробуй ещё раз", parse_mode=ParseMode.HTML)

# ============================================================================
# CASHBACK
# ============================================================================

@router.message(Command("cashback"), F.chat.type == "private")
@router.message(F.text.lower().in_({"кэшбэк","кешбек","cashback","💰 кэшбэк"}))
async def cmd_cashback(m: Message, bot: Bot):
    await ensure_user(m)
    uid = m.from_user.id
    if not await ensure_subscribed(uid, bot):
        await subscription_wall(m, bot); return
    u = await get_user(uid)
    now = datetime.now(timezone.utc)
    period = timedelta(days=CASHBACK_PERIOD_DAYS)

    # Проверяем кулдаун
    if u["last_cashback"]:
        last = datetime.fromisoformat(u["last_cashback"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        next_cb = last + period
        if now < next_cb:
            remaining = next_cb - now
            hours   = int(remaining.total_seconds() // 3600)
            minutes = int((remaining.total_seconds() % 3600) // 60)
            await m.answer(
                f"💰 <b>Кэшбэк</b>\n\n"
                f"Следующий кэшбэк через: <b>{hours}ч {minutes}мин</b>\n\n"
                f"Кэшбэк начисляется раз в {CASHBACK_PERIOD_DAYS} дней — "
                f"{int(CASHBACK_RATE*100)}% от проигрышей за период."
            ); return

    # Считаем проигрыши за последние 7 дней
    since = (now - period).isoformat()
    row = await db_fetchone(
        "SELECT COALESCE(SUM(ABS(profit)), 0) AS total_loss "
        "FROM game_stats WHERE user_id=? AND profit < 0 AND timestamp >= ?",
        (uid, since)
    )
    total_loss = float(row["total_loss"]) if row else 0.0

    if total_loss < CASHBACK_MIN_LOSS:
        await m.answer(
            f"💰 <b>Кэшбэк</b>\n\n"
            f"Минимальный проигрыш для кэшбэка: <b>{fmt(CASHBACK_MIN_LOSS)} 💎</b>\n"
            f"Твои проигрыши за {CASHBACK_PERIOD_DAYS} дней: <b>{fmt(total_loss)} 💎</b>\n\n"
            f"Играй больше, чтобы получить кэшбэк!"
        ); return

    cashback = round(total_loss * CASHBACK_RATE)
    await update_balance(uid, cashback)
    await db_exec("UPDATE users SET last_cashback=? WHERE user_id=?", (now.isoformat(), uid))
    cur = await get_currency()
    await m.answer(
        f"💰 <b>Кэшбэк получен!</b> ✨\n\n"
        f"📊 Проигрыши за {CASHBACK_PERIOD_DAYS} дней: <b>{fmt(total_loss)}</b> {cur}\n"
        f"💎 Кэшбэк {int(CASHBACK_RATE*100)}%: <b>+{fmt(cashback)}</b> {cur}\n\n"
        f"Следующий кэшбэк доступен через {CASHBACK_PERIOD_DAYS} дней."
    )

# ============================================================================
# HELP
# ============================================================================

@router.message(Command("help"), F.chat.type == "private")
@router.message(F.text.lower().in_({"помощь","хелп","команды","help","📖 помощь"}))
async def cmd_help(m: Message, bot: Bot):
    await ensure_user(m)
    if not await ensure_subscribed(m.from_user.id, bot):
        await subscription_wall(m, bot); return
    await m.answer(
        "<b>📖 Команды</b>\n\n"
        "<b>👤 Профиль</b>: <code>профиль</code>\n"
        "<b>🎀 Бонус</b>: <code>бонус</code>\n"
        "<b>🎟 Промокод</b>: <code>промо КОД</code>\n"
        "<b>👥 Реферал</b>: <code>реферал</code>\n"
        "<b>💸 Перевод</b>: <code>перевод ID СУММА</code> или ответом\n"
        "<b>🏬 Магазин</b>: <code>магазин</code>\n\n"
        "<b>🎲 Игры:</b>\n"
        "• <code>куб 100 чет|нечет|число 6</code>\n"
        "• <code>фут 100 гол|мимо</code>\n"
        "• <code>дартс 100 центр|красное|белое|мимо</code>\n"
        "• <code>боул 100 страйк|ничего</code>\n"
        "• <code>слот 100</code>\n"
        "• <code>рул 100 красное|17|10-15|чет|1д</code>\n"
        "• <code>монетка 100 орел|решка</code>\n"
        "• <code>скачки 100 3</code>\n"
        "• <code>мины 100 3</code>\n"
        "• <code>блэкджек 100</code>\n"
    )

# ============================================================================
# PROFILE / BALANCE / DAILY / TOP
# ============================================================================

async def render_profile(user_id: int) -> str:
    u = await get_user(user_id)
    if not u: return "Сначала /start"
    achs = json.loads(u["achievements"] or "[]")
    ach_str = " ".join(ACHIEVEMENTS[a] for a in achs if a in ACHIEVEMENTS) or "—"
    badge = prestige_badge(u["prestige"])
    name = u["custom_nickname"] or u["first_name"] or u["username"] or str(u["user_id"])
    cur = await get_currency()
    return (
        f"╔══════════════════╗\n"
        f"  <tg-emoji emoji-id='{EMOJI_IDS['profile']}'>👤</tg-emoji> <b>{name}</b> {badge}\n"
        f"╚══════════════════╝\n"
        f"🆔 <code>{u['user_id']}</code>\n"
        f"{cur} Баланс: <b>{fmt(u['balance'])}</b>\n"
        f"🌟 Уровень: <b>{u['level']}</b> ({u['exp']}/10) | Престиж: {u['prestige']}\n"
        f"🎮 Игр: {u['total_games']} | <tg-emoji emoji-id='{EMOJI_IDS['checkmark']}'>✅</tg-emoji> {u['total_won']} | <tg-emoji emoji-id='{EMOJI_IDS['cross']}'>❌</tg-emoji> {u['total_lost']}\n"
        f"<tg-emoji emoji-id='{EMOJI_IDS['chart']}'>📊</tg-emoji> Прибыль: <b>{fmt(u['total_profit'])}</b>\n"
        f"🔥 Серия: {u['current_streak']} (рекорд {u['best_streak']})\n"
        f"<tg-emoji emoji-id='{EMOJI_IDS['gift']}'>🎀</tg-emoji> Daily: {u['daily_streak']}\n"
        f"<tg-emoji emoji-id='{EMOJI_IDS['people']}'>👥</tg-emoji> Рефов: {u['total_refs']}\n"
        f"🏆 {ach_str}\n"
    )

@router.message(F.text.lower().in_({"б","профиль","profile","я","👤 профиль"}))
@router.message(Command("profile"), F.chat.type == "private")
async def cmd_profile(m: Message, bot: Bot):
    await ensure_user(m)
    if not await ensure_subscribed(m.from_user.id, bot):
        await subscription_wall(m, bot); return
    await m.answer(await render_profile(m.from_user.id), reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)

@router.message(F.text.lower().in_({"баланс","balance","💎 баланс"}))
async def cmd_balance(m: Message, bot: Bot):
    await ensure_user(m)
    if not await ensure_subscribed(m.from_user.id, bot):
        await subscription_wall(m, bot); return
    u = await get_user(m.from_user.id)
    cur = await get_currency()
    await m.answer(f"{cur} Баланс: <b>{fmt(u['balance'])}</b>", parse_mode=ParseMode.HTML)

@router.message(Command("daily"), F.chat.type == "private")
@router.message(F.text.lower().in_({"бонус","daily","ежедневный","🎀 бонус"}))
async def cmd_daily(m: Message, bot: Bot):
    u = await ensure_user(m)
    if not await ensure_subscribed(m.from_user.id, bot):
        await subscription_wall(m, bot); return
    now = datetime.now(timezone.utc)
    if u["last_daily"]:
        last = datetime.fromisoformat(u["last_daily"])
        diff = (now - last).total_seconds()
        if diff < 86400:
            left = 86400 - diff
            h, rem = divmod(int(left), 3600)
            mins = rem // 60
            await m.answer(f"⌛ Бонус можно забрать через <b>{h}ч {mins}мин</b>", parse_mode=ParseMode.HTML)
            return
        streak = u["daily_streak"] + 1 if diff < 172800 else 1
    else:
        streak = 1
    bonus = random.randint(DAILY_MIN, DAILY_MAX)
    if streak >= 7: bonus = int(bonus * 1.5)
    if u["is_premium"]: bonus = int(bonus * 1.2)
    await update_balance(m.from_user.id, bonus)
    await db_exec("UPDATE users SET last_daily=?, daily_streak=? WHERE user_id=?",
                  (now.isoformat(), streak, m.from_user.id))
    await m.answer(
        f"<tg-emoji emoji-id='{EMOJI_IDS['gift']}'>🎀</tg-emoji> Ежедневный бонус: <b>+{fmt(bonus)} 💎</b>\n"
        f"🔥 Streak: {streak} дн." + (f" <tg-emoji emoji-id='{EMOJI_IDS['celebrate']}'>🌟</tg-emoji> (+50% бонус!)" if streak >= 7 else "")
    )

MEDALS = ["🥇","🥈","🥉"] + ["4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

def top_tab_kb(active: str) -> InlineKeyboardMarkup:
    tabs = [
        ("Баланс",   "top:balance", EMOJI_IDS["coins"]),
        ("Прибыль",  "top:profit", EMOJI_IDS["chart"]),
        ("Рефералы", "top:refs", EMOJI_IDS["people"]),
    ]
    rows = [[
        InlineKeyboardButton(
            text=f"› {label} ‹" if active == cb.split(":")[1] else label,
            callback_data=cb,
            icon_custom_emoji_id=emoji_id
        )
        for label, cb, emoji_id in tabs
    ]]
    rows.append([InlineKeyboardButton(text="Меню", callback_data="menu", icon_custom_emoji_id=EMOJI_IDS["back"])])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def render_top(tab: str) -> str:
    medals = MEDALS
    if tab == "balance":
        rows = await db_fetchall(
            "SELECT user_id,first_name,username,custom_nickname,balance "
            "FROM users ORDER BY balance DESC LIMIT 10"
        )
        lines = [f"<tg-emoji emoji-id='{EMOJI_IDS['coins']}'>💎</tg-emoji> <b>Топ-10 по балансу</b>\n"]
        for i, r in enumerate(rows):
            name = r["custom_nickname"] or r["first_name"] or r["username"] or str(r["user_id"])
            lines.append(f"{medals[i]} {name} — {fmt(r['balance'])} 💎")
    elif tab == "profit":
        rows = await db_fetchall(
            "SELECT user_id,first_name,username,custom_nickname,total_profit "
            "FROM users WHERE total_profit > 0 ORDER BY total_profit DESC LIMIT 10"
        )
        lines = [f"<tg-emoji emoji-id='{EMOJI_IDS['chart']}'>📊</tg-emoji> <b>Топ-10 по прибыли</b>\n"]
        for i, r in enumerate(rows):
            name = r["custom_nickname"] or r["first_name"] or r["username"] or str(r["user_id"])
            lines.append(f"{medals[i]} {name} — +{fmt(r['total_profit'])} 💎")
    elif tab == "refs":
        rows = await db_fetchall(
            "SELECT r.inviter_id, u.first_name, u.username, u.custom_nickname, "
            "COUNT(DISTINCT r.invited_id) AS buyers, "
            "SUM(CASE WHEN r.level=0 THEN r.bonus_given ELSE 0 END) AS stars_bonus, "
            "SUM(r.bonus_given) AS total_bonus "
            "FROM referrals r JOIN users u ON u.user_id=r.inviter_id "
            "WHERE r.level=0 "
            "GROUP BY r.inviter_id "
            "ORDER BY stars_bonus DESC LIMIT 10"
        )
        lines = ["💫 <b>Топ рефералов — бонусы со Stars-покупок</b>\n"]
        if not rows:
            lines.append("Пока никто не получил реферальный бонус за покупку.")
        for i, r in enumerate(rows):
            name = r["custom_nickname"] or r["first_name"] or r["username"] or str(r["inviter_id"])
            buyers = r["buyers"] or 0
            bonus  = r["stars_bonus"] or 0
            total  = r["total_bonus"] or 0
            lines.append(
                f"{medals[i]} {name}\n"
                f"   💳 Покупок рефералов: {buyers}  |  ⭐ Бонус: {fmt(bonus)} 💎"
                f"  |  Всего реф.доход: {fmt(total)} 💎"
            )
    else:
        return ""
    return "\n".join(lines)

@router.message(F.text.lower().in_({"топ","top","лидеры","🏆 топ"}))
async def cmd_top(m: Message, bot: Bot):
    await ensure_user(m)
    if not await ensure_subscribed(m.from_user.id, bot):
        await subscription_wall(m, bot); return
    text = await render_top("balance")
    await m.answer(text, reply_markup=top_tab_kb("balance"))

@router.callback_query(F.data.startswith("top:"))
async def cb_top(c: CallbackQuery):
    tab = c.data.split(":")[1]
    text = await render_top(tab)
    if not text:
        await c.answer(); return
    try:
        await c.message.edit_text(text, reply_markup=top_tab_kb(tab))
    except TelegramBadRequest:
        pass
    await c.answer()

# ============================================================================
# SHOP
# ============================================================================

async def render_shop() -> Tuple[str, InlineKeyboardMarkup]:
    items = await db_fetchall("SELECT * FROM shop_items WHERE is_active=1")
    txt = f'<tg-emoji emoji-id="{EMOJI_IDS["tag"]}">🏷</tg-emoji> <b>Магазин</b>\n\n'
    rows = []
    for it in items:
        txt += f"• <b>{it['name']}</b> — {fmt(it['price'])} 💎\n  {it['description']}\n\n"
        rows.append([InlineKeyboardButton(text=f"Купить {it['name']}", callback_data=f"buy:{it['id']}", icon_custom_emoji_id=EMOJI_IDS["coins"])])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="menu", icon_custom_emoji_id=EMOJI_IDS["back"])])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(F.text.lower().in_({"магазин","shop","🏬 магазин"}))
async def cmd_shop(m: Message, bot: Bot):
    await ensure_user(m)
    if not await ensure_subscribed(m.from_user.id, bot):
        await subscription_wall(m, bot); return
    txt, kb = await render_shop()
    await m.answer(txt, reply_markup=kb)

@router.callback_query(F.data.startswith("buy:stars:"))
async def buy_stars_cb(c: CallbackQuery, bot: Bot):
    try:
        idx = int(c.data.split(":")[2])
        if idx < 0 or idx >= len(STARS_PACKAGES):
            await c.answer("❌ Неверный пакет", show_alert=True); return
        pkg  = STARS_PACKAGES[idx]
        disc = SHOP_DISCOUNT.get(c.from_user.id, {})
        pct  = disc.get("discount", 0.0)
        stars = max(1, int(pkg["stars"] * (1 - pct / 100))) if pct > 0 else pkg["stars"]
        payload = f"diamonds:{pkg['diamonds']}"
        if disc.get("code"):
            payload += f":promo:{disc['code']}"
        await bot.send_invoice(
            chat_id=c.from_user.id,
            title=f"💎 {pkg['label']}",
            description=f"Пополнение баланса на {pkg['label']} в Diamond Mines"
                        + (f" (скидка {int(pct)}%)" if pct else ""),
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=pkg["label"], amount=stars)],
        )
        await c.answer()
        log.info(f"Invoice created for user {c.from_user.id}: {stars} XTR for {pkg['diamonds']} diamonds")
    except Exception as e:
        log.error(f"Invoice error for user {c.from_user.id}: {e}")
        await c.answer(f"❌ Ошибка при создании счета: {str(e)[:50]}", show_alert=True, parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(c: CallbackQuery):
    item_id = int(c.data.split(":")[1])
    it = await db_fetchone("SELECT * FROM shop_items WHERE id=?", (item_id,))
    if not it:
        await c.answer("Товар не найден.", show_alert=True); return
    u = await get_user(c.from_user.id)
    if u["balance"] < it["price"]:
        await c.answer("Недостаточно 💎.", show_alert=True); return
    await update_balance(c.from_user.id, -it["price"], parse_mode=ParseMode.HTML)
    if it["item_type"] == "premium":
        until = datetime.now(timezone.utc) + timedelta(days=30)
        await db_exec("UPDATE users SET is_premium=1,premium_until=? WHERE user_id=?",
                      (until.isoformat(), c.from_user.id))
    await db_exec(
        "INSERT INTO user_inventory(user_id,item_id,quantity,used,purchased_at) VALUES(?,?,?,?,?)",
        (c.from_user.id, item_id, 1, 0, now_ts()),
    )
    await c.answer(f"✅ Куплено: {it['name']}", show_alert=True, parse_mode=ParseMode.HTML)

# ============================================================================
# NICK COMMAND
# ============================================================================

@router.message(Command("nick"), F.chat.type == "private")
async def cmd_nick(m: Message):
    await ensure_user(m)
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.answer(
            "🔰 <b>Смена никнейма</b>\n\n"
            "Использование: <code>/nick Новое имя</code>\n\n"
            "Стоимость: 1 000 💎 или предмет 🔰 из магазина."
        )
        return
    new_nick = parts[1].strip()[:32]
    # Проверяем наличие предмета «Никнейм» в инвентаре
    inv_item = await db_fetchone(
        "SELECT ui.id FROM user_inventory ui "
        "JOIN shop_items si ON si.id=ui.item_id "
        "WHERE ui.user_id=? AND si.item_type='nickname' AND ui.used=0",
        (m.from_user.id,),
    )
    if inv_item:
        await db_exec("UPDATE user_inventory SET used=1 WHERE id=?", (inv_item["id"],))
    else:
        u = await get_user(m.from_user.id)
        if u["balance"] < 1000:
            await m.answer("❌ Недостаточно 💎. Смена ника стоит 1 000 💎\nИли купи предмет 🔰 в магазине.", parse_mode=ParseMode.HTML)
            return
        await update_balance(m.from_user.id, -1000)
    await db_exec("UPDATE users SET custom_nickname=? WHERE user_id=?", (new_nick, m.from_user.id))
    await m.answer(f"✅ Никнейм изменён: <b>{new_nick}</b>", parse_mode=ParseMode.HTML)

# ============================================================================
# REFERRAL
# ============================================================================

@router.message(F.text.lower().in_({"реферал","referral","👥 реферал"}))
async def cmd_ref(m: Message, bot: Bot):
    await ensure_user(m)
    u = await get_user(m.from_user.id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={u['ref_code']}"
    rows = await db_fetchall("SELECT level,COUNT(*) c,SUM(bonus_given) s FROM referrals WHERE inviter_id=? GROUP BY level",
                              (m.from_user.id,))
    summary = "\n".join(f"  L{r['level']}: {r['c']} чел. (+{fmt(r['s'])} 💎)" for r in rows) or "  —"
    await m.answer(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n"
        f"💰 Бонусы: L1={REF_BONUS_L1} | L2={REF_BONUS_L2} | L3={REF_BONUS_L3} 💎\n\n"
        f"📊 Приглашено:\n{summary}"
    )

# ============================================================================
# TRANSFER
# ============================================================================

@router.message(Command("pay"), F.chat.type == "private")
@router.message(F.text.regexp(r"(?i)^перевод(\s+\d+){1,2}$"))
async def cmd_pay(m: Message, bot: Bot):
    await ensure_user(m)
    parts = (m.text or "").split()
    target: Optional[int] = None
    amount: Optional[int] = None
    target_name: Optional[str] = None
    if m.reply_to_message and m.reply_to_message.from_user and len(parts) >= 2:
        ru = m.reply_to_message.from_user
        if ru.is_bot:
            await m.answer("❌ Боту перевод нельзя."); return
        target = ru.id
        target_name = ru.full_name or f"@{ru.username}" if ru.username else str(ru.id)
        try: amount = int(parts[1])
        except Exception:
            await m.answer("❌ Сумма должна быть числом."); return
    else:
        if len(parts) < 3:
            await m.answer("Использование:\n• <code>перевод ID СУММА</code>\n• Или ответом: <code>перевод СУММА</code>")
            return
        try: target = int(parts[1]); amount = int(parts[2])
        except Exception:
            await m.answer("❌ Неверные параметры."); return
    if target == m.from_user.id:
        await m.answer("❌ Нельзя себе."); return
    fee_rate = float(await get_setting("transfer_fee", str(TRANSFER_FEE)))
    min_tr = TRANSFER_MIN
    if amount < min_tr:
        await m.answer(f"❌ Минимум: {min_tr} 💎"); return
    sender = await get_user(m.from_user.id)
    if sender["balance"] < amount:
        await m.answer("❌ Недостаточно 💎."); return
    receiver = await get_user(target)
    if not receiver:
        if m.reply_to_message and m.reply_to_message.from_user:
            ru = m.reply_to_message.from_user
            await db_exec(
                "INSERT OR IGNORE INTO users(user_id,username,first_name,balance,register_date) VALUES(?,?,?,?,?)",
                (target, ru.username or "", ru.first_name or "", 0, now_ts()),
            )
            receiver = await get_user(target)
        if not receiver:
            await m.answer("❌ Получатель не зарегистрирован."); return
    fee = round(amount * fee_rate)
    net = amount - fee
    await update_balance(m.from_user.id, -amount)
    await update_balance(target, net)
    await db_exec("UPDATE jackpot SET amount=amount+? WHERE game_type='slots'", (fee,))
    try:
        await bot.send_message(target, f"💎 Перевод от <code>{m.from_user.id}</code>: <b>+{fmt(net)}</b> 💎")
    except Exception: pass
    who = target_name or f"<code>{target}</code>"
    await m.answer(f"✅ Переведено <b>{fmt(net)}</b> 💎 → {who}\n💸 Комиссия: {fmt(fee)} 💎", parse_mode=ParseMode.HTML)

# ============================================================================
# PROMO
# ============================================================================

@router.message(F.text.regexp(r"(?i)^промо\s+\S+"))
async def cmd_promo(m: Message):
    await ensure_user(m)
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: <code>промо КОД</code>", parse_mode=ParseMode.HTML); return
    code = parts[1].strip().upper()
    pc = await db_fetchone("SELECT * FROM promo_codes WHERE code=? AND is_active=1", (code,))
    if not pc:
        await m.answer("❌ Промокод не найден или недействителен.", parse_mode=ParseMode.HTML); return
    if pc["expires_at"] and datetime.fromisoformat(pc["expires_at"]) < datetime.now(timezone.utc):
        await m.answer("❌ Промокод истёк.", parse_mode=ParseMode.HTML); return
    if pc["uses_left"] <= 0:
        await m.answer("❌ Промокод исчерпан.", parse_mode=ParseMode.HTML); return
    existing = await db_fetchone("SELECT id FROM promo_activations WHERE code=? AND user_id=?",
                                  (code, m.from_user.id))
    if existing:
        await m.answer("❌ Вы уже активировали этот промокод.", parse_mode=ParseMode.HTML); return
    
    # Handle shop discount promo codes (used in stars shop)
    if pc["promo_type"] == "shop_discount":
        pct = float(pc["discount_percent"])
        SHOP_DISCOUNT[m.from_user.id] = {"code": code, "discount": pct}
        await db_exec("UPDATE promo_codes SET uses_left=uses_left-1 WHERE code=?", (code,))
        await db_exec("INSERT INTO promo_activations(code,user_id,activated_at) VALUES(?,?,?)",
                      (code, m.from_user.id, now_ts()))
        await m.answer(
            f"✅ Промокод <code>{code}</code> применён!\n"
            f"🎟 Скидка <b>{int(pct)}%</b> на следующую покупку звёзд в магазине.",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Handle regular diamond reward promo codes
    reward = pc["reward"]
    await update_balance(m.from_user.id, reward)
    await db_exec("UPDATE promo_codes SET uses_left=uses_left-1 WHERE code=?", (code,))
    await db_exec("INSERT INTO promo_activations(code,user_id,activated_at) VALUES(?,?,?)",
                  (code, m.from_user.id, now_ts()))
    await m.answer(f"✅ Промокод активирован! +{fmt(reward)} 💎", parse_mode=ParseMode.HTML)

# ============================================================================
# GAMES — shared dice helper
# ============================================================================

async def play_dice_send(m: Message, bot: Bot, emoji: DiceEmoji,
                          bet: float, decide_fn, game_type: str):
    flood_err = await check_flood(m.from_user.id)
    if flood_err:
        await m.answer(flood_err); return
    ok, err = await can_bet(m.from_user.id, bet)
    if not ok:
        await m.answer(err); return
    await update_balance(m.from_user.id, -bet)
    msg = await m.answer_dice(emoji=emoji)
    val = msg.dice.value
    await asyncio.sleep(2.0)
    mult, label = decide_fn(val)
    pmult = await premium_multiplier(m.from_user.id)
    win = bet * mult * pmult if mult > 0 else 0.0
    if win > 0: await update_balance(m.from_user.id, win)
    profit = win - bet
    res = "win" if profit > 0 else ("lose" if profit < 0 else "draw")
    await record_game(m.from_user.id, game_type, bet, val, mult, res, profit)
    await add_exp(m.from_user.id, 1)
    cur = await get_currency()
    _ricon = "🎊 Выигрыш" if profit > 0 else "💔 Проигрыш"
    await m.answer(
        f"🎲 Выпало: <b>{val}</b> — {label}\n"
        f"{_ricon}: <b>{fmt(profit)}</b> {cur}"
    )
    await check_post_game_achievements(m.from_user.id, bet, game_type, bot)

# ============================================================================
# DICE
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(куб|кости|dice)\s+(\d+|все|вб|all)"))
async def cmd_dice(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "dice", bot): return
    parts = (m.text or "").lower().split()
    bet = await resolve_bet(parts[1], m.from_user.id)
    if bet is None:
        await m.answer(f"Ставка от {MIN_BET}"); return
    mode = parts[2] if len(parts) > 2 else None
    extra = parts[3] if len(parts) > 3 else None

    DICE_ALIAS = {"ч": "чет", "н": "нечет", "б": "больше", "м": "меньше", "чс": "число"}
    if mode is not None:
        mode = DICE_ALIAS.get(mode, mode)

    VALID_DICE_MODES = {"чет","even","нечет","odd","число","number","больше","high","меньше","low"}
    if mode is not None and mode not in VALID_DICE_MODES:
        await m.answer(
            f"❌ Нет такой ставки «{mode}».\n"
            "Доступные: <code>чет (ч) | нечет (н) | больше (б) | меньше (м) | число 1-6</code>\n"
            "Пример: <code>куб 100 ч</code> или <code>куб 100 число 4</code>"
        ); return

    def decide(v: int):
        if mode in ("чет","even"):   return (2.0,"чёт ✅") if v%2==0 else (0.0,"нечёт ❌")
        if mode in ("нечет","odd"):  return (2.0,"нечёт ✅") if v%2==1 else (0.0,"чёт ❌")
        if mode in ("число","number") and extra and extra.isdigit():
            t = int(extra)
            return (6.0,f"число {t} ✅") if 1<=t<=6 and v==t else (0.0,f"не {t}")
        if mode in ("больше","high"): return (1.8,"больше ✅") if v>=4 else (0.0,"меньше ❌")
        if mode in ("меньше","low"):  return (1.8,"меньше ✅") if v<=3 else (0.0,"больше ❌")
        return (2.0,"чёт ✅") if v%2==0 else (0.0,"нечёт ❌")

    await play_dice_send(m, bot, DiceEmoji.DICE, bet, decide, "dice")

# ============================================================================
# FOOTBALL
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(фут|футбол|гол|мимо)\s*\d*"))
async def cmd_foot(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "foot", bot): return
    parts = (m.text or "").lower().split()
    bet = None; side = "гол"
    if parts[0] in ("гол","мимо"):
        side = parts[0]
        if len(parts) > 1: bet = await resolve_bet(parts[1], m.from_user.id)
    else:
        if len(parts) > 1: bet = await resolve_bet(parts[1], m.from_user.id)
        if len(parts) > 2: side = parts[2]
    if bet is None:
        await m.answer("Пример: <code>фут 100 гол</code>"); return
    FOOT_ALIAS = {"г": "гол", "м": "мимо"}
    side = FOOT_ALIAS.get(side, side)
    if side not in ("гол","мимо","goal","miss"):
        await m.answer(
            f"❌ Нет такой ставки «{side}».\n"
            "Доступные: <code>гол (г) | мимо (м)</code>\n"
            "Пример: <code>фут 100 г</code>"
        ); return

    def decide(v: int):
        goal = v in (3,4,5)
        if side in ("гол","goal"): return (1.3,"Гол! ⚽") if goal else (0.0,"Мимо")
        return (2.0,"Сейв 🧤") if not goal else (0.0,"Гол ⚽")

    await play_dice_send(m, bot, DiceEmoji.FOOTBALL, bet, decide, "football")

# ============================================================================
# DARTS
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(дартс|дротик)\s+(\d+|все|вб|all)"))
async def cmd_darts(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "darts", bot): return
    parts = (m.text or "").lower().split()
    bet = await resolve_bet(parts[1], m.from_user.id)
    if bet is None:
        await m.answer(f"Ставка от {MIN_BET}"); return
    target = parts[2] if len(parts) > 2 else "центр"

    DARTS_ALIAS = {"к": "красное", "ч": "белое"}
    target = DARTS_ALIAS.get(target, target)

    VALID_DARTS = {"центр","красное","белое","мимо"}
    if target not in VALID_DARTS:
        await m.answer(
            f"❌ Нет такой ставки «{target}».\n"
            "Доступные: <code>центр | красное (к) | белое (ч) | мимо</code>\n"
            "Пример: <code>дартс 100 центр</code>"
        ); return

    def decide(v: int):
        # 1=мимо, 2=красн.внешн, 3=бел.внешн, 4=красн.внутр, 5=бел.внутр, 6=яблочко
        if target == "центр":   return (6.0,"Яблочко 🎯") if v==6 else (0.0,"мимо центра")
        if target == "красное": return (2.0,"Красное ✅") if v in (2,4) else (0.0,"не красное")
        if target == "белое":   return (2.0,"Белое ✅") if v in (3,5) else (0.0,"не белое")
        if target == "мимо":    return (3.5,"Мимо ✅") if v==1 else (0.0,"не мимо")
        return (0.0,"неизвестный режим")

    await play_dice_send(m, bot, DiceEmoji.DART, bet, decide, "darts")

# ============================================================================
# BOWLING
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(боул|кегли|страйк)\s*\d*"))
async def cmd_bowl(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "bowl", bot): return
    parts = (m.text or "").lower().split()
    side = "страйк"; bet = None
    if parts[0] == "страйк":
        if len(parts) > 1: bet = await resolve_bet(parts[1], m.from_user.id)
    else:
        if len(parts) > 1: bet = await resolve_bet(parts[1], m.from_user.id)
        if len(parts) > 2: side = parts[2]
    if bet is None:
        await m.answer("Пример: <code>боул 100 страйк</code>"); return
    BOWL_ALIAS = {"с": "страйк", "н": "ничего"}
    side = BOWL_ALIAS.get(side, side)
    if side not in ("страйк","ничего","strike","nothing"):
        await m.answer(
            f"❌ Нет такой ставки «{side}».\n"
            "Доступные: <code>страйк (с) | ничего (н)</code>\n"
            "Пример: <code>боул 100 с</code>"
        ); return

    def decide(v: int):
        if side in ("страйк","strike"): return (6.0,"Страйк! 🎳") if v==6 else ((1.5,"Частично") if 2<=v<=5 else (0.0,"Пусто"))
        return (6.0,"0 кеглей ✅") if v==1 else (0.0,"что-то сбил")

    await play_dice_send(m, bot, DiceEmoji.BOWLING, bet, decide, "bowling")

# ============================================================================
# SLOTS (pure random — 3 combos)
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(слот|слоты|slot)\s+(\d+|все|вб|all)"))
async def cmd_slots(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "slot", bot): return
    parts = (m.text or "").lower().split()
    bet = await resolve_bet(parts[1], m.from_user.id)
    if bet is None:
        await m.answer(f"Ставка от {MIN_BET}"); return
    flood_err = await check_flood(m.from_user.id)
    if flood_err:
        await m.answer(flood_err); return
    ok, err = await can_bet(m.from_user.id, bet)
    if not ok:
        await m.answer(err); return

    await update_balance(m.from_user.id, -bet)
    contrib = bet * SLOTS_JACKPOT_RATE
    await db_exec("UPDATE jackpot SET amount=amount+? WHERE game_type='slots'", (contrib,))

    # Анимация спина
    spin_frames = [
        "🎰 | ❓ | ❓ | ❓ |", "🎰 | 7️⃣ | ❓ | ❓ |",
        "🎰 | 7️⃣ | 🍒 | ❓ |", "🎰 | … | … | … |",
    ]
    msg = await m.answer(spin_frames[0])
    for frame in spin_frames[1:]:
        await asyncio.sleep(0.55)
        try: await msg.edit_text(frame)
        except TelegramBadRequest: pass

    syms, mult, label = spin_slots()
    display = slot_display(syms)
    pmult = await premium_multiplier(m.from_user.id)
    jackpot_bonus = 0.0
    win = 0.0
    if mult > 0:
        win = bet * mult * pmult
        if syms == ["7","7","7"]:
            jp = await db_fetchone("SELECT amount FROM jackpot WHERE game_type='slots'")
            if jp and jp["amount"] > 0:
                jackpot_bonus = jp["amount"]
                win += jackpot_bonus
                await db_exec("UPDATE jackpot SET amount=0 WHERE game_type='slots'")
                await db_exec(
                    "INSERT INTO jackpot_history(user_id,amount,game_type,timestamp) VALUES(?,?,?,?)",
                    (m.from_user.id, win, "slots", now_ts()),
                )
                await grant_achievement(m.from_user.id, "jackpot", bot)
        await update_balance(m.from_user.id, win)

    profit = win - bet
    res = "win" if profit > 0 else "lose"
    await record_game(m.from_user.id, "slots", bet, 0, mult, res, profit)
    await add_exp(m.from_user.id, 1)
    cur = await get_currency()
    extra = f"\n🏆 Джекпот: +{fmt(jackpot_bonus)} {cur}" if jackpot_bonus else ""
    _slot_icon = "🎊 " if profit > 0 else "💔 "
    _slot_res = "✨ Выигрыш" if profit > 0 else "Проигрыш"
    try:
        await msg.edit_text(
            f"🎰 {display}\n"
            f"{_slot_icon}{label}\n"
            f"Ставка: {fmt(bet)} | Множитель: ×{mult}{extra}\n"
            f"{_slot_res}: <b>{fmt(profit)}</b> {cur}"
        )
    except TelegramBadRequest:
        pass
    await check_post_game_achievements(m.from_user.id, bet, "slots", bot)

# ============================================================================
# ROULETTE
# ============================================================================

ROULETTE_STICKERS: Dict[int, str] = {}

@router.message(F.text.regexp(r"(?i)^(рул|рулетка|roulette)\s+(\d+|все|вб|all)"))
async def cmd_roulette(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "roul", bot): return
    parts = (m.text or "").split(maxsplit=2)
    bet = await resolve_bet(parts[1], m.from_user.id)
    if bet is None:
        await m.answer("Пример: <code>рул 100 17</code>"); return
    spec = parts[2] if len(parts) > 2 else "красное"
    parsed = parse_roulette_bet(spec)
    if not parsed:
        await m.answer("❓ Пример: <code>рул 100 красное</code> или <code>рул 100 10-15</code>"); return
    win_nums, mult, label = parsed
    flood_err = await check_flood(m.from_user.id)
    if flood_err:
        await m.answer(flood_err); return
    ok, err = await can_bet(m.from_user.id, bet)
    if not ok:
        await m.answer(err); return
    await update_balance(m.from_user.id, -bet)
    msg = await m.answer("🎡 Рулетка крутится...")
    for _ in range(5):
        fake = random.randint(0, 36)
        try: await msg.edit_text(f"🎡 <b>{fake:02d}</b> {roul_color_emoji(fake)} ...")
        except TelegramBadRequest: pass
        await asyncio.sleep(0.45)
    result = random.randint(0, 36)
    is_win = result in win_nums
    pmult = await premium_multiplier(m.from_user.id)
    win = bet * mult * pmult if is_win else 0.0
    if win > 0: await update_balance(m.from_user.id, win)
    profit = win - bet
    res = "win" if profit > 0 else "lose"
    await record_game(m.from_user.id, "roulette", bet, result, mult, res, profit)
    await add_exp(m.from_user.id, 1)
    cur = await get_currency()
    head = (
        f"🎡 <b>Выпало: {result}</b> {roul_color_emoji(result)} ({roul_color_name(result)})\n"
        f"🎯 Ставка: <b>{label}</b> | ×{mult}\n"
    )
    head += f"🎊 <b>Победа! +{fmt(profit)}</b> {cur}" if is_win else f"💔 Проигрыш: {fmt(profit)} {cur}"
    try: await msg.edit_text(head)
    except Exception: await m.answer(head)
    stk = ROULETTE_STICKERS.get(result)
    if stk:
        try: await m.answer_sticker(stk)
        except Exception: pass
    await check_post_game_achievements(m.from_user.id, bet, "roulette", bot)

# ============================================================================
# COIN FLIP
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(монетка|coin|орёл|орел|решка)\s*\S*\s*(\d+|все|вб|all)"))
async def cmd_coin(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "coin", bot): return
    parts = (m.text or "").lower().replace("ё","е").split()
    side = "орел"; bet = None
    if parts[0] in ("орел","решка"):
        side = parts[0]
        if len(parts) > 1: bet = await resolve_bet(parts[1], m.from_user.id)
    else:
        if len(parts) > 1: bet = await resolve_bet(parts[1], m.from_user.id)
        if len(parts) > 2: side = parts[2]
    if bet is None:
        await m.answer("Пример: <code>монетка 100 орел</code>"); return
    COIN_ALIAS = {"о": "орел", "р": "решка"}
    side = COIN_ALIAS.get(side, side)
    if side not in ("орел","решка","heads","tails"):
        await m.answer(
            f"❌ Нет такой ставки «{side}».\n"
            "Доступные: <code>орел (о) | решка (р)</code>\n"
            "Пример: <code>монетка 100 о</code>"
        ); return

    def decide(v: int):
        heads = v in (1,2,3)
        user_heads = side in ("орел","heads")
        return (2.0,"Орёл 🦅 ✅") if (heads and user_heads) else (
            (2.0,"Решка ✅") if (not heads and not user_heads) else (0.0,"Не угадал"))

    await play_dice_send(m, bot, DiceEmoji.DICE, bet, decide, "coin")

# ============================================================================
# HORSE RACING
# ============================================================================

HORSE_NAMES = ["💨 Вихрь","🔥 Огонь","⚡ Молния","🌊 Волна","🌿 Ветер","🌙 Ночь"]
HORSE_ODDS  = [4.0, 3.5, 3.0, 2.5, 2.0, 1.5]

@router.message(F.text.regexp(r"(?i)^(скачки|лошади|horse)\s+(\d+|все|вб|all)\s+\d+"))
async def cmd_horse(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "horse", bot): return
    parts = (m.text or "").lower().split()
    bet = await resolve_bet(parts[1], m.from_user.id)
    try: choice = int(parts[2])
    except Exception:
        await m.answer("Пример: <code>скачки 100 3</code>"); return
    if not 1 <= choice <= 6:
        await m.answer("Лошадь от 1 до 6."); return
    if bet is None:
        await m.answer(f"Ставка от {MIN_BET}"); return
    flood_err = await check_flood(m.from_user.id)
    if flood_err:
        await m.answer(flood_err); return
    ok, err = await can_bet(m.from_user.id, bet)
    if not ok:
        await m.answer(err); return
    await update_balance(m.from_user.id, -bet)
    msg = await m.answer("🏇 Скачки начались!")
    weights = [1.0/o for o in HORSE_ODDS]
    winner = random.choices(range(1,7), weights=weights)[0]
    for i in range(5):
        lead = random.randint(1,6)
        try: await msg.edit_text(f"🏇 Скачка... Впереди лошадь №{lead}!")
        except TelegramBadRequest: pass
        await asyncio.sleep(0.5)
    win = choice == winner
    mult = HORSE_ODDS[winner-1] if win else 0.0
    pmult = await premium_multiplier(m.from_user.id)
    gained = bet * mult * pmult if win else 0.0
    if gained > 0: await update_balance(m.from_user.id, gained)
    profit = gained - bet
    res = "win" if profit > 0 else "lose"
    await record_game(m.from_user.id, "horse", bet, winner, mult, res, profit)
    await add_exp(m.from_user.id, 1)
    cur = await get_currency()
    _horse_icon = "🎊" if win else "💔"
    _horse_res = "✨ Выигрыш" if win else "Проигрыш"
    try:
        await msg.edit_text(
            f"🏆 Победила: {HORSE_NAMES[winner-1]} (№{winner})\n"
            f"{_horse_icon} {_horse_res}: <b>{fmt(profit)}</b> {cur}"
        )
    except TelegramBadRequest: pass
    await check_post_game_achievements(m.from_user.id, bet, "horse", bot)

# ============================================================================
# MINES
# ============================================================================

def mines_kb_for(user_id: int) -> InlineKeyboardMarkup:
    return mines_board_kb(user_id)

@router.message(F.text.regexp(r"(?i)^(мины|сапер|mines)\s+(\d+|все|вб|all)\s+\d+"))
async def cmd_mines(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "mines", bot): return
    # Проверяем незавершённую игру
    if m.from_user.id in MINES_SESSIONS:
        sess = MINES_SESSIONS[m.from_user.id]
        link_text = "ссылки нет"
        try:
            link_text = f"https://t.me/c/{str(sess.get('chat_id',''))[4:]}/{sess.get('message_id','')}"
        except Exception: pass
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить игру и вернуть ставку", callback_data="mines:cancel_old"),
        ]])
        await m.answer(
            f"⚠️ У вас есть незавершённая игра в Мины!\n"
            f"Ставка: {fmt(sess['bet'])} 💎\n"
            f"Нажмите кнопку ниже, чтобы отменить её.",
            reply_markup=kb,
        )
        return
    parts = (m.text or "").lower().split()
    bet = await resolve_bet(parts[1], m.from_user.id)
    try: mines_count = int(parts[2])
    except Exception:
        await m.answer("Пример: <code>мины 100 3</code> (мин: 1–24)"); return
    if not (1 <= mines_count <= 24):
        await m.answer("❌ Количество мин: от 1 до 24"); return
    if bet is None:
        await m.answer(f"Ставка от {MIN_BET}"); return
    flood_err = await check_flood(m.from_user.id)
    if flood_err:
        await m.answer(flood_err); return
    ok, err = await can_bet(m.from_user.id, bet)
    if not ok:
        await m.answer(err); return
    await update_balance(m.from_user.id, -bet)
    mine_positions = set(random.sample(range(25), mines_count))
    first_mult = mines_mult(1, mines_count)
    MINES_SESSIONS[m.from_user.id] = {
        "mines": mine_positions,
        "revealed": set(),
        "bet": bet,
        "mines_count": mines_count,
        "show_mines": False,
        "chat_id": m.chat.id,
    }
    safe_cells = 25 - mines_count
    msg = await m.answer(
        f"💣 <b>Мины</b> | Ставка: {fmt(bet)} 💎 | Мин: {mines_count} | Клеток: {safe_cells}\n"
        f"💎 1-й клик: ×{first_mult} | Открывай клетки!\n"
        "Попадёшь на мину — потеряешь всё.",
        reply_markup=mines_kb_for(m.from_user.id),
    )
    MINES_SESSIONS[m.from_user.id]["message_id"] = msg.message_id
    await save_mines_session(m.from_user.id, msg.message_id, m.chat.id)

@router.callback_query(F.data == "mines:cancel_old")
async def mines_cancel_old(c: CallbackQuery):
    await cancel_mines_session(c.from_user.id)
    await safe_edit(c, "✅ Предыдущая игра отменена, ставка возвращена.")
    await c.answer()

@router.callback_query(F.data.startswith("mines:"))
async def mines_callback(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    sess = MINES_SESSIONS.get(uid)
    if not sess:
        await c.answer("Игра не активна.", show_alert=True); return
    action = c.data.split(":", 2)[1] if ":" in c.data else c.data

    if action == "cancel":
        await cancel_mines_session(uid)
        await safe_edit(c, "❌ Игра отменена. Ставка возвращена.")
        await c.answer(); return

    if action == "cashout":
        revealed = len(sess["revealed"])
        if revealed == 0:
            await c.answer("Сначала открой хотя бы одну клетку!", show_alert=True); return
        mult = mines_mult(revealed, sess["mines_count"])
        pmult = await premium_multiplier(uid)
        win = sess["bet"] * mult * pmult
        await update_balance(uid, win)
        profit = win - sess["bet"]
        await record_game(uid, "mines", sess["bet"], revealed, mult, "win", profit)
        await add_exp(uid, 2)
        del MINES_SESSIONS[uid]
        await db_exec("DELETE FROM pending_games WHERE user_id=?", (uid,))
        cur = await get_currency()
        try:
            await c.message.edit_text(
                f"💰 Забрал выигрыш!\n"
                f"Открыто: {revealed} клеток | Множитель: ×{mult}\n"
                f"🎊 ✨ +{fmt(profit)} {cur}"
            )
        except TelegramBadRequest: pass
        await c.answer()
        await check_post_game_achievements(uid, sess["bet"], "mines", bot)
        return

    if action == "cell":
        parts = c.data.split(":")
        if len(parts) < 3:
            await c.answer(); return
        idx = int(parts[2])
        if idx in sess["revealed"]:
            await c.answer("Уже открыто!"); return
        if idx in sess["mines"]:
            # Boom!
            sess["show_mines"] = True
            await record_game(uid, "mines", sess["bet"], idx, 0.0, "lose", -sess["bet"])
            await add_exp(uid, 1)
            del MINES_SESSIONS[uid]
            await db_exec("DELETE FROM pending_games WHERE user_id=?", (uid,))
            try:
                await c.message.edit_text(
                    f"💥 БУМ! Вы попали на мину!\n💔 Потеряно: {fmt(sess['bet'])} 💎",
                    reply_markup=None,
                )
            except TelegramBadRequest: pass
            await c.answer("💥 МИНА!", show_alert=True)
            return
        sess["revealed"].add(idx)
        # Обновляем сессию в БД
        await save_mines_session(uid, sess.get("message_id", 0), sess.get("chat_id", 0))
        current_mult = mines_mult(len(sess["revealed"]), sess["mines_count"])
        next_mult = mines_mult(len(sess["revealed"]) + 1, sess["mines_count"])
        safe_left = (25 - sess["mines_count"]) - len(sess["revealed"])
        try:
            await c.message.edit_text(
                f"💣 <b>Мины</b> | Ставка: {fmt(sess['bet'])} 💎 | Мин: {sess['mines_count']}\n"
                f"✅ Открыто: {len(sess['revealed'])} | 💰 Сейчас: ×{current_mult}\n"
                f"➡️ След. клик: ×{next_mult} | Осталось клеток: {safe_left}",
                reply_markup=mines_kb_for(uid),
            )
        except TelegramBadRequest: pass
        await c.answer(f"✅ Безопасно! ×{current_mult}", parse_mode=ParseMode.HTML)

    if action == "done":
        await c.answer()

# ============================================================================
# BLACKJACK
# ============================================================================

@router.message(F.text.regexp(r"(?i)^(блэкджек|блекджек|blackjack|21)\s+(\d+|все|вб|all)"))
async def cmd_bj(m: Message, bot: Bot):
    await ensure_user(m)
    if not await guard_game(m, "bj", bot): return
    parts = (m.text or "").split()
    bet = await resolve_bet(parts[1], m.from_user.id)
    if bet is None:
        await m.answer(f"Ставка от {MIN_BET}"); return
    flood_err = await check_flood(m.from_user.id)
    if flood_err:
        await m.answer(flood_err); return
    ok, err = await can_bet(m.from_user.id, bet)
    if not ok:
        await m.answer(err); return
    await update_balance(m.from_user.id, -bet)
    deck = bj_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    BJ_SESSIONS[m.from_user.id] = {
        "bet": bet, "player": player, "dealer": dealer, "deck": deck
    }
    sess = BJ_SESSIONS[m.from_user.id]
    if bj_total(player) == 21:
        pmult = await premium_multiplier(m.from_user.id)
        win = bet * 2.5 * pmult
        await update_balance(m.from_user.id, win)
        profit = win - bet
        await record_game(m.from_user.id, "bj", bet, 21, 2.5, "win", profit)
        del BJ_SESSIONS[m.from_user.id]
        await m.answer(f"🃏 Блэкджек! {player}\n🎊 ✨ Выигрыш ×2.5: +{fmt(profit)} 💎", parse_mode=ParseMode.HTML)
        return
    await m.answer(bj_msg(sess), reply_markup=bj_kb(m.from_user.id))

async def bj_finish(uid: int, c: CallbackQuery, bot: Bot):
    sess = BJ_SESSIONS.pop(uid, None)
    if not sess: return
    dealer = sess["dealer"]
    deck = sess["deck"]
    while bj_total(dealer) < 17:
        dealer.append(deck.pop())
    pt = bj_total(sess["player"])
    dt = bj_total(dealer)
    if pt > 21:       mult, res, label = 0.0, "lose", "Перебор у игрока 💔"
    elif dt > 21:     mult, res, label = 2.0, "win", "Перебор у дилера 🎊"
    elif pt > dt:     mult, res, label = 2.0, "win", "Вы победили! 🎊 ✨"
    elif pt == dt:    mult, res, label = 1.0, "draw", "Ничья 🤝"
    else:             mult, res, label = 0.0, "lose", "Дилер победил 💔"
    pmult = await premium_multiplier(uid)
    win = sess["bet"] * mult * pmult
    if win > 0: await update_balance(uid, win)
    profit = win - sess["bet"]
    await record_game(uid, "bj", sess["bet"], pt, mult, res, profit)
    await add_exp(uid, 1)
    cur = await get_currency()
    text = (
        f"🃏 Блэкджек\n"
        f"Вы: {sess['player']} = {pt}\n"
        f"Дилер: {dealer} = {dt}\n"
        f"{label}\n{'Выигрыш' if profit>0 else 'Проигрыш'}: <b>{fmt(profit)}</b> {cur}"
    )
    try: await c.message.edit_text(text)
    except TelegramBadRequest: pass
    await check_post_game_achievements(uid, sess["bet"], "bj", bot)

@router.callback_query(F.data == "bj:hit")
async def bj_hit(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    sess = BJ_SESSIONS.get(uid)
    if not sess:
        await c.answer("Игра не активна.", show_alert=True); return
    sess["player"].append(sess["deck"].pop())
    if bj_total(sess["player"]) > 21:
        await bj_finish(uid, c, bot)
    else:
        try: await c.message.edit_text(bj_msg(sess), reply_markup=bj_kb(uid))
        except TelegramBadRequest: pass
    await c.answer()

@router.callback_query(F.data == "bj:stand")
async def bj_stand(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    if uid not in BJ_SESSIONS:
        await c.answer("Игра не активна.", show_alert=True); return
    await bj_finish(uid, c, bot)
    await c.answer()

# ============================================================================
# GAMES MENU (callback)
# ============================================================================

@router.message(F.text.lower().in_({"игры","🎮 игры"}))
async def cmd_games(m: Message):
    await ensure_user(m)
    await m.answer("🎮 Выбери игру:", reply_markup=await games_menu_kb(), parse_mode=ParseMode.HTML)

GAME_HINTS = {
    "dice":  "🎲 <b>Кости</b>\n<code>куб 100 чет|нечет|число 6</code>",
    "foot":  "⚽ <b>Футбол</b>\n<code>фут 100 гол|мимо</code>",
    "darts": "🎯 <b>Дартс</b>\n<code>дартс 100 центр|красное|белое|мимо</code>",
    "bowl":  "🎳 <b>Боулинг</b>\n<code>боул 100 страйк|ничего</code>",
    "slot":  "🎰 <b>Слоты</b>\n<code>слот 100</code>\n777=×100 | BAR=×20 | CHERRY=×5",
    "roul":  "🎡 <b>Рулетка</b>\n<code>рул 100 17|красное|10-15|чет|1д</code>",
    "coin":  "🪙 <b>Монетка</b>\n<code>монетка 100 орел|решка</code>",
    "horse": "🏇 <b>Скачки</b>\n<code>скачки 100 3</code>",
    "mines": "💣 <b>Мины</b>\n<code>мины 100 3</code>\nМин: 1,3,5,10,15,20,24",
    "bj":    "🃏 <b>Блэкджек</b>\n<code>блэкджек 100</code>",
}

@router.callback_query(F.data.startswith("g:"))
async def cb_game(c: CallbackQuery):
    code = c.data.split(":")[1]
    txt = GAME_HINTS.get(code, "Игра")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅ К играм", callback_data="games")
    ]])
    await c.message.answer(txt, reply_markup=kb)
    await c.answer()

# ============================================================================
# MAIN MENU CALLBACKS
# ============================================================================

@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await safe_edit(c, "🎰 Главное меню:", main_menu_kb())
    await c.answer()

@router.callback_query(F.data == "buy_menu")
async def cb_buy_menu(c: CallbackQuery):
    await ensure_user(c)
    uid = c.from_user.id
    try:
        await c.message.edit_text(shop_text(uid), reply_markup=stars_shop_kb(uid))
    except TelegramBadRequest:
        await c.message.answer(shop_text(uid), reply_markup=stars_shop_kb(uid))
    await c.answer()

@router.callback_query(F.data == "games")
async def cb_games(c: CallbackQuery):
    await safe_edit(c, "🎮 Выбери игру:", await games_menu_kb())
    await c.answer()

@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    await safe_edit(c, await render_profile(c.from_user.id), main_menu_kb())
    await c.answer()

@router.callback_query(F.data == "balance")
async def cb_balance(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    cur = await get_currency()
    await c.answer(f"{cur} Баланс: {fmt(u['balance'])}", show_alert=True, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "daily")
async def cb_daily(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    if not u:
        await c.answer("Сначала /start", show_alert=True); return
    now = datetime.now(timezone.utc)
    if u["last_daily"]:
        diff = (now - datetime.fromisoformat(u["last_daily"])).total_seconds()
        if diff < 86400:
            left = 86400 - diff
            h,rem = divmod(int(left),3600); mins = rem//60
            await c.answer(f"⏰ Через {h}ч {mins}мин", show_alert=True); return
        streak = u["daily_streak"]+1 if diff < 172800 else 1
    else:
        streak = 1
    bonus = random.randint(DAILY_MIN, DAILY_MAX)
    if streak >= 7: bonus = int(bonus * 1.5)
    if u["is_premium"]: bonus = int(bonus * 1.2)
    await update_balance(c.from_user.id, bonus)
    await db_exec("UPDATE users SET last_daily=?,daily_streak=? WHERE user_id=?",
                  (now.isoformat(), streak, c.from_user.id))
    await c.answer(f"🎀 +{fmt(bonus)} 💎 (streak {streak})", show_alert=True, parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "top")
async def cb_top(c: CallbackQuery):
    rows = await db_fetchall(
        "SELECT user_id,first_name,custom_nickname,balance FROM users ORDER BY balance DESC LIMIT 10"
    )
    lines = ["🏆 <b>Топ по балансу</b>\n"]
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i,r in enumerate(rows):
        n = r["custom_nickname"] or r["first_name"] or str(r["user_id"])
        lines.append(f"{medals[i]} {n} — {fmt(r['balance'])} 💎")
    await safe_edit(c, "\n".join(lines), main_menu_kb())
    await c.answer()

@router.callback_query(F.data == "shop")
async def cb_shop(c: CallbackQuery):
    txt, kb = await render_shop()
    await safe_edit(c, txt, kb)
    await c.answer()

@router.callback_query(F.data == "ref")
async def cb_ref(c: CallbackQuery, bot: Bot):
    u = await get_user(c.from_user.id)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={u['ref_code']}"
    txt = (f"👥 <b>Реферал</b>\n🔗 <code>{link}</code>\n"
           f"L1: {REF_BONUS_L1} | L2: {REF_BONUS_L2} | L3: {REF_BONUS_L3} 💎")
    await safe_edit(c, txt, main_menu_kb())
    await c.answer()

@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    txt = (
        "<b>📖 Команды</b>\n\n"
        "<b>🎲 Игры:</b>\n"
        "• <code>куб 100 чет</code> • <code>фут 100 гол</code>\n"
        "• <code>дартс 100 центр</code> • <code>боул 100 страйк</code>\n"
        "• <code>слот 100</code> • <code>рул 100 красное</code>\n"
        "• <code>монетка 100 орел</code> • <code>скачки 100 3</code>\n"
        "• <code>мины 100 3</code> • <code>блэкджек 100</code>\n\n"
        "<b>💸 Перевод:</b> <code>перевод ID СУММА</code> или ответом\n"
        "<b>🎟 Промо:</b> <code>промо КОД</code>"
    )
    await safe_edit(c, txt, main_menu_kb())
    await c.answer()

# ============================================================================
# ADMIN PANEL
# ============================================================================

@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return
    maint = await get_setting("maintenance","0")
    status = "🔧 ТО включено!" if maint=="1" else ""
    await m.answer(f"👑 <b>Админ-панель</b> {status}", reply_markup=admin_main_kb(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "adm:back")
async def adm_back(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_PENDING.pop(c.from_user.id, None)
    await safe_edit(c, "👑 <b>Админ-панель</b>", admin_main_kb())
    await c.answer()

# --- 1. DASHBOARD ---

@router.callback_query(F.data == "adm:dash")
async def adm_dash(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    total = await db_fetchone("SELECT COUNT(*) c FROM users")
    now = datetime.now(timezone.utc)
    new24 = await db_fetchone("SELECT COUNT(*) c FROM users WHERE register_date>=?",
                               ((now-timedelta(hours=24)).isoformat(),))
    active24 = await db_fetchone("SELECT COUNT(*) c FROM users WHERE last_game>=?",
                                  ((now-timedelta(hours=24)).isoformat(),))
    total_bal = await db_fetchone("SELECT SUM(balance) s FROM users")
    games_today = await db_fetchone("SELECT COUNT(*) c FROM game_stats WHERE timestamp>=?",
                                     ((now.replace(hour=0,minute=0,second=0)).isoformat(),))
    profit_today = await db_fetchone("SELECT SUM(-profit) s FROM game_stats WHERE timestamp>=?",
                                      ((now.replace(hour=0,minute=0,second=0)).isoformat(),))
    profit_week = await db_fetchone("SELECT SUM(-profit) s FROM game_stats WHERE timestamp>=?",
                                     ((now-timedelta(days=7)).isoformat(),))
    profit_month = await db_fetchone("SELECT SUM(-profit) s FROM game_stats WHERE timestamp>=?",
                                      ((now-timedelta(days=30)).isoformat(),))
    top_game = await db_fetchone(
        "SELECT game_type, COUNT(*) c FROM game_stats GROUP BY game_type ORDER BY c DESC LIMIT 1"
    )
    top_players = await db_fetchall(
        "SELECT u.first_name,u.custom_nickname,COUNT(*) c FROM game_stats gs "
        "JOIN users u ON u.user_id=gs.user_id "
        f"WHERE gs.timestamp>='{(now-timedelta(hours=24)).isoformat()}' "
        "GROUP BY gs.user_id ORDER BY c DESC LIMIT 5"
    )

    def bar(val, max_val=100, width=10):
        if not max_val: return "░"*width
        filled = int(min(val/max_val, 1.0) * width)
        return "█"*filled + "░"*(width-filled)

    pt_today = profit_today["s"] or 0
    pt_week  = profit_week["s"] or 0
    pt_month = profit_month["s"] or 0
    max_p = max(abs(pt_today), abs(pt_week), abs(pt_month), 1)

    lines = [
        "📊 <b>Дашборд</b>\n",
        f"👥 Пользователей: {total['c']} (+{new24['c']} за 24ч)",
        f"⚡ Активных сегодня: {active24['c']}",
        f"💎 Общий баланс: {fmt(total_bal['s'] or 0)}",
        f"🎮 Игр сегодня: {games_today['c']}",
        f"🎰 Популярна: {top_game['game_type'] if top_game else '—'}",
        "",
        "📈 Прибыль бота:",
        f"  Сегодня: {bar(abs(pt_today),max_p)} {fmt(pt_today)} 💎",
        f"  Неделя:  {bar(abs(pt_week), max_p)} {fmt(pt_week)} 💎",
        f"  Месяц:   {bar(abs(pt_month),max_p)} {fmt(pt_month)} 💎",
        "",
        "🏆 Топ-5 за 24ч:",
    ]
    for p in top_players:
        n = p["custom_nickname"] or p["first_name"] or "—"
        lines.append(f"  {n}: {p['c']} игр")

    await safe_edit(c, "\n".join(lines), back_to_admin())
    await c.answer()

# --- 2. USERS ---

@router.callback_query(F.data == "adm:users")
async def adm_users(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_PENDING[c.from_user.id] = {"action": "user_search"}
    await safe_edit(c,
        "👥 <b>Управление пользователями</b>\n\nВведи ID, @username или никнейм:",
        back_to_admin())
    await c.answer()

async def render_user_admin(uid: int) -> Tuple[str, InlineKeyboardMarkup]:
    u = await get_user(uid)
    if not u: return "Пользователь не найден", back_to_admin()
    name = u["custom_nickname"] or u["first_name"] or str(u["user_id"])
    prem = "✅" if u["is_premium"] else "❌"
    ban = "🚫 Заблокирован" if u["is_blocked"] else "✅ Активен"
    text = (
        f"👤 <b>{name}</b> (<code>{uid}</code>)\n"
        f"💎 Баланс: {fmt(u['balance'])}\n"
        f"🌟 Уровень {u['level']} | Престиж {u['prestige']}\n"
        f"🎮 Игр: {u['total_games']} | 📊 {fmt(u['total_profit'])}\n"
        f"💎 Премиум: {prem}\n"
        f"Статус: {ban}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Баланс", callback_data=f"adm:u:bal+:{uid}"),
         InlineKeyboardButton(text="➖ Баланс", callback_data=f"adm:u:bal-:{uid}")],
        [InlineKeyboardButton(text="💫 Премиум", callback_data=f"adm:u:prem:{uid}"),
         InlineKeyboardButton(text="📋 История", callback_data=f"adm:u:hist:{uid}")],
        [InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data=f"adm:u:ban:{uid}")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")],
    ])
    return text, kb

@router.callback_query(F.data.startswith("adm:u:"))
async def adm_user_action(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    parts = c.data.split(":")
    act = parts[2]; uid = int(parts[3]) if len(parts) > 3 else 0

    if act == "bal+":
        ADMIN_PENDING[c.from_user.id] = {"action": "bal_plus", "uid": uid}
        await safe_edit(c, f"Введи сумму для начисления пользователю {uid}:", back_to_admin())
    elif act == "bal-":
        ADMIN_PENDING[c.from_user.id] = {"action": "bal_minus", "uid": uid}
        await safe_edit(c, f"Введи сумму для снятия у пользователя {uid}:", back_to_admin())
    elif act == "prem":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1 день",  callback_data=f"adm:u:prem_set:{uid}:1"),
             InlineKeyboardButton(text="7 дней",  callback_data=f"adm:u:prem_set:{uid}:7"),
             InlineKeyboardButton(text="30 дней", callback_data=f"adm:u:prem_set:{uid}:30")],
            [InlineKeyboardButton(text="Снять", callback_data=f"adm:u:prem_set:{uid}:0")],
            [InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")],
        ])
        await safe_edit(c, f"Выбери срок премиума для {uid}:", kb)
    elif act == "prem_set":
        days = int(parts[4]) if len(parts) > 4 else 0
        if days == 0:
            await db_exec("UPDATE users SET is_premium=0,premium_until=NULL WHERE user_id=?", (uid,))
            await add_admin_log(c.from_user.id, "remove_premium", uid)
            await c.answer("Премиум снят.", show_alert=True)
        else:
            until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            await db_exec("UPDATE users SET is_premium=1,premium_until=? WHERE user_id=?", (until, uid))
            await add_admin_log(c.from_user.id, "give_premium", uid, f"{days}d")
            await c.answer(f"✅ Премиум {days}д выдан.", show_alert=True, parse_mode=ParseMode.HTML)
        txt, kb = await render_user_admin(uid)
        await safe_edit(c, txt, kb)
    elif act == "ban":
        u = await get_user(uid)
        if u["is_blocked"]:
            await db_exec("UPDATE users SET is_blocked=0 WHERE user_id=?", (uid,))
            await add_admin_log(c.from_user.id, "unban", uid)
            await c.answer("✅ Разбан.", show_alert=True, parse_mode=ParseMode.HTML)
        else:
            await db_exec("UPDATE users SET is_blocked=1 WHERE user_id=?", (uid,))
            await add_admin_log(c.from_user.id, "ban", uid)
            await c.answer("🚫 Заблокирован.", show_alert=True, parse_mode=ParseMode.HTML)
        txt, kb = await render_user_admin(uid)
        await safe_edit(c, txt, kb)
    elif act == "hist":
        rows = await db_fetchall(
            "SELECT game_type,bet,multiplier,result,profit,timestamp FROM game_stats "
            "WHERE user_id=? ORDER BY timestamp DESC LIMIT 10", (uid,)
        )
        lines = [f"📋 История игр {uid}\n"]
        for r in rows:
            emoji = "✅" if r["result"]=="win" else "❌"
            lines.append(f"{emoji} {r['game_type']} | {fmt(r['bet'])} | ×{r['multiplier']} | {fmt(r['profit'])}")
        await safe_edit(c, "\n".join(lines), back_to_admin())
    await c.answer()

# --- 3. GAMES TOGGLE ---

@router.callback_query(F.data == "adm:games")
async def adm_games(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    disabled = await get_disabled_games()
    rows = []
    for key, label in GAME_KEYS.items():
        mark = "🔴 ВЫКЛ" if key in disabled else "🟢 ВКЛ"
        rows.append([InlineKeyboardButton(
            text=f"{label} — {mark}", callback_data=f"adm:tg:{key}")])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")])
    await safe_edit(c, "🎮 <b>Управление играми</b>", InlineKeyboardMarkup(inline_keyboard=rows))
    await c.answer()

@router.callback_query(F.data.startswith("adm:tg:"))
async def adm_toggle_game(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    key = c.data.split(":",2)[2]
    disabled = await get_disabled_games()
    if key in disabled:
        disabled.discard(key)
        await c.answer(f"✅ {GAME_KEYS[key]} включена", parse_mode=ParseMode.HTML)
        await add_admin_log(c.from_user.id, "game_enable", details=key)
    else:
        disabled.add(key)
        await c.answer(f"⛔ {GAME_KEYS[key]} выключена", show_alert=True)
        await add_admin_log(c.from_user.id, "game_disable", details=key)
    await set_disabled_games(disabled)
    disabled = await get_disabled_games()
    rows = []
    for k, label in GAME_KEYS.items():
        mark = "🔴 ВЫКЛ" if k in disabled else "🟢 ВКЛ"
        rows.append([InlineKeyboardButton(text=f"{label} — {mark}", callback_data=f"adm:tg:{k}")])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")])
    try: await c.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest: pass

# --- 4. PROMO ---

@router.callback_query(F.data == "adm:promo")
async def adm_promo(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    promos = await db_fetchall(
        "SELECT code,reward,discount_percent,promo_type,uses_left,uses_max,is_active "
        "FROM promo_codes ORDER BY rowid DESC LIMIT 15"
    )
    lines = ["🎟 <b>Промокоды</b>\n"]
    for p in promos:
        status = "✅" if p["is_active"] else "❌"
        ptype = p["promo_type"] or "diamonds"
        if ptype == "shop_discount":
            lines.append(f"{status} <code>{p['code']}</code> — скидка {int(p['discount_percent'] or 0)}% 🏬 ({p['uses_left']}/{p['uses_max']})")
        else:
            lines.append(f"{status} <code>{p['code']}</code> — {fmt(p['reward'])} 💎 ({p['uses_left']}/{p['uses_max']})")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промо (💎 алмазы)",    callback_data="adm:promo:new")],
        [InlineKeyboardButton(text="🏬 Создать промо (% скидка магаз.)", callback_data="adm:promo:discount:new")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")],
    ])
    await safe_edit(c, "\n".join(lines) or "Нет промокодов", kb)
    await c.answer()

@router.callback_query(F.data == "adm:promo:new")
async def adm_promo_new(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_PENDING[c.from_user.id] = {"action": "promo_create"}
    await safe_edit(c,
        "Введи данные промокода (💎 алмазы):\n"
        "<code>КОД СУММА КОЛИЧЕСТВО ЧАСОВ</code>\n"
        "Пример: <code>NEW2026 1000 100 24</code>",
        back_to_admin()
    )
    await c.answer()

@router.callback_query(F.data == "adm:promo:discount:new")
async def adm_promo_discount_new(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ADMIN_PENDING[c.from_user.id] = {"action": "promo_discount_create"}
    await safe_edit(c,
        "🏬 <b>Создать скидочный промокод на магазин</b>\n\n"
        "Формат: <code>КОД ПРОЦЕНТ КОЛИЧЕСТВО ЧАСОВ</code>\n"
        "Пример: <code>SALE20 20 50 72</code>\n\n"
        "• КОД — текст промокода\n"
        "• ПРОЦЕНТ — скидка 1–99%\n"
        "• КОЛИЧЕСТВО — сколько раз можно использовать\n"
        "• ЧАСОВ — срок действия (0 = бессрочно)",
        back_to_admin()
    )
    await c.answer()

@router.message(Command("newpromo"), F.chat.type == "private")
async def cmd_newpromo(m: Message):
    if not is_admin(m.from_user.id): return
    parts = (m.text or "").split()
    if len(parts) < 3:
        await m.answer("Использование: /newpromo КОД СУММА [USES] [HOURS]"); return
    code = parts[1].upper()
    try:
        reward = float(parts[2])
        uses = int(parts[3]) if len(parts) > 3 else 100
        hours = int(parts[4]) if len(parts) > 4 else 0
    except Exception:
        await m.answer("Неверные параметры."); return
    expires = (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat() if hours else None
    try:
        await db_exec(
            "INSERT INTO promo_codes(code,reward,reward_type,uses_left,uses_max,created_by,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (code, reward, "fixed", uses, uses, m.from_user.id, now_ts(), expires),
        )
        await add_admin_log(m.from_user.id, "create_promo", details=f"{code} {reward} {uses}")
        await m.answer(f"✅ Промокод <code>{code}</code> создан.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await m.answer(f"❌ {e}", parse_mode=ParseMode.HTML)

# --- 5. BROADCAST ---

@router.callback_query(F.data == "adm:bcast")
async def adm_bcast(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Всем",             callback_data="adm:bcast:all")],
        [InlineKeyboardButton(text="⚡ Активным 7д",      callback_data="adm:bcast:active7")],
        [InlineKeyboardButton(text="💎 Премиум",          callback_data="adm:bcast:premium")],
        [InlineKeyboardButton(text="🆕 Новичкам (24ч)",   callback_data="adm:bcast:new24")],
        [InlineKeyboardButton(text="⬅ Назад",             callback_data="adm:back")],
    ])
    await safe_edit(c, "📢 <b>Рассылка</b>\nВыбери аудиторию:", kb)
    await c.answer()

@router.callback_query(F.data.startswith("adm:bcast:"))
async def adm_bcast_filter(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    ftype = c.data.split(":",2)[2]
    if ftype in ("all","active7","premium","new24"):
        ADMIN_PENDING[c.from_user.id] = {"action": "broadcast", "filter": ftype}
        await safe_edit(c, f"Введи текст рассылки (фильтр: {ftype}):", back_to_admin())
    await c.answer()

# --- 6. JACKPOT ---

@router.callback_query(F.data == "adm:jp")
async def adm_jp(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    jp = await db_fetchone("SELECT amount FROM jackpot WHERE game_type='slots'")
    hist = await db_fetchall(
        "SELECT jh.user_id,u.first_name,jh.amount,jh.timestamp FROM jackpot_history jh "
        "LEFT JOIN users u ON u.user_id=jh.user_id ORDER BY jh.id DESC LIMIT 10"
    )
    lines = [f"💰 <b>Джекпот</b>: {fmt(jp['amount'] if jp else 0)} 💎\n"]
    lines.append("📋 Последние выигрыши:")
    for h in hist:
        n = h["first_name"] or str(h["user_id"])
        lines.append(f"  {n}: +{fmt(h['amount'])} 💎 | {h['timestamp'][:10]}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить",  callback_data="adm:jp:add"),
         InlineKeyboardButton(text="➖ Снять",     callback_data="adm:jp:sub")],
        [InlineKeyboardButton(text="🗑 Сбросить",  callback_data="adm:jp:reset")],
        [InlineKeyboardButton(text="⬅ Назад",      callback_data="adm:back")],
    ])
    await safe_edit(c, "\n".join(lines), kb)
    await c.answer()

@router.callback_query(F.data.startswith("adm:jp:"))
async def adm_jp_action(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    act = c.data.split(":",2)[2]
    if act == "add":
        ADMIN_PENDING[c.from_user.id] = {"action": "jp_add"}
        await safe_edit(c, "Введи сумму для добавления в джекпот:", back_to_admin())
    elif act == "sub":
        ADMIN_PENDING[c.from_user.id] = {"action": "jp_sub"}
        await safe_edit(c, "Введи сумму для снятия из джекпота:", back_to_admin())
    elif act == "reset":
        await db_exec("UPDATE jackpot SET amount=0 WHERE game_type='slots'")
        await add_admin_log(c.from_user.id, "jp_reset")
        await c.answer("✅ Джекпот сброшен!", show_alert=True, parse_mode=ParseMode.HTML)
    await c.answer()

# --- 7. SETTINGS ---

@router.callback_query(F.data == "adm:settings")
async def adm_settings(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    maint = await get_setting("maintenance","0")
    sb = await get_setting("start_bonus", str(START_BONUS))
    fee = await get_setting("transfer_fee", str(TRANSFER_FEE))
    mnb = await get_setting("min_bet", str(MIN_BET))
    mxb = await get_setting("max_bet", str(MAX_BET))
    cur = await get_setting("currency","💎")
    txt = (
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"🔧 Тех.обслуживание: {'ВКЛ ⚠️' if maint=='1' else 'ВЫКЛ ✅'}\n"
        f"🎀 Стартовый бонус: {sb}\n"
        f"💸 Комиссия перевода: {float(fee)*100:.1f}%\n"
        f"📉 Мин. ставка: {mnb}\n"
        f"📈 Макс. ставка: {mxb}\n"
        f"💎 Валюта: {cur}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔧 ТО вкл/выкл",     callback_data="adm:set:maintenance")],
        [InlineKeyboardButton(text="🎀 Старт бонус",      callback_data="adm:set:start_bonus")],
        [InlineKeyboardButton(text="💸 Комиссия %",        callback_data="adm:set:transfer_fee")],
        [InlineKeyboardButton(text="📉 Мин. ставка",       callback_data="adm:set:min_bet")],
        [InlineKeyboardButton(text="📈 Макс. ставка",      callback_data="adm:set:max_bet")],
        [InlineKeyboardButton(text="💎 Валюта",            callback_data="adm:set:currency")],
        [InlineKeyboardButton(text="⬅ Назад",             callback_data="adm:back")],
    ])
    await safe_edit(c, txt, kb)
    await c.answer()

@router.callback_query(F.data.startswith("adm:set:"))
async def adm_set_action(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    key = c.data.split(":",2)[2]
    if key == "maintenance":
        cur = await get_setting("maintenance","0")
        new = "0" if cur=="1" else "1"
        await set_setting("maintenance", new)
        await add_admin_log(c.from_user.id, "maintenance", details=new)
        await c.answer(f"ТО {'включено' if new=='1' else 'выключено'}!", show_alert=True)
        await adm_settings(c)
    else:
        ADMIN_PENDING[c.from_user.id] = {"action": f"set_{key}"}
        labels = {"start_bonus":"стартового бонуса","transfer_fee":"комиссии (0.02=2%)","min_bet":"мин. ставки","max_bet":"макс. ставки","currency":"валюты (например 💎)"}
        await safe_edit(c, f"Введи новое значение {labels.get(key, key)}:", back_to_admin())
    await c.answer()

# --- 8. LOGS ---

@router.callback_query(F.data == "adm:logs")
async def adm_logs(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    rows = await db_fetchall(
        "SELECT al.admin_id,al.action,al.target_id,al.details,al.timestamp "
        "FROM admin_logs al ORDER BY al.id DESC LIMIT 20"
    )
    if not rows:
        await safe_edit(c, "Логов нет.", back_to_admin()); await c.answer(); return
    lines = ["📜 <b>Логи (последние 20)</b>\n"]
    for r in rows:
        lines.append(f"[{r['timestamp'][:16]}] adm={r['admin_id']} {r['action']} tgt={r['target_id']} {r['details'] or ''}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Экспорт .txt", callback_data="adm:logs:export")],
        [InlineKeyboardButton(text="⬅ Назад",          callback_data="adm:back")],
    ])
    await safe_edit(c, "\n".join(lines), kb)
    await c.answer()

@router.callback_query(F.data == "adm:logs:export")
async def adm_logs_export(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return
    rows = await db_fetchall("SELECT * FROM admin_logs ORDER BY id DESC")
    buf = io.StringIO()
    buf.write("id,admin_id,action,target_id,details,timestamp\n")
    for r in rows:
        buf.write(f"{r['id']},{r['admin_id']},{r['action']},{r['target_id']},{r['details'] or ''},{r['timestamp']}\n")
    data = buf.getvalue().encode("utf-8")
    await c.message.answer_document(
        BufferedInputFile(data, filename=f"admin_logs_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"),
        caption="📜 Логи администратора"
    )
    await c.answer("✅ Готово", parse_mode=ParseMode.HTML)

# --- 9. DB BACKUP ---

@router.callback_query(F.data == "adm:db")
async def adm_db(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать casino.db",    callback_data="adm:db:download")],
        [InlineKeyboardButton(text="📦 Бэкап с датой",        callback_data="adm:db:backup")],
        [InlineKeyboardButton(text="🗑 Очистить логи >30д",   callback_data="adm:db:cleanlogs")],
        [InlineKeyboardButton(text="🔧 VACUUM (оптимизация)", callback_data="adm:db:vacuum")],
        [InlineKeyboardButton(text="⬅ Назад",                 callback_data="adm:back")],
    ])
    size = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    await safe_edit(c, f"💾 <b>База данных</b>\nРазмер: {size} KB", kb)
    await c.answer()

@router.callback_query(F.data.startswith("adm:db:"))
async def adm_db_action(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return
    act = c.data.split(":",2)[2]
    if act == "download":
        if not os.path.exists(DB_PATH):
            await c.answer("DB не найдена.", show_alert=True); return
        with open(DB_PATH, "rb") as f: data = f.read()
        await c.message.answer_document(
            BufferedInputFile(data, filename="casino.db"), caption="💾 База данных"
        )
        await add_admin_log(c.from_user.id, "db_download")
        await c.answer("✅ Отправлено", parse_mode=ParseMode.HTML)
    elif act == "backup":
        if not os.path.exists(DB_PATH):
            await c.answer("DB не найдена.", show_alert=True); return
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        with open(DB_PATH, "rb") as f: data = f.read()
        await c.message.answer_document(
            BufferedInputFile(data, filename=f"casino_backup_{ts}.db"), caption=f"📦 Бэкап {ts}"
        )
        await add_admin_log(c.from_user.id, "db_backup")
        await c.answer("✅ Бэкап создан", parse_mode=ParseMode.HTML)
    elif act == "cleanlogs":
        cutoff = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        await db_exec("DELETE FROM admin_logs WHERE timestamp<?", (cutoff,))
        await add_admin_log(c.from_user.id, "db_cleanlogs")
        await c.answer("✅ Логи очищены", show_alert=True, parse_mode=ParseMode.HTML)
    elif act == "vacuum":
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("VACUUM")
            await db.commit()
        await add_admin_log(c.from_user.id, "db_vacuum")
        await c.answer("✅ VACUUM выполнен", show_alert=True, parse_mode=ParseMode.HTML)

# --- 10. SESSIONS ---

@router.callback_query(F.data == "adm:sessions")
async def adm_sessions(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    rows = await db_fetchall("SELECT pg.*,u.first_name FROM pending_games pg LEFT JOIN users u ON u.user_id=pg.user_id")
    if not rows:
        await safe_edit(c, "🧹 Незавершённых игр в Мины нет.", back_to_admin()); await c.answer(); return
    lines = ["🧹 <b>Активные сессии мин</b>\n"]
    kb_rows = []
    for r in rows:
        n = r["first_name"] or str(r["user_id"], parse_mode=ParseMode.HTML)
        lines.append(f"• {n} ({r['user_id']}) — {fmt(r['bet'])} 💎 | {r['created_at'][:16]}")
        kb_rows.append([InlineKeyboardButton(
            text=f"❌ Закрыть {r['user_id']}", callback_data=f"adm:ses:close:{r['user_id']}")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть ВСЕ",  callback_data="adm:ses:closeall")])
    kb_rows.append([InlineKeyboardButton(text="⬅ Назад",        callback_data="adm:back")])
    await safe_edit(c, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()

@router.callback_query(F.data.startswith("adm:ses:"))
async def adm_ses_action(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return
    parts = c.data.split(":")
    act = parts[2]
    if act == "closeall":
        rows = await db_fetchall("SELECT user_id,bet FROM pending_games")
        for r in rows:
            await cancel_mines_session(r["user_id"], bot)
        await add_admin_log(c.from_user.id, "ses_closeall")
        await c.answer(f"✅ Закрыто {len(rows)} сессий", show_alert=True, parse_mode=ParseMode.HTML)
        await adm_sessions(c)
    elif act == "close":
        uid = int(parts[3])
        await cancel_mines_session(uid, bot)
        await add_admin_log(c.from_user.id, "ses_close", uid)
        await c.answer(f"✅ Сессия {uid} закрыта", parse_mode=ParseMode.HTML)
        await adm_sessions(c)

# --- 11. TEMPLATES ---

@router.callback_query(F.data == "adm:templates")
async def adm_templates(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    rows = await db_fetchall("SELECT key,text FROM message_templates")
    lines = ["📝 <b>Шаблоны сообщений</b>\n"]
    kb_rows = []
    for r in rows:
        preview = r["text"][:40].replace("\n"," ")
        lines.append(f"<b>{r['key']}</b>: {preview}...")
        kb_rows.append([InlineKeyboardButton(
            text=f"✏️ {r['key']}", callback_data=f"adm:tpl:edit:{r['key']}")])
    kb_rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")])
    await safe_edit(c, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()

@router.callback_query(F.data.startswith("adm:tpl:edit:"))
async def adm_tpl_edit(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    key = c.data.split(":",3)[3]
    cur = await db_fetchone("SELECT text FROM message_templates WHERE key=?", (key,))
    ADMIN_PENDING[c.from_user.id] = {"action": "tpl_edit", "key": key}
    cur_txt = cur["text"] if cur else "—"
    await safe_edit(c, f"Текущий шаблон <b>{key}</b>:\n<code>{cur_txt}</code>\n\nВведи новый текст:", back_to_admin())
    await c.answer()

# --- 12. PREMIUM EMOJI EDITOR ---

def _emoji_list_kb() -> InlineKeyboardMarkup:
    rows = []
    for key, label in EMOJI_LABELS.items():
        emoji_id = EMOJI_IDS.get(key, "")
        short = f"…{emoji_id[-8:]}" if emoji_id else "не задан"
        rows.append([InlineKeyboardButton(
            text=f"{label}  [{short}]",
            callback_data=f"adm:emoji:edit:{key}"
        )])
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "adm:emoji")
async def adm_emoji(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    txt = (
        "✨ <b>Управление Premium Эмодзи</b>\n\n"
        "Здесь ты можешь изменить ID любого премиум-эмодзи.\n"
        "ID берётся из ссылки на эмодзи или через @stickers бота.\n\n"
        "Нажми на нужный эмодзи чтобы изменить его ID:"
    )
    await safe_edit(c, txt, _emoji_list_kb())
    await c.answer()

@router.callback_query(F.data.startswith("adm:emoji:edit:"))
async def adm_emoji_edit(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    key = c.data.split(":", 3)[3]
    if key not in EMOJI_IDS:
        await c.answer("Неизвестный ключ.", show_alert=True); return
    label = EMOJI_LABELS.get(key, key)
    cur_id = EMOJI_IDS.get(key, "—")
    ADMIN_PENDING[c.from_user.id] = {"action": "emoji_edit", "key": key}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅ Назад к списку эмодзи", callback_data="adm:emoji")]
    ])
    await safe_edit(
        c,
        f"✨ Редактирование эмодзи: <b>{label}</b>\n\n"
        f"Текущий ID: <code>{cur_id}</code>\n\n"
        f"Введи новый числовой ID эмодзи\n"
        f"(только цифры, например: <code>5904462880941545555</code>):",
        kb
    )
    await c.answer()

# --- 13. EXPORT / IMPORT ---

@router.callback_query(F.data == "adm:export")
async def adm_export(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Экспорт настроек JSON",  callback_data="adm:exp:settings")],
        [InlineKeyboardButton(text="📤 Экспорт пользов. CSV",  callback_data="adm:exp:users")],
        [InlineKeyboardButton(text="⬅ Назад",                  callback_data="adm:back")],
    ])
    await safe_edit(c, "🔄 <b>Экспорт / Импорт</b>", kb)
    await c.answer()

@router.callback_query(F.data.startswith("adm:exp:"))
async def adm_exp_action(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return
    act = c.data.split(":",2)[2]
    if act == "settings":
        rows = await db_fetchall("SELECT key,value FROM settings")
        data = {r["key"]: r["value"] for r in rows}
        game_rows = await db_fetchall("SELECT game,key,value FROM game_settings")
        data["game_settings"] = [{"game":r["game"],"key":r["key"],"value":r["value"]} for r in game_rows]
        j = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        await c.message.answer_document(
            BufferedInputFile(j, filename=f"settings_{datetime.now().strftime('%Y%m%d')}.json"),
            caption="⚙️ Настройки бота"
        )
        await c.answer("✅ Экспорт настроек", parse_mode=ParseMode.HTML)
    elif act == "users":
        rows = await db_fetchall(
            "SELECT user_id,username,first_name,balance,level,total_games,total_profit,is_premium,register_date FROM users"
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id","username","first_name","balance","level","total_games","total_profit","is_premium","register_date"])
        for r in rows:
            writer.writerow([r["user_id"],r["username"],r["first_name"],r["balance"],r["level"],r["total_games"],r["total_profit"],r["is_premium"],r["register_date"]])
        data = buf.getvalue().encode("utf-8-sig")
        await c.message.answer_document(
            BufferedInputFile(data, filename=f"users_{datetime.now().strftime('%Y%m%d')}.csv"),
            caption="👥 Пользователи"
        )
        await c.answer("✅ Экспорт пользователей", parse_mode=ParseMode.HTML)

# ============================================================================
# ADMIN TEXT INPUT HANDLER
# ============================================================================

@router.message(F.text)
async def universal_handler(m: Message, bot: Bot):
    uid = m.from_user.id

    # --- Admin pending action ---
    if is_admin(uid) and uid in ADMIN_PENDING:
        pending = ADMIN_PENDING.pop(uid)
        action = pending["action"]
        text = (m.text or "").strip()

        if action == "user_search":
            # Search by ID, username, or nickname
            rows = []
            try:
                rows = await db_fetchall("SELECT user_id,first_name,username,custom_nickname FROM users WHERE user_id=?", (int(text),))
            except ValueError: pass
            if not rows:
                rows = await db_fetchall(
                    "SELECT user_id,first_name,username,custom_nickname FROM users WHERE username LIKE ? OR custom_nickname LIKE ?",
                    (f"%{text}%", f"%{text}%"),
                )
            if not rows:
                await m.answer("Пользователь не найден.", reply_markup=admin_main_kb()); return
            if len(rows) == 1:
                txt2, kb2 = await render_user_admin(rows[0]["user_id"])
                await m.answer(txt2, reply_markup=kb2)
            else:
                btns = [[InlineKeyboardButton(
                    text=f"{r['first_name'] or r['username'] or r['user_id']}",
                    callback_data=f"adm:u:view:{r['user_id']}"
                )] for r in rows[:10]]
                btns.append([InlineKeyboardButton(text="⬅ Назад", callback_data="adm:back")])
                await m.answer("Найдено несколько:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
            return

        if action == "bal_plus":
            try:
                amount = float(text)
                target = pending["uid"]
                await update_balance(target, amount)
                await add_admin_log(uid, "balance_add", target, str(amount))
                await m.answer(f"✅ Начислено +{fmt(amount)} 💎 пользователю {target}", parse_mode=ParseMode.HTML)
            except Exception as e:
                await m.answer(f"❌ Ошибка: {e}", parse_mode=ParseMode.HTML)
            return

        if action == "bal_minus":
            try:
                amount = float(text)
                target = pending["uid"]
                await update_balance(target, -amount)
                await add_admin_log(uid, "balance_sub", target, str(amount))
                await m.answer(f"✅ Снято -{fmt(amount)} 💎 у пользователя {target}", parse_mode=ParseMode.HTML)
            except Exception as e:
                await m.answer(f"❌ Ошибка: {e}", parse_mode=ParseMode.HTML)
            return

        if action == "promo_create":
            parts = text.split()
            if len(parts) < 2:
                await m.answer("Формат: КОД СУММА [КОЛИЧЕСТВО] [ЧАСОВ]"); return
            code = parts[0].upper()
            try:
                reward = float(parts[1])
                uses = int(parts[2]) if len(parts) > 2 else 100
                hours = int(parts[3]) if len(parts) > 3 else 0
            except Exception:
                await m.answer("Неверные параметры."); return
            expires = (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat() if hours else None
            try:
                await db_exec(
                    "INSERT INTO promo_codes(code,reward,reward_type,promo_type,uses_left,uses_max,created_by,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (code, reward, "fixed", "diamonds", uses, uses, uid, now_ts(), expires),
                )
                await add_admin_log(uid, "create_promo", details=f"{code} {reward} {uses}")
                await m.answer(f"✅ Промокод <code>{code}</code> создан: {fmt(reward)} 💎 x{uses}", reply_markup=admin_main_kb(), parse_mode=ParseMode.HTML)
            except Exception as e:
                await m.answer(f"❌ {e}", parse_mode=ParseMode.HTML)
            return

        if action == "promo_discount_create":
            parts = text.split()
            if len(parts) < 2:
                await m.answer("Формат: КОД ПРОЦЕНТ [КОЛИЧЕСТВО] [ЧАСОВ]"); return
            code = parts[0].upper()
            try:
                pct = float(parts[1])
                if not (1 <= pct <= 99):
                    await m.answer("❌ Процент должен быть от 1 до 99"); return
                uses = int(parts[2]) if len(parts) > 2 else 100
                hours = int(parts[3]) if len(parts) > 3 else 0
            except Exception:
                await m.answer("Неверные параметры."); return
            expires = (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat() if hours else None
            try:
                await db_exec(
                    "INSERT INTO promo_codes(code,reward,reward_type,promo_type,discount_percent,uses_left,uses_max,created_by,created_at,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (code, 0, "discount", "shop_discount", pct, uses, uses, uid, now_ts(), expires),
                )
                await add_admin_log(uid, "create_promo_discount", details=f"{code} {pct}% {uses}")
                await m.answer(
                    f"✅ Скидочный промокод <code>{code}</code> создан!\n"
                    f"🏬 Скидка: <b>{int(pct)}%</b> на покупку в магазине | x{uses} активаций",
                    reply_markup=admin_main_kb()
                )
            except Exception as e:
                await m.answer(f"❌ {e}", parse_mode=ParseMode.HTML)
            return

        if action == "broadcast":
            ftype = pending.get("filter","all")
            now = datetime.now(timezone.utc)
            if ftype == "all":
                user_rows = await db_fetchall("SELECT user_id FROM users")
            elif ftype == "active7":
                user_rows = await db_fetchall("SELECT user_id FROM users WHERE last_game>=?",
                                               ((now-timedelta(days=7)).isoformat(),))
            elif ftype == "premium":
                user_rows = await db_fetchall("SELECT user_id FROM users WHERE is_premium=1")
            elif ftype == "new24":
                user_rows = await db_fetchall("SELECT user_id FROM users WHERE register_date>=?",
                                               ((now-timedelta(hours=24)).isoformat(),))
            else:
                user_rows = await db_fetchall("SELECT user_id FROM users")
            sent = 0; failed = 0
            status_msg = await m.answer(f"📢 Рассылка ({ftype})... 0/{len(user_rows)}", parse_mode=ParseMode.HTML)
            for i, row in enumerate(user_rows):
                try:
                    await bot.send_message(row["user_id"], text)
                    sent += 1
                except Exception:
                    failed += 1
                if (i+1) % 20 == 0:
                    try: await status_msg.edit_text(f"📢 Прогресс: {i+1}/{len(user_rows)}")
                    except Exception: pass
                await asyncio.sleep(0.05)
            await add_admin_log(uid, "broadcast", details=f"sent={sent} fail={failed} filter={ftype}")
            try: await status_msg.edit_text(f"✅ Рассылка завершена: {sent} доставлено, {failed} ошибок.")
            except Exception: pass
            return

        if action == "shop_promo_apply":
            code = text.strip().upper()
            pc = await db_fetchone(
                "SELECT * FROM promo_codes WHERE code=? AND is_active=1 AND promo_type='shop_discount'", (code,)
            )
            if not pc:
                await m.answer("❌ Промокод не найден или не является скидочным."); return
            if pc["expires_at"] and datetime.fromisoformat(pc["expires_at"]) < datetime.now(timezone.utc):
                await m.answer("❌ Промокод истёк."); return
            if pc["uses_left"] <= 0:
                await m.answer("❌ Промокод исчерпан."); return
            existing = await db_fetchone(
                "SELECT id FROM promo_activations WHERE code=? AND user_id=?", (code, uid)
            )
            if existing:
                await m.answer("❌ Вы уже использовали этот промокод."); return
            pct = float(pc["discount_percent"])
            SHOP_DISCOUNT[uid] = {"code": code, "discount": pct}
            await m.answer(
                f"✅ Промокод <code>{code}</code> применён!\n"
                f"🎟 Скидка <b>{int(pct)}%</b> на следующую покупку в магазине.",
                reply_markup=stars_shop_kb(uid)
            )
            return

        if action == "jp_add":
            try:
                amount = float(text)
                await db_exec("UPDATE jackpot SET amount=amount+? WHERE game_type='slots'", (amount,))
                await add_admin_log(uid, "jp_add", details=str(amount))
                await m.answer(f"✅ Добавлено {fmt(amount)} 💎 в джекпот", parse_mode=ParseMode.HTML)
            except Exception as e:
                await m.answer(f"❌ {e}", parse_mode=ParseMode.HTML)
            return

        if action == "jp_sub":
            try:
                amount = float(text)
                await db_exec("UPDATE jackpot SET amount=MAX(0,amount-?) WHERE game_type='slots'", (amount,))
                await add_admin_log(uid, "jp_sub", details=str(amount))
                await m.answer(f"✅ Снято {fmt(amount)} 💎 из джекпота", parse_mode=ParseMode.HTML)
            except Exception as e:
                await m.answer(f"❌ {e}", parse_mode=ParseMode.HTML)
            return

        if action and action.startswith("set_"):
            key = action[4:]
            await set_setting(key, text)
            await add_admin_log(uid, f"setting_change", details=f"{key}={text}")
            await m.answer(f"✅ Настройка <b>{key}</b> = <code>{text}</code> сохранена", parse_mode=ParseMode.HTML)
            return

        if action == "tpl_edit":
            key = pending.get("key","")
            await db_exec(
                "INSERT INTO message_templates(key,text) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET text=excluded.text",
                (key, text),
            )
            await add_admin_log(uid, "tpl_edit", details=key)
            await m.answer(f"✅ Шаблон <b>{key}</b> обновлён.", parse_mode=ParseMode.HTML)
            return

        if action == "emoji_edit":
            key = pending.get("key", "")
            emoji_id = text.strip()
            if not emoji_id.isdigit():
                await m.answer("❌ ID должен содержать только цифры. Попробуй ещё раз.", parse_mode=ParseMode.HTML)
                return
            await save_emoji_id(key, emoji_id)
            await add_admin_log(uid, "emoji_edit", details=f"{key}={emoji_id}")
            label = EMOJI_LABELS.get(key, key)
            await m.answer(
                f"✅ Эмодзи <b>{label}</b> обновлён.\n"
                f"Новый ID: <code>{emoji_id}</code>",
                parse_mode=ParseMode.HTML
            )
            return

        if action == "ch_add":
            channel_id = text.strip()
            if not channel_id.startswith("@") and not channel_id.lstrip("-").isdigit():
                channel_id = "@" + channel_id
            try:
                chat = await bot.get_chat(channel_id)
                title = chat.title or channel_id
                invite = chat.invite_link or f"https://t.me/{str(chat.username or '').lstrip('@')}"
                cid = str(chat.id)
                await db_exec(
                    "INSERT OR IGNORE INTO required_channels(channel_id,channel_title,invite_link,added_at) VALUES(?,?,?,?)",
                    (cid, title, invite, now_ts()),
                )
                await add_admin_log(uid, "ch_add", details=f"{cid} {title}")
                await m.answer(
                    f"✅ Канал <b>{title}</b> добавлен как обязательный.\n"
                    f"ID: <code>{cid}</code>",
                    reply_markup=admin_main_kb()
                )
            except Exception as e:
                await m.answer(
                    f"❌ Не удалось получить инфо о канале: {e}\n\n"
                    f"Убедись что:\n"
                    f"• Бот добавлен в канал как администратор\n"
                    f"• Username или ID указан правильно"
                )
            return

    # --- Basic text triggers ---
    txt_lower = (m.text or "").lower()
    if txt_lower in ("б","профиль","profile","я","👤 профиль"):
        await ensure_user(m)
        await m.answer(await render_profile(uid), reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
    elif txt_lower in ("баланс","💎 баланс"):
        await ensure_user(m)
        u = await get_user(uid)
        cur = await get_currency()
        await m.answer(f"{cur} Баланс: <b>{fmt(u['balance'])}</b>", parse_mode=ParseMode.HTML)
    elif txt_lower in ("бонус","🎀 бонус"):
        await cmd_daily(m)
    elif txt_lower in ("магазин","🏬 магазин"):
        await ensure_user(m)
        txt2, kb2 = await render_shop()
        await m.answer(txt2, reply_markup=kb2)
    elif txt_lower in ("топ","🏆 топ"):
        await cmd_top(m)
    elif txt_lower in ("реферал","👥 реферал"):
        await cmd_ref(m, bot)
    elif txt_lower in ("помощь","📖 помощь"):
        await cmd_help(m)
    elif txt_lower in ("игры","🎮 игры"):
        await ensure_user(m)
        await m.answer("🎮 Выбери игру:", reply_markup=await games_menu_kb(), parse_mode=ParseMode.HTML)
    elif txt_lower in ("купить","💫 купить","stars","купить кристаллы"):
        await ensure_user(m)
        await cmd_buy(m)

# view user by callback
@router.callback_query(F.data.startswith("adm:u:view:"))
async def adm_user_view(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    uid = int(c.data.split(":",3)[3])
    txt, kb = await render_user_admin(uid)
    await safe_edit(c, txt, kb)
    await c.answer()

# ============================================================================
# ADMIN — REQUIRED CHANNELS
# ============================================================================

def channels_kb(channels) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        title = ch["channel_title"] or ch["channel_id"]
        rows.append([InlineKeyboardButton(
            text=f"❌ Удалить: {title}",
            callback_data=f"adm:ch:del:{ch['id']}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm:ch:add")])
    rows.append([InlineKeyboardButton(text="⬅ Назад",          callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.callback_query(F.data == "adm:channels")
async def adm_channels(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    channels = await db_fetchall("SELECT * FROM required_channels")
    if channels:
        lines = ["📡 <b>Обязательные каналы</b>\n"]
        for ch in channels:
            lines.append(f"• {ch['channel_title'] or ch['channel_id']} (<code>{ch['channel_id']}</code>)")
        txt = "\n".join(lines)
    else:
        txt = "📡 <b>Обязательные каналы</b>\n\nСписок пуст. Добавь первый канал."
    await safe_edit(c, txt, channels_kb(channels))
    await c.answer()

@router.callback_query(F.data.startswith("adm:ch:"))
async def adm_ch_action(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id): return
    parts = c.data.split(":", 3)
    act = parts[2]
    if act == "add":
        ADMIN_PENDING[c.from_user.id] = {"action": "ch_add"}
        await safe_edit(c,
            "📡 Введи <b>username</b> канала или его <b>числовой ID</b>.\n\n"
            "Примеры:\n• <code>@mychannel</code>\n• <code>-1001234567890</code>\n\n"
            "⚠️ Бот должен быть администратором в этом канале!",
            back_to_admin()
        )
    elif act == "del":
        row_id = int(parts[3])
        ch = await db_fetchone("SELECT * FROM required_channels WHERE id=?", (row_id,))
        if ch:
            await db_exec("DELETE FROM required_channels WHERE id=?", (row_id,))
            await add_admin_log(c.from_user.id, "ch_del", details=ch["channel_id"])
            await c.answer(f"✅ Канал {ch['channel_title'] or ch['channel_id']} удалён", show_alert=True, parse_mode=ParseMode.HTML)
        channels = await db_fetchall("SELECT * FROM required_channels")
        txt = "📡 <b>Обязательные каналы</b>\n\n" + (
            "\n".join(f"• {ch['channel_title'] or ch['channel_id']}" for ch in channels)
            if channels else "Список пуст."
        )
        await safe_edit(c, txt, channels_kb(channels))
    await c.answer()

# ============================================================================
# SUBSCRIPTION CHECK CALLBACK
# ============================================================================

@router.callback_query(F.data == "sub:check")
async def sub_check_cb(c: CallbackQuery, bot: Bot):
    ok, missing = await check_subscriptions(c.from_user.id, bot)
    if ok:
        await c.answer("✅ Отлично! Теперь ты можешь пользоваться ботом.", show_alert=True, parse_mode=ParseMode.HTML)
        try: await c.message.delete()
        except Exception: pass
    else:
        titles = ", ".join(ch["channel_title"] or ch["channel_id"] for ch in missing)
        await c.answer(f"❌ Ещё не подписан на: {titles}", show_alert=True, parse_mode=ParseMode.HTML)

# ============================================================================
# STARS SHOP — BUY DIAMONDS
# ============================================================================

def stars_shop_kb(user_id: int = 0) -> InlineKeyboardMarkup:
    disc = SHOP_DISCOUNT.get(user_id, {})
    pct  = disc.get("discount", 0.0)
    rows = []
    for i, pkg in enumerate(STARS_PACKAGES):
        stars = pkg["stars"]
        if pct > 0:
            stars = max(1, int(stars * (1 - pct / 100)))
            label = f"{pkg['label']} — ~~{pkg['stars']}~~ {stars} ⭐ (-{int(pct)}%)"
        else:
            label = f"{pkg['label']} — {stars} ⭐"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:stars:{i}")])
    if pct > 0:
        code = disc.get("code", "")
        rows.append([InlineKeyboardButton(
            text=f"🎟 Скидка {int(pct)}% применена ({code}) ✅",
            callback_data="buy:promo:remove"
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="🎟 Ввести промокод на скидку",
            callback_data="buy:promo"
        )])
    rows.append([InlineKeyboardButton(
        text="📩 Другое кол-во — @DiamondMinesAdmin",
        url="https://t.me/DiamondMinesAdmin"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def shop_text(user_id: int = 0) -> str:
    disc = SHOP_DISCOUNT.get(user_id, {})
    pct  = disc.get("discount", 0.0)
    lines = [f"<tg-emoji emoji-id='{EMOJI_IDS['coins']}'>💎</tg-emoji> <b>Купить кристаллы за Telegram Stars</b>\n"]
    for pkg in STARS_PACKAGES:
        stars = pkg["stars"]
        if pct > 0:
            discounted = max(1, int(stars * (1 - pct / 100)))
            lines.append(f"🔹 {pkg['label']} — <s>{stars}</s> <b>{discounted} ⭐</b> (-{int(pct)}%)")
        else:
            lines.append(f"🔹 {pkg['label']} — <b>{stars} ⭐</b>")
    lines.append(f"\n💬 Другое количество — напиши @DiamondMinesAdmin")
    return "\n".join(lines)

@router.message(Command("buy"), F.chat.type == "private")
@router.message(F.text.lower().in_({"купить", "💫 купить", "магазин звёзд", "stars"}))
async def cmd_buy(m: Message):
    await ensure_user(m)
    uid = m.from_user.id
    await m.answer(shop_text(uid), reply_markup=stars_shop_kb(uid))

@router.callback_query(F.data == "buy:promo")
async def buy_promo_ask(c: CallbackQuery):
    await ensure_user(c)
    ADMIN_PENDING[c.from_user.id] = {"action": "shop_promo_apply",
                                     "msg_id": c.message.message_id}
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("🎟 Отправь промокод на скидку:")
    await c.answer()

@router.callback_query(F.data == "buy:promo:remove")
async def buy_promo_remove(c: CallbackQuery):
    SHOP_DISCOUNT.pop(c.from_user.id, None)
    await c.message.edit_text(shop_text(c.from_user.id), reply_markup=stars_shop_kb(c.from_user.id))
    await c.answer("Скидка убрана")

@router.pre_checkout_query()
async def pre_checkout(pq: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pq.id, ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(m: Message, bot: Bot):
    payload = m.successful_payment.invoice_payload
    if payload.startswith("diamonds:"):
        parts_pl = payload.split(":")
        diamonds = int(parts_pl[1])
        # consume shop discount promo if used
        if len(parts_pl) >= 4 and parts_pl[2] == "promo":
            promo_code = parts_pl[3]
            await db_exec("UPDATE promo_codes SET uses_left=uses_left-1 WHERE code=?", (promo_code,))
            await db_exec("INSERT OR IGNORE INTO promo_activations(code,user_id,activated_at) VALUES(?,?,?)",
                          (promo_code, m.from_user.id, now_ts()))
            SHOP_DISCOUNT.pop(m.from_user.id, None)
        await update_balance(m.from_user.id, diamonds)
        cur = await get_currency()
        await m.answer(
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"💎 На ваш баланс зачислено <b>{fmt(diamonds)}</b> {cur}\n\n"
            f"Спасибо за поддержку! 🎰"
        )
        # Реферальный бонус 5% тому, кто пригласил
        buyer = await get_user(m.from_user.id)
        if buyer and buyer["invited_by"]:
            bonus = int(diamonds * 0.05)
            if bonus > 0:
                await update_balance(buyer["invited_by"], bonus)
                await db_exec(
                    "INSERT INTO referrals(inviter_id,invited_id,level,bonus_given,date) VALUES(?,?,?,?,?)",
                    (buyer["invited_by"], m.from_user.id, 0, bonus, now_ts()),
                )
                try:
                    inviter_name = m.from_user.first_name or f"id{m.from_user.id}"
                    await bot.send_message(
                        buyer["invited_by"],
                        f"💫 <b>Реферальный бонус!</b>\n\n"
                        f"Ваш реферал {inviter_name} купил кристаллы.\n"
                        f"💎 Вам начислено <b>+{fmt(bonus)}</b> {cur} (5% от покупки)"
                    )
                except Exception:
                    pass

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def mines_timeout_task(bot: Bot) -> None:
    """Cancel mines sessions older than MINES_TIMEOUT_MIN minutes."""
    while True:
        await asyncio.sleep(300)  # every 5 min
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=MINES_TIMEOUT_MIN)).isoformat()
            expired = await db_fetchall("SELECT user_id FROM pending_games WHERE created_at<?", (cutoff,))
            for row in expired:
                log.info("Auto-cancelling mines session for user %s", row["user_id"])
                await cancel_mines_session(row["user_id"], bot)
        except Exception as e:
            log.warning("mines_timeout_task error: %s", e)

async def broadcast_task(bot: Bot) -> None:
    """Process scheduled broadcasts from broadcast_queue."""
    while True:
        await asyncio.sleep(60)
        try:
            now = datetime.now(timezone.utc).isoformat()
            rows = await db_fetchall(
                "SELECT * FROM broadcast_queue WHERE sent=0 AND scheduled_at<=?", (now,)
            )
            for bcast in rows:
                ftype = bcast["filter_type"]
                if ftype == "all":
                    users = await db_fetchall("SELECT user_id FROM users")
                elif ftype == "active7":
                    users = await db_fetchall("SELECT user_id FROM users WHERE last_game>=?",
                                               ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),))
                elif ftype == "premium":
                    users = await db_fetchall("SELECT user_id FROM users WHERE is_premium=1")
                else:
                    users = await db_fetchall("SELECT user_id FROM users")
                for u in users:
                    try: await bot.send_message(u["user_id"], bcast["text"])
                    except Exception: pass
                    await asyncio.sleep(0.05)
                await db_exec("UPDATE broadcast_queue SET sent=1 WHERE id=?", (bcast["id"],))
                log.info("Broadcast %s sent to %d users.", bcast["id"], len(users))
        except Exception as e:
            log.warning("broadcast_task error: %s", e)

async def db_backup_task(bot: Bot) -> None:
    """Send database backup to admin every 15 minutes."""
    while True:
        await asyncio.sleep(900)  # every 15 min
        try:
            admin_id = list(ADMIN_IDS)[0] if ADMIN_IDS else None
            if not admin_id:
                log.warning("No admin IDs configured for DB backup")
                continue
            if os.path.exists("casino.db"):
                await bot.send_document(
                    admin_id,
                    FSInputFile("casino.db"),
                    caption=f"📊 Database backup\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                log.info("Database backup sent to admin")
            else:
                log.warning("Database file not found")
        except Exception as e:
            log.warning("db_backup_task error: %s", e)

# ============================================================================
# MAIN
# ============================================================================

async def main():
    if BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        log.warning("BOT_TOKEN не задан!")
    await init_db()
    await load_emoji_ids()
    await load_mines_sessions()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning("delete_webhook: %s", e)
    log.info("Bot started.")
    asyncio.create_task(mines_timeout_task(bot))
    asyncio.create_task(broadcast_task(bot))
    asyncio.create_task(db_backup_task(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
