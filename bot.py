import asyncio
import logging
import os
import re
import random
import string
import io
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, CallbackQuery, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, LabeledPrice, Message, PreCheckoutQuery, ReplyKeyboardMarkup
)
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError,
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PasswordHashInvalidError, FloodWaitError
)
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.types import (
    InputReportReasonSpam, InputReportReasonViolence,
    InputReportReasonPornography, InputReportReasonOther,
    InputReportReasonCopyright, InputReportReasonFake,
    Channel, Chat, User
)
from telethon.sessions import StringSession, MemorySession

import database as db

# ============================================================
#                        НАСТРОЙКИ
# ============================================================

BOT_TOKEN         = "ТОКЕН_ОСНОВНОГО_БОТА"
CRYPTOBOT_TOKEN   = "ТОКЕН_CRYPTOBOT"
SUPERADMIN_IDS    = {853173723, 1090307552}

TELETHON_API_ID   = 35989820
TELETHON_API_HASH = "18cec00c9bef93d0dd475baba4e6c3f4"

MAIN_BOT_USERNAME   = "Pizza_FenixBot"
BACKUP_BOT_USERNAME = ""

DB_PATH = "bot_database.db"

# Кулдаун между жалобами — 30 минут
REPORT_COOLDOWN_SECONDS = 1800
# Бонус за реферала (дней подписки) — выдаётся только при первой оплате реферала
REFERRAL_BONUS_DAYS = 1

# ============================================================

db.DB_PATH = DB_PATH

log_handler = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[log_handler, logging.StreamHandler()]
)
logger = logging.getLogger("bot")

router = Router()
user_last_report: dict[int, datetime] = {}
_auth_clients: dict[int, TelegramClient] = {}
_session_status: dict[int, str] = {}  # session_id -> status string

REPORT_REASONS = {
    "spam":       ("🗑 Спам",               InputReportReasonSpam()),
    "violence":   ("🔪 Насилие",            InputReportReasonViolence()),
    "porn":       ("🔞 Порнография",         InputReportReasonPornography()),
    "copyright":  ("©️ Авторские права",    InputReportReasonCopyright()),
    "fake":       ("🎭 Фейк / самозванство", InputReportReasonFake()),
    "other":      ("❓ Другое",             InputReportReasonOther()),
}

TYPE_LABELS = {
    "user":    "👤 Пользователь",
    "bot":     "🤖 Бот",
    "channel": "📢 Канал",
    "group":   "👥 Группа",
}
TYPE_ICONS = {
    "user":    "👤",
    "bot":     "🤖",
    "channel": "📢",
    "group":   "👥",
}


def _generate_report_id() -> str:
    """Генерирует уникальный 8-символьный идентификатор жалобы."""
    chars = string.ascii_uppercase + string.digits
    return "RPT-" + "".join(random.choices(chars, k=6))


class States(StatesGroup):
    # Обращения
    waiting_report_type    = State()
    waiting_report_link    = State()
    waiting_report_reason  = State()
    waiting_custom_text    = State()
    waiting_confirm        = State()
    waiting_peer_username  = State()
    waiting_peer_reason    = State()
    waiting_peer_custom    = State()
    waiting_peer_confirm   = State()
    # Сессии
    waiting_session_string = State()
    waiting_auth_key_hex   = State()
    waiting_auth_key_dc    = State()
    waiting_phone          = State()
    waiting_code           = State()
    waiting_2fa            = State()
    waiting_del_session    = State()
    # Админ
    waiting_admin_id_add   = State()
    waiting_channel_add    = State()
    waiting_rules_url      = State()
    waiting_rules_text     = State()
    waiting_backup_token   = State()
    waiting_broadcast_text = State()
    waiting_grant_uid      = State()
    waiting_grant_days     = State()
    # Промокоды
    waiting_promo_new_code = State()
    waiting_promo_new_days = State()
    waiting_promo_new_uses = State()
    waiting_promo_activate = State()
    # Белый список
    waiting_wl_target      = State()
    waiting_wl_type        = State()
    # Тарифы
    waiting_plan_days      = State()
    waiting_plan_price     = State()
    waiting_plan_label     = State()
    # Группа логов
    waiting_log_group_id   = State()
    # Снятие подписки
    waiting_revoke_uid     = State()
    waiting_revoke_reason  = State()
    # Premium цена (для админа)
    waiting_premium_price  = State()
    # Бан, Stars
    waiting_ban_uid        = State()
    waiting_ban_reason     = State()
    waiting_unban_uid      = State()
    waiting_stars_price    = State()


# ─── Утилиты ───────────────────────────────────────────────

def parse_tg_link(link: str) -> tuple[str | None, str | None, bool]:
    """
    Возвращает (chat_id, message_id, is_private).
    is_private=True для ссылок вида t.me/c/... (приватные каналы/группы).
    """
    link = link.strip()
    m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if m:
        return "-100" + m.group(1), m.group(2), True
    m = re.match(r"https?://t\.me/([^/]+)/(\d+)", link)
    if m:
        return m.group(1), m.group(2), False
    return None, None, False


def _parse_peer_username(text: str) -> str | None:
    text = text.strip()
    m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", text)
    if m:
        return m.group(1)
    if text.startswith("@") and len(text) >= 5:
        return text[1:]
    if re.match(r"^[A-Za-z0-9_]{4,}$", text):
        return text
    return None


async def check_channels(bot: Bot, user_id: int) -> list:
    channels = await db.get_force_channels()
    not_subscribed = []
    for ch in channels:
        try:
            ref = ch["channel_id"] if ch["channel_id"] else f"@{ch['channel_username']}"
            member = await bot.get_chat_member(ref, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return not_subscribed


async def _get_telethon_client() -> TelegramClient | None:
    """Возвращает первый рабочий Telethon-клиент из сессий или None."""
    sessions = await db.get_all_sessions()
    for sess in sessions:
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                return client
            await client.disconnect()
        except Exception:
            pass
    return None


async def check_peer_accessible(username: str) -> tuple[bool, str, str]:
    """
    Проверяет доступность через Telethon и определяет тип сущности.
    Возвращает (ok, error_code, entity_type).
    error_code:   'private' | 'not_found' | '' (всё ок)
    entity_type:  'channel' | 'group' | 'bot' | 'user' | ''
    """
    client = await _get_telethon_client()
    if client is None:
        return True, "", ""  # нет сессий — пропускаем проверку
    try:
        entity = await client.get_entity(username)
        if isinstance(entity, Channel):
            if not entity.username:
                return False, "private", ""
            etype = "group" if entity.megagroup else "channel"
            return True, "", etype
        elif isinstance(entity, User):
            etype = "bot" if entity.bot else "user"
            return True, "", etype
        elif isinstance(entity, Chat):
            return True, "", "group"
        return True, "", ""
    except ChannelPrivateError:
        return False, "private", ""
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return False, "not_found", ""
    except Exception:
        return True, "", ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _wl_type_label(wtype: str) -> str:
    return TYPE_LABELS.get(wtype, wtype)


# ─── Клавиатуры ────────────────────────────────────────────

async def get_main_keyboard(user_id: int, is_adm: bool) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📨 Подать обращение"), KeyboardButton(text="💎 Купить подписку")],
        [KeyboardButton(text="📄 Моя подписка"),     KeyboardButton(text="🎟 Промокод")],
        [KeyboardButton(text="📊 Моя статистика"),   KeyboardButton(text="👥 Пригласить друга")],
    ]
    if is_adm:
        buttons.append([KeyboardButton(text="🔧 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def admin_kb(is_superadmin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить админа",      callback_data="admin:add_admin"),
         InlineKeyboardButton(text="➖ Удалить админа",       callback_data="admin:del_admin")],
        [InlineKeyboardButton(text="📂 Управление сессиями",  callback_data="admin:sessions")],
        [InlineKeyboardButton(text="👥 Подписчики",           callback_data="admin:subscribers"),
         InlineKeyboardButton(text="📋 Логи",                 callback_data="admin:logs")],
        [InlineKeyboardButton(text="📢 Обязательные каналы",  callback_data="admin:channels")],
        [InlineKeyboardButton(text="⚙️ Правила",              callback_data="admin:rules"),
         InlineKeyboardButton(text="💎 Цена Premium",         callback_data="admin:premium_price")],
        [InlineKeyboardButton(text="📣 Рассылка",              callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🎁 Выдать подписку",       callback_data="admin:grant_sub"),
         InlineKeyboardButton(text="🎟 Промокоды",             callback_data="admin:promos")],
        [InlineKeyboardButton(text="💰 Тарифы подписок",       callback_data="admin:plans")],
        [InlineKeyboardButton(text="🛡 Белый список",          callback_data="admin:whitelist")],
        [InlineKeyboardButton(text="📊 Группа логов",          callback_data="admin:log_group"),
         InlineKeyboardButton(text="❌ Снять подписку",        callback_data="admin:revoke_sub")],
        [InlineKeyboardButton(text="🚫 Чёрный список",         callback_data="admin:banned"),
         InlineKeyboardButton(text="⭐ Stars тарифы",          callback_data="admin:stars")],
        [InlineKeyboardButton(text="🏆 Топ рефереров",         callback_data="admin:ref_leaderboard")],
        [InlineKeyboardButton(text="📈 Статистика бота",       callback_data="admin:stats")],
        [InlineKeyboardButton(text="📤 Выгрузить базу данных", callback_data="admin:export_db")],
    ]
    if is_superadmin:
        rows.append([InlineKeyboardButton(text="🤖 Резервный бот", callback_data="admin:backup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sessions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Вставить StringSession",    callback_data="sess:upload")],
        [InlineKeyboardButton(text="🔑 Добавить Auth Key (HEX)",   callback_data="sess:authkey")],
        [InlineKeyboardButton(text="📱 Авторизоваться по номеру",  callback_data="sess:phone")],
        [InlineKeyboardButton(text="🗑 Удалить сессию",            callback_data="sess:delete")],
        [InlineKeyboardButton(text="✅ Проверить все сессии",      callback_data="sess:check")],
        [InlineKeyboardButton(text="◀️ Назад",                     callback_data="admin:back")],
    ])


def whitelist_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в белый список", callback_data="wl:add")],
        [InlineKeyboardButton(text="📋 Просмотр",                callback_data="wl:list:0")],
        [InlineKeyboardButton(text="◀️ Назад",                   callback_data="admin:back")],
    ])


def whitelist_type_kb() -> InlineKeyboardMarkup:
    # Deprecated: используйте inline-кнопки с wlsave: вместо этой клавиатуры
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Пользователь", callback_data="wltype:user")],
        [InlineKeyboardButton(text="🤖 Бот",          callback_data="wltype:bot")],
        [InlineKeyboardButton(text="📢 Канал",        callback_data="wltype:channel")],
        [InlineKeyboardButton(text="👥 Группа",       callback_data="wltype:group")],
        [InlineKeyboardButton(text="◀️ Отмена",       callback_data="admin:whitelist")],
    ])


async def _send_to_log_group(bot: Bot, text: str):
    """Отправляет сообщение в настроенную группу логов."""
    group_id = await db.get_setting("log_group_id")
    if not group_id:
        return
    try:
        await bot.send_message(int(group_id), text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"log_group send error: {e}")


# ─── CryptoBot ─────────────────────────────────────────────

async def create_invoice(amount: float, days: int, user_id: int) -> dict:
    desc = "Подписка навсегда" if days == 0 else f"Подписка на {days} дней"
    label = f"sub_{user_id}_{days}_{int(datetime.now().timestamp())}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json={"asset": "USDT", "amount": str(amount), "description": desc, "payload": label}
        ) as resp:
            data = await resp.json()
    if data.get("ok"):
        inv = data["result"]
        return {"invoice_id": str(inv["invoice_id"]), "pay_url": inv["pay_url"]}
    raise Exception(f"CryptoBot ошибка: {data}")


async def poll_payments(bot: Bot):
    while True:
        await asyncio.sleep(30)
        try:
            pending = await db.get_pending_payments()
            if not pending:
                continue
            ids = ",".join(p["crypto_invoice_id"] for p in pending)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://pay.crypt.bot/api/getInvoices",
                    headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                    params={"invoice_ids": ids}
                ) as resp:
                    data = await resp.json()
            if not data.get("ok"):
                continue
            for inv in data["result"].get("items", []):
                if inv["status"] == "paid":
                    inv_id = str(inv["invoice_id"])
                    payment = await db.get_payment_by_invoice(inv_id)
                    if payment and payment["status"] == "pending":
                        await db.update_payment_status(inv_id, "paid")

                        # Premium оплата
                        if payment.get("payment_type") == "premium":
                            await db.activate_premium(payment["user_id"])
                            try:
                                await bot.send_message(
                                    payment["user_id"],
                                    "💎 <b>Premium активирован!</b>\n\n"
                                    "✅ Теперь вам доступны жалобы на каналы, группы и ботов.\n"
                                    "⚠️ Premium активен пока действует ваша обычная подписка.",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                            continue

                        # Обычная оплата
                        await db.activate_subscription(payment["user_id"], payment["duration_days"])
                        label = "навсегда" if payment["duration_days"] == 0 else f"на {payment['duration_days']} дней"
                        try:
                            await bot.send_message(
                                payment["user_id"],
                                f"✅ <b>Оплата подтверждена!</b>\n\n"
                                f"🎉 Подписка активирована {label}.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                        # Реферальный бонус: +1 день обоим при первой оплате >= 1 дня
                        if payment["duration_days"] >= 1:
                            referrer_id = await db.get_referrer_for_payment_bonus(payment["user_id"])
                            if referrer_id:
                                await db.mark_referral_bonus_given(payment["user_id"])
                                # +1 день рефералу (тому кто купил)
                                ref_end = await db.grant_subscription(payment["user_id"], REFERRAL_BONUS_DAYS)
                                try:
                                    ref_date = ref_end.strftime("%d.%m.%Y") if ref_end else "навсегда"
                                    await bot.send_message(
                                        payment["user_id"],
                                        f"🎁 <b>+{REFERRAL_BONUS_DAYS} день подписки в подарок!</b>\n\n"
                                        f"Вы зарегистрировались по реферальной ссылке — при первой покупке вам начислен бонусный день.\n"
                                        f"📅 Ваша подписка продлена до: <b>{ref_date}</b>",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
                                # +1 день тому кто пригласил
                                referrer_end = await db.grant_subscription(referrer_id, REFERRAL_BONUS_DAYS)
                                try:
                                    referrer_date = referrer_end.strftime("%d.%m.%Y") if referrer_end else "навсегда"
                                    await bot.send_message(
                                        referrer_id,
                                        f"🎉 <b>+{REFERRAL_BONUS_DAYS} день подписки!</b>\n\n"
                                        f"Ваш реферал впервые оплатил подписку.\n"
                                        f"📅 Ваша подписка продлена до: <b>{referrer_date}</b>",
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    pass
        except Exception as e:
            logger.error(f"poll_payments: {e}")


async def _check_sessions_with_timeout() -> tuple[list[int], list[str]]:
    """Проверяет все сессии с таймаутом. Возвращает (bad_ids, status_strings)."""
    sessions = await db.get_all_sessions()
    bad: list[int] = []
    for s in sessions:
        client = None
        try:
            client = TelegramClient(StringSession(s["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await asyncio.wait_for(client.connect(), timeout=15.0)
            ok = await asyncio.wait_for(client.is_user_authorized(), timeout=10.0)
            if ok:
                me = await asyncio.wait_for(client.get_me(), timeout=10.0)
                name = f"@{me.username}" if me.username else str(me.id)
                sp = int(s.get("success_reports", 0) or 0)
                tp = int(s.get("total_reports", 0) or 0)
                rate_str = f" ({round(sp/tp*100)}%)" if tp > 0 else ""
                _session_status[s["id"]] = f"✅ {name}{rate_str}"
            else:
                _session_status[s["id"]] = "❌ не авторизован"
                bad.append(s["id"])
        except asyncio.TimeoutError:
            _session_status[s["id"]] = "⏱ таймаут"
            bad.append(s["id"])
        except Exception as e:
            _session_status[s["id"]] = f"⚠️ {str(e)[:30]}"
            bad.append(s["id"])
        finally:
            if client:
                try: await client.disconnect()
                except Exception: pass
        await asyncio.sleep(0.3)
    return bad, list(_session_status.values())


async def hourly_session_check(bot: Bot):
    while True:
        await asyncio.sleep(3600)
        try:
            bad, _ = await _check_sessions_with_timeout()
            if bad:
                for adm in await db.get_all_admins():
                    try:
                        await bot.send_message(
                            adm["user_id"],
                            f"⚠️ Проблемные сессии (ID): {', '.join(map(str, bad))}\n"
                            "Удалите их в Админ панель → Управление сессиями."
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"hourly_session_check: {e}")


async def startup_session_check(bot: Bot):
    """Однократная проверка всех сессий при старте бота."""
    await asyncio.sleep(5)
    try:
        sessions = await db.get_all_sessions()
        if not sessions:
            return
        logger.info(f"Startup: проверяю {len(sessions)} сессий...")
        bad, _ = await _check_sessions_with_timeout()
        good = len(sessions) - len(bad)
        logger.info(f"Startup: сессий ✅ {good} / ❌ {len(bad)}")
        if bad:
            for adm in await db.get_all_admins():
                try:
                    await bot.send_message(
                        adm["user_id"],
                        f"🚀 <b>Бот запущен.</b>\n\n"
                        f"📋 Сессий проверено: {len(sessions)}\n"
                        f"✅ Рабочих: {good} | ❌ Проблемных: {len(bad)}\n\n"
                        f"Проблемные ID: {', '.join(map(str, bad))}\n"
                        f"Удалите их: Админ панель → Управление сессиями.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"startup_session_check: {e}")


async def daily_backup_reminder(bot: Bot):
    while True:
        try:
            now = datetime.now(timezone.utc)
            next_run = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - now).total_seconds())
            if not BACKUP_BOT_USERNAME:
                continue
            users = await db.get_all_users()
            text = (
                f"ℹ️ <b>Напоминание</b>\n\nЕсли основной бот недоступен, используйте резервный:\n"
                f"👉 @{BACKUP_BOT_USERNAME}\n\nРезервный бот имеет те же функции и подписку."
            )
            for uid in users:
                try:
                    await bot.send_message(uid, text, parse_mode="HTML")
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"daily_backup_reminder: {e}")
            await asyncio.sleep(3600)


async def daily_sub_reminder(bot: Bot):
    """Уведомляет пользователей об истечении подписки за 3 дня и за 1 день."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            next_run = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - now).total_seconds())

            today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            last_run = await db.get_setting("sub_reminder_last_run")
            if last_run == today_key:
                await asyncio.sleep(3600)
                continue
            await db.set_setting("sub_reminder_last_run", today_key)

            for days_ahead in [3, 1]:
                expiring = await db.get_expiring_subscriptions(days_ahead)
                for user in expiring:
                    try:
                        days_text = "3 дня" if days_ahead == 3 else "1 день"
                        await bot.send_message(
                            user["user_id"],
                            f"⚠️ <b>Подписка истекает через {days_text}!</b>\n\n"
                            f"📅 Дата окончания: {str(user['subscription_end'])[:10]}\n\n"
                            f"Продлите подписку, чтобы не потерять доступ к функциям бота.",
                            parse_mode="HTML",
                            reply_markup=await _buy_keyboard()
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"daily_sub_reminder: {e}")
            await asyncio.sleep(3600)


# ─── /start ────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = message.from_user
    is_new_user = await db.get_user(u.id) is None
    await db.upsert_user(u.id, u.username or "", u.first_name or "")
    if u.id in SUPERADMIN_IDS:
        await db.set_admin(u.id, True)

    # Обработка реферального кода из /start ref_XXXXXXX
    args = message.text.split(maxsplit=1)[1] if message.text and len(message.text.split()) > 1 else ""
    if args.startswith("ref_") and is_new_user:
        ref_code = args[4:]
        referrer = await db.get_user_by_ref_code(ref_code)
        if referrer and referrer["user_id"] != u.id and not await db.has_been_referred(u.id):
            # Только записываем реферала. Бонус (+1 день) выдаётся обоим
            # при первой оплате реферала минимум на 1 день (в poll_payments).
            await db.add_referral(referrer["user_id"], u.id, REFERRAL_BONUS_DAYS)

    # Капча для новых пользователей
    if u.id not in SUPERADMIN_IDS and not await db.is_admin(u.id):
        if not await db.has_captcha_confirmed(u.id):
            await message.answer(
                f"👋 Привет, <b>{u.first_name}</b>!\n\n"
                f"Для продолжения подтвердите, что вы не бот:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я не бот, продолжить", callback_data="captcha:confirm")]
                ])
            )
            return

    is_adm = await db.is_admin(u.id) or u.id in SUPERADMIN_IDS
    has_sub = is_adm or await db.has_active_subscription(u.id)
    has_prem = is_adm or await db.has_active_premium(u.id)
    not_ch = [] if is_adm else await check_channels(bot, u.id)

    if is_adm:
        status_line = "👑 <b>Администратор</b> — доступ неограничен"
    elif has_sub and has_prem:
        status_line = "✅ <b>Подписка активна</b>  •  💎 <b>Premium</b>"
    elif has_sub:
        status_line = "✅ <b>Подписка активна</b>"
    else:
        status_line = "⚠️ <b>Нет подписки</b> — нажмите 💎 Купить подписку"

    ch_text = ("\n\n📢 <b>Подпишитесь на каналы:</b>\n" + "\n".join(f"• @{c['channel_username']}" for c in not_ch)) if not_ch else ""

    rules_url = await db.get_setting("rules_url")
    rules_kb = None
    if rules_url:
        rules_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📜 Правила использования", url=rules_url)]
        ])

    await message.answer(
        f"👋 Привет, <b>{u.first_name}</b>!\n\n"
        f"🛡 <b>Сервис верификации и модерации контента</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📨  Жалобы на сообщения\n"
        f"📢  Жалобы на каналы и группы  <i>[Premium]</i>\n"
        f"🤖  Жалобы на ботов  <i>[Premium]</i>\n"
        f"👥  Реферальная программа\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_line}{ch_text}",
        parse_mode="HTML",
        reply_markup=await get_main_keyboard(u.id, is_adm)
    )
    if rules_kb:
        await message.answer("📜 Ознакомьтесь с правилами перед использованием:", reply_markup=rules_kb)


@router.callback_query(F.data == "my:history")
async def cb_my_history(call: CallbackQuery):
    uid = call.from_user.id
    history = await db.get_user_report_history(uid, limit=7)
    if not history:
        await call.answer("📭 История обращений пуста", show_alert=True); return
    icons = {"message": "💬", "channel": "📢", "group": "👥", "bot": "🤖", "user": "👤"}
    lines = []
    for r in history:
        icon = icons.get(r["rtype"], "📨")
        target = r["target"] or "—"
        rid = r.get("report_id") or "—"
        reason = r.get("reason_name") or "—"
        ok = r["success_count"]
        tot = r["total_count"]
        dt = r["report_time"][:10] if r.get("report_time") else ""
        lines.append(
            f"{icon} <code>{target}</code>  {dt}\n"
            f"   📌 {reason}  •  ✅{ok}/{tot}  •  <code>{rid}</code>"
        )
    await call.message.edit_text(
        f"📋 <b>История обращений</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="my:stats_back")]
        ])
    )
    await call.answer()


@router.callback_query(F.data == "my:trending")
async def cb_my_trending(call: CallbackQuery):
    rows = await db.get_top_reported_targets(10)
    if not rows:
        await call.answer("🔥 Пока нет данных о жалобах", show_alert=True); return
    lines = []
    medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 10
    for i, r in enumerate(rows):
        target = r.get("target") or "—"
        cnt = r.get("report_count", 0)
        lines.append(f"{medals[i]} <code>{target}</code>  — <b>{cnt}</b> жалоб")
    await call.message.edit_text(
        "🔥 <b>Горячие цели</b>\n<i>Чаще всего жалуются на:</i>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="my:stats_back")]
        ])
    )
    await call.answer()


@router.callback_query(F.data == "my:stats_back")
async def cb_my_stats_back(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


@router.message(Command("trending"))
async def cmd_trending(message: Message):
    rows = await db.get_top_reported_targets(10)
    if not rows:
        await message.answer("🔥 Пока нет данных о жалобах."); return
    lines = []
    medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 10
    for i, r in enumerate(rows):
        target = r.get("target") or "—"
        cnt = r.get("report_count", 0)
        lines.append(f"{medals[i]} <code>{target}</code>  — <b>{cnt}</b> жалоб")
    await message.answer(
        "🔥 <b>Горячие цели</b>\n<i>Топ объектов по количеству жалоб:</i>\n\n" + "\n".join(lines),
        parse_mode="HTML"
    )


@router.message(Command("myreports"))
async def cmd_myreports(message: Message):
    uid = message.from_user.id
    history = await db.get_user_report_history(uid, limit=7)
    if not history:
        await message.answer("📭 Вы ещё не подавали ни одного обращения."); return
    icons = {"message": "💬", "channel": "📢", "group": "👥", "bot": "🤖", "user": "👤"}
    lines = []
    for r in history:
        icon = icons.get(r["rtype"], "📨")
        target = r["target"] or "—"
        rid = r.get("report_id") or "—"
        reason = r.get("reason_name") or "—"
        ok = r["success_count"]
        tot = r["total_count"]
        dt = r["report_time"][:10] if r.get("report_time") else ""
        lines.append(
            f"{icon} <code>{target}</code>  {dt}\n"
            f"   📌 {reason}  •  ✅{ok}/{tot}  •  <code>{rid}</code>"
        )
    await message.answer(
        f"📋 <b>Мои обращения (последние {len(history)})</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML"
    )


@router.message(Command("support"))
async def cmd_support(message: Message):
    links = " | ".join(f'<a href="tg://user?id={sid}">{sid}</a>' for sid in SUPERADMIN_IDS)
    await message.answer(f"📞 Супер-администраторы: {links}", parse_mode="HTML")


# ─── Личная статистика ──────────────────────────────────────

@router.message(F.text == "📊 Моя статистика")
async def btn_my_stats(message: Message):
    uid = message.from_user.id
    stats = await db.get_user_stats(uid)
    ref_count = await db.get_referral_count(uid)
    history = await db.get_user_report_history(uid, limit=30)
    total = stats["total"]

    has_prem = await db.has_active_premium(uid)
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS

    # Считаем реальный процент успеха
    total_ok = stats["msg_success"] + stats["peer_success"]
    total_sent_sessions = sum(r["total_count"] for r in history) if history else 0
    pct = round(total_ok / total_sent_sessions * 100) if total_sent_sessions > 0 else 0

    # Серия (streak) — сколько дней подряд были репорты
    if history:
        dates = sorted({r["report_time"][:10] for r in history if r.get("report_time")}, reverse=True)
        streak = 1
        from datetime import date
        for i in range(1, len(dates)):
            d1 = date.fromisoformat(dates[i - 1])
            d2 = date.fromisoformat(dates[i])
            if (d1 - d2).days == 1:
                streak += 1
            else:
                break
        streak_str = f"🔥 Серия активности: <b>{streak} дн.</b>\n" if streak > 1 else ""
    else:
        streak_str = ""

    # Бейджи за достижения
    badges = []
    if total >= 1:   badges.append("📨 Первое обращение")
    if total >= 10:  badges.append("🎯 10 жалоб")
    if total >= 50:  badges.append("⚡ 50 жалоб")
    if total >= 100: badges.append("💪 100 жалоб")
    if ref_count >= 1:  badges.append("👥 Реферер")
    if ref_count >= 5:  badges.append("🌟 Топ-реферер")
    if has_prem:         badges.append("💎 Premium")
    if is_adm:           badges.append("🛡 Админ")
    badges_str = ("🏆 <b>Значки:</b> " + "  ".join(badges) + "\n") if badges else ""

    if total == 0:
        body = "📭 Вы ещё не подавали обращений.\n\nПерешлите сообщение из любого канала или нажмите <b>📨 Подать обращение</b>."
    else:
        bar_filled = round(pct / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        body = (
            f"💬 На сообщения: <b>{stats['msg_count']}</b>\n"
            f"📢 На каналы / группы / ботов: <b>{stats['peer_count']}</b>\n"
            f"📊 Всего: <b>{total}</b>\n"
            f"✅ Успешность: [{bar}] <b>{pct}%</b>\n"
            f"{streak_str}"
        )

    await message.answer(
        f"📊 <b>Ваша статистика</b>\n\n"
        f"{badges_str}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{body}"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Приглашено друзей: <b>{ref_count}</b> чел.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 История обращений", callback_data="my:history"),
             InlineKeyboardButton(text="🔥 Горячие цели",      callback_data="my:trending")],
        ])
    )


# ─── Реферальная система ────────────────────────────────────

@router.message(F.text == "👥 Пригласить друга")
async def btn_referral(message: Message):
    uid = message.from_user.id
    code = await db.get_or_create_ref_code(uid)
    ref_count = await db.get_referral_count(uid)
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{code}"
    await message.answer(
        f"👥 <b>Реферальная программа</b>\n\n"
        f"🎁 <b>Как работает бонус:</b>\n"
        f"1. Ваш друг переходит по ссылке и регистрируется\n"
        f"2. Друг покупает подписку <b>минимум на 1 день</b>\n"
        f"3. <b>Оба</b> автоматически получают <b>+{REFERRAL_BONUS_DAYS} день</b> подписки бесплатно!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n<code>{ref_link}</code>\n\n"
        f"📌 Ссылка работает только для <b>новых</b> пользователей\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>Приглашено друзей:</b> {ref_count} чел.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой",
                                  url=f"https://t.me/share/url?url={ref_link}&text=🔥+Крутой+сервис+для+верификации+контента%21+Регистрируйся+по+моей+ссылке:")]])
    )


# ─── Подписка / покупка ─────────────────────────────────────

@router.message(F.text == "📜 Правила")
async def btn_rules(message: Message):
    url = await db.get_setting("rules_url")
    if not url:
        await message.answer("❌ Правила не заданы."); return
    text = await db.get_setting("rules_text") or "Правила использования бота:"
    await message.answer(f"📜 {text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Открыть", url=url)]]))


@router.message(F.text == "📄 Моя подписка")
async def btn_my_sub(message: Message):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 <b>Администратор</b>\n\n♾️ Подписка бессрочная\n💎 Premium доступ включён", parse_mode="HTML"); return
    user = await db.get_user(uid)
    if not user:
        await message.answer("Напишите /start"); return

    has_prem = await db.has_active_premium(uid)
    prem_badge = "\n💎 <b>Premium:</b> активен ✅" if has_prem else "\n💎 <b>Premium:</b> не активен"

    if user.get("subscription_lifetime"):
        await message.answer(
            f"♾️ <b>Бессрочная подписка</b>\n\n"
            f"✅ Доступ: навсегда{prem_badge}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💎 Купить Premium", callback_data="buy_premium")]
            ]) if not has_prem else None
        )
    elif user.get("subscription_end"):
        try:
            end = datetime.fromisoformat(user["subscription_end"]).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if end > now:
                days_left = (end - now).days
                hours_left = int((end - now).total_seconds() // 3600)
                if days_left >= 1:
                    time_str = f"{days_left} дн."
                else:
                    time_str = f"{hours_left} ч."
                # Progress bar (max 30 days visual)
                filled = min(10, max(1, days_left // 3))
                bar = "█" * filled + "░" * (10 - filled)
                await message.answer(
                    f"✅ <b>Подписка активна</b>\n\n"
                    f"📅 До: <b>{end.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
                    f"⏳ Осталось: <b>{time_str}</b>\n"
                    f"[{bar}]{prem_badge}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💎 Купить Premium", callback_data="buy_premium")]
                    ]) if not has_prem else None
                )
            else:
                await message.answer(
                    f"❌ <b>Подписка истекла</b>\n\nОформите новую подписку для продолжения работы.",
                    parse_mode="HTML",
                    reply_markup=await _buy_keyboard()
                )
        except Exception:
            await message.answer("❌ Ошибка данных.")
    else:
        await message.answer(
            "❌ <b>Нет активной подписки</b>\n\nВыберите тариф ниже:",
            parse_mode="HTML",
            reply_markup=await _buy_keyboard()
        )


async def _buy_keyboard() -> InlineKeyboardMarkup:
    plans = await db.get_subscription_plans()
    if not plans:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 30 дней — 10 USD",  callback_data="buy:30:10.0")],
            [InlineKeyboardButton(text="♾️ Навсегда — 100 USD", callback_data="buy:0:100.0")],
        ])
    rows = []
    for p in plans:
        days_str = '♾️ Навсегда' if p['days'] == 0 else f"📅 {p['days']} дней"
        label = f"{days_str} — {p['price']:.0f} USD"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:{p['days']}:{p['price']}")])
        sp = p.get("stars_price", 0) or 0
        if sp > 0:
            rows.append([InlineKeyboardButton(
                text=f"⭐ {days_str} — {sp} Stars",
                callback_data=f"buy_stars:{p['id']}"
            )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "💎 Купить подписку")
async def btn_buy(message: Message, bot: Bot):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 Вы администратор — подписка уже бессрочная!"); return
    not_ch = await check_channels(bot, uid)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await message.answer(f"📢 Сначала подпишитесь:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]])); return
    await message.answer("💎 Выберите тариф:", reply_markup=await _buy_keyboard())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    parts = call.data.split(":")
    days = int(parts[1])
    amount = float(parts[2])
    await call.message.edit_text("⏳ Создаю счёт...")
    try:
        inv = await create_invoice(amount, days, call.from_user.id)
        await db.add_payment(call.from_user.id, amount, days, inv["invoice_id"])
        label = "навсегда" if days == 0 else f"{days} дней"
        await call.message.edit_text(
            f"💳 Счёт создан!\n💰 <b>{amount:.0f} USDT</b> — {label}\n\n"
            f"После оплаты подписка активируется автоматически (до 30 сек).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {amount:.0f} USDT", url=inv["pay_url"])]
            ]))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")
    await call.answer()


@router.callback_query(F.data == "check_channels")
async def cb_check_channels(call: CallbackQuery, bot: Bot):
    not_ch = await check_channels(bot, call.from_user.id)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await call.message.edit_text(f"❌ Ещё не подписаны:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Снова", callback_data="check_channels")]]))
    else:
        await call.message.edit_text("✅ Отлично! Все каналы подписаны.")
    await call.answer()


# ─── Подача обращения ───────────────────────────────────────

@router.message(F.text == "📨 Подать обращение")
async def btn_report(message: Message, bot: Bot, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    if not is_adm:
        banned = await db.get_banned_user(uid)
        if banned:
            reason = banned.get("reason") or "нарушение правил"
            await message.answer(
                f"🚫 <b>Ваш аккаунт заблокирован.</b>\n\nПричина: {reason}\n\nОбратитесь к администратору.",
                parse_mode="HTML"
            ); return
        if not await db.has_captcha_confirmed(uid):
            await message.answer("⚠️ Нажмите /start для подтверждения."); return
        now = datetime.now(timezone.utc)
        last = user_last_report.get(uid)
        if last and (now - last).total_seconds() < REPORT_COOLDOWN_SECONDS:
            remain = int(REPORT_COOLDOWN_SECONDS - (now - last).total_seconds())
            mins, secs = divmod(remain, 60)
            await message.answer(
                f"⏳ <b>Кулдаун</b>\n\n"
                f"Следующее обращение (любого типа) доступно через "
                f"<b>{mins} мин. {secs} сек.</b>\n\n"
                f"⏱ Ограничение 30 минут распространяется на все жалобы: "
                f"сообщения, каналы, группы и боты.",
                parse_mode="HTML"); return
        if not await db.has_active_subscription(uid):
            await message.answer("❌ Нет подписки:", reply_markup=await _buy_keyboard()); return
        not_ch = await check_channels(bot, uid)
        if not_ch:
            names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
            await message.answer(f"📢 Подпишитесь:\n{names}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]])); return
    await state.set_state(States.waiting_report_type)
    await message.answer(
        "📨 <b>Подать обращение</b>\n\nЧто вы хотите обжаловать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Сообщение / пост", callback_data="rtype:message")],
            [InlineKeyboardButton(text="📢 Канал",            callback_data="rtype:channel")],
            [InlineKeyboardButton(text="👥 Группа",           callback_data="rtype:group")],
            [InlineKeyboardButton(text="🤖 Бот",              callback_data="rtype:bot")],
        ])
    )


@router.message(F.forward_from_chat | F.forward_origin)
async def handle_forwarded_global(message: Message, bot: Bot, state: FSMContext):
    """Перехват любого пересланного сообщения — предлагаем быструю жалобу."""
    uid = message.from_user.id
    chat_id, msg_id, is_private = _extract_forward(message)
    if not chat_id:
        return  # не из канала/группы — игнорируем

    # Узнаём имя исходника для кнопки
    fc = getattr(message, "forward_from_chat", None)
    src_title = (getattr(fc, "title", None) or getattr(fc, "username", None) or chat_id) if fc else chat_id

    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    if not is_adm:
        banned = await db.get_banned_user(uid)
        if banned:
            reason = banned.get("reason") or "нарушение правил"
            await message.answer(
                f"🚫 <b>Аккаунт заблокирован.</b>\nПричина: {reason}",
                parse_mode="HTML"
            ); return
        if not await db.has_active_subscription(uid):
            await message.answer("❌ Для подачи обращения нужна подписка.",
                                  reply_markup=await _buy_keyboard()); return
        now = datetime.now(timezone.utc)
        last = user_last_report.get(uid)
        if last and (now - last).total_seconds() < REPORT_COOLDOWN_SECONDS:
            remain = int(REPORT_COOLDOWN_SECONDS - (now - last).total_seconds())
            mins, secs = divmod(remain, 60)
            await message.answer(
                f"⏳ Кулдаун: повторите через <b>{mins} мин. {secs} сек.</b>",
                parse_mode="HTML"); return

    await state.update_data(chat_id=chat_id, message_id=msg_id)
    await state.set_state(States.waiting_report_reason)
    await message.answer(
        f"⚡ <b>Быстрая жалоба</b>\n\n"
        f"📌 Источник: <b>{src_title}</b>\n"
        f"🔗 Пост: <code>{msg_id}</code>\n\n"
        f"Выберите причину:",
        parse_mode="HTML",
        reply_markup=_reason_keyboard()
    )


@router.callback_query(F.data.startswith("rtype:"), States.waiting_report_type)
async def cb_report_type(call: CallbackQuery, state: FSMContext):
    rtype = call.data.split(":")[1]
    uid = call.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS

    # Premium check: всё кроме жалоб на сообщения требует Premium
    if rtype != "message" and not is_adm:
        if not await db.has_active_premium(uid):
            premium_price = await db.get_setting("premium_price") or "15"
            has_sub = await db.has_active_subscription(uid)
            if has_sub:
                note = f"💰 Стоимость: <b>{premium_price} USDT</b> — навсегда (одноразовая покупка)"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"💎 Купить Premium — {premium_price} USDT", callback_data="buy_premium")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_report_type")],
                ])
            else:
                note = "⚠️ Для покупки Premium сначала нужна активная обычная подписка."
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Купить подписку", callback_data="goto_buy_sub")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_report_type")],
                ])
            icon = {"channel": "📢", "group": "👥", "bot": "🤖"}.get(rtype, "📢")
            type_ru = {"channel": "каналы", "group": "группы", "bot": "ботов"}.get(rtype, "объекты")
            await call.message.edit_text(
                f"{icon} <b>Жалобы на {type_ru} — только Premium</b>\n\n"
                f"Этот тип обращений доступен только для пользователей с Premium подпиской.\n\n"
                f"{note}\n\n"
                f"<i>Premium не отключается — покупается один раз навсегда (пока активна обычная подписка)</i>",
                parse_mode="HTML",
                reply_markup=kb
            )
            await call.answer()
            return

    await state.update_data(peer_type=rtype)
    if rtype == "message":
        await state.set_state(States.waiting_report_link)
        await call.message.edit_text(
            "🔗 <b>Жалоба на сообщение</b>\n\n"
            "Отправьте ссылку на публикацию:\n"
            "• <code>https://t.me/username/123</code>\n\n"
            "⚠️ Ссылки на приватные каналы (<code>t.me/c/...</code>) не принимаются.",
            parse_mode="HTML")
    elif rtype == "channel":
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "📢 <b>Жалоба на канал</b>\n\n"
            "Отправьте @юзернейм или ссылку:\n"
            "• <code>@channame</code>\n• <code>https://t.me/channame</code>\n\n"
            "ℹ️ Тип объекта будет определён автоматически.",
            parse_mode="HTML")
    elif rtype == "group":
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "👥 <b>Жалоба на группу</b>\n\n"
            "Отправьте @юзернейм или ссылку:\n"
            "• <code>@mygroup</code>\n• <code>https://t.me/mygroup</code>\n\n"
            "ℹ️ Тип объекта будет определён автоматически.",
            parse_mode="HTML")
    else:
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "🤖 <b>Жалоба на бота</b>\n\n"
            "Отправьте @юзернейм бота:\n• <code>@somebot</code>\n\n"
            "ℹ️ Тип объекта будет определён автоматически.",
            parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "back_to_report_type")
async def cb_back_to_report_type(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_report_type)
    await call.message.edit_text(
        "📨 <b>Подать обращение</b>\n\nЧто вы хотите обжаловать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Сообщение / пост", callback_data="rtype:message")],
            [InlineKeyboardButton(text="📢 Канал",            callback_data="rtype:channel")],
            [InlineKeyboardButton(text="👥 Группа",           callback_data="rtype:group")],
            [InlineKeyboardButton(text="🤖 Бот",              callback_data="rtype:bot")],
        ])
    )
    await call.answer()


@router.callback_query(F.data == "goto_buy_sub")
async def cb_goto_buy_sub(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("💎 Выберите тариф подписки:", reply_markup=await _buy_keyboard())
    await call.answer()


@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(call: CallbackQuery):
    uid = call.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await call.answer("👑 У администраторов Premium уже активен!", show_alert=True); return
    if not await db.has_active_subscription(uid):
        await call.answer("❌ Сначала приобретите обычную подписку!", show_alert=True); return
    if await db.has_active_premium(uid):
        await call.answer("✅ Premium уже активен!", show_alert=True); return

    premium_price_str = await db.get_setting("premium_price") or "15"
    try:
        premium_price = float(premium_price_str)
    except ValueError:
        premium_price = 15.0

    await call.message.edit_text("⏳ Создаю счёт на Premium...")
    try:
        inv = await create_invoice(premium_price, 0, uid)
        await db.add_premium_payment(uid, premium_price, inv["invoice_id"])
        await call.message.edit_text(
            f"💎 <b>Premium подписка</b>\n\n"
            f"💰 Стоимость: <b>{premium_price:.0f} USDT</b> — навсегда\n\n"
            f"✅ После оплаты Premium активируется автоматически.\n"
            f"⚠️ Premium работает пока активна обычная подписка. При истечении обычной — Premium приостанавливается и возобновляется при продлении.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {premium_price:.0f} USDT", url=inv["pay_url"])]
            ]))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка создания счёта: {e}")
    await call.answer()


def _extract_forward(message: Message):
    """Вернуть (chat_id_str, msg_id_str, is_private) из пересланного сообщения или (None, None, False)."""
    fc = getattr(message, "forward_from_chat", None)
    fmid = getattr(message, "forward_from_message_id", None)
    if fc and fmid:
        username = getattr(fc, "username", None)
        if username:
            return username, str(fmid), False
        # приватный канал / группа — числовой ID
        return str(fc.id), str(fmid), True
    return None, None, False


@router.message(States.waiting_report_link)
async def got_link(message: Message, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS

    # Пересланное сообщение — извлекаем автоматически
    if message.forward_from_chat or message.forward_origin:
        chat_id, msg_id, is_private = _extract_forward(message)
        if chat_id:
            await message.answer("🔗 Ссылка извлечена из пересланного сообщения ✅")
        else:
            await message.answer("❌ Не удалось извлечь источник пересланного сообщения. Отправьте ссылку вручную."); return
    else:
        if not message.text:
            await message.answer("❌ Отправьте ссылку на публикацию или перешлите сообщение."); return
        chat_id, msg_id, is_private = parse_tg_link(message.text.strip())

    if not chat_id:
        await message.answer("❌ Неверный формат. Пример: https://t.me/username/123"); return

    if is_private and not is_adm:
        await state.clear()
        await message.answer(
            "⛔ <b>Обращение отклонено.</b>\n\n"
            "Репорты на приватные каналы и группы не принимаются.\n"
            "Подавайте обращения только на публичные каналы с @юзернеймом.",
            parse_mode="HTML",
            reply_markup=await get_main_keyboard(uid, False)); return

    # Проверка белого списка
    wl_entry = await db.is_whitelisted(chat_id)
    if wl_entry and not is_adm:
        await state.clear()
        await message.answer(
            f"⛔ <b>Обращение отклонено.</b>\n\n"
            f"Публикация из <b>@{wl_entry['target']}</b> находится в белом списке.",
            parse_mode="HTML",
            reply_markup=await get_main_keyboard(uid, False)); return

    # Проверка повторного обращения
    if await db.has_reported_before(uid, chat_id, msg_id):
        if is_adm:
            await message.answer("⚠️ Повторное обращение — для администраторов разрешено.")
        else:
            await db.revoke_subscription(uid)
            await state.clear()
            await message.answer(
                "❌ <b>Повторное обращение на ту же публикацию недопустимо.</b>\n\n"
                "Ваша подписка аннулирована. Оформите новую подписку для продолжения.",
                parse_mode="HTML",
                reply_markup=await get_main_keyboard(uid, False)); return

    await state.update_data(chat_id=chat_id, message_id=msg_id)
    await state.set_state(States.waiting_report_reason)
    await message.answer(
        f"📋 <b>Причина обращения</b>\n\n"
        f"🔗 <code>t.me/{chat_id}/{msg_id}</code>\n\n"
        f"Выберите категорию нарушения:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",               callback_data="reason:spam"),
             InlineKeyboardButton(text="🔪 Насилие",            callback_data="reason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",        callback_data="reason:porn"),
             InlineKeyboardButton(text="©️ Авт. права",         callback_data="reason:copyright")],
            [InlineKeyboardButton(text="🎭 Фейк",               callback_data="reason:fake"),
             InlineKeyboardButton(text="❓ Другое",             callback_data="reason:other")],
            [InlineKeyboardButton(text="✏️ Описать своими словами", callback_data="reason:custom")],
        ]))


@router.callback_query(F.data == "reason:custom", States.waiting_report_reason)
async def cb_reason_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_custom_text)
    await call.message.edit_text(
        "✏️ <b>Опишите нарушение своими словами</b>\n\n"
        "<i>Например: «Публикует персональные данные», «Призывает к насилию»</i>\n\n"
        "Максимум 512 символов:",
        parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_custom_text)
async def got_custom_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым."); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512."); return
    data = await state.get_data()
    chat_id = data["chat_id"]
    msg_id = data["message_id"]
    await state.update_data(custom_text=text, reason_key="other", reason_name="✏️ Свой текст")
    await state.set_state(States.waiting_confirm)
    await message.answer(
        f"📋 <b>Подтвердите обращение</b>\n\n"
        f"🔗 Публикация: <code>t.me/{chat_id}/{msg_id}</code>\n"
        f"📌 Причина: ✏️ <b>Свой текст</b>\n"
        f"📝 Описание: <i>{text[:120]}{'...' if len(text) > 120 else ''}</i>\n\n"
        f"⚠️ После подтверждения обращение будет отправлено через все сессии верификации.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить жалобу", callback_data="confirm_report:yes")],
            [InlineKeyboardButton(text="❌ Отмена",           callback_data="confirm_report:no")],
        ])
    )


@router.callback_query(F.data.startswith("reason:"), States.waiting_report_reason)
async def cb_reason(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    reason_name, _ = REPORT_REASONS[key]
    data = await state.get_data()
    chat_id = data["chat_id"]
    msg_id = data["message_id"]
    await state.update_data(reason_key=key, reason_name=reason_name)
    await state.set_state(States.waiting_confirm)
    await call.message.edit_text(
        f"📋 <b>Подтвердите обращение</b>\n\n"
        f"🔗 Публикация: <code>t.me/{chat_id}/{msg_id}</code>\n"
        f"📌 Причина: <b>{reason_name}</b>\n\n"
        f"⚠️ После подтверждения жалоба будет отправлена через все сессии верификации.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить жалобу", callback_data="confirm_report:yes")],
            [InlineKeyboardButton(text="◀️ Изменить причину", callback_data="confirm_report:back")],
            [InlineKeyboardButton(text="❌ Отмена",           callback_data="confirm_report:no")],
        ])
    )
    await call.answer()


@router.callback_query(F.data.startswith("confirm_report:"), States.waiting_confirm)
async def cb_confirm_report(call: CallbackQuery, state: FSMContext, bot: Bot):
    action = call.data.split(":")[1]
    uid = call.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS

    if action == "no":
        await state.clear()
        await call.message.edit_text("❌ Обращение отменено.")
        await bot.send_message(uid, "Главное меню:", reply_markup=await get_main_keyboard(uid, is_adm))
        await call.answer(); return

    if action == "back":
        data = await state.get_data()
        chat_id = data.get("chat_id", "")
        msg_id = data.get("message_id", "")
        await state.set_state(States.waiting_report_reason)
        await call.message.edit_text(
            f"📋 <b>Причина обращения</b>\n\n"
            f"🔗 <code>t.me/{chat_id}/{msg_id}</code>\n\n"
            f"Выберите категорию нарушения:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Спам",               callback_data="reason:spam"),
                 InlineKeyboardButton(text="🔪 Насилие",            callback_data="reason:violence")],
                [InlineKeyboardButton(text="🔞 Порнография",        callback_data="reason:porn"),
                 InlineKeyboardButton(text="©️ Авт. права",         callback_data="reason:copyright")],
                [InlineKeyboardButton(text="🎭 Фейк",               callback_data="reason:fake"),
                 InlineKeyboardButton(text="❓ Другое",             callback_data="reason:other")],
                [InlineKeyboardButton(text="✏️ Описать своими словами", callback_data="reason:custom")],
            ])
        )
        await call.answer(); return

    data = await state.get_data()
    await state.clear()
    key = data.get("reason_key", "other")
    reason_name, reason_obj = REPORT_REASONS.get(key, REPORT_REASONS["other"])
    custom_text = data.get("custom_text", "")
    await call.answer()
    await _send_reports(
        bot=bot, user_id=uid,
        chat_id_str=data["chat_id"], msg_id_str=data["message_id"],
        reason_name=reason_name, reason_obj=reason_obj,
        custom_text=custom_text, call=call
    )


# ─── Peer-репорты (канал / бот) ─────────────────────────────

@router.message(States.waiting_peer_username)
async def got_peer_username(message: Message, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    uname = _parse_peer_username(message.text or "")
    if not uname:
        await message.answer("❌ Неверный формат. Отправьте @username или https://t.me/username:"); return

    data = await state.get_data()
    peer_type = data.get("peer_type", "channel")

    # Проверка белого списка
    wl_entry = await db.is_whitelisted(uname)
    if wl_entry and not is_adm:
        await state.clear()
        ttype = {"channel": "Канал", "bot": "Бот", "user": "Пользователь", "group": "Группа"}.get(
            wl_entry.get("target_type", ""), "Объект")
        await message.answer(
            f"⛔ <b>Обращение отклонено.</b>\n\n"
            f"{ttype} <b>@{wl_entry['target']}</b> находится в белом списке.",
            parse_mode="HTML",
            reply_markup=await get_main_keyboard(uid, False)); return

    # Проверка публичности и авто-определение типа через Telethon
    # Проверяем только каналы/группы у не-администраторов (как в оригинале),
    # плюс timeout 10 сек чтобы бот не висел при проблемах с Telethon
    if peer_type in ("channel", "group") and not is_adm:
        wait_msg = await message.answer("🔍 Проверяю доступность...")
        try:
            ok, err, detected_type = await asyncio.wait_for(
                check_peer_accessible(uname), timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            ok, err, detected_type = True, "", ""
        try:
            await wait_msg.delete()
        except Exception:
            pass

        if not ok:
            await state.clear()
            type_label = {"channel": "Канал", "group": "Группа"}.get(peer_type, "Канал/группа")
            if err == "private":
                await message.answer(
                    f"⛔ <b>Обращение отклонено.</b>\n\n"
                    f"{type_label} <b>@{uname}</b> является приватным.\n"
                    f"Репорты принимаются только на публичные объекты.",
                    parse_mode="HTML",
                    reply_markup=await get_main_keyboard(uid, False))
            else:
                await message.answer(
                    f"❌ {type_label} <b>@{uname}</b> не найден(а).",
                    parse_mode="HTML",
                    reply_markup=await get_main_keyboard(uid, False))
            return

        # Если Telethon определил точный тип (канал vs группа) — переопределяем
        if detected_type in ("channel", "group"):
            peer_type = detected_type
            await state.update_data(peer_type=peer_type)

    # Проверка повторного репорта
    if await db.has_peer_reported_before(uid, uname) and not is_adm:
        await db.revoke_subscription(uid)
        await state.clear()
        icon = TYPE_ICONS.get(peer_type, "📢")
        await message.answer(
            f"❌ <b>Повторное обращение на {icon} @{uname} недопустимо.</b>\n\n"
            f"Ваша подписка аннулирована. Оформите новую подписку для продолжения.",
            parse_mode="HTML",
            reply_markup=await get_main_keyboard(uid, False)); return

    await state.update_data(peer_username=uname)
    await state.set_state(States.waiting_peer_reason)
    icon = TYPE_ICONS.get(peer_type, "📢")
    type_label = {"channel": "Канал", "group": "Группа", "bot": "Бот", "user": "Пользователь"}.get(peer_type, "Объект")
    await message.answer(
        f"{icon} <b>{type_label}: <code>@{uname}</code></b>\n\n"
        f"📋 Выберите причину обращения:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",               callback_data="preason:spam"),
             InlineKeyboardButton(text="🔪 Насилие",            callback_data="preason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",        callback_data="preason:porn"),
             InlineKeyboardButton(text="©️ Авт. права",         callback_data="preason:copyright")],
            [InlineKeyboardButton(text="🎭 Фейк",               callback_data="preason:fake"),
             InlineKeyboardButton(text="❓ Другое",             callback_data="preason:other")],
            [InlineKeyboardButton(text="✏️ Описать своими словами", callback_data="preason:custom")],
        ]))


@router.callback_query(F.data == "preason:custom", States.waiting_peer_reason)
async def cb_peer_reason_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_peer_custom)
    await call.message.edit_text(
        "✏️ <b>Опишите нарушение своими словами</b>\n\n"
        "<i>Например: «Распространяет мошеннические схемы», «Нарушает авторские права»</i>\n\n"
        "Максимум 512 символов:",
        parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_peer_custom)
async def got_peer_custom(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым."); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512."); return
    data = await state.get_data()
    peer_username = data.get("peer_username", "")
    peer_type = data.get("peer_type", "channel")
    icon = TYPE_ICONS.get(peer_type, "📢")
    await state.update_data(custom_text=text, reason_key="other", reason_name="✏️ Свой текст")
    await state.set_state(States.waiting_peer_confirm)
    await message.answer(
        f"📋 <b>Подтвердите обращение</b>\n\n"
        f"{icon} Объект: <code>@{peer_username}</code>\n"
        f"📌 Причина: ✏️ <b>Свой текст</b>\n"
        f"📝 Описание: <i>{text[:120]}{'...' if len(text) > 120 else ''}</i>\n\n"
        f"⚠️ После подтверждения жалоба будет отправлена через все сессии верификации.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить жалобу", callback_data="confirm_peer:yes")],
            [InlineKeyboardButton(text="❌ Отмена",           callback_data="confirm_peer:no")],
        ])
    )


@router.callback_query(F.data.startswith("preason:"), States.waiting_peer_reason)
async def cb_peer_reason(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[1]
    reason_name, _ = REPORT_REASONS[key]
    data = await state.get_data()
    peer_username = data.get("peer_username", "")
    peer_type = data.get("peer_type", "channel")
    icon = TYPE_ICONS.get(peer_type, "📢")
    await state.update_data(reason_key=key, reason_name=reason_name)
    await state.set_state(States.waiting_peer_confirm)
    await call.message.edit_text(
        f"📋 <b>Подтвердите обращение</b>\n\n"
        f"{icon} Объект: <code>@{peer_username}</code>\n"
        f"📌 Причина: <b>{reason_name}</b>\n\n"
        f"⚠️ После подтверждения жалоба будет отправлена через все сессии верификации.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить жалобу", callback_data="confirm_peer:yes")],
            [InlineKeyboardButton(text="◀️ Изменить причину", callback_data="confirm_peer:back")],
            [InlineKeyboardButton(text="❌ Отмена",           callback_data="confirm_peer:no")],
        ])
    )
    await call.answer()


@router.callback_query(F.data.startswith("confirm_peer:"), States.waiting_peer_confirm)
async def cb_confirm_peer_report(call: CallbackQuery, state: FSMContext, bot: Bot):
    action = call.data.split(":")[1]
    uid = call.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS

    if action == "no":
        await state.clear()
        await call.message.edit_text("❌ Обращение отменено.")
        await bot.send_message(uid, "Главное меню:", reply_markup=await get_main_keyboard(uid, is_adm))
        await call.answer(); return

    if action == "back":
        data = await state.get_data()
        peer_username = data.get("peer_username", "")
        peer_type = data.get("peer_type", "channel")
        icon = TYPE_ICONS.get(peer_type, "📢")
        await state.set_state(States.waiting_peer_reason)
        await call.message.edit_text(
            f"{icon} <code>@{peer_username}</code>\n\n📋 Выберите причину обращения:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Спам",               callback_data="preason:spam"),
                 InlineKeyboardButton(text="🔪 Насилие",            callback_data="preason:violence")],
                [InlineKeyboardButton(text="🔞 Порнография",        callback_data="preason:porn"),
                 InlineKeyboardButton(text="©️ Авт. права",         callback_data="preason:copyright")],
                [InlineKeyboardButton(text="🎭 Фейк",               callback_data="preason:fake"),
                 InlineKeyboardButton(text="❓ Другое",             callback_data="preason:other")],
                [InlineKeyboardButton(text="✏️ Описать своими словами", callback_data="preason:custom")],
            ])
        )
        await call.answer(); return

    data = await state.get_data()
    await state.clear()
    key = data.get("reason_key", "other")
    reason_name, reason_obj = REPORT_REASONS.get(key, REPORT_REASONS["other"])
    custom_text = data.get("custom_text", "")
    await call.answer()
    await _send_peer_reports(
        bot=bot, user_id=uid,
        peer_username=data["peer_username"], peer_type=data.get("peer_type", "channel"),
        reason_name=reason_name, reason_obj=reason_obj,
        custom_text=custom_text, call=call
    )


# ─── Отправка репортов ──────────────────────────────────────

async def _send_peer_reports(bot: Bot, user_id: int, peer_username: str, peer_type: str,
                              reason_name: str, reason_obj, custom_text: str = "",
                              reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()
    icon = TYPE_ICONS.get(peer_type, "📢")
    type_ru = {"channel": "канал", "group": "группу", "bot": "бота", "user": "пользователя"}.get(peer_type, "объект")
    report_id = _generate_report_id()

    if not sessions:
        txt = "❌ Нет активных сессий. Обратитесь к администратору."
        if call: await call.message.edit_text(txt)
        elif reply_target: await reply_target.answer(txt)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))
        return

    total = len(sessions)
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    if call:
        await call.message.edit_text(
            f"🔄 <b>Обработка жалобы…</b>\n\n"
            f"{icon} <code>@{peer_username}</code>\n"
            f"📌 {reason_name}\n\n"
            f"⏳ Запускаю верификацию через {total} сессий…",
            parse_mode="HTML")
        status_msg = await call.message.answer(f"⏳ Верифицирую… (0/{total})")
    else:
        status_msg = await reply_target.answer(f"⏳ Верифицирую… (0/{total})")

    success = errors = 0
    flood_wait = False
    for i, sess in enumerate(sessions, 1):
        client = None
        _sess_ok = False
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await asyncio.wait_for(client.connect(), timeout=15.0)
            if await asyncio.wait_for(client.is_user_authorized(), timeout=10.0):
                try:
                    # get_entity более надёжен чем get_input_entity для ботов и пользователей
                    entity = await asyncio.wait_for(client.get_entity(peer_username), timeout=10.0)
                    peer = await client.get_input_entity(entity)
                    await asyncio.wait_for(
                        client(ReportPeerRequest(peer=peer, reason=reason_obj, message=custom_text)),
                        timeout=15.0)
                    success += 1
                    _sess_ok = True
                except FloodWaitError as e:
                    logger.warning(f"Peer-сессия {sess['id']} FloodWait {e.seconds}s")
                    flood_wait = True
                    errors += 1
                except Exception as e:
                    logger.warning(f"Peer-сессия {sess['id']}: {type(e).__name__}: {e}")
                    errors += 1
            else:
                errors += 1
        except (asyncio.TimeoutError, Exception) as e:
            logger.error(f"Peer-сессия {sess['id']} подключение: {e}")
            errors += 1
        finally:
            if client:
                try: await client.disconnect()
                except Exception: pass
        await db.increment_session_stats(sess["id"], 1 if _sess_ok else 0, 1)
        await asyncio.sleep(random.uniform(0.4, 0.8))
        spin = spinner[i % len(spinner)]
        try:
            await status_msg.edit_text(
                f"{spin} Верифицирую… ({i}/{total})  ✅{success} ❌{errors}")
        except Exception:
            pass

    await db.add_peer_report_log(user_id, peer_username, peer_type, success, total, report_id, reason_name)
    if not is_adm:
        user_last_report[user_id] = datetime.now(timezone.utc)

    pct = round(success / total * 100) if total > 0 else 0
    bar_filled = round(pct / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    extra = f"\n📝 <i>{custom_text[:80]}{'…' if len(custom_text) > 80 else ''}</i>" if custom_text else ""
    flood_note = "\n\n⚠️ <i>Некоторые аккаунты временно ограничены (FloodWait). Повторите позже для лучшего результата.</i>" if flood_wait else ""
    result_text = (
        f"📊 <b>Обращение обработано</b>\n\n"
        f"{icon} Объект: <code>@{peer_username}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n\n"
        f"[{bar}] {pct}%\n"
        f"✅ Принято: <b>{success}</b> из <b>{total}</b>{flood_note}\n\n"
        f"🆔 ID обращения: <code>{report_id}</code>"
    )
    await status_msg.edit_text(result_text, parse_mode="HTML")

    # Отправка в группу логов
    user_obj = await db.get_user(user_id)
    uname_str = f"@{user_obj['username']}" if user_obj and user_obj.get("username") else f"id:{user_id}"
    log_text = (
        f"{icon} <b>Жалоба на {type_ru}</b>  |  <code>{report_id}</code>\n\n"
        f"👤 От: {uname_str} (<code>{user_id}</code>)\n"
        f"🎯 Цель: <code>@{peer_username}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n"
        f"✅ Принято: <b>{success}</b> / {total}  ({pct}%)"
    )
    await _send_to_log_group(bot, log_text)

    await bot.send_message(user_id, "📨 Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))


async def _send_reports(bot: Bot, user_id: int, chat_id_str: str, msg_id_str: str,
                        reason_name: str, reason_obj, custom_text: str = "",
                        reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()
    report_id = _generate_report_id()

    if not sessions:
        txt = "❌ Нет активных сессий. Обратитесь к администратору."
        if call: await call.message.edit_text(txt)
        elif reply_target: await reply_target.answer(txt)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))
        return

    total = len(sessions)
    spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    target_link = (f"t.me/{chat_id_str}/{msg_id_str}"
                   if not chat_id_str.lstrip("-").isdigit()
                   else f"t.me/c/{str(chat_id_str).lstrip('-').lstrip('100')}/{msg_id_str}")
    if call:
        await call.message.edit_text(
            f"🔄 <b>Обработка жалобы…</b>\n\n"
            f"🔗 <code>{target_link}</code>\n"
            f"📌 {reason_name}\n\n"
            f"⏳ Запускаю верификацию через {total} сессий…",
            parse_mode="HTML")
        status_msg = await call.message.answer(f"⏳ Верифицирую… (0/{total})")
    else:
        status_msg = await reply_target.answer(f"⏳ Верифицирую… (0/{total})")

    success = errors = 0
    flood_wait = False
    for i, sess in enumerate(sessions, 1):
        client = None
        _sess_ok = False
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await asyncio.wait_for(client.connect(), timeout=15.0)
            if await asyncio.wait_for(client.is_user_authorized(), timeout=10.0):
                try:
                    chat_id_resolved = (int(chat_id_str)
                                        if chat_id_str.lstrip("-").isdigit()
                                        else chat_id_str)
                    entity = await asyncio.wait_for(
                        client.get_entity(chat_id_resolved), timeout=10.0)
                    peer = await client.get_input_entity(entity)
                    await asyncio.wait_for(
                        client(ReportRequest(peer=peer, id=[int(msg_id_str)],
                                             reason=reason_obj, message=custom_text)),
                        timeout=15.0)
                    success += 1
                    _sess_ok = True
                except FloodWaitError as e:
                    logger.warning(f"Сессия {sess['id']} FloodWait {e.seconds}s")
                    flood_wait = True
                    errors += 1
                except Exception as e:
                    logger.warning(f"Сессия {sess['id']}: {type(e).__name__}: {e}")
                    errors += 1
            else:
                errors += 1
        except (asyncio.TimeoutError, Exception) as e:
            logger.error(f"Сессия {sess['id']} подключение: {e}")
            errors += 1
        finally:
            if client:
                try: await client.disconnect()
                except Exception: pass
        await db.increment_session_stats(sess["id"], 1 if _sess_ok else 0, 1)
        await asyncio.sleep(random.uniform(0.4, 0.8))
        spin = spinner[i % len(spinner)]
        try:
            await status_msg.edit_text(
                f"{spin} Верифицирую… ({i}/{total})  ✅{success} ❌{errors}")
        except Exception:
            pass

    await db.add_report_log(user_id, chat_id_str, msg_id_str, success, total, report_id, reason_name)
    if not is_adm:
        user_last_report[user_id] = datetime.now(timezone.utc)

    pct = round(success / total * 100) if total > 0 else 0
    bar_filled = round(pct / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    extra = f"\n📝 <i>{custom_text[:80]}{'…' if len(custom_text) > 80 else ''}</i>" if custom_text else ""
    flood_note = "\n\n⚠️ <i>Некоторые аккаунты временно ограничены (FloodWait). Повторите позже для лучшего результата.</i>" if flood_wait else ""
    result_text = (
        f"📊 <b>Обращение обработано</b>\n\n"
        f"🔗 Публикация: <code>{target_link}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n\n"
        f"[{bar}] {pct}%\n"
        f"✅ Принято: <b>{success}</b> из <b>{total}</b>{flood_note}\n\n"
        f"🆔 ID обращения: <code>{report_id}</code>"
    )
    await status_msg.edit_text(result_text, parse_mode="HTML")

    # Отправка в группу логов
    user_obj = await db.get_user(user_id)
    uname_str = f"@{user_obj['username']}" if user_obj and user_obj.get("username") else f"id:{user_id}"
    log_text = (
        f"💬 <b>Жалоба на сообщение</b>  |  <code>{report_id}</code>\n\n"
        f"👤 От: {uname_str} (<code>{user_id}</code>)\n"
        f"🔗 Цель: <code>{target_link}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n"
        f"✅ Принято: <b>{success}</b> / {total}  ({pct}%)"
    )
    await _send_to_log_group(bot, log_text)

    await bot.send_message(user_id, "📨 Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))


# ─── Админ-панель ──────────────────────────────────────────

@router.message(F.text == "🔧 Админ панель")
async def btn_admin(message: Message):
    uid = message.from_user.id
    if not (await db.is_admin(uid) or uid in SUPERADMIN_IDS):
        await message.answer("❌ Нет доступа."); return
    await message.answer("🔧 <b>Админ-панель</b>", parse_mode="HTML",
                         reply_markup=admin_kb(uid in SUPERADMIN_IDS))


@router.callback_query(F.data == "admin:back")
async def cb_back(call: CallbackQuery):
    await call.message.edit_text("🔧 <b>Админ-панель</b>", parse_mode="HTML",
                                 reply_markup=admin_kb(call.from_user.id in SUPERADMIN_IDS))
    await call.answer()


# ─── Экспорт БД ────────────────────────────────────────────

@router.callback_query(F.data == "admin:export_db")
async def cb_export_db(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await call.answer("📤 Отправляю базу данных...")
    try:
        db_file = FSInputFile(DB_PATH, filename="bot_database.db")
        now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        await call.message.answer_document(
            db_file,
            caption=f"📦 <b>База данных бота</b>\n\n🕐 Выгружено: {now_str}",
            parse_mode="HTML"
        )
    except Exception as e:
        await call.message.answer(f"❌ Ошибка выгрузки: {e}")


# ─── Управление сессиями ────────────────────────────────────

@router.callback_query(F.data == "admin:sessions")
async def cb_sessions(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sessions = await db.get_all_sessions()
    n = len(sessions)
    if _session_status and sessions:
        lines = []
        for s in sessions:
            status = _session_status.get(s["id"], "❓ не проверена")
            lines.append(f"• #{s['id']} — {status}")
        sess_list = "\n".join(lines)
        sess_info = f"\n\n<b>Сессии:</b>\n{sess_list}"
    else:
        sess_info = "\n\n<i>Нажмите «Проверить все» для просмотра статусов.</i>"
    await call.message.edit_text(
        f"📂 <b>Управление сессиями</b>\n\n"
        f"Активных сессий: <b>{n}</b>{sess_info}\n\n"
        f"<b>Добавить сессию:</b> StringSession, Auth Key (HEX) или авторизация по номеру телефона.",
        parse_mode="HTML",
        reply_markup=sessions_kb())
    await call.answer()


@router.callback_query(F.data == "sess:upload")
async def cb_sess_upload(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_session_string)
    await call.message.edit_text(
        "📝 <b>Добавление StringSession</b>\n\n"
        "Вставьте строку сессии Telethon (StringSession).\n\n"
        "⚠️ Бот проверит валидность сессии перед сохранением.\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")]
        ])
    )
    await call.answer()


@router.message(States.waiting_session_string)
async def got_session_string(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)); return

    session_str = (message.text or "").strip()
    if not session_str:
        await message.answer("❌ Строка пустая. Попробуйте снова или /cancel."); return

    status_msg = await message.answer("🔄 Проверяю сессию...")

    try:
        client = TelegramClient(StringSession(session_str), TELETHON_API_ID, TELETHON_API_HASH)
        await client.connect()
        is_auth = await client.is_user_authorized()
        if is_auth:
            me = await client.get_me()
            await client.disconnect()
            await state.clear()
            sess_id = await db.add_session(session_str)
            await db.log_admin_action(message.from_user.id, "add_session", f"id={sess_id} user={me.username or me.id}")
            await status_msg.edit_text(
                f"✅ <b>Сессия добавлена!</b>\n\n"
                f"👤 Аккаунт: <code>{me.first_name or ''} {me.last_name or ''}</code>\n"
                f"📱 Username: @{me.username or '—'}\n"
                f"🆔 ID сессии: <code>{sess_id}</code>",
                parse_mode="HTML"
            )
        else:
            await client.disconnect()
            await status_msg.edit_text(
                "❌ <b>Сессия недействительна.</b>\n\n"
                "Аккаунт не авторизован. Получите новую StringSession и попробуйте снова.",
                parse_mode="HTML"
            )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка проверки сессии:</b>\n\n<code>{e}</code>\n\n"
            "Убедитесь что строка верная и попробуйте снова.",
            parse_mode="HTML"
        )


# ─── Добавление сессии по Auth Key (HEX) ────────────────────

_DC_SERVERS = {
    1: ("149.154.175.53",  443),
    2: ("149.154.167.51",  443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91",  443),
    5: ("91.108.56.130",   443),
}


@router.callback_query(F.data == "sess:authkey")
async def cb_sess_authkey(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_auth_key_hex)
    await call.message.edit_text(
        "🔑 <b>Добавление сессии по Auth Key (HEX)</b>\n\n"
        "Вставьте Auth Key в формате HEX (512 символов, 256 байт).\n\n"
        "Это авторизационный ключ аккаунта Telegram, который можно получить\n"
        "из большинства клиентов (TDLib, MadelineProto, и др.).\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")]
        ])
    )
    await call.answer()


@router.message(States.waiting_auth_key_hex)
async def got_auth_key_hex(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=sessions_kb()); return

    raw = (message.text or "").strip().lower()
    if len(raw) != 512 or not all(c in "0123456789abcdef" for c in raw):
        await message.answer(
            "❌ <b>Неверный формат.</b>\n\n"
            "Auth Key должен быть ровно 512 hex-символов (256 байт).\n"
            "Попробуйте снова или /cancel.",
            parse_mode="HTML"
        )
        return

    await state.update_data(auth_key_hex=raw)
    await state.set_state(States.waiting_auth_key_dc)
    await message.answer(
        "📡 <b>Введите номер DC-сервера (1–5)</b>\n\n"
        "Укажите датацентр, к которому привязан аккаунт.\n"
        "Если не знаете — попробуйте <b>2</b> (основной датацентр).\n\n"
        "<code>DC1</code> — 149.154.175.53\n"
        "<code>DC2</code> — 149.154.167.51\n"
        "<code>DC3</code> — 149.154.175.100\n"
        "<code>DC4</code> — 149.154.167.91\n"
        "<code>DC5</code> — 91.108.56.130\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="DC1", callback_data="authkey_dc:1"),
                InlineKeyboardButton(text="DC2", callback_data="authkey_dc:2"),
                InlineKeyboardButton(text="DC3", callback_data="authkey_dc:3"),
                InlineKeyboardButton(text="DC4", callback_data="authkey_dc:4"),
                InlineKeyboardButton(text="DC5", callback_data="authkey_dc:5"),
            ],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")],
        ])
    )


async def _connect_authkey_session(auth_key_hex: str, dc_id: int, user_id: int) -> tuple:
    """Создаёт сессию из Auth Key HEX, проверяет авторизацию и возвращает (sess_id, me) или бросает исключение."""
    from telethon.crypto import AuthKey as TelethonAuthKey

    ip, port = _DC_SERVERS[dc_id]
    auth_key_bytes = bytes.fromhex(auth_key_hex)

    session = MemorySession()
    session.set_dc(dc_id, ip, port)
    session.auth_key = TelethonAuthKey(data=auth_key_bytes)

    client = TelegramClient(session, TELETHON_API_ID, TELETHON_API_HASH)
    await client.connect()
    try:
        is_auth = await client.is_user_authorized()
        if not is_auth:
            raise ValueError("Аккаунт не авторизован с данным ключом.")
        me = await client.get_me()
        session_str = StringSession.save(client.session)
        if not session_str:
            raise ValueError("Не удалось получить StringSession из MemorySession.")
    finally:
        await client.disconnect()

    sess_id = await db.add_session(session_str)
    await db.log_admin_action(user_id, "add_session_authkey", f"id={sess_id} dc={dc_id} user={getattr(me, 'username', None) or getattr(me, 'id', '?')}")
    return sess_id, me


async def _handle_authkey_dc(dc_id: int, state: FSMContext, user_id: int, reply_fn):
    """Общая логика подключения по DC для text- и callback-обработчиков."""
    data = await state.get_data()
    auth_key_hex = data.get("auth_key_hex")
    if not auth_key_hex:
        await state.clear()
        await reply_fn("❌ Данные сессии потеряны. Начните заново.", reply_markup=sessions_kb())
        return

    status_msg = await reply_fn("🔄 Подключаюсь к DC{} и проверяю Auth Key...".format(dc_id))
    try:
        sess_id, me = await _connect_authkey_session(auth_key_hex, dc_id, user_id)
        await state.clear()
        name = f"{getattr(me, 'first_name', '') or ''} {getattr(me, 'last_name', '') or ''}".strip()
        uname = getattr(me, 'username', None)
        await status_msg.edit_text(
            f"✅ <b>Сессия добавлена!</b>\n\n"
            f"👤 Аккаунт: <code>{name}</code>\n"
            f"📱 Username: @{uname or '—'}\n"
            f"📡 DC: {dc_id}\n"
            f"🆔 ID сессии: <code>{sess_id}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка:</b> <code>{e}</code>\n\n"
            "Проверьте Auth Key и номер DC. Попробуйте другой датацентр или /cancel.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="DC1", callback_data="authkey_dc:1"),
                    InlineKeyboardButton(text="DC2", callback_data="authkey_dc:2"),
                    InlineKeyboardButton(text="DC3", callback_data="authkey_dc:3"),
                    InlineKeyboardButton(text="DC4", callback_data="authkey_dc:4"),
                    InlineKeyboardButton(text="DC5", callback_data="authkey_dc:5"),
                ],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")],
            ])
        )


@router.callback_query(F.data.startswith("authkey_dc:"))
async def cb_authkey_dc(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    current_state = await state.get_state()
    if current_state != States.waiting_auth_key_dc:
        await call.answer("❌ Сначала введите Auth Key.", show_alert=True); return
    dc_id = int(call.data.split(":")[1])
    await call.answer()

    async def reply_fn(text, **kwargs):
        return await call.message.answer(text, **kwargs)

    await _handle_authkey_dc(dc_id, state, call.from_user.id, reply_fn)


@router.message(States.waiting_auth_key_dc)
async def got_auth_key_dc(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=sessions_kb()); return

    text = (message.text or "").strip()
    if not text.isdigit() or int(text) not in _DC_SERVERS:
        await message.answer("❌ Введите номер датацентра от 1 до 5."); return

    dc_id = int(text)

    async def reply_fn(text, **kwargs):
        return await message.answer(text, **kwargs)

    await _handle_authkey_dc(dc_id, state, message.from_user.id, reply_fn)


# ─── Авторизация по номеру телефона ─────────────────────────

@router.callback_query(F.data == "sess:phone")
async def cb_sess_phone(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_phone)
    await call.message.edit_text(
        "📱 <b>Авторизация по номеру телефона</b>\n\n"
        "Введите номер телефона в международном формате:\n"
        "• <code>+79001234567</code>\n"
        "• <code>+380501234567</code>\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")]
        ])
    )
    await call.answer()


@router.message(States.waiting_phone)
async def got_phone(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=sessions_kb()); return

    phone = (message.text or "").strip()
    if not (phone.startswith("+") and phone[1:].isdigit() and len(phone) >= 8):
        await message.answer(
            "❌ Неверный формат номера. Введите в виде <code>+79001234567</code>:",
            parse_mode="HTML"
        ); return

    status_msg = await message.answer("🔄 Отправляю код подтверждения...")
    uid = message.from_user.id

    try:
        client = TelegramClient(StringSession(), TELETHON_API_ID, TELETHON_API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        _auth_clients[uid] = client
        await state.update_data(phone=phone, phone_code_hash=result.phone_code_hash)
        await state.set_state(States.waiting_code)
        await status_msg.edit_text(
            f"📲 <b>Код отправлен!</b>\n\n"
            f"На номер <code>{phone}</code> отправлен SMS-код.\n\n"
            f"Введите полученный код (только цифры, например <code>12345</code>):\n\n"
            f"/cancel — отмена",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")]
            ])
        )
    except FloodWaitError as e:
        await status_msg.edit_text(
            f"⏳ Слишком много попыток. Подождите <b>{e.seconds} сек.</b> и попробуйте снова.",
            parse_mode="HTML"
        )
        await state.clear()
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Ошибка при отправке кода:\n<code>{e}</code>",
            parse_mode="HTML"
        )
        await state.clear()


@router.message(States.waiting_code)
async def got_code(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return

    uid = message.from_user.id
    if message.text and message.text.strip() == "/cancel":
        client = _auth_clients.pop(uid, None)
        if client:
            try: await client.disconnect()
            except Exception: pass
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=sessions_kb()); return

    code = (message.text or "").strip().replace(" ", "")
    if not code.isdigit():
        await message.answer("❌ Код должен состоять только из цифр. Попробуйте снова:"); return

    client = _auth_clients.get(uid)
    if not client:
        await state.clear()
        await message.answer(
            "❌ Сессия авторизации истекла. Начните процесс заново через кнопку «Авторизоваться по номеру»."
        ); return

    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    status_msg = await message.answer("🔄 Проверяю код...")

    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        me = await client.get_me()
        session_str = client.session.save()
        await client.disconnect()
        _auth_clients.pop(uid, None)
        await state.clear()
        sess_id = await db.add_session(session_str)
        await db.log_admin_action(uid, "add_session_phone", f"id={sess_id} user={me.username or me.id}")
        await status_msg.edit_text(
            f"✅ <b>Авторизация успешна!</b>\n\n"
            f"👤 Аккаунт: <code>{(me.first_name or '')} {(me.last_name or '')}</code>\n"
            f"📱 Username: @{me.username or '—'}\n"
            f"🆔 ID сессии: <code>{sess_id}</code>",
            parse_mode="HTML"
        )
    except SessionPasswordNeededError:
        await state.set_state(States.waiting_2fa)
        await status_msg.edit_text(
            "🔐 <b>Требуется пароль двухфакторной аутентификации (2FA)</b>\n\n"
            "Введите ваш пароль облачного шифрования Telegram:\n\n"
            "/cancel — отмена",
            parse_mode="HTML"
        )
    except PhoneCodeInvalidError:
        await status_msg.edit_text(
            "❌ Неверный код. Проверьте и введите снова:"
        )
    except PhoneCodeExpiredError:
        _auth_clients.pop(uid, None)
        await state.clear()
        await status_msg.edit_text(
            "❌ Срок действия кода истёк. Запросите новый через кнопку «Авторизоваться по номеру»."
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Ошибка при входе:\n<code>{e}</code>",
            parse_mode="HTML"
        )


@router.message(States.waiting_2fa)
async def got_2fa(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return

    uid = message.from_user.id
    if message.text and message.text.strip() == "/cancel":
        client = _auth_clients.pop(uid, None)
        if client:
            try: await client.disconnect()
            except Exception: pass
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=sessions_kb()); return

    password = (message.text or "").strip()
    if not password:
        await message.answer("❌ Пароль не может быть пустым. Введите ваш пароль 2FA:"); return

    client = _auth_clients.get(uid)
    if not client:
        await state.clear()
        await message.answer(
            "❌ Сессия авторизации истекла. Начните процесс заново через кнопку «Авторизоваться по номеру»."
        ); return

    status_msg = await message.answer("🔄 Проверяю пароль...")
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        session_str = client.session.save()
        await client.disconnect()
        _auth_clients.pop(uid, None)
        await state.clear()
        sess_id = await db.add_session(session_str)
        await db.log_admin_action(uid, "add_session_phone_2fa", f"id={sess_id} user={me.username or me.id}")
        await status_msg.edit_text(
            f"✅ <b>Авторизация успешна!</b>\n\n"
            f"👤 Аккаунт: <code>{(me.first_name or '')} {(me.last_name or '')}</code>\n"
            f"📱 Username: @{me.username or '—'}\n"
            f"🆔 ID сессии: <code>{sess_id}</code>",
            parse_mode="HTML"
        )
    except PasswordHashInvalidError:
        await status_msg.edit_text(
            "❌ Неверный пароль 2FA. Попробуйте снова:"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Ошибка при проверке пароля:\n<code>{e}</code>",
            parse_mode="HTML"
        )


@router.callback_query(F.data == "sess:check")
async def cb_sess_check(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("📭 Нет сохранённых сессий.", reply_markup=sessions_kb())
        await call.answer(); return
    await call.message.edit_text(f"🔄 Проверяю сессии (0/{len(sessions)})...")
    results = []
    for i, sess in enumerate(sessions, 1):
        client = None
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await asyncio.wait_for(client.connect(), timeout=15.0)
            ok = await asyncio.wait_for(client.is_user_authorized(), timeout=10.0)
            if ok:
                me = await asyncio.wait_for(client.get_me(), timeout=10.0)
                name = f"@{me.username}" if me.username else str(me.id)
                sp = int(sess.get("success_reports", 0) or 0)
                tp = int(sess.get("total_reports", 0) or 0)
                rate_str = f" ({round(sp/tp*100)}%)" if tp > 0 else ""
                _session_status[sess["id"]] = f"✅ {name}{rate_str}"
                results.append(f"✅ #{sess['id']} — {name}{rate_str}")
            else:
                _session_status[sess["id"]] = "❌ не авторизован"
                results.append(f"❌ #{sess['id']} — не авторизован")
        except asyncio.TimeoutError:
            _session_status[sess["id"]] = "⏱ таймаут"
            results.append(f"⏱ #{sess['id']} — таймаут подключения")
        except Exception as e:
            _session_status[sess["id"]] = f"⚠️ {str(e)[:30]}"
            results.append(f"⚠️ #{sess['id']} — {str(e)[:40]}")
        finally:
            if client:
                try: await client.disconnect()
                except Exception: pass
        try:
            await call.message.edit_text(f"🔄 Проверяю сессии ({i}/{len(sessions)})...")
        except Exception:
            pass
        await asyncio.sleep(0.3)

    good = sum(1 for r in results if r.startswith("✅"))
    text = f"📋 <b>Статус сессий ({good}/{len(sessions)} рабочих):</b>\n\n" + "\n".join(results)
    await call.message.edit_text(text[:4000], parse_mode="HTML", reply_markup=sessions_kb())
    await call.answer()


@router.callback_query(F.data == "sess:delete")
async def cb_sess_delete(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("📭 Нет сохранённых сессий.", reply_markup=sessions_kb())
        await call.answer(); return
    buttons = [[InlineKeyboardButton(text=f"🗑 Сессия #{s['id']}", callback_data=f"sess:del:{s['id']}")] for s in sessions]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")])
    await call.message.edit_text("Выберите сессию для удаления:",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("sess:del:"))
async def cb_sess_del_confirm(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sess_id = int(call.data.split(":")[2])
    await db.delete_session(sess_id)
    await db.log_admin_action(call.from_user.id, "delete_session", f"id={sess_id}")
    await call.answer(f"✅ Сессия #{sess_id} удалена", show_alert=True)
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("📭 Нет сессий.", reply_markup=sessions_kb()); return
    buttons = [[InlineKeyboardButton(text=f"🗑 Сессия #{s['id']}", callback_data=f"sess:del:{s['id']}")] for s in sessions]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")])
    await call.message.edit_text("Выберите сессию для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ─── Тарифы подписки ───────────────────────────────────────

def _plans_text(plans: list) -> str:
    if not plans:
        return "💰 <b>Тарифы подписок</b>\n\nТарифов пока нет. Добавьте хотя бы один."
    lines = ["💰 <b>Тарифы подписок</b>\n\nНажмите на тариф чтобы удалить его:\n"]
    for p in plans:
        label = "♾️ Навсегда" if p["days"] == 0 else f"📅 {p['days']} дней"
        lines.append(f"• {label} — <b>{p['price']:.0f} USD</b>")
    return "\n".join(lines)


def _plans_kb(plans: list) -> InlineKeyboardMarkup:
    rows = []
    for p in plans:
        label = ("♾️ Навсегда" if p["days"] == 0 else f"📅 {p['days']} дн.") + f" — {p['price']:.0f}$"
        rows.append([InlineKeyboardButton(text=f"🗑 {label}", callback_data=f"plan:del:{p['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить тариф", callback_data="plan:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:plans")
async def cb_plans(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    plans = await db.get_subscription_plans()
    await call.message.edit_text(_plans_text(plans), parse_mode="HTML", reply_markup=_plans_kb(plans))
    await call.answer()


@router.callback_query(F.data.startswith("plan:del:"))
async def cb_plan_del(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    plan_id = int(call.data.split(":")[2])
    await db.delete_subscription_plan(plan_id)
    await db.log_admin_action(call.from_user.id, "delete_plan", f"id={plan_id}")
    plans = await db.get_subscription_plans()
    await call.message.edit_text(_plans_text(plans), parse_mode="HTML", reply_markup=_plans_kb(plans))
    await call.answer("✅ Тариф удалён")


@router.callback_query(F.data == "plan:add")
async def cb_plan_add(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_plan_days)
    await call.message.edit_text(
        "➕ <b>Новый тариф</b>\n\n"
        "Введите количество дней подписки:\n"
        "• Введите <b>0</b> для бессрочной подписки\n"
        "• Например: <b>30</b> — месяц, <b>365</b> — год",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:plans")]
        ])
    )
    await call.answer()


@router.message(States.waiting_plan_days)
async def got_plan_days(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        days = int(message.text.strip())
        if days < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число >= 0:"); return
    await state.update_data(plan_days=days)
    await state.set_state(States.waiting_plan_price)
    label = "бессрочная" if days == 0 else f"{days} дней"
    await message.answer(
        f"✅ Дней: <b>{label}</b>\n\nТеперь введите цену в USD:\n"
        "• Например: <b>10</b> или <b>9.99</b>",
        parse_mode="HTML"
    )


@router.message(States.waiting_plan_price)
async def got_plan_price(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число (например: 10 или 9.99):"); return
    data = await state.get_data()
    await state.clear()
    days = data["plan_days"]
    label = "♾️ Навсегда" if days == 0 else f"📅 {days} дней"
    plan_id = await db.add_subscription_plan(days, price, label)
    await db.log_admin_action(message.from_user.id, "add_plan", f"id={plan_id} days={days} price={price}")
    plans = await db.get_subscription_plans()
    await message.answer(
        f"✅ <b>Тариф добавлен!</b>\n\n"
        f"🏷 Название: {label}\n"
        f"💰 Цена: <b>{price:.2f} USD</b>",
        parse_mode="HTML"
    )
    await message.answer(_plans_text(plans), parse_mode="HTML", reply_markup=_plans_kb(plans))


# ─── Белый список ──────────────────────────────────────────

@router.callback_query(F.data == "admin:whitelist")
async def cb_whitelist(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    count = len(await db.get_whitelist())
    await call.message.edit_text(
        f"🛡 <b>Белый список</b>\n\nОбъектов в списке: <b>{count}</b>\n\n"
        "Объекты из белого списка не могут получить жалобу.",
        parse_mode="HTML",
        reply_markup=whitelist_main_kb())
    await call.answer()


@router.callback_query(F.data == "wl:add")
async def cb_wl_add(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_wl_target)
    await call.message.edit_text(
        "🛡 <b>Добавить в белый список</b>\n\n"
        "Отправьте @username или числовой ID:\n"
        "• <code>@username</code>\n• <code>https://t.me/username</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:whitelist")]
        ]))
    await call.answer()


@router.message(States.waiting_wl_target)
async def got_wl_target(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    raw = (message.text or "").strip()
    m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", raw)
    target = m.group(1) if m else (raw[1:] if raw.startswith("@") else raw)
    if not target or len(target) < 3:
        await message.answer("❌ Не могу распознать. Введите @username или ссылку."); return
    await state.clear()
    # Храним target в callback data — не зависим от состояния FSM
    type_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Пользователь", callback_data=f"wlsave:{target}:user")],
        [InlineKeyboardButton(text="🤖 Бот",          callback_data=f"wlsave:{target}:bot")],
        [InlineKeyboardButton(text="📢 Канал",        callback_data=f"wlsave:{target}:channel")],
        [InlineKeyboardButton(text="👥 Группа",       callback_data=f"wlsave:{target}:group")],
        [InlineKeyboardButton(text="◀️ Отмена",       callback_data="admin:whitelist")],
    ])
    await message.answer(
        f"✅ Объект: <code>{target}</code>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=type_kb
    )


@router.callback_query(F.data.startswith("wlsave:"))
async def cb_wl_save(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer("❌ Ошибка данных", show_alert=True); return
    target = parts[1]
    wtype = parts[2]
    added = await db.add_to_whitelist(target, wtype, call.from_user.id)
    if added:
        await db.log_admin_action(call.from_user.id, "whitelist_add", f"target={target} type={wtype}")
        await call.message.edit_text(
            f"✅ <b>Добавлено в белый список!</b>\n\n"
            f"🎯 Объект: <code>@{target}</code>\n"
            f"🏷 Тип: {_wl_type_label(wtype)}",
            parse_mode="HTML", reply_markup=whitelist_main_kb())
    else:
        await call.message.edit_text(
            f"⚠️ <code>@{target}</code> уже в белом списке.",
            parse_mode="HTML", reply_markup=whitelist_main_kb())
    await call.answer()


@router.callback_query(F.data.startswith("wl:list:"))
async def cb_wl_list(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    page = int(call.data.split(":")[2])
    per_page = 8
    wl = await db.get_whitelist()
    if not wl:
        await call.message.edit_text("🛡 <b>Белый список пуст.</b>", parse_mode="HTML",
                                     reply_markup=whitelist_main_kb())
        await call.answer(); return
    total = len(wl)
    start = page * per_page
    items = wl[start:min(start + per_page, total)]
    lines = [f"🛡 <b>Белый список</b> ({page+1}/{(total-1)//per_page+1}):\n"]
    buttons = []
    for item in items:
        lines.append(f"• <code>@{item['target']}</code> — {_wl_type_label(item.get('target_type',''))}")
        buttons.append([InlineKeyboardButton(text=f"🗑 @{item['target']}", callback_data=f"wl:del:{item['id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="◀️", callback_data=f"wl:list:{page-1}"))
    if start + per_page < total: nav.append(InlineKeyboardButton(text="▶️", callback_data=f"wl:list:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:whitelist")])
    await call.message.edit_text("\n".join(lines), parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("wl:del:"))
async def cb_wl_del(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    wl_id = int(call.data.split(":")[2])
    await db.remove_from_whitelist(wl_id)
    await db.log_admin_action(call.from_user.id, "whitelist_remove", f"id={wl_id}")
    await call.answer("✅ Удалено", show_alert=True)
    wl = await db.get_whitelist()
    if not wl:
        await call.message.edit_text("🛡 <b>Белый список пуст.</b>", parse_mode="HTML",
                                     reply_markup=whitelist_main_kb()); return
    lines = ["🛡 <b>Белый список:</b>\n"]
    buttons = []
    for item in wl[:8]:
        lines.append(f"• <code>@{item['target']}</code> — {_wl_type_label(item.get('target_type',''))}")
        buttons.append([InlineKeyboardButton(text=f"🗑 @{item['target']}", callback_data=f"wl:del:{item['id']}")])
    if len(wl) > 8: buttons.append([InlineKeyboardButton(text="▶️", callback_data="wl:list:1")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:whitelist")])
    await call.message.edit_text("\n".join(lines), parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ─── Подписчики / логи ─────────────────────────────────────

@router.callback_query(F.data == "admin:subscribers")
async def cb_subscribers(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    subs = await db.get_all_subscribers()
    if not subs:
        text = "👥 Нет активных подписчиков."
    else:
        lines = ["👥 <b>Подписчики:</b>\n"]
        for u in subs:
            name = u.get("first_name") or str(u["user_id"])
            end = "♾️ Навсегда" if u.get("subscription_lifetime") else str(u.get("subscription_end", "—"))[:16]
            lines.append(f"• <code>{u['user_id']}</code> {name} — {end}")
        text = "\n".join(lines)
    await call.message.edit_text(text[:4000], parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
    await call.answer()


@router.callback_query(F.data == "admin:logs")
async def cb_logs(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    logs = await db.get_admin_logs(30)
    text = "📋 Логи пусты." if not logs else "📋 <b>Последние действия:</b>\n\n" + "\n".join(
        f"• [{l['created_at'][:16]}] <code>{l['admin_id']}</code>: {l['action']} {l['details']}" for l in logs)
    await call.message.edit_text(text[:4000], parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
    await call.answer()


# ─── Добавить/удалить админа ────────────────────────────────

@router.callback_query(F.data == "admin:add_admin")
async def cb_add_admin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    await state.set_state(States.waiting_admin_id_add)
    await call.message.edit_text("👤 Введите Telegram ID нового администратора:")
    await call.answer()


@router.message(States.waiting_admin_id_add)
async def got_admin_id(message: Message, state: FSMContext):
    if message.from_user.id not in SUPERADMIN_IDS:
        await state.clear(); return
    try:
        new_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой ID."); return
    await db.upsert_user(new_id, "", "")
    await db.set_admin(new_id, True)
    await db.log_admin_action(message.from_user.id, "add_admin", str(new_id))
    await state.clear()
    await message.answer(f"✅ Пользователь <code>{new_id}</code> назначен администратором.",
                         parse_mode="HTML", reply_markup=admin_kb(True))


@router.callback_query(F.data == "admin:del_admin")
async def cb_del_admin(call: CallbackQuery):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    admins = [a for a in await db.get_all_admins() if a["user_id"] not in SUPERADMIN_IDS]
    if not admins:
        await call.message.edit_text("ℹ️ Нет других администраторов.", reply_markup=admin_kb(True))
        await call.answer(); return
    buttons = [[InlineKeyboardButton(
        text=f"{a.get('first_name') or ''} (@{a.get('username') or ''}) [{a['user_id']}]",
        callback_data=f"deladm:{a['user_id']}")] for a in admins]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    await call.message.edit_text("Выберите для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("deladm:"))
async def cb_do_deladm(call: CallbackQuery):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    tid = int(call.data.split(":")[1])
    await db.set_admin(tid, False)
    await db.log_admin_action(call.from_user.id, "del_admin", str(tid))
    await call.message.edit_text(f"✅ Администратор <code>{tid}</code> удалён.",
                                 parse_mode="HTML", reply_markup=admin_kb(True))
    await call.answer()


# ─── Рассылка ──────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_broadcast_text)
    await call.message.edit_text(
        "📣 <b>Рассылка</b>\n\n"
        "Отправьте текст (поддерживается HTML). Будет отправлено всем пользователям.\n\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]]))
    await call.answer()


@router.message(States.waiting_broadcast_text)
async def got_broadcast_text(message: Message, bot: Bot, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена."); return
    await state.clear()
    users = await db.get_all_users()
    status_msg = await message.answer(f"📤 Рассылка... (0/{len(users)})")
    ok = fail = 0
    for i, uid in enumerate(users, 1):
        try:
            if message.text: await bot.send_message(uid, message.text, parse_mode="HTML")
            else: await message.copy_to(uid)
            ok += 1
        except Exception:
            fail += 1
        if i % 20 == 0:
            try: await status_msg.edit_text(f"📤 Рассылка... ({i}/{len(users)})")
            except Exception: pass
        await asyncio.sleep(0.05)
    await status_msg.edit_text(
        f"✅ Готово!\n\n📨 Отправлено: <b>{ok}</b>\n❌ Не доставлено: <b>{fail}</b>", parse_mode="HTML")
    await db.log_admin_action(message.from_user.id, "broadcast", f"ok={ok} fail={fail}")


# ─── Выдача подписки ────────────────────────────────────────

@router.callback_query(F.data == "admin:grant_sub")
async def cb_grant_sub(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_grant_uid)
    await call.message.edit_text("🎁 <b>Выдать подписку</b>\n\nВведите Telegram ID:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]]))
    await call.answer()


@router.message(States.waiting_grant_uid)
async def got_grant_uid(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID:"); return
    await state.update_data(grant_uid=uid)
    await state.set_state(States.waiting_grant_days)
    await message.answer(f"👤 ID: <code>{uid}</code>\n\nВведите количество дней (0 = бессрочная):", parse_mode="HTML")


@router.message(States.waiting_grant_days)
async def got_grant_days(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        days = int(message.text.strip())
        if days < 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число >= 0:"); return
    data = await state.get_data()
    await state.clear()
    uid = data["grant_uid"]
    user = await db.get_user(uid)
    if not user:
        await message.answer("❌ Пользователь не найден в боте."); return
    if days == 0:
        await db.activate_subscription(uid, 0); desc = "бессрочная"
    else:
        new_end = await db.grant_subscription(uid, days)
        desc = f"до {new_end.strftime('%d.%m.%Y %H:%M')} UTC (+{days} дн.)"
    await db.log_admin_action(message.from_user.id, "grant_sub", f"uid={uid} days={days}")
    await message.answer(f"✅ Подписка выдана!\n\n👤 ID: <code>{uid}</code>\n📅 {desc}", parse_mode="HTML")
    try:
        label = "бессрочная ♾️" if days == 0 else f"на {days} дн."
        await bot.send_message(uid, f"🎉 Вам выдана подписка {label}!\nСпасибо, что вы с нами.")
    except Exception:
        pass


# ─── Промокоды ─────────────────────────────────────────────

def _promo_list_text(promos: list) -> str:
    if not promos:
        return "🎟 <b>Промокоды</b>\n\nПромокодов пока нет."
    lines = ["🎟 <b>Промокоды</b>\n"]
    for p in promos:
        lines.append(f"• <code>{p['code']}</code> — {p['days']} дн. | {p['uses']}/{p['max_uses']} активаций")
    return "\n".join(lines)


def _promo_list_kb(promos: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 {p['code']}", callback_data=f"promo:del:{p['code']}")] for p in promos]
    rows.append([InlineKeyboardButton(text="➕ Создать", callback_data="admin:promo_new")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:promos")
async def cb_promos(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    promos = await db.get_all_promo_codes()
    await call.message.edit_text(_promo_list_text(promos), parse_mode="HTML", reply_markup=_promo_list_kb(promos))
    await call.answer()


@router.callback_query(F.data.startswith("promo:del:"))
async def cb_promo_delete(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    code = call.data.split("promo:del:")[1]
    await db.delete_promo_code(code)
    await db.log_admin_action(call.from_user.id, "delete_promo", f"code={code}")
    promos = await db.get_all_promo_codes()
    await call.message.edit_text(_promo_list_text(promos), parse_mode="HTML", reply_markup=_promo_list_kb(promos))
    await call.answer(f"✅ Промокод {code} удалён")


@router.callback_query(F.data == "admin:promo_new")
async def cb_promo_new(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_promo_new_code)
    await call.message.edit_text("➕ <b>Новый промокод</b>\n\nВведите код (латиница/цифры):", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:promos")]]))
    await call.answer()


@router.message(States.waiting_promo_new_code)
async def got_promo_code(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    code = message.text.strip().upper()
    if not code.replace("_","").replace("-","").isalnum():
        await message.answer("❌ Только буквы, цифры, _ и -."); return
    if await db.get_promo_code(code):
        await message.answer(f"❌ Код <code>{code}</code> уже существует.", parse_mode="HTML"); return
    await state.update_data(promo_code=code)
    await state.set_state(States.waiting_promo_new_days)
    await message.answer(f"✅ Код: <code>{code}</code>\n\nВведите количество дней:", parse_mode="HTML")


@router.message(States.waiting_promo_new_days)
async def got_promo_days(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        days = int(message.text.strip())
        if days <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число > 0:"); return
    await state.update_data(promo_days=days)
    await state.set_state(States.waiting_promo_new_uses)
    await message.answer(f"📅 Дней: <b>{days}</b>\n\nВведите максимальное количество активаций:", parse_mode="HTML")


@router.message(States.waiting_promo_new_uses)
async def got_promo_uses(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        uses = int(message.text.strip())
        if uses <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число > 0:"); return
    data = await state.get_data()
    await state.clear()
    code, days = data["promo_code"], data["promo_days"]
    await db.create_promo_code(code, days, uses, message.from_user.id)
    await db.log_admin_action(message.from_user.id, "create_promo", f"code={code} days={days} max={uses}")
    await message.answer(
        f"✅ Промокод создан!\n\n🎟 <code>{code}</code>\n📅 {days} дн.\n🔢 Активаций: {uses}",
        parse_mode="HTML")


@router.message(F.text == "🎟 Промокод")
async def btn_promo(message: Message, state: FSMContext):
    await state.set_state(States.waiting_promo_activate)
    await message.answer("🎟 <b>Активация промокода</b>\n\nВведите ваш промокод:", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="promo:cancel")]]))


@router.callback_query(F.data == "promo:cancel")
async def cb_promo_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer()


@router.message(States.waiting_promo_activate)
async def got_promo_activate(message: Message, state: FSMContext):
    await state.clear()
    code = message.text.strip().upper()
    promo = await db.get_promo_code(code)
    if not promo:
        await message.answer("❌ Промокод не найден."); return
    if promo["uses"] >= promo["max_uses"]:
        await message.answer("❌ Промокод уже исчерпан."); return
    uid = message.from_user.id
    new_end = await db.grant_subscription(uid, promo["days"])
    await db.use_promo_code(code)
    end_str = new_end.strftime('%d.%m.%Y %H:%M') + " UTC" if new_end else ""
    await message.answer(
        f"✅ Промокод активирован!\n\n🎁 +{promo['days']} дней\n📅 Подписка до: <b>{end_str}</b>",
        parse_mode="HTML")


# ─── Обязательные каналы ────────────────────────────────────

@router.callback_query(F.data == "admin:channels")
async def cb_channels_menu(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    channels = await db.get_force_channels()
    lines = ["📢 <b>Обязательные каналы</b>\n\nПользователи должны подписаться до использования бота.\n"]
    buttons = []
    for ch in channels:
        lines.append(f"• @{ch['channel_username']}")
        buttons.append([InlineKeyboardButton(text=f"🗑 @{ch['channel_username']}", callback_data=f"chan:del:{ch['id']}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="chan:add")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data == "chan:add")
async def cb_chan_add(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_channel_add)
    await call.message.edit_text("📢 Введите @username канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:channels")]]))
    await call.answer()


@router.message(States.waiting_channel_add)
async def got_channel_add(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    raw = message.text.strip().lstrip("@")
    await db.add_force_channel(raw)
    await db.log_admin_action(message.from_user.id, "add_channel", f"@{raw}")
    await state.clear()
    await message.answer(f"✅ Канал @{raw} добавлен.")


@router.callback_query(F.data.startswith("chan:del:"))
async def cb_chan_del(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    ch_id = int(call.data.split(":")[2])
    await db.delete_force_channel(ch_id)
    await db.log_admin_action(call.from_user.id, "del_channel", f"id={ch_id}")
    await call.answer("✅ Канал удалён", show_alert=True)
    channels = await db.get_force_channels()
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(text=f"🗑 @{ch['channel_username']}", callback_data=f"chan:del:{ch['id']}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="chan:add")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    await call.message.edit_text("📢 <b>Обязательные каналы</b>", parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ─── Группа логов ──────────────────────────────────────────

@router.callback_query(F.data == "admin:log_group")
async def cb_log_group(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    current = await db.get_setting("log_group_id") or "не настроена"
    await state.set_state(States.waiting_log_group_id)
    await call.message.edit_text(
        f"📊 <b>Группа логов</b>\n\n"
        f"Текущий ID: <code>{current}</code>\n\n"
        f"Отправьте числовой ID группы (например <code>-1001234567890</code>).\n\n"
        f"ℹ️ Чтобы узнать ID — перешлите любое сообщение из группы боту @userinfobot\n\n"
        f"Введите <code>0</code> чтобы отключить.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]
        ])
    )
    await call.answer()


@router.message(States.waiting_log_group_id)
async def got_log_group_id(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    text = (message.text or "").strip()
    try:
        gid = int(text)
    except ValueError:
        await message.answer("❌ Введите числовой ID группы:"); return
    if gid == 0:
        await db.set_setting("log_group_id", "")
        await state.clear()
        await message.answer("✅ Группа логов отключена.", reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS))
        return
    # Проверяем доступность группы
    try:
        test = await bot.send_message(gid, "✅ Группа логов успешно настроена! Сюда будут приходить все жалобы.")
        await db.set_setting("log_group_id", str(gid))
        await state.clear()
        await message.answer(
            f"✅ Группа логов настроена!\n\n🆔 ID: <code>{gid}</code>",
            parse_mode="HTML",
            reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)
        )
    except Exception as e:
        await message.answer(
            f"❌ Не удалось отправить сообщение в группу <code>{gid}</code>.\n\n"
            f"Убедитесь что:\n"
            f"• Бот добавлен в группу\n"
            f"• Бот имеет право отправлять сообщения\n\n"
            f"Ошибка: <code>{e}</code>",
            parse_mode="HTML"
        )


# ─── Снятие подписки ────────────────────────────────────────

@router.callback_query(F.data == "admin:revoke_sub")
async def cb_revoke_sub(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    subs = await db.get_all_subscribers()
    if not subs:
        await call.message.edit_text(
            "❌ Нет активных подписчиков.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
        await call.answer(); return
    # Показываем список — можно выбрать из списка или ввести ID вручную
    buttons = []
    for u in subs[:10]:
        name = u.get("first_name") or str(u["user_id"])
        uid_str = f"#{u['user_id']}"
        end = "♾️" if u.get("subscription_lifetime") else (str(u.get("subscription_end",""))[:10])
        buttons.append([InlineKeyboardButton(
            text=f"{name} ({uid_str}) до {end}",
            callback_data=f"revoke:{u['user_id']}")])
    if len(subs) > 10:
        buttons.append([InlineKeyboardButton(text="✏️ Ввести ID вручную", callback_data="revoke:manual")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    await call.message.edit_text(
        "❌ <b>Снятие подписки</b>\n\nВыберите пользователя или введите ID:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data == "revoke:manual")
async def cb_revoke_manual(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_revoke_uid)
    await call.message.edit_text(
        "❌ <b>Снятие подписки</b>\n\nВведите Telegram ID пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:revoke_sub")]]))
    await call.answer()


@router.message(States.waiting_revoke_uid)
async def got_revoke_uid(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой ID:"); return
    user = await db.get_user(uid)
    if not user or not (user.get("subscription_lifetime") or user.get("subscription_end")):
        await message.answer(f"❌ У пользователя <code>{uid}</code> нет активной подписки.", parse_mode="HTML"); return
    await state.update_data(revoke_uid=uid)
    await state.set_state(States.waiting_revoke_reason)
    name = user.get("first_name") or str(uid)
    await message.answer(
        f"👤 Пользователь: <b>{name}</b> (<code>{uid}</code>)\n\n"
        f"Напишите причину снятия подписки (будет отправлена пользователю):",
        parse_mode="HTML")


@router.callback_query(F.data.startswith("revoke:"), lambda c: c.data != "revoke:manual")
async def cb_revoke_selected(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    uid = int(call.data.split(":")[1])
    user = await db.get_user(uid)
    if not user:
        await call.message.edit_text("❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
        await call.answer(); return
    await state.update_data(revoke_uid=uid)
    await state.set_state(States.waiting_revoke_reason)
    name = user.get("first_name") or str(uid)
    await call.message.edit_text(
        f"👤 Пользователь: <b>{name}</b> (<code>{uid}</code>)\n\n"
        f"Напишите причину снятия подписки (будет отправлена пользователю):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:revoke_sub")]]))
    await call.answer()


@router.message(States.waiting_revoke_reason)
async def got_revoke_reason(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("❌ Причина не может быть пустой."); return
    data = await state.get_data()
    await state.clear()
    uid = data["revoke_uid"]
    await db.revoke_subscription(uid)
    await db.log_admin_action(message.from_user.id, "revoke_sub", f"uid={uid} reason={reason[:100]}")
    await message.answer(
        f"✅ Подписка пользователя <code>{uid}</code> снята.\n\n📝 Причина: {reason}",
        parse_mode="HTML", reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS))
    try:
        await bot.send_message(
            uid,
            f"⚠️ <b>Ваша подписка была снята администратором.</b>\n\n"
            f"📝 Причина: <i>{reason}</i>\n\n"
            f"Если считаете это ошибкой — обратитесь к поддержке.",
            parse_mode="HTML")
    except Exception:
        pass


# ─── Правила ────────────────────────────────────────────────

@router.callback_query(F.data == "admin:rules")
async def cb_rules_menu(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    url = await db.get_setting("rules_url") or "не задан"
    await state.set_state(States.waiting_rules_url)
    await call.message.edit_text(
        f"⚙️ <b>Правила</b>\n\nТекущий URL: <code>{url}</code>\n\nОтправьте новый URL:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]]))
    await call.answer()


@router.message(States.waiting_rules_url)
async def got_rules_url(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    await db.set_setting("rules_url", message.text.strip())
    await state.clear()
    await message.answer("✅ URL правил обновлён.")


# ─── Цена Premium (админ) ───────────────────────────────────

@router.callback_query(F.data == "admin:premium_price")
async def cb_premium_price(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    current = await db.get_setting("premium_price") or "15"
    await state.set_state(States.waiting_premium_price)
    await call.message.edit_text(
        f"💎 <b>Цена Premium подписки</b>\n\n"
        f"Текущая цена: <b>{current} USDT</b> (навсегда)\n\n"
        f"Введите новую цену в USDT (например: <code>15</code> или <code>9.99</code>):\n\n"
        f"<i>Premium — одноразовая покупка, действует пока активна обычная подписка.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]
        ])
    )
    await call.answer()


@router.message(States.waiting_premium_price)
async def got_premium_price(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0: raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число (например: 15 или 9.99):"); return
    await db.set_setting("premium_price", str(price))
    await db.log_admin_action(message.from_user.id, "set_premium_price", f"price={price}")
    await state.clear()
    await message.answer(
        f"✅ <b>Цена Premium обновлена!</b>\n\n💰 Новая цена: <b>{price:.2f} USDT</b>",
        parse_mode="HTML",
        reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)
    )


# ─── Статистика бота (админ) ───────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await call.answer()
    s = await db.get_global_stats()

    top_text = ""
    if s["top_peers"]:
        top_text = "\n\n🏆 <b>Топ целей жалоб:</b>\n"
        for i, p in enumerate(s["top_peers"], 1):
            top_text += f"{i}. @{p['username']} — {p['count']} раз (✅ {p['success']})\n"

    sess_text = ""
    if _session_status:
        good_sess = sum(1 for v in _session_status.values() if v.startswith("✅"))
        sess_text = f"\n📡 Сессий работает: <b>{good_sess}/{len(_session_status)}</b>"

    text = (
        f"📈 <b>Статистика бота</b>\n\n"
        f"👤 Пользователей: <b>{s['user_count']}</b>\n"
        f"💎 Активных подписок: <b>{s['sub_count']}</b>\n"
        f"📂 Сессий Telethon: <b>{s['sess_count']}</b>"
        f"{sess_text}\n\n"
        f"📨 Жалоб на сообщения: <b>{s['msg_count']}</b>\n"
        f"📢 Жалоб на каналы/группы: <b>{s['peer_count']}</b>\n"
        f"📊 Всего обращений: <b>{s['total']}</b>\n"
        f"✅ Успешных: <b>{s['total_success']}</b> ({s['success_rate']}%)\n"
        f"👥 Рефералов: <b>{s['ref_count']}</b>"
        f"{top_text}"
    )
    await call.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))


# ─── Капча ─────────────────────────────────────────────────

@router.callback_query(F.data == "captcha:confirm")
async def cb_captcha_confirm(call: CallbackQuery, bot: Bot):
    uid = call.from_user.id
    await db.set_captcha_confirmed(uid)
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    has_sub = is_adm or await db.has_active_subscription(uid)
    has_prem = is_adm or await db.has_active_premium(uid)
    not_ch = [] if is_adm else await check_channels(bot, uid)

    if is_adm:
        status_line = "👑 <b>Администратор</b> — доступ неограничен"
    elif has_sub and has_prem:
        status_line = "✅ <b>Подписка активна</b>  •  💎 <b>Premium</b>"
    elif has_sub:
        status_line = "✅ <b>Подписка активна</b>"
    else:
        status_line = "⚠️ <b>Нет подписки</b> — нажмите 💎 Купить подписку"

    ch_text = ""
    if not_ch:
        ch_text = "\n\n📢 <b>Подпишитесь на каналы:</b>\n" + "\n".join(f"• @{c['channel_username']}" for c in not_ch)

    await call.message.edit_text(
        f"✅ <b>Проверка пройдена!</b>\n\n"
        f"👋 Добро пожаловать, <b>{call.from_user.first_name}</b>!\n\n"
        f"{status_line}{ch_text}",
        parse_mode="HTML"
    )
    await call.message.answer(
        "📨 Главное меню:",
        reply_markup=await get_main_keyboard(uid, is_adm)
    )
    await call.answer()


# ─── Чёрный список (админ) ─────────────────────────────────

@router.callback_query(F.data == "admin:banned")
async def cb_admin_banned(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    banned = await db.get_all_banned()
    if banned:
        lines = []
        for b in banned[:20]:
            reason = b.get("reason") or "—"
            lines.append(f"• <code>{b['user_id']}</code> — {reason}")
        text = "🚫 <b>Чёрный список</b>\n\n" + "\n".join(lines)
    else:
        text = "🚫 <b>Чёрный список пуст</b>"
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Забанить",   callback_data="admin:ban_user"),
             InlineKeyboardButton(text="✅ Разбанить",  callback_data="admin:unban_user")],
            [InlineKeyboardButton(text="◀️ Назад",      callback_data="admin:back")],
        ])
    )
    await call.answer()


@router.callback_query(F.data == "admin:ban_user")
async def cb_ban_user_start(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_ban_uid)
    await call.message.edit_text(
        "🚫 <b>Забанить пользователя</b>\n\nВведите user_id пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:banned")]
        ])
    )
    await call.answer()


@router.message(States.waiting_ban_uid)
async def got_ban_uid(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear(); await message.answer("❌ Отменено."); return
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await message.answer("❌ Введите числовой user_id:"); return
    await state.update_data(ban_uid=int(text))
    await state.set_state(States.waiting_ban_reason)
    await message.answer("📝 Введите причину бана (или /skip чтобы оставить пустой):")


@router.message(States.waiting_ban_reason)
async def got_ban_reason(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    data = await state.get_data()
    uid = data.get("ban_uid")
    reason = "" if (message.text or "").strip() == "/skip" else (message.text or "").strip()
    await db.ban_user(uid, reason, message.from_user.id)
    await db.log_admin_action(message.from_user.id, "ban_user", f"uid={uid} reason={reason}")
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{uid}</code> заблокирован.\nПричина: {reason or '—'}",
        parse_mode="HTML",
        reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)
    )


@router.callback_query(F.data == "admin:unban_user")
async def cb_unban_user_start(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_unban_uid)
    await call.message.edit_text(
        "✅ <b>Разбанить пользователя</b>\n\nВведите user_id:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:banned")]
        ])
    )
    await call.answer()


@router.message(States.waiting_unban_uid)
async def got_unban_uid(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear(); await message.answer("❌ Отменено."); return
    text = (message.text or "").strip()
    if not text.lstrip("-").isdigit():
        await message.answer("❌ Введите числовой user_id:"); return
    uid = int(text)
    banned = await db.get_banned_user(uid)
    if not banned:
        await message.answer(f"⚠️ Пользователь <code>{uid}</code> не в чёрном списке.", parse_mode="HTML")
        await state.clear(); return
    await db.unban_user(uid)
    await db.log_admin_action(message.from_user.id, "unban_user", f"uid={uid}")
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{uid}</code> разбанен.",
        parse_mode="HTML",
        reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)
    )


# ─── Stars тарифы (админ) ─────────────────────────────────

@router.callback_query(F.data == "admin:stars")
async def cb_admin_stars(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    plans = await db.get_subscription_plans()
    lines = []
    buttons = []
    for p in plans:
        sp = p.get("stars_price", 0) or 0
        sp_str = f"{sp} ⭐" if sp > 0 else "отключено"
        lines.append(f"• {p['label']} — {sp_str}")
        buttons.append([InlineKeyboardButton(
            text=f"✏️ {p['label']}",
            callback_data=f"stars_set_plan:{p['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    await call.message.edit_text(
        "⭐ <b>Stars тарифы</b>\n\n"
        "Цена 0 = кнопка Stars не показывается пользователям.\n\n"
        + ("\n".join(lines) if lines else "Нет тарифов"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await call.answer()


@router.callback_query(F.data.startswith("stars_set_plan:"))
async def cb_stars_set_plan(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    plan_id = int(call.data.split(":")[1])
    plans = await db.get_subscription_plans()
    plan = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        await call.answer("❌ Тариф не найден", show_alert=True); return
    current = plan.get("stars_price", 0) or 0
    await state.update_data(stars_plan_id=plan_id)
    await state.set_state(States.waiting_stars_price)
    await call.message.edit_text(
        f"⭐ <b>Stars цена: {plan['label']}</b>\n\n"
        f"Текущая цена: <b>{current} ⭐</b>\n\n"
        f"Введите новую цену в Stars (целое число, 0 — отключить):\n\n"
        f"/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:stars")]
        ])
    )
    await call.answer()


@router.message(States.waiting_stars_price)
async def got_stars_price(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)); return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❌ Введите целое неотрицательное число:"); return
    data = await state.get_data()
    plan_id = data.get("stars_plan_id")
    stars_price = int(text)
    await db.update_plan_stars_price(plan_id, stars_price)
    await db.log_admin_action(message.from_user.id, "set_stars_price", f"plan={plan_id} stars={stars_price}")
    await state.clear()
    status = f"{stars_price} ⭐" if stars_price > 0 else "отключено"
    await message.answer(
        f"✅ Stars цена обновлена: <b>{status}</b>",
        parse_mode="HTML",
        reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS)
    )


# ─── Stars оплата ─────────────────────────────────────────

@router.callback_query(F.data.startswith("buy_stars:"))
async def cb_buy_stars(call: CallbackQuery, bot: Bot):
    plan_id = int(call.data.split(":")[1])
    plans = await db.get_subscription_plans()
    plan = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        await call.answer("❌ Тариф не найден", show_alert=True); return
    stars_price = plan.get("stars_price", 0) or 0
    if stars_price <= 0:
        await call.answer("❌ Stars оплата для этого тарифа не настроена.", show_alert=True); return
    days = plan["days"]
    label = "навсегда" if days == 0 else f"{days} дней"
    title = f"Подписка {'навсегда' if days == 0 else f'на {days} дней'}"
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=f"Подписка на сервис верификации — {label}",
        payload=f"stars:{call.from_user.id}:{plan_id}:{days}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars_price)]
    )


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, bot: Bot):
    payload = message.successful_payment.invoice_payload
    parts = payload.split(":")
    if parts[0] != "stars" or len(parts) < 4:
        return
    user_id = int(parts[1])
    plan_id = int(parts[2])
    days = int(parts[3])
    await db.activate_subscription(user_id, days)
    label = "навсегда" if days == 0 else f"{days} дней"
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    await message.answer(
        f"✅ <b>Оплата Stars прошла!</b>\n\n"
        f"🎉 Подписка активирована: <b>{label}</b>.",
        parse_mode="HTML",
        reply_markup=await get_main_keyboard(user_id, is_adm)
    )
    await db.log_admin_action(0, "stars_payment", f"user={user_id} days={days} plan={plan_id}")
    if days >= 1:
        referrer_id = await db.get_referrer_for_payment_bonus(user_id)
        if referrer_id:
            await db.mark_referral_bonus_given(user_id)
            await db.grant_subscription(user_id, REFERRAL_BONUS_DAYS)
            await db.grant_subscription(referrer_id, REFERRAL_BONUS_DAYS)
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 <b>+{REFERRAL_BONUS_DAYS} день подписки!</b>\nВаш реферал оплатил подписку.",
                    parse_mode="HTML"
                )
            except Exception:
                pass


# ─── Реферальный лидерборд (админ) ─────────────────────────

@router.callback_query(F.data == "admin:ref_leaderboard")
async def cb_ref_leaderboard(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    top = await db.get_referral_leaderboard(10)
    if not top:
        await call.message.edit_text(
            "🏆 <b>Топ рефереров</b>\n\nПока нет данных.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]
            ])
        )
        await call.answer(); return

    medals = ["🥇", "🥈", "🥉"] + ["▪️"] * 10
    lines = []
    for i, r in enumerate(top):
        uname = f"@{r['username']}" if r.get("username") else f"id:{r['referrer_id']}"
        name = r.get("first_name") or ""
        lines.append(f"{medals[i]} {uname} {name} — <b>{r['ref_count']}</b> рефералов")

    await call.message.edit_text(
        "🏆 <b>Топ рефереров</b>\n\n"
        + "\n".join(lines) + "\n\n"
        + "🥇 +10 дней  •  🥈 +5 дней  •  🥉 +2 дня",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Наградить топ-3", callback_data="admin:reward_top3")],
            [InlineKeyboardButton(text="🔄 Обновить",        callback_data="admin:ref_leaderboard")],
            [InlineKeyboardButton(text="◀️ Назад",           callback_data="admin:back")],
        ])
    )
    await call.answer()


@router.callback_query(F.data == "admin:reward_top3")
async def cb_reward_top3(call: CallbackQuery, bot: Bot):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    top = await db.get_referral_leaderboard(3)
    if not top:
        await call.answer("⚠️ Нет данных для награждения", show_alert=True); return

    rewards = [10, 5, 2]
    medals = ["🥇", "🥈", "🥉"]
    results = []

    for i, r in enumerate(top[:3]):
        uid = r["referrer_id"]
        days = rewards[i]
        try:
            await db.grant_subscription(uid, days)
            results.append(f"{medals[i]} <code>{uid}</code> +{days} дн.")
            await db.log_admin_action(call.from_user.id, "reward_referrer", f"uid={uid} days={days} place={i+1}")
            try:
                await bot.send_message(
                    uid,
                    f"{medals[i]} <b>Поздравляем!</b>\n\n"
                    f"Вы заняли <b>{i+1} место</b> в реферальном рейтинге!\n"
                    f"🎁 <b>+{days} дней</b> подписки в подарок!",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        except Exception as e:
            results.append(f"{medals[i]} <code>{uid}</code> — ошибка: {e}")

    await call.message.edit_text(
        "🎁 <b>Топ-3 награждены!</b>\n\n" + "\n".join(results),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К лидерборду", callback_data="admin:ref_leaderboard")]
        ])
    )
    await call.answer()


# ─── Запуск ────────────────────────────────────────────────

async def main():
    await db.init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(poll_payments(bot))
    asyncio.create_task(hourly_session_check(bot))
    asyncio.create_task(daily_backup_reminder(bot))
    asyncio.create_task(daily_sub_reminder(bot))
    asyncio.create_task(startup_session_check(bot))
    logger.info("Бот запущен ✅")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "pre_checkout_query"])


if __name__ == "__main__":
    asyncio.run(main())
