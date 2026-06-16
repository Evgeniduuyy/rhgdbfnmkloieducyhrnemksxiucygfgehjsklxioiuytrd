import asyncio
import logging
import os
import re
import random
import tempfile
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
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PasswordHashInvalidError, FloodWaitError
)
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.types import (
    InputReportReasonSpam, InputReportReasonViolence,
    InputReportReasonPornography, InputReportReasonOther
)
from telethon.sessions import StringSession

import database as db

# ============================================================
#                   НАСТРОЙКИ — ЗАПОЛНИ ЭТО
# ============================================================

BOT_TOKEN = "8770214132:AAEth6uS5IWQNgEcsAuf9eaKUtA_MqM4RwA"
CRYPTOBOT_TOKEN = "596342:AApk7WCgW3Ae8xlUwsGmo4RNFMOFe3lQyFR"
SUPERADMIN_IDS = {853173723, 1090307552}   # все суперадмины

# Получить на https://my.telegram.org → API Development Tools
TELETHON_API_ID = 35989820
TELETHON_API_HASH = "18cec00c9bef93d0dd475baba4e6c3f4"

# Имя основного бота (без @) — нужно резервному боту
MAIN_BOT_USERNAME = "Pizza_FenixBot"

# Имя резервного бота (без @) — для ежедневного напоминания пользователям
# Оставьте пустым "", если резервный бот не настроен
BACKUP_BOT_USERNAME = ""

# Путь к файлу базы данных
DB_PATH = "bot_database.db"

# ============================================================

import database as db
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

REPORT_REASONS = {
    "spam":     ("🗑 Спам",        InputReportReasonSpam()),
    "violence": ("🔪 Насилие",     InputReportReasonViolence()),
    "porn":     ("🔞 Порнография", InputReportReasonPornography()),
    "other":    ("❓ Другое",      InputReportReasonOther()),
}


class States(StatesGroup):
    waiting_report_link    = State()
    waiting_report_reason  = State()
    waiting_custom_text    = State()
    waiting_admin_id_add   = State()
    waiting_session_data   = State()   # вставить StringSession строку
    waiting_session_file   = State()   # загрузить .session файл
    waiting_phone          = State()   # авторизация по номеру — шаг 1
    waiting_code           = State()   # авторизация по номеру — шаг 2 (код)
    waiting_2fa            = State()   # авторизация по номеру — шаг 3 (2FA)
    waiting_channel_add    = State()
    waiting_rules_url      = State()
    waiting_rules_text     = State()
    waiting_backup_token   = State()
    waiting_broadcast_text = State()
    waiting_grant_uid      = State()
    waiting_grant_days     = State()
    waiting_promo_new_code = State()
    waiting_promo_new_days = State()
    waiting_promo_new_uses = State()
    waiting_promo_activate = State()
    waiting_report_type    = State()   # выбор типа: message/channel/bot
    waiting_peer_username  = State()   # @username канала или бота
    waiting_peer_reason    = State()   # причина для канала/бота
    waiting_peer_custom    = State()   # свой текст для канала/бота


# Хранилище Telethon-клиентов во время авторизации по номеру
# (FSMContext не может хранить живые объекты)
_auth_clients: dict[int, TelegramClient] = {}


# ─── Утилиты ───────────────────────────────────────────────

def parse_tg_link(link: str):
    """Разбирает ссылку на сообщение и возвращает (chat_id, message_id)"""
    link = link.strip()
    private = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if private:
        return "-100" + private.group(1), private.group(2)
    public = re.match(r"https?://t\.me/([^/]+)/(\d+)", link)
    if public:
        return public.group(1), public.group(2)
    return None, None


async def check_channels(bot: Bot, user_id: int) -> list:
    """Возвращает список каналов, на которые пользователь НЕ подписан"""
    channels = await db.get_force_channels()
    not_subscribed = []
    for ch in channels:
        try:
            chat_ref = ch["channel_id"] if ch["channel_id"] else f"@{ch['channel_username']}"
            member = await bot.get_chat_member(chat_ref, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return not_subscribed


# ─── Клавиатуры ────────────────────────────────────────────

async def get_main_keyboard(user_id: int, is_adm: bool) -> ReplyKeyboardMarkup:
    rules_url = await db.get_setting("rules_url")
    buttons = [
        [KeyboardButton(text="📨 Подать обращение"), KeyboardButton(text="💎 Купить подписку")],
        [KeyboardButton(text="📄 Моя подписка"),     KeyboardButton(text="🎟 Промокод")],
    ]
    if rules_url:
        buttons.append([KeyboardButton(text="📜 Правила")])
    if is_adm:
        buttons.append([KeyboardButton(text="🔧 Админ панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def admin_kb(is_superadmin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Добавить админа",     callback_data="admin:add_admin"),
         InlineKeyboardButton(text="➖ Удалить админа",      callback_data="admin:del_admin")],
        [InlineKeyboardButton(text="📂 Управление сессиями", callback_data="admin:sessions")],
        [InlineKeyboardButton(text="👥 Подписчики",          callback_data="admin:subscribers"),
         InlineKeyboardButton(text="📋 Логи",                callback_data="admin:logs")],
        [InlineKeyboardButton(text="📢 Обязательные каналы", callback_data="admin:channels")],
        [InlineKeyboardButton(text="⚙️ Настроить правила",   callback_data="admin:rules")],
        [InlineKeyboardButton(text="📣 Рассылка",             callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🎁 Выдать подписку",      callback_data="admin:grant_sub"),
         InlineKeyboardButton(text="🎟 Промокоды",            callback_data="admin:promos")],
    ]
    if is_superadmin:
        rows.append([InlineKeyboardButton(text="🤖 Резервный бот", callback_data="admin:backup")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sessions_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Авторизоваться по номеру", callback_data="sess:phone")],
        [InlineKeyboardButton(text="📁 Загрузить .session файл",  callback_data="sess:file")],
        [InlineKeyboardButton(text="📝 Вставить StringSession",   callback_data="sess:upload")],
        [InlineKeyboardButton(text="🗑 Удалить сессию",           callback_data="sess:delete")],
        [InlineKeyboardButton(text="✅ Проверить все сессии",     callback_data="sess:check")],
        [InlineKeyboardButton(text="🧪 Тест сессии",              callback_data="sess:test")],
        [InlineKeyboardButton(text="◀️ Назад",                    callback_data="admin:back")],
    ])


def channels_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал",  callback_data="chan:add")],
        [InlineKeyboardButton(text="🗑 Удалить канал",   callback_data="chan:delete")],
        [InlineKeyboardButton(text="📋 Список каналов", callback_data="chan:list")],
        [InlineKeyboardButton(text="◀️ Назад",           callback_data="admin:back")],
    ])


# ─── CryptoBot ─────────────────────────────────────────────

async def create_invoice(amount: float, days: int, user_id: int) -> dict:
    desc = "Подписка навсегда" if days == 0 else f"Подписка на {days} дней"
    label = f"sub_{user_id}_{days}_{int(datetime.now().timestamp())}"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json={"asset": "USDT", "amount": str(amount),
                  "description": desc, "payload": label}
        ) as resp:
            data = await resp.json()
    if data.get("ok"):
        inv = data["result"]
        return {"invoice_id": str(inv["invoice_id"]), "pay_url": inv["pay_url"]}
    raise Exception(f"CryptoBot: {data}")


async def poll_payments(bot: Bot):
    """Каждые 30 сек проверяет оплаченные инвойсы и активирует подписки"""
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
                        days = payment["duration_days"]
                        await db.activate_subscription(payment["user_id"], days)
                        label = "навсегда" if days == 0 else f"на {days} дней"
                        try:
                            await bot.send_message(
                                payment["user_id"],
                                f"✅ Оплата подтверждена! Подписка активирована {label}. 🎉"
                            )
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"poll_payments: {e}")


async def hourly_session_check(bot: Bot):
    """Каждый час проверяет валидность сессий"""
    while True:
        await asyncio.sleep(3600)
        try:
            sessions = await db.get_all_sessions()
            bad = []
            for s in sessions:
                try:
                    client = TelegramClient(StringSession(s["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
                    await client.connect()
                    if not await client.is_user_authorized():
                        bad.append(s["id"])
                    await client.disconnect()
                except Exception:
                    bad.append(s["id"])
            if bad:
                for adm in await db.get_all_admins():
                    try:
                        await bot.send_message(
                            adm["user_id"],
                            f"⚠️ Проблемные сессии (ID): {', '.join(map(str, bad))}\n"
                            f"Удалите их в Админ панель → Управление сессиями."
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"hourly_session_check: {e}")


async def daily_backup_reminder(bot: Bot):
    """Каждый день в 12:00 UTC присылает всем пользователям напоминание о резервном боте.
    Если BACKUP_BOT_USERNAME не задан — ничего не отправляется."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            # До следующего 12:00 UTC
            next_run = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run = next_run.replace(day=next_run.day + 1)
            wait_secs = (next_run - now).total_seconds()
            await asyncio.sleep(wait_secs)

            if not BACKUP_BOT_USERNAME:
                continue  # резервный бот не настроен — пропускаем

            users = await db.get_all_users()
            text = (
                f"ℹ️ <b>Напоминание</b>\n\n"
                f"Если основной бот недоступен, используйте резервный:\n"
                f"👉 @{BACKUP_BOT_USERNAME}\n\n"
                f"Резервный бот имеет те же функции и подписку."
            )
            for uid in users:
                try:
                    await bot.send_message(uid, text, parse_mode="HTML")
                except Exception:
                    pass
                await asyncio.sleep(0.05)

            logger.info(f"daily_backup_reminder: отправлено {len(users)} пользователям")
        except Exception as e:
            logger.error(f"daily_backup_reminder: {e}")
            await asyncio.sleep(3600)


# ─── Хендлеры ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = message.from_user
    await db.upsert_user(u.id, u.username or "", u.first_name or "")
    if u.id in SUPERADMIN_IDS:
        await db.set_admin(u.id, True)
    is_adm = await db.is_admin(u.id) or u.id in SUPERADMIN_IDS
    has_sub = is_adm or await db.has_active_subscription(u.id)
    not_ch = [] if is_adm else await check_channels(bot, u.id)
    kb = await get_main_keyboard(u.id, is_adm)
    status = "✅ Подписка активна." if has_sub else "⚠️ Нет подписки — нажмите <b>💎 Купить подписку</b>."
    ch_text = ("\n\n📢 Подпишитесь на: " + ", ".join(f"@{c['channel_username']}" for c in not_ch)) if not_ch else ""
    await message.answer(
        f"👋 Привет, <b>{u.first_name}</b>!\n\n"
        f"🛡 <b>Сервис верификации и модерации контента.</b>\n\n"
        f"📌 <b>Возможности:</b>\n"
        f"• 📨 Многоуровневая верификация обращений\n"
        f"• 💎 Подписка 30 дней / навсегда\n"
        f"• 📄 Просмотр статуса доступа\n\n"
        f"{status}{ch_text}",
        parse_mode="HTML", reply_markup=kb
    )


@router.message(Command("support"))
async def cmd_support(message: Message):
    links = " | ".join(f'<a href="tg://user?id={sid}">{sid}</a>' for sid in SUPERADMIN_IDS)
    await message.answer(f"📞 Супер-администраторы: {links}", parse_mode="HTML")


@router.message(F.text == "📜 Правила")
async def btn_rules(message: Message):
    url = await db.get_setting("rules_url")
    if not url:
        await message.answer("❌ Правила не заданы.")
        return
    text = await db.get_setting("rules_text") or "Правила использования бота:"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Открыть", url=url)]])
    await message.answer(f"📜 {text}", reply_markup=kb)


@router.message(F.text == "📄 Моя подписка")
async def btn_my_sub(message: Message):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 Администратор — подписка бессрочная.")
        return
    user = await db.get_user(uid)
    if not user:
        await message.answer("Напишите /start")
        return
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


@router.message(F.text == "💎 Купить подписку")
async def btn_buy(message: Message, bot: Bot):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 Вы администратор — подписка уже бессрочная!")
        return
    not_ch = await check_channels(bot, uid)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await message.answer(f"📢 Сначала подпишитесь:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]]))
        return
    await message.answer("💎 Выберите срок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 30 дней — 10 USD",  callback_data="buy:30")],
            [InlineKeyboardButton(text="♾️ Навсегда — 100 USD", callback_data="buy:0")],
        ]))


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    days = int(call.data.split(":")[1])
    amount = 10.0 if days == 30 else 100.0
    await call.message.edit_text("⏳ Создаю счёт...")
    try:
        inv = await create_invoice(amount, days, call.from_user.id)
        await db.add_payment(call.from_user.id, amount, days, inv["invoice_id"])
        label = "30 дней" if days == 30 else "навсегда"
        await call.message.edit_text(
            f"💳 Счёт создан!\n💰 <b>{amount} USDT</b> — {label}\n\n"
            f"После оплаты подписка активируется автоматически (до 30 сек).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {amount} USDT", url=inv["pay_url"])]
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
                [InlineKeyboardButton(text="🔄 Проверить снова", callback_data="check_channels")]]))
    else:
        await call.message.edit_text("✅ Отлично! Все каналы подписаны.")
    await call.answer()


# ─── Обращения ─────────────────────────────────────────────

@router.message(F.text == "📨 Подать обращение")
async def btn_report(message: Message, bot: Bot, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    if not is_adm:
        now = datetime.now(timezone.utc)
        last = user_last_report.get(uid)
        if last and (now - last).total_seconds() < 1200:
            remain = int(1200 - (now - last).total_seconds())
            mins, secs = divmod(remain, 60)
            await message.answer(f"⏳ Следующее обращение доступно через {mins} мин. {secs} сек.")
            return
        if not await db.has_active_subscription(uid):
            await message.answer("❌ Нет подписки:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📅 30 дней — 10 USD",  callback_data="buy:30")],
                    [InlineKeyboardButton(text="♾️ Навсегда — 100 USD", callback_data="buy:0")],
                ]))
            return
        not_ch = await check_channels(bot, uid)
        if not_ch:
            names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
            await message.answer(f"📢 Подпишитесь:\n{names}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]]))
            return
    await state.set_state(States.waiting_report_type)
    await message.answer(
        "📨 <b>Подать обращение</b>\n\nЧто вы хотите обжаловать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Сообщение / пост", callback_data="rtype:message")],
            [InlineKeyboardButton(text="📢 Канал / группа",   callback_data="rtype:channel")],
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
            "• <code>https://t.me/username/123</code>\n"
            "• <code>https://t.me/c/1234567890/123</code>",
            parse_mode="HTML"
        )
    elif rtype == "channel":
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "📢 <b>Жалоба на канал / группу</b>\n\n"
            "Отправьте @юзернейм или ссылку на канал:\n"
            "• <code>@durov</code>\n"
            "• <code>https://t.me/durov</code>",
            parse_mode="HTML"
        )
    else:  # bot
        await state.set_state(States.waiting_peer_username)
        await call.message.edit_text(
            "🤖 <b>Жалоба на бота</b>\n\n"
            "Отправьте @юзернейм бота:\n"
            "• <code>@somebot</code>\n"
            "• <code>https://t.me/somebot</code>",
            parse_mode="HTML"
        )
    await call.answer()


@router.message(States.waiting_report_link)
async def got_link(message: Message, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    chat_id, msg_id = parse_tg_link(message.text.strip())
    if not chat_id:
        await message.answer("❌ Неверный формат. Пример: https://t.me/username/123")
        return
    if await db.has_reported_before(uid, chat_id, msg_id):
        if is_adm:
            await message.answer("⚠️ Повторное обращение на эту публикацию — для администраторов разрешено.")
        else:
            await db.revoke_subscription(uid)
            await state.clear()
            await message.answer(
                "❌ Повторное обращение на ту же публикацию недопустимо.\nДоступ приостановлен. Оформите новую подписку.",
                reply_markup=await get_main_keyboard(uid, False))
            return
    await state.update_data(chat_id=chat_id, message_id=msg_id)
    await state.set_state(States.waiting_report_reason)
    await message.answer("📋 Выберите категорию нарушения или опишите своими словами:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",          callback_data="reason:spam")],
            [InlineKeyboardButton(text="🔪 Насилие",       callback_data="reason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",   callback_data="reason:porn")],
            [InlineKeyboardButton(text="❓ Другое",        callback_data="reason:other")],
            [InlineKeyboardButton(text="✏️ Описать нарушение", callback_data="reason:custom")],
        ]))


@router.callback_query(F.data == "reason:custom", States.waiting_report_reason)
async def cb_reason_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_custom_text)
    await call.message.edit_text(
        "✏️ Опишите нарушение своими словами:\n\n"
        "<i>Например: «Контент нарушает правила сообщества и содержит вводящую в заблуждение информацию»</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(States.waiting_custom_text)
async def got_custom_text(message: Message, state: FSMContext, bot: Bot):
    custom_text = message.text.strip()
    if not custom_text:
        await message.answer("❌ Описание не может быть пустым. Введите текст:")
        return
    if len(custom_text) > 512:
        await message.answer(f"❌ Текст слишком длинный ({len(custom_text)} символов). Максимум 512. Попробуйте короче:")
        return
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ Описание принято. Обрабатываю обращение...\n\n📝 <i>{custom_text[:100]}{'...' if len(custom_text)>100 else ''}</i>", parse_mode="HTML")
    await _send_reports(
        bot=bot,
        user_id=message.from_user.id,
        chat_id_str=data["chat_id"],
        msg_id_str=data["message_id"],
        reason_name="✏️ Свой текст",
        reason_obj=InputReportReasonOther(),
        custom_text=custom_text,
        reply_target=message
    )


def _parse_peer_username(text: str) -> str | None:
    """Извлекает username из @username или t.me/username. Возвращает чистый username или None."""
    text = text.strip()
    m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", text)
    if m:
        return m.group(1)
    if text.startswith("@"):
        uname = text[1:]
        if len(uname) >= 4:
            return uname
    if re.match(r"^[A-Za-z0-9_]{4,}$", text):
        return text
    return None


@router.message(States.waiting_peer_username)
async def got_peer_username(message: Message, state: FSMContext):
    uname = _parse_peer_username(message.text or "")
    if not uname:
        await message.answer(
            "❌ Неверный формат. Отправьте @username или https://t.me/username:"
        )
        return
    data = await state.get_data()
    peer_type = data.get("peer_type", "channel")
    await state.update_data(peer_username=uname)
    await state.set_state(States.waiting_peer_reason)
    icon = "📢" if peer_type == "channel" else "🤖"
    await message.answer(
        f"{icon} <code>@{uname}</code>\n\n"
        "📋 Выберите причину обращения:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",              callback_data="preason:spam")],
            [InlineKeyboardButton(text="🔪 Насилие",           callback_data="preason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",       callback_data="preason:porn")],
            [InlineKeyboardButton(text="❓ Другое",            callback_data="preason:other")],
            [InlineKeyboardButton(text="✏️ Описать нарушение", callback_data="preason:custom")],
        ])
    )


@router.callback_query(F.data == "preason:custom", States.waiting_peer_reason)
async def cb_peer_reason_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(States.waiting_peer_custom)
    await call.message.edit_text(
        "✏️ Опишите нарушение своими словами (до 512 символов):",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(States.waiting_peer_custom)
async def got_peer_custom(message: Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым:"); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512:"); return
    data = await state.get_data()
    await state.clear()
    await message.answer(
        f"✅ Описание принято. Обрабатываю обращение...\n\n"
        f"📝 <i>{text[:100]}{'...' if len(text) > 100 else ''}</i>",
        parse_mode="HTML"
    )
    await _send_peer_reports(
        bot=bot,
        user_id=message.from_user.id,
        peer_username=data["peer_username"],
        peer_type=data.get("peer_type", "channel"),
        reason_name="✏️ Свой текст",
        reason_obj=InputReportReasonOther(),
        custom_text=text,
        reply_target=message
    )


@router.callback_query(F.data.startswith("preason:"), States.waiting_peer_reason)
async def cb_peer_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[key]
    data = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_peer_reports(
        bot=bot,
        user_id=call.from_user.id,
        peer_username=data["peer_username"],
        peer_type=data.get("peer_type", "channel"),
        reason_name=reason_name,
        reason_obj=reason_obj,
        call=call
    )


async def _send_peer_reports(bot: Bot, user_id: int, peer_username: str, peer_type: str,
                              reason_name: str, reason_obj, custom_text: str = "",
                              reply_target=None, call=None):
    """Отправка обращений на весь канал / бота через пул аккаунтов."""
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()
    icon = "📢" if peer_type == "channel" else "🤖"

    async def _no_sessions():
        txt = "❌ Нет активных каналов верификации. Обратитесь к администратору."
        if call:
            await call.message.edit_text(txt)
        elif reply_target:
            await reply_target.answer(txt)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))

    if not sessions:
        await _no_sessions(); return

    total = len(sessions)
    if call:
        await call.message.edit_text(f"⏳ Обрабатываю обращение на {icon} @{peer_username}...")
        status_msg = await call.message.answer(f"⏳ Верифицирую... (0/{total})")
    else:
        status_msg = await reply_target.answer(f"⏳ Верифицирую... (0/{total})")

    success = errors = 0
    for i, sess in enumerate(sessions, 1):
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                try:
                    peer = await client.get_input_entity(peer_username)
                    await client(ReportPeerRequest(
                        peer=peer,
                        reason=reason_obj,
                        message=custom_text
                    ))
                    success += 1
                except Exception as e:
                    logger.warning(f"Peer-сессия {sess['id']} ошибка: {e}")
                    errors += 1
            else:
                errors += 1
            await client.disconnect()
        except Exception as e:
            logger.error(f"Peer-сессия {sess['id']} подключение: {e}")
            errors += 1
        await asyncio.sleep(random.uniform(0.5, 1.0))
        try:
            await status_msg.edit_text(f"⏳ Верифицирую... ({i}/{total})")
        except Exception:
            pass

    if not is_adm:
        user_last_report[user_id] = datetime.now(timezone.utc)

    marks = "✅ " * min(success, 20) + "❌ " * min(errors, 20)
    extra = f"\n\n📝 Описание: <i>{custom_text[:80]}{'...' if len(custom_text)>80 else ''}</i>" if custom_text else ""
    await status_msg.edit_text(
        f"📊 <b>Обращение обработано</b>\n\n"
        f"{icon} Объект: <code>@{peer_username}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n\n"
        f"🔁 Каналов верификации: <b>{total}</b>\n"
        f"✅ Принято: <b>{success}</b>\n"
        f"❌ Отклонено: <b>{errors}</b>\n\n{marks}",
        parse_mode="HTML"
    )
    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))


async def _send_reports(bot: Bot, user_id: int, chat_id_str: str, msg_id_str: str,
                        reason_name: str, reason_obj, custom_text: str = "",
                        reply_target=None, call=None):
    """Отправка обращений через пул аккаунтов."""
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()

    async def _no_sessions():
        text = "❌ Нет активных каналов верификации. Обратитесь к администратору."
        if call:
            await call.message.edit_text(text)
        elif reply_target:
            await reply_target.answer(text)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))

    if not sessions:
        await _no_sessions()
        return

    total = len(sessions)

    if call:
        await call.message.edit_text(f"⏳ Обрабатываю обращение ({reason_name})...")
        status_msg = await call.message.answer(f"⏳ Верифицирую... (0/{total})")
    else:
        status_msg = await reply_target.answer(f"⏳ Верифицирую... (0/{total})")

    success = errors = 0
    report_text = custom_text

    for i, sess in enumerate(sessions, 1):
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                try:
                    peer = await client.get_input_entity(
                        int(chat_id_str) if chat_id_str.lstrip("-").isdigit() else chat_id_str
                    )
                    await client(ReportRequest(
                        peer=peer,
                        id=[int(msg_id_str)],
                        reason=reason_obj,
                        message=report_text   # ← свой текст или "" для стандартных причин
                    ))
                    success += 1
                except Exception as e:
                    logger.warning(f"Сессия {sess['id']} ошибка: {e}")
                    errors += 1
            else:
                errors += 1
            await client.disconnect()
        except Exception as e:
            logger.error(f"Сессия {sess['id']} подключение: {e}")
            errors += 1
        await asyncio.sleep(random.uniform(0.5, 1.0))
        try:
            await status_msg.edit_text(f"⏳ Верифицирую... ({i}/{total})")
        except Exception:
            pass

    await db.add_report_log(user_id, chat_id_str, msg_id_str, success, total)
    if not is_adm:
        user_last_report[user_id] = datetime.now(timezone.utc)

    marks = "✅ " * min(success, 20) + "❌ " * min(errors, 20)
    extra = f"\n\n📝 Описание: <i>{custom_text[:80]}{'...' if len(custom_text)>80 else ''}</i>" if custom_text else ""
    await status_msg.edit_text(
        f"📊 <b>Обращение обработано</b>\n\n"
        f"📌 Категория: {reason_name}{extra}\n\n"
        f"🔁 Каналов верификации: <b>{total}</b>\n"
        f"✅ Принято: <b>{success}</b>\n"
        f"❌ Отклонено: <b>{errors}</b>\n\n{marks}",
        parse_mode="HTML"
    )
    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_keyboard(user_id, is_adm))


@router.callback_query(F.data.startswith("reason:"), States.waiting_report_reason)
async def cb_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    reason_key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[reason_key]
    data = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_reports(
        bot=bot,
        user_id=call.from_user.id,
        chat_id_str=data["chat_id"],
        msg_id_str=data["message_id"],
        reason_name=reason_name,
        reason_obj=reason_obj,
        call=call
    )


# ─── Админ-панель ──────────────────────────────────────────

@router.message(F.text == "🔧 Админ панель")
async def btn_admin(message: Message):
    uid = message.from_user.id
    if not (await db.is_admin(uid) or uid in SUPERADMIN_IDS):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("🔧 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb(uid in SUPERADMIN_IDS))


@router.callback_query(F.data == "admin:back")
async def cb_back(call: CallbackQuery):
    await call.message.edit_text("🔧 <b>Админ-панель</b>", parse_mode="HTML",
                                 reply_markup=admin_kb(call.from_user.id in SUPERADMIN_IDS))
    await call.answer()


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


# ─── Рассылка ──────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_broadcast_text)
    await call.message.edit_text(
        "📣 <b>Рассылка</b>\n\n"
        "Отправьте текст сообщения (поддерживается HTML-форматирование).\n"
        "Сообщение будет отправлено <b>всем пользователям</b> бота.\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]
        ])
    )
    await call.answer()


@router.message(States.waiting_broadcast_text)
async def got_broadcast_text(message: Message, bot: Bot, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.")
        return

    await state.clear()
    users = await db.get_all_users()
    status_msg = await message.answer(f"📤 Начинаю рассылку... (0/{len(users)})")

    ok = 0
    fail = 0
    for i, uid in enumerate(users, 1):
        try:
            if message.text:
                await bot.send_message(uid, message.text, parse_mode="HTML")
            else:
                await message.copy_to(uid)
            ok += 1
        except Exception:
            fail += 1
        if i % 20 == 0:
            try:
                await status_msg.edit_text(f"📤 Рассылка... ({i}/{len(users)})")
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"📨 Отправлено: <b>{ok}</b>\n"
        f"❌ Не доставлено: <b>{fail}</b>",
        parse_mode="HTML"
    )
    await db.log_admin_action(message.from_user.id, "broadcast", f"ok={ok} fail={fail}")


# ─── Выдача подписки ────────────────────────────────────────

@router.callback_query(F.data == "admin:grant_sub")
async def cb_grant_sub(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_grant_uid)
    await call.message.edit_text(
        "🎁 <b>Выдать подписку</b>\n\n"
        "Введите Telegram ID пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]
        ])
    )
    await call.answer()


@router.message(States.waiting_grant_uid)
async def got_grant_uid(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный формат. Введите числовой Telegram ID:"); return
    await state.update_data(grant_uid=uid)
    await state.set_state(States.waiting_grant_days)
    await message.answer(
        f"👤 ID: <code>{uid}</code>\n\n"
        "Введите количество дней подписки\n"
        "(0 = бессрочная):",
        parse_mode="HTML"
    )


@router.message(States.waiting_grant_days)
async def got_grant_days(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        days = int(message.text.strip())
        if days < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число >= 0:"); return

    data = await state.get_data()
    await state.clear()
    uid = data["grant_uid"]

    user = await db.get_user(uid)
    if not user:
        await message.answer("❌ Пользователь с таким ID не найден в боте."); return

    if days == 0:
        await db.activate_subscription(uid, 0)
        desc = "бессрочная"
    else:
        new_end = await db.grant_subscription(uid, days)
        desc = f"до {new_end.strftime('%d.%m.%Y %H:%M')} UTC (+{days} дн.)"

    await db.log_admin_action(message.from_user.id, "grant_sub", f"uid={uid} days={days}")
    await message.answer(
        f"✅ Подписка выдана!\n\n"
        f"👤 ID: <code>{uid}</code>\n"
        f"📅 {desc}",
        parse_mode="HTML"
    )
    try:
        label = "бессрочная ♾️" if days == 0 else f"на {days} дн."
        await bot.send_message(
            uid,
            f"🎉 Вам выдана подписка {label}!\n"
            "Спасибо, что вы с нами."
        )
    except Exception:
        pass


# ─── Промокоды (админ) ──────────────────────────────────────

def _promo_list_text(promos: list) -> str:
    if not promos:
        return "🎟 <b>Промокоды</b>\n\nПромокодов пока нет."
    lines = ["🎟 <b>Промокоды</b>\n"]
    for p in promos:
        remaining = p["max_uses"] - p["uses"]
        lines.append(
            f"• <code>{p['code']}</code> — {p['days']} дн. | "
            f"активаций: {p['uses']}/{p['max_uses']} (осталось: {remaining})"
        )
    return "\n".join(lines)


def _promo_list_kb(promos: list) -> InlineKeyboardMarkup:
    rows = []
    for p in promos:
        rows.append([InlineKeyboardButton(
            text=f"🗑 {p['code']}",
            callback_data=f"promo:del:{p['code']}"
        )])
    rows.append([InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promo_new")])
    rows.append([InlineKeyboardButton(text="◀️ Назад",            callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:promos")
async def cb_promos(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    promos = await db.get_all_promo_codes()
    await call.message.edit_text(
        _promo_list_text(promos),
        parse_mode="HTML",
        reply_markup=_promo_list_kb(promos)
    )
    await call.answer()


@router.callback_query(F.data.startswith("promo:del:"))
async def cb_promo_delete(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    code = call.data.split("promo:del:")[1]
    await db.delete_promo_code(code)
    await db.log_admin_action(call.from_user.id, "delete_promo", f"code={code}")
    promos = await db.get_all_promo_codes()
    await call.message.edit_text(
        _promo_list_text(promos),
        parse_mode="HTML",
        reply_markup=_promo_list_kb(promos)
    )
    await call.answer(f"✅ Промокод {code} удалён")


@router.callback_query(F.data == "admin:promo_new")
async def cb_promo_new(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_promo_new_code)
    await call.message.edit_text(
        "➕ <b>Новый промокод</b>\n\n"
        "Введите код (латинские буквы/цифры, без пробелов):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:promos")]
        ])
    )
    await call.answer()


@router.message(States.waiting_promo_new_code)
async def got_promo_new_code(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    code = message.text.strip().upper()
    if not code.replace("_", "").replace("-", "").isalnum():
        await message.answer("❌ Только латинские буквы, цифры, _ и -. Повторите:"); return
    existing = await db.get_promo_code(code)
    if existing:
        await message.answer(f"❌ Промокод <code>{code}</code> уже существует. Введите другой:", parse_mode="HTML"); return
    await state.update_data(promo_code=code)
    await state.set_state(States.waiting_promo_new_days)
    await message.answer(
        f"✅ Код: <code>{code}</code>\n\n"
        "Введите количество дней подписки:",
        parse_mode="HTML"
    )


@router.message(States.waiting_promo_new_days)
async def got_promo_new_days(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число > 0:"); return
    await state.update_data(promo_days=days)
    await state.set_state(States.waiting_promo_new_uses)
    await message.answer(
        f"📅 Дней: <b>{days}</b>\n\n"
        "Введите максимальное количество активаций:",
        parse_mode="HTML"
    )


@router.message(States.waiting_promo_new_uses)
async def got_promo_new_uses(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        uses = int(message.text.strip())
        if uses <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число > 0:"); return

    data = await state.get_data()
    await state.clear()
    code = data["promo_code"]
    days = data["promo_days"]

    await db.create_promo_code(code, days, uses, message.from_user.id)
    await db.log_admin_action(message.from_user.id, "create_promo", f"code={code} days={days} max_uses={uses}")
    await message.answer(
        f"✅ Промокод создан!\n\n"
        f"🎟 Код: <code>{code}</code>\n"
        f"📅 Дней: <b>{days}</b>\n"
        f"🔢 Активаций: <b>{uses}</b>",
        parse_mode="HTML"
    )


# ─── Промокод (пользователь) ────────────────────────────────

@router.message(F.text == "🎟 Промокод")
async def btn_promo(message: Message, state: FSMContext):
    await state.set_state(States.waiting_promo_activate)
    await message.answer(
        "🎟 <b>Активация промокода</b>\n\n"
        "Введите ваш промокод:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="promo:cancel")]
        ])
    )


@router.callback_query(F.data == "promo:cancel")
async def cb_promo_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer()


@router.message(States.waiting_promo_activate)
async def got_promo_activate(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    code = message.text.strip().upper()
    promo = await db.get_promo_code(code)
    if not promo:
        await message.answer("❌ Промокод не найден.")
        return
    if promo["uses"] >= promo["max_uses"]:
        await message.answer("❌ Промокод уже использован максимальное количество раз.")
        return

    uid = message.from_user.id
    days = promo["days"]
    new_end = await db.grant_subscription(uid, days)
    await db.use_promo_code(code)

    end_str = new_end.strftime('%d.%m.%Y %H:%M') + " UTC" if new_end else ""
    await message.answer(
        f"✅ Промокод активирован!\n\n"
        f"🎁 Добавлено дней: <b>{days}</b>\n"
        f"📅 Подписка до: <b>{end_str}</b>",
        parse_mode="HTML"
    )


# ─── Сессии ────────────────────────────────────────────────

@router.callback_query(F.data == "admin:sessions")
async def cb_sessions(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    n = len(await db.get_all_sessions())
    await call.message.edit_text(f"📂 <b>Сессии</b>\nАктивных: <b>{n}</b>", parse_mode="HTML", reply_markup=sessions_kb())
    await call.answer()


@router.callback_query(F.data == "sess:upload")
async def cb_sess_upload(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_session_data)
    await call.message.edit_text(
        "📤 <b>Как получить StringSession:</b>\n\n"
        "1. Установить telethon: <code>pip install telethon</code>\n"
        "2. Запустить скрипт:\n\n"
        "<code>from telethon.sync import TelegramClient\n"
        "from telethon.sessions import StringSession\n"
        "with TelegramClient(StringSession(), API_ID, API_HASH) as c:\n"
        "    print(c.session.save())</code>\n\n"
        "3. Авторизоваться по номеру телефона\n"
        "4. Скопировать длинную строку и отправить сюда 👇",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(States.waiting_session_data)
async def got_session(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    raw = message.text.strip()
    await message.answer("⏳ Проверяю сессию...")
    try:
        client = TelegramClient(StringSession(raw), TELETHON_API_ID, TELETHON_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await message.answer("❌ Сессия недействительна (аккаунт не авторизован).")
            return
        me = await client.get_me()
        await client.disconnect()
    except Exception as e:
        await message.answer(f"❌ Ошибка проверки: {e}"); return
    sid = await db.add_session(raw)
    await db.log_admin_action(message.from_user.id, "add_session", str(sid))
    await state.clear()
    await message.answer(
        f"✅ Сессия добавлена (ID: {sid})\n"
        f"👤 Аккаунт: <b>{me.first_name}</b> (@{me.username or '—'})",
        parse_mode="HTML", reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS))


# ─── Добавление сессии: авторизация по номеру ──────────────

@router.callback_query(F.data == "sess:phone")
async def cb_sess_phone(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_phone)
    await call.message.edit_text(
        "📱 <b>Авторизация через номер телефона</b>\n\n"
        "Введите номер в международном формате:\n"
        "<code>+79001234567</code>\n\n"
        "⚠️ Telegram пришлёт код подтверждения в приложение или SMS.",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(States.waiting_phone)
async def got_phone(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    phone = message.text.strip()
    if not re.match(r"^\+\d{7,15}$", phone):
        await message.answer("❌ Неверный формат. Введите номер вида <code>+79001234567</code>:", parse_mode="HTML")
        return
    uid = message.from_user.id
    wait_msg = await message.answer("⏳ Подключаюсь и запрашиваю код...")
    client = TelegramClient(StringSession(), TELETHON_API_ID, TELETHON_API_HASH)
    try:
        await client.connect()
        result = await client.send_code_request(phone)
    except FloodWaitError as e:
        await client.disconnect()
        await wait_msg.edit_text(f"❌ Flood-лимит Telegram: подождите {e.seconds} секунд и попробуйте снова.")
        await state.clear(); return
    except Exception as e:
        await client.disconnect()
        await wait_msg.edit_text(f"❌ Ошибка: {e}")
        await state.clear(); return
    _auth_clients[uid] = client
    await state.update_data(phone=phone, phone_code_hash=result.phone_code_hash)
    await state.set_state(States.waiting_code)
    await wait_msg.edit_text(
        "✅ Код отправлен!\n\n"
        "📲 Введите код из Telegram (или SMS).\n"
        "Вводите <b>без пробелов</b>, например: <code>12345</code>",
        parse_mode="HTML"
    )


@router.message(States.waiting_code)
async def got_code(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    uid = message.from_user.id
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    client = _auth_clients.get(uid)
    if not client:
        await message.answer("❌ Сессия авторизации истекла. Начните снова через меню сессий.")
        await state.clear(); return
    try:
        await client.sign_in(data["phone"], code, phone_code_hash=data["phone_code_hash"])
    except SessionPasswordNeededError:
        await state.set_state(States.waiting_2fa)
        await message.answer(
            "🔐 На аккаунте включена <b>двухфакторная аутентификация</b>.\n\n"
            "Введите пароль 2FA:",
            parse_mode="HTML"
        )
        return
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте ещё раз:")
        return
    except PhoneCodeExpiredError:
        await client.disconnect()
        _auth_clients.pop(uid, None)
        await message.answer("❌ Код устарел. Начните авторизацию заново через меню сессий.")
        await state.clear(); return
    except Exception as e:
        await client.disconnect()
        _auth_clients.pop(uid, None)
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear(); return
    await _finish_phone_auth(message, state, client, uid)


@router.message(States.waiting_2fa)
async def got_2fa(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    uid = message.from_user.id
    password = message.text.strip()
    client = _auth_clients.get(uid)
    if not client:
        await message.answer("❌ Сессия авторизации истекла. Начните снова через меню сессий.")
        await state.clear(); return
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        await message.answer("❌ Неверный пароль 2FA. Попробуйте ещё раз:")
        return
    except Exception as e:
        await client.disconnect()
        _auth_clients.pop(uid, None)
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear(); return
    await _finish_phone_auth(message, state, client, uid)


async def _finish_phone_auth(message: Message, state: FSMContext, client: TelegramClient, uid: int):
    """Сохраняет сессию после успешной авторизации по номеру."""
    try:
        me = await client.get_me()
        session_str = client.session.save()
        await client.disconnect()
    except Exception as e:
        await client.disconnect()
        _auth_clients.pop(uid, None)
        await message.answer(f"❌ Ошибка получения данных: {e}")
        await state.clear(); return
    _auth_clients.pop(uid, None)
    sid = await db.add_session(session_str)
    await db.log_admin_action(uid, "add_session_phone", str(sid))
    await state.clear()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    username = f"@{me.username}" if me.username else "—"
    await message.answer(
        f"✅ <b>Авторизация успешна!</b>\n\n"
        f"👤 {name} ({username})\n"
        f"📱 ID: <code>{me.id}</code>\n"
        f"🗂 Сессия #{sid} сохранена в базе.",
        parse_mode="HTML",
        reply_markup=sessions_kb()
    )


# ─── Добавление сессии: загрузка .session файла ────────────

@router.callback_query(F.data == "sess:file")
async def cb_sess_file(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_session_file)
    await call.message.edit_text(
        "📁 <b>Загрузка .session файла</b>\n\n"
        "Отправьте файл с расширением <code>.session</code> как документ.\n\n"
        "Это стандартный файл сессии Telethon (SQLite).\n"
        "Бот автоматически конвертирует его в StringSession.",
        parse_mode="HTML"
    )
    await call.answer()


@router.message(States.waiting_session_file)
async def got_session_file(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if not message.document:
        await message.answer("❌ Пришлите файл <code>.session</code> как документ (не как фото).", parse_mode="HTML")
        return
    fname = message.document.file_name or ""
    if not fname.endswith(".session"):
        await message.answer("❌ Файл должен иметь расширение <code>.session</code>", parse_mode="HTML")
        return
    wait_msg = await message.answer("⏳ Загружаю и проверяю файл...")
    uid = message.from_user.id
    # Скачиваем во временный файл
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, "upload")        # без .session — Telethon сам добавит
    tmp_file = tmp_path + ".session"
    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, destination=tmp_file)
        # Открываем как SQLiteSession через TelegramClient
        client = TelegramClient(tmp_path, TELETHON_API_ID, TELETHON_API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await wait_msg.edit_text("❌ Сессия недействительна или устарела.")
            return
        me = await client.get_me()
        session_str = client.session.save()
        await client.disconnect()
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка: {e}")
        return
    finally:
        # Удаляем временные файлы
        try:
            os.remove(tmp_file)
            os.rmdir(tmp_dir)
        except Exception:
            pass
    sid = await db.add_session(session_str)
    await db.log_admin_action(uid, "add_session_file", str(sid))
    await state.clear()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    username = f"@{me.username}" if me.username else "—"
    await wait_msg.edit_text(
        f"✅ <b>Сессия из файла добавлена!</b>\n\n"
        f"👤 {name} ({username})\n"
        f"📱 ID: <code>{me.id}</code>\n"
        f"🗂 Сессия #{sid} сохранена в базе.",
        parse_mode="HTML",
        reply_markup=sessions_kb()
    )


@router.callback_query(F.data == "sess:delete")
async def cb_sess_delete(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("ℹ️ Нет сессий.", reply_markup=sessions_kb())
        await call.answer(); return
    buttons = [[InlineKeyboardButton(text=f"Сессия #{s['id']} ({str(s.get('created_at',''))[:16]})",
        callback_data=f"delsess:{s['id']}")] for s in sessions]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")])
    await call.message.edit_text("Выберите сессию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("delsess:"))
async def cb_do_delsess(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sid = int(call.data.split(":")[1])
    await db.delete_session(sid)
    await db.log_admin_action(call.from_user.id, "del_session", str(sid))
    await call.message.edit_text(f"✅ Сессия #{sid} удалена.", reply_markup=sessions_kb())
    await call.answer()


@router.callback_query(F.data == "sess:check")
async def cb_sess_check(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await call.message.edit_text("⏳ Проверяю сессии...")
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("ℹ️ Нет сессий.", reply_markup=sessions_kb())
        await call.answer(); return
    lines = []
    for s in sessions:
        try:
            client = TelegramClient(StringSession(s["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await client.connect()
            ok = await client.is_user_authorized()
            if ok:
                me = await client.get_me()
                lines.append(f"✅ #{s['id']} — {me.first_name} (@{me.username or '—'})")
            else:
                lines.append(f"❌ #{s['id']} — не авторизован")
            await client.disconnect()
        except Exception as e:
            lines.append(f"❌ #{s['id']} — {e}")
    await call.message.edit_text("📋 <b>Проверка сессий:</b>\n\n" + "\n".join(lines),
                                 parse_mode="HTML", reply_markup=sessions_kb())
    await call.answer()


@router.callback_query(F.data == "sess:test")
async def cb_sess_test(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("❌ Нет сессий.", reply_markup=sessions_kb())
        await call.answer(); return
    s = sessions[0]
    try:
        client = TelegramClient(StringSession(s["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            await call.message.edit_text(
                f"✅ Тест пройден!\nСессия #{s['id']} — <b>{me.first_name}</b> (@{me.username or '—'})",
                parse_mode="HTML", reply_markup=sessions_kb())
        else:
            await client.disconnect()
            await call.message.edit_text("❌ Сессия недействительна.", reply_markup=sessions_kb())
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка теста: {e}", reply_markup=sessions_kb())
    await call.answer()


# ─── Каналы ────────────────────────────────────────────────

@router.callback_query(F.data == "admin:channels")
async def cb_channels(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await call.message.edit_text("📢 <b>Обязательные каналы</b>", parse_mode="HTML", reply_markup=channels_kb())
    await call.answer()


@router.callback_query(F.data == "chan:add")
async def cb_chan_add(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_channel_add)
    await call.message.edit_text(
        "➕ Введите username или ID канала:\n"
        "Примеры: <code>@mychannel</code> или <code>-1001234567890</code>\n\n"
        "⚠️ Бот должен быть администратором канала!", parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_channel_add)
async def got_channel(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    raw = message.text.strip()
    username = raw.lstrip("@")
    chat_ref = int(raw) if raw.lstrip("-").isdigit() else f"@{username}"
    try:
        bot_id = (await bot.get_me()).id
        member = await bot.get_chat_member(chat_ref, bot_id)
        if member.status not in ("administrator", "creator"):
            await message.answer("❌ Бот не является администратором канала.\nДобавьте бота и повторите.")
            return
        chat = await bot.get_chat(chat_ref)
        await db.add_force_channel(username, chat.id)
        await db.log_admin_action(message.from_user.id, "add_channel", username)
        await state.clear()
        await message.answer(f"✅ Канал @{username} добавлен.",
                             reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "chan:delete")
async def cb_chan_delete(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    channels = await db.get_force_channels()
    if not channels:
        await call.message.edit_text("ℹ️ Нет каналов.", reply_markup=channels_kb())
        await call.answer(); return
    buttons = [[InlineKeyboardButton(text=f"@{c['channel_username']}", callback_data=f"delchan:{c['id']}")] for c in channels]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:channels")])
    await call.message.edit_text("Выберите канал:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("delchan:"))
async def cb_do_delchan(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    cid = int(call.data.split(":")[1])
    await db.delete_force_channel(cid)
    await db.log_admin_action(call.from_user.id, "del_channel", str(cid))
    await call.message.edit_text("✅ Канал удалён.", reply_markup=channels_kb())
    await call.answer()


@router.callback_query(F.data == "chan:list")
async def cb_chan_list(call: CallbackQuery):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    channels = await db.get_force_channels()
    def chan_line(c):
        u = c["channel_username"]
        return f"• <a href='https://t.me/{u}'>@{u}</a>"
    text = "ℹ️ Нет каналов." if not channels else "📋 <b>Каналы:</b>\n\n" + "\n".join(chan_line(c) for c in channels)
    await call.message.edit_text(text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:channels")]]))
    await call.answer()


# ─── Правила ───────────────────────────────────────────────

@router.callback_query(F.data == "admin:rules")
async def cb_rules(call: CallbackQuery, state: FSMContext):
    if not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(States.waiting_rules_url)
    await call.message.edit_text("⚙️ Введите URL правил (например: <code>https://example.com/rules</code>):",
                                 parse_mode="HTML")
    await call.answer()


@router.message(States.waiting_rules_url)
async def got_rules_url(message: Message, state: FSMContext):
    await db.set_setting("rules_url", message.text.strip())
    await state.set_state(States.waiting_rules_text)
    await message.answer("✅ URL сохранён!\nТеперь введите текст перед ссылкой (или <code>-</code> чтобы пропустить):",
                         parse_mode="HTML")


@router.message(States.waiting_rules_text)
async def got_rules_text(message: Message, state: FSMContext):
    if message.text.strip() != "-":
        await db.set_setting("rules_text", message.text.strip())
    await state.clear()
    await message.answer("✅ Правила настроены.", reply_markup=admin_kb(message.from_user.id in SUPERADMIN_IDS))


# ─── Резервный бот ─────────────────────────────────────────

@router.callback_query(F.data == "admin:backup")
async def cb_backup(call: CallbackQuery):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    rec = await db.get_backup_bot()
    status = ("🟢 Активен" if rec.get("is_active") else "🟡 В ожидании") if rec else "❌ Не настроен"
    await call.message.edit_text(
        f"🤖 <b>Резервный бот</b>\nСтатус: {status}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Настроить токен", callback_data="backup:set")],
            [InlineKeyboardButton(text="◀️ Назад",            callback_data="admin:back")],
        ]))
    await call.answer()


@router.callback_query(F.data == "backup:set")
async def cb_backup_set(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    await state.set_state(States.waiting_backup_token)
    await call.message.edit_text("🤖 Введите токен резервного бота:")
    await call.answer()


@router.message(States.waiting_backup_token)
async def got_backup_token(message: Message, state: FSMContext):
    if message.from_user.id not in SUPERADMIN_IDS:
        await state.clear(); return
    token = message.text.strip()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.telegram.org/bot{token}/getMe") as r:
                data = await r.json()
        if not data.get("ok"):
            await message.answer("❌ Недействительный токен."); return
        username = data["result"]["username"]
        await db.save_backup_bot(token)
        await db.log_admin_action(message.from_user.id, "set_backup_bot", f"@{username}")
        await state.clear()
        await message.answer(f"✅ Резервный бот @{username} настроен.", reply_markup=admin_kb(True))
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ─── Запуск ────────────────────────────────────────────────

async def main():
    await db.init_db()
    for _sid in SUPERADMIN_IDS:
        await db.upsert_user(_sid, "superadmin", "SuperAdmin")
        await db.set_admin(_sid, True)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(poll_payments(bot))
    asyncio.create_task(hourly_session_check(bot))
    asyncio.create_task(daily_backup_reminder(bot))

    logger.info("Бот запущен ✅")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
