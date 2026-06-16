import asyncio
import logging
import os
import re
import random
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
    KeyboardButton, Message, ReplyKeyboardMarkup
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
    Channel, Chat, User
)
from telethon.sessions import StringSession

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
    "spam":     ("🗑 Спам",        InputReportReasonSpam()),
    "violence": ("🔪 Насилие",     InputReportReasonViolence()),
    "porn":     ("🔞 Порнография", InputReportReasonPornography()),
    "other":    ("❓ Другое",      InputReportReasonOther()),
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


class States(StatesGroup):
    # Обращения
    waiting_report_type    = State()
    waiting_report_link    = State()
    waiting_report_reason  = State()
    waiting_custom_text    = State()
    waiting_peer_username  = State()
    waiting_peer_reason    = State()
    waiting_peer_custom    = State()
    # Сессии
    waiting_session_string = State()
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
    rules_url = await db.get_setting("rules_url")
    buttons = [
        [KeyboardButton(text="📨 Подать обращение"), KeyboardButton(text="💎 Купить подписку")],
        [KeyboardButton(text="📄 Моя подписка"),     KeyboardButton(text="🎟 Промокод")],
        [KeyboardButton(text="📊 Моя статистика"),   KeyboardButton(text="👥 Пригласить друга")],
    ]
    if rules_url:
        buttons.append([KeyboardButton(text="📜 Правила")])
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
        [InlineKeyboardButton(text="⚙️ Настроить правила",    callback_data="admin:rules")],
        [InlineKeyboardButton(text="📣 Рассылка",              callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🎁 Выдать подписку",       callback_data="admin:grant_sub"),
         InlineKeyboardButton(text="🎟 Промокоды",             callback_data="admin:promos")],
        [InlineKeyboardButton(text="💰 Тарифы подписок",       callback_data="admin:plans")],
        [InlineKeyboardButton(text="🛡 Белый список",          callback_data="admin:whitelist")],
        [InlineKeyboardButton(text="📊 Группа логов",          callback_data="admin:log_group"),
         InlineKeyboardButton(text="❌ Снять подписку",        callback_data="admin:revoke_sub")],
        [InlineKeyboardButton(text="📈 Статистика бота",       callback_data="admin:stats")],
        [InlineKeyboardButton(text="📤 Выгрузить базу данных", callback_data="admin:export_db")],
    ]
    if is_superadmin:
        rows.append([InlineKeyboardButton(text="🤖 Резервный бот", callback_data="admin:backup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sessions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Вставить StringSession",  callback_data="sess:upload")],
        [InlineKeyboardButton(text="📱 Авторизоваться по номеру", callback_data="sess:phone")],
        [InlineKeyboardButton(text="🗑 Удалить сессию",          callback_data="sess:delete")],
        [InlineKeyboardButton(text="✅ Проверить все сессии",    callback_data="sess:check")],
        [InlineKeyboardButton(text="◀️ Назад",                   callback_data="admin:back")],
    ])


def whitelist_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в белый список", callback_data="wl:add")],
        [InlineKeyboardButton(text="📋 Просмотр",                callback_data="wl:list:0")],
        [InlineKeyboardButton(text="◀️ Назад",                   callback_data="admin:back")],
    ])


def whitelist_type_kb() -> InlineKeyboardMarkup:
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
                        await db.activate_subscription(payment["user_id"], payment["duration_days"])
                        label = "навсегда" if payment["duration_days"] == 0 else f"на {payment['duration_days']} дней"
                        try:
                            await bot.send_message(payment["user_id"], f"✅ Оплата подтверждена! Подписка активирована {label}. 🎉")
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
                _session_status[s["id"]] = f"✅ {name}"
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

    is_adm = await db.is_admin(u.id) or u.id in SUPERADMIN_IDS
    has_sub = is_adm or await db.has_active_subscription(u.id)
    not_ch = [] if is_adm else await check_channels(bot, u.id)
    status = "✅ Подписка активна." if has_sub else "⚠️ Нет подписки — нажмите <b>💎 Купить подписку</b>."
    ch_text = ("\n\n📢 Подпишитесь: " + ", ".join(f"@{c['channel_username']}" for c in not_ch)) if not_ch else ""
    await message.answer(
        f"👋 Привет, <b>{u.first_name}</b>!\n\n"
        f"🛡 <b>Сервис верификации и модерации контента.</b>\n\n"
        f"📌 <b>Возможности:</b>\n"
        f"• 📨 Многоуровневая верификация обращений\n"
        f"• 💎 Подписка 30 дней / навсегда\n"
        f"• 📄 Просмотр статуса доступа\n\n"
        f"{status}{ch_text}",
        parse_mode="HTML",
        reply_markup=await get_main_keyboard(u.id, is_adm)
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

    if stats["total"] == 0:
        report_block = "📭 Вы ещё не подавали обращений."
    else:
        rate = round(stats["total_success"] / (stats["msg_success"] + stats["peer_success"] + 1) * 100) if (stats["msg_success"] + stats["peer_success"]) > 0 else 0
        total_sessions = stats["msg_count"] * 1 + stats["peer_count"] * 1
        if total_sessions > 0:
            rate = round((stats["msg_success"] + stats["peer_success"]) / total_sessions * 100)
        else:
            rate = 0
        report_block = (
            f"📨 <b>Жалобы на сообщения:</b> {stats['msg_count']} шт. (✅ {stats['msg_success']} успешно)\n"
            f"📢 <b>Жалобы на каналы/группы:</b> {stats['peer_count']} шт. (✅ {stats['peer_success']} успешно)\n"
            f"📊 <b>Всего обращений:</b> {stats['total']}\n"
        )

    await message.answer(
        f"📊 <b>Ваша статистика</b>\n\n"
        f"{report_block}\n"
        f"👥 <b>Приглашено друзей:</b> {ref_count} чел. (+{ref_count * REFERRAL_BONUS_DAYS} дн. к подписке)",
        parse_mode="HTML"
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
        f"Приглашайте друзей — за каждого нового пользователя вы получаете "
        f"<b>+{REFERRAL_BONUS_DAYS} дней</b> подписки!\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n<code>{ref_link}</code>\n\n"
        f"📌 Ссылка работает только для <b>новых</b> пользователей, которые ещё не регистрировались в боте.\n\n"
        f"👤 <b>Приглашено:</b> {ref_count} чел.\n"
        f"🎁 <b>Заработано:</b> {ref_count * REFERRAL_BONUS_DAYS} дней подписки",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой",
                                  url=f"https://t.me/share/url?url={ref_link}&text=Присоединяйся+к+нам!")]])
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
        await message.answer("👑 Администратор — подписка бессрочная."); return
    user = await db.get_user(uid)
    if not user:
        await message.answer("Напишите /start"); return
    if user.get("subscription_lifetime"):
        await message.answer("♾️ У вас <b>бессрочная подписка</b>!", parse_mode="HTML")
    elif user.get("subscription_end"):
        try:
            end = datetime.fromisoformat(user["subscription_end"]).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if end > now:
                await message.answer(
                    f"✅ Подписка активна.\n📅 До: <b>{end.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
                    f"⏳ Осталось: <b>{(end - now).days} дн.</b>", parse_mode="HTML")
            else:
                await message.answer("❌ Подписка истекла. Купите новую.")
        except Exception:
            await message.answer("❌ Ошибка данных.")
    else:
        await message.answer("❌ Нет подписки. Нажмите <b>💎 Купить подписку</b>.", parse_mode="HTML")


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


@router.callback_query(F.data.startswith("rtype:"), States.waiting_report_type)
async def cb_report_type(call: CallbackQuery, state: FSMContext):
    rtype = call.data.split(":")[1]
    await state.update_data(peer_type=rtype)
    if rtype == "message":
        await state.set_state(States.waiting_report_link)
        await call.message.edit_text(
            "🔗 Отправьте ссылку на публикацию:\n\n"
            "• <code>https://t.me/username/123</code>\n\n"
            "⚠️ Ссылки на приватные каналы (<code>t.me/c/...</code>) не принимаются.",
            parse_mode="HTML")
    elif rtype == "channel":
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "📢 <b>Жалоба на канал</b>\n\n"
            "Отправьте @юзернейм или ссылку:\n"
            "• <code>@durov</code>\n• <code>https://t.me/durov</code>\n\n"
            "ℹ️ Тип (канал/группа/бот) будет определён автоматически.",
            parse_mode="HTML")
    elif rtype == "group":
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "👥 <b>Жалоба на группу</b>\n\n"
            "Отправьте @юзернейм или ссылку:\n"
            "• <code>@mygroup</code>\n• <code>https://t.me/mygroup</code>\n\n"
            "ℹ️ Тип (канал/группа/бот) будет определён автоматически.",
            parse_mode="HTML")
    else:
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "🤖 <b>Жалоба на бота</b>\n\n"
            "Отправьте @юзернейм бота:\n• <code>@somebot</code>\n\n"
            "ℹ️ Тип (канал/группа/бот) будет определён автоматически.",
            parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_report_link)
async def got_link(message: Message, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
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
    await message.answer("📋 Выберите категорию нарушения:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",              callback_data="reason:spam")],
            [InlineKeyboardButton(text="🔪 Насилие",           callback_data="reason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",       callback_data="reason:porn")],
            [InlineKeyboardButton(text="❓ Другое",            callback_data="reason:other")],
            [InlineKeyboardButton(text="✏️ Описать нарушение", callback_data="reason:custom")],
        ]))


@router.callback_query(F.data == "reason:custom", States.waiting_report_reason)
async def cb_reason_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_custom_text)
    await call.message.edit_text(
        "✏️ Опишите нарушение своими словами:\n\n"
        "<i>Например: «Контент нарушает правила сообщества»</i>",
        parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_custom_text)
async def got_custom_text(message: Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым."); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512."); return
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ Принято. Обрабатываю...\n\n📝 <i>{text[:100]}...</i>", parse_mode="HTML")
    await _send_reports(bot=bot, user_id=message.from_user.id,
        chat_id_str=data["chat_id"], msg_id_str=data["message_id"],
        reason_name="✏️ Свой текст", reason_obj=InputReportReasonOther(),
        custom_text=text, reply_target=message)


@router.callback_query(F.data.startswith("reason:"), States.waiting_report_reason)
async def cb_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[key]
    data = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_reports(bot=bot, user_id=call.from_user.id,
        chat_id_str=data["chat_id"], msg_id_str=data["message_id"],
        reason_name=reason_name, reason_obj=reason_obj, call=call)


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
    await message.answer(
        f"{icon} <code>@{uname}</code>\n\n📋 Выберите причину обращения:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",              callback_data="preason:spam")],
            [InlineKeyboardButton(text="🔪 Насилие",           callback_data="preason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",       callback_data="preason:porn")],
            [InlineKeyboardButton(text="❓ Другое",            callback_data="preason:other")],
            [InlineKeyboardButton(text="✏️ Описать нарушение", callback_data="preason:custom")],
        ]))


@router.callback_query(F.data == "preason:custom", States.waiting_peer_reason)
async def cb_peer_reason_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_peer_custom)
    await call.message.edit_text("✏️ Опишите нарушение своими словами (до 512 символов):", parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_peer_custom)
async def got_peer_custom(message: Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым."); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512."); return
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ Принято. Обрабатываю...\n\n📝 <i>{text[:100]}...</i>", parse_mode="HTML")
    await _send_peer_reports(bot=bot, user_id=message.from_user.id,
        peer_username=data["peer_username"], peer_type=data.get("peer_type", "channel"),
        reason_name="✏️ Свой текст", reason_obj=InputReportReasonOther(),
        custom_text=text, reply_target=message)


@router.callback_query(F.data.startswith("preason:"), States.waiting_peer_reason)
async def cb_peer_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[key]
    data = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_peer_reports(bot=bot, user_id=call.from_user.id,
        peer_username=data["peer_username"], peer_type=data.get("peer_type", "channel"),
        reason_name=reason_name, reason_obj=reason_obj, call=call)


# ─── Отправка репортов ──────────────────────────────────────

async def _send_peer_reports(bot: Bot, user_id: int, peer_username: str, peer_type: str,
                              reason_name: str, reason_obj, custom_text: str = "",
                              reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()
    icon = TYPE_ICONS.get(peer_type, "📢")
    type_ru = {"channel": "канал", "group": "группу", "bot": "бота", "user": "пользователя"}.get(peer_type, "объект")

    if not sessions:
        txt = "❌ Нет активных сессий. Обратитесь к администратору."
        if call: await call.message.edit_text(txt)
        elif reply_target: await reply_target.answer(txt)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))
        return

    total = len(sessions)
    if call:
        await call.message.edit_text(f"⏳ Обрабатываю обращение на {icon} @{peer_username}...")
        status_msg = await call.message.answer(f"⏳ Верифицирую... (0/{total})")
    else:
        status_msg = await reply_target.answer(f"⏳ Верифицирую... (0/{total})")

    success = errors = 0
    for i, sess in enumerate(sessions, 1):
        client = None
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await asyncio.wait_for(client.connect(), timeout=15.0)
            if await asyncio.wait_for(client.is_user_authorized(), timeout=10.0):
                try:
                    peer = await asyncio.wait_for(client.get_input_entity(peer_username), timeout=10.0)
                    await asyncio.wait_for(
                        client(ReportPeerRequest(peer=peer, reason=reason_obj, message=custom_text)),
                        timeout=15.0)
                    success += 1
                except Exception as e:
                    logger.warning(f"Peer-сессия {sess['id']}: {e}")
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
        await asyncio.sleep(random.uniform(0.3, 0.7))
        try: await status_msg.edit_text(f"⏳ Верифицирую... ({i}/{total})")
        except Exception: pass

    await db.add_peer_report_log(user_id, peer_username, peer_type, success, total)
    if not is_adm:
        user_last_report[user_id] = datetime.now(timezone.utc)

    marks = "✅ " * min(success, 20) + "❌ " * min(errors, 20)
    extra = f"\n\n📝 <i>{custom_text[:80]}{'...' if len(custom_text)>80 else ''}</i>" if custom_text else ""
    result_text = (
        f"📊 <b>Обращение обработано</b>\n\n"
        f"{icon} Объект: <code>@{peer_username}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n\n"
        f"🔁 Каналов верификации: <b>{total}</b>\n"
        f"✅ Принято: <b>{success}</b>\n❌ Отклонено: <b>{errors}</b>\n\n{marks}"
    )
    await status_msg.edit_text(result_text, parse_mode="HTML")

    # Отправка в группу логов
    user_obj = await db.get_user(user_id)
    uname_str = f"@{user_obj['username']}" if user_obj and user_obj.get("username") else f"id:{user_id}"
    log_text = (
        f"{icon} <b>Жалоба на {type_ru}</b>\n\n"
        f"👤 От: {uname_str} (<code>{user_id}</code>)\n"
        f"🎯 Цель: <code>@{peer_username}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n"
        f"✅ Принято: <b>{success}</b> / {total}"
    )
    await _send_to_log_group(bot, log_text)

    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))


async def _send_reports(bot: Bot, user_id: int, chat_id_str: str, msg_id_str: str,
                        reason_name: str, reason_obj, custom_text: str = "",
                        reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()

    if not sessions:
        txt = "❌ Нет активных сессий. Обратитесь к администратору."
        if call: await call.message.edit_text(txt)
        elif reply_target: await reply_target.answer(txt)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))
        return

    total = len(sessions)
    if call:
        await call.message.edit_text(f"⏳ Обрабатываю ({reason_name})...")
        status_msg = await call.message.answer(f"⏳ Верифицирую... (0/{total})")
    else:
        status_msg = await reply_target.answer(f"⏳ Верифицирую... (0/{total})")

    success = errors = 0
    for i, sess in enumerate(sessions, 1):
        client = None
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await asyncio.wait_for(client.connect(), timeout=15.0)
            if await asyncio.wait_for(client.is_user_authorized(), timeout=10.0):
                try:
                    peer = await asyncio.wait_for(client.get_input_entity(
                        int(chat_id_str) if chat_id_str.lstrip("-").isdigit() else chat_id_str),
                        timeout=10.0)
                    await asyncio.wait_for(
                        client(ReportRequest(peer=peer, id=[int(msg_id_str)], reason=reason_obj, message=custom_text)),
                        timeout=15.0)
                    success += 1
                except Exception as e:
                    logger.warning(f"Сессия {sess['id']}: {e}")
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
        await asyncio.sleep(random.uniform(0.3, 0.7))
        try: await status_msg.edit_text(f"⏳ Верифицирую... ({i}/{total})")
        except Exception: pass

    await db.add_report_log(user_id, chat_id_str, msg_id_str, success, total)
    if not is_adm:
        user_last_report[user_id] = datetime.now(timezone.utc)

    marks = "✅ " * min(success, 20) + "❌ " * min(errors, 20)
    extra = f"\n\n📝 <i>{custom_text[:80]}{'...' if len(custom_text)>80 else ''}</i>" if custom_text else ""
    result_text = (
        f"📊 <b>Обращение обработано</b>\n\n"
        f"📌 Категория: {reason_name}{extra}\n\n"
        f"🔁 Каналов верификации: <b>{total}</b>\n"
        f"✅ Принято: <b>{success}</b>\n❌ Отклонено: <b>{errors}</b>\n\n{marks}"
    )
    await status_msg.edit_text(result_text, parse_mode="HTML")

    # Отправка в группу логов
    user_obj = await db.get_user(user_id)
    uname_str = f"@{user_obj['username']}" if user_obj and user_obj.get("username") else f"id:{user_id}"
    target_link = f"t.me/{chat_id_str}/{msg_id_str}" if not chat_id_str.lstrip('-').isdigit() else f"t.me/c/{chat_id_str.lstrip('-100')}/{msg_id_str}"
    log_text = (
        f"💬 <b>Жалоба на сообщение</b>\n\n"
        f"👤 От: {uname_str} (<code>{user_id}</code>)\n"
        f"🔗 Цель: <code>{target_link}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n"
        f"✅ Принято: <b>{success}</b> / {total}"
    )
    await _send_to_log_group(bot, log_text)

    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))


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
    n = len(await db.get_all_sessions())
    await call.message.edit_text(
        f"📂 <b>Управление сессиями</b>\n\nАктивных сессий: <b>{n}</b>\n\n"
        f"Для добавления StringSession вставьте строку после нажатия соответствующей кнопки.",
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
                _session_status[sess["id"]] = f"✅ {name}"
                results.append(f"✅ #{sess['id']} — {name}")
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
    if not target:
        await message.answer("❌ Не могу распознать."); return
    await state.update_data(wl_target=target)
    await state.set_state(States.waiting_wl_type)
    await message.answer(f"✅ Объект: <code>{target}</code>\n\nВыберите тип:", parse_mode="HTML",
                         reply_markup=whitelist_type_kb())


@router.callback_query(F.data.startswith("wltype:"), States.waiting_wl_type)
async def cb_wl_type(call: CallbackQuery, state: FSMContext):
    wtype = call.data.split(":")[1]
    data = await state.get_data()
    target = data["wl_target"]
    await state.clear()
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
            f"⚠️ <code>@{target}</code> уже в белом списке.", parse_mode="HTML",
            reply_markup=whitelist_main_kb())
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


# ─── Запуск ────────────────────────────────────────────────

async def main():
    await db.init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(poll_payments(bot))
    asyncio.create_task(hourly_session_check(bot))
    asyncio.create_task(daily_backup_reminder(bot))
    asyncio.create_task(startup_session_check(bot))
    logger.info("Бот запущен ✅")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
