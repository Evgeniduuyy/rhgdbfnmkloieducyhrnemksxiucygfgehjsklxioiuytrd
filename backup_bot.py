import asyncio
import logging
import re
import random
from datetime import datetime, timezone
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
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.types import (
    InputReportReasonSpam, InputReportReasonViolence,
    InputReportReasonPornography, InputReportReasonOther
)
from telethon.sessions import StringSession

import database as db

# ============================================================
#           НАСТРОЙКИ — ЗАПОЛНИ ТЕ ЖЕ ЗНАЧЕНИЯ ЧТО В bot.py
# ============================================================

# Токен резервного бота (другой бот, НЕ тот что в bot.py)
BACKUP_BOT_TOKEN = "токен_резервного_бота"   # Нужен отдельный бот — создай у @BotFather

# Токен основного бота — нужен для мониторинга его доступности
MAIN_BOT_TOKEN = "8770214132:AAEth6uS5IWQNgEcsAuf9eaKUtA_MqM4RwA"

CRYPTOBOT_TOKEN = "596342:AApk7WCgW3Ae8xlUwsGmo4RNFMOFe3lQyFR"
SUPERADMIN_IDS  = {853173723, 1090307552}   # все суперадмины
MAIN_BOT_USERNAME = "Pizza_FenixBot"         # Без @

TELETHON_API_ID   = 35989820
TELETHON_API_HASH = "18cec00c9bef93d0dd475baba4e6c3f4"

DB_PATH = "bot_database.db"  # Тот же файл БД что у основного бота!

# ============================================================

db.DB_PATH = DB_PATH

log_handler = RotatingFileHandler("backup_bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[log_handler, logging.StreamHandler()]
)
logger = logging.getLogger("backup_bot")

router = Router()
user_last_report: dict[int, datetime] = {}
main_fail_count = 0
backup_is_active = False

REPORT_REASONS = {
    "spam":     ("🗑 Спам",        InputReportReasonSpam()),
    "violence": ("🔪 Насилие",     InputReportReasonViolence()),
    "porn":     ("🔞 Порнография", InputReportReasonPornography()),
    "other":    ("❓ Другое",      InputReportReasonOther()),
}


class S(StatesGroup):
    waiting_report_link   = State()
    waiting_report_reason = State()
    waiting_custom_text   = State()
    waiting_new_token     = State()
    waiting_report_type   = State()   # выбор типа: message/channel/bot
    waiting_peer_username = State()   # @username канала или бота
    waiting_peer_reason   = State()   # причина для канала/бота
    waiting_peer_custom   = State()   # свой текст для канала/бота


# ─── Утилиты ───────────────────────────────────────────────

def parse_tg_link(link: str):
    link = link.strip()
    m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if m:
        return "-100" + m.group(1), m.group(2)
    m = re.match(r"https?://t\.me/([^/]+)/(\d+)", link)
    if m:
        return m.group(1), m.group(2)
    return None, None


async def check_channels(bot: Bot, user_id: int) -> list:
    channels = await db.get_force_channels()
    result = []
    for ch in channels:
        try:
            ref = ch["channel_id"] if ch["channel_id"] else f"@{ch['channel_username']}"
            member = await bot.get_chat_member(ref, user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                result.append(ch)
        except Exception:
            result.append(ch)
    return result


async def get_main_kb(user_id: int, is_adm: bool) -> ReplyKeyboardMarkup:
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
    raise Exception(f"CryptoBot: {data}")


# ─── Фоновые задачи ────────────────────────────────────────

async def monitor_main_bot(bot: Bot):
    """Мониторит основной бот. После 3 неудач — активирует резервный."""
    global main_fail_count, backup_is_active
    while True:
        await asyncio.sleep(30)
        if backup_is_active:
            await asyncio.sleep(270)
            continue
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/getMe",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
            alive = data.get("ok", False)
        except Exception:
            alive = False

        if alive:
            main_fail_count = 0
        else:
            main_fail_count += 1
            logger.warning(f"Основной бот недоступен. Попытка {main_fail_count}/3")
            if main_fail_count >= 3:
                backup_is_active = True
                await db.set_backup_bot_active(True)
                logger.info("Резервный бот АКТИВИРОВАН")
                try:
                    for _sid in SUPERADMIN_IDS:
                      await bot.send_message(
                        _sid,
                        "🚨 Основной бот недоступен!\n"
                        "Резервный бот автоматически активирован.\n\n"
                        "Когда основной бот будет восстановлен — используйте кнопку "
                        "<b>🔄 Вернуть основной бот</b> в админ-панели.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass


async def poll_payments(bot: Bot):
    """Проверяет оплату каждые 30 сек (только в активном режиме)."""
    while True:
        await asyncio.sleep(30)
        if not backup_is_active:
            continue
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
                    p = await db.get_payment_by_invoice(inv_id)
                    if p and p["status"] == "pending":
                        await db.update_payment_status(inv_id, "paid")
                        await db.activate_subscription(p["user_id"], p["duration_days"])
                        label = "навсегда" if p["duration_days"] == 0 else f"на {p['duration_days']} дней"
                        try:
                            await bot.send_message(p["user_id"], f"✅ Оплата подтверждена! Подписка {label}. 🎉")
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"poll_payments: {e}")


# ─── Режим ожидания: все сообщения ─────────────────────────

@router.message()
async def handle_any(message: Message, bot: Bot, state: FSMContext):
    if not backup_is_active:
        # Режим ожидания — только информируем
        await message.answer(
            f"⚠️ Данный бот временно не активен.\n"
            f"Используйте основной бот: @{MAIN_BOT_USERNAME}"
        )
        return
    # Активный режим — полноценная работа
    await dispatch(message, bot, state)


async def dispatch(message: Message, bot: Bot, state: FSMContext):
    """Маршрутизация сообщений в активном режиме."""
    cur = await state.get_state()
    text = message.text or ""

    if text.startswith("/start"):
        await do_start(message, bot, state); return
    if text == "/support":
        links = " | ".join(f'<a href="tg://user?id={sid}">{sid}</a>' for sid in SUPERADMIN_IDS)
        await message.answer(f"📞 Супер-администраторы: {links}", parse_mode="HTML"); return
    if cur == S.waiting_report_link:
        await do_got_link(message, state); return
    if cur == S.waiting_custom_text:
        await do_got_custom_text(message, state, bot); return
    if cur == S.waiting_new_token:
        await do_new_token(message, state, bot); return
    if cur == S.waiting_peer_username:
        await do_got_peer_username(message, state); return
    if cur == S.waiting_peer_custom:
        await do_got_peer_custom(message, state, bot); return
    if text == "📜 Правила":
        url = await db.get_setting("rules_url")
        if url:
            await message.answer(await db.get_setting("rules_text") or "Правила:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Открыть", url=url)]]))
        else:
            await message.answer("❌ Правила не заданы.")
        return
    if text == "📄 Моя подписка":    await do_my_sub(message); return
    if text == "💎 Купить подписку": await do_buy_menu(message, bot); return
    if text == "📨 Подать обращение": await do_report_start(message, bot, state); return
    if text == "🎟 Промокод":        await do_promo_activate(message, state); return
    if text == "🔧 Админ панель":    await do_admin(message); return


# ─── Активный режим: хендлеры ──────────────────────────────

async def do_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = message.from_user
    await db.upsert_user(u.id, u.username or "", u.first_name or "")
    if u.id in SUPERADMIN_IDS:
        await db.set_admin(u.id, True)
    is_adm = await db.is_admin(u.id) or u.id in SUPERADMIN_IDS
    has_sub = is_adm or await db.has_active_subscription(u.id)
    not_ch = [] if is_adm else await check_channels(bot, u.id)
    kb = await get_main_kb(u.id, is_adm)
    status = "✅ Подписка активна." if has_sub else "⚠️ Нет подписки — нажмите <b>💎 Купить подписку</b>."
    ch_text = ("\n\n📢 Подпишитесь: " + ", ".join(f"@{c['channel_username']}" for c in not_ch)) if not_ch else ""
    await message.answer(
        f"👋 Привет, <b>{u.first_name}</b>!\n\n"
        f"⚠️ Сейчас работает <b>резервный бот</b>.\n\n"
        f"🛡 <b>Сервис верификации и модерации контента.</b>\n\n"
        f"{status}{ch_text}",
        parse_mode="HTML", reply_markup=kb
    )


async def do_my_sub(message: Message):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 Администратор — бессрочная подписка."); return
    user = await db.get_user(uid)
    if not user:
        await message.answer("Напишите /start"); return
    if user.get("subscription_lifetime"):
        await message.answer("♾️ <b>Бессрочная подписка</b>!", parse_mode="HTML")
    elif user.get("subscription_end"):
        try:
            end = datetime.fromisoformat(user["subscription_end"]).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if end > now:
                await message.answer(f"✅ До: <b>{end.strftime('%d.%m.%Y %H:%M')}</b> ({(end-now).days} дн.)", parse_mode="HTML")
            else:
                await message.answer("❌ Подписка истекла.")
        except Exception:
            await message.answer("❌ Ошибка данных.")
    else:
        await message.answer("❌ Нет подписки.")


async def do_buy_menu(message: Message, bot: Bot):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 Вы администратор — подписка уже бессрочная!"); return
    not_ch = await check_channels(bot, uid)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await message.answer(f"📢 Подпишитесь:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]])); return
    await message.answer("💎 Выберите срок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 30 дней — 10 USD",  callback_data="buy:30")],
            [InlineKeyboardButton(text="♾️ Навсегда — 100 USD", callback_data="buy:0")],
        ]))


async def do_report_start(message: Message, bot: Bot, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    if not is_adm:
        now = datetime.now(timezone.utc)
        last = user_last_report.get(uid)
        if last and (now - last).total_seconds() < 1200:
            remain = int(1200 - (now - last).total_seconds())
            mins, secs = divmod(remain, 60)
            await message.answer(f"⏳ Следующее обращение доступно через {mins} мин. {secs} сек."); return
        if not await db.has_active_subscription(uid):
            await message.answer("❌ Нет подписки.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📅 30 дней — 10 USD",  callback_data="buy:30")],
                    [InlineKeyboardButton(text="♾️ Навсегда — 100 USD", callback_data="buy:0")],
                ])); return
        not_ch = await check_channels(bot, uid)
        if not_ch:
            names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
            await message.answer(f"📢 Подпишитесь:\n{names}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]])); return
    await state.set_state(S.waiting_report_type)
    await message.answer(
        "📨 <b>Подать обращение</b>\n\nЧто вы хотите обжаловать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Сообщение / пост", callback_data="rtype:message")],
            [InlineKeyboardButton(text="📢 Канал / группа",   callback_data="rtype:channel")],
            [InlineKeyboardButton(text="🤖 Бот",              callback_data="rtype:bot")],
        ])
    )


async def do_got_link(message: Message, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    chat_id, msg_id = parse_tg_link(message.text.strip())
    if not chat_id:
        await message.answer("❌ Неверный формат. Пример: https://t.me/username/123"); return
    if await db.has_reported_before(uid, chat_id, msg_id):
        if is_adm:
            await message.answer("⚠️ Вы уже жаловались, но администраторам разрешено повторно.")
        else:
            await db.revoke_subscription(uid)
            await state.clear()
            await message.answer("❌ Повторное обращение на ту же публикацию недопустимо. Доступ приостановлен.",
                reply_markup=await get_main_kb(uid, False)); return
    await state.update_data(chat_id=chat_id, message_id=msg_id)
    await state.set_state(S.waiting_report_reason)
    await message.answer("📋 Выберите категорию нарушения или опишите своими словами:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",              callback_data="reason:spam")],
            [InlineKeyboardButton(text="🔪 Насилие",           callback_data="reason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",       callback_data="reason:porn")],
            [InlineKeyboardButton(text="❓ Другое",            callback_data="reason:other")],
            [InlineKeyboardButton(text="✏️ Описать нарушение", callback_data="reason:custom")],
        ]))


async def do_got_custom_text(message: Message, state: FSMContext, bot: Bot):
    custom_text = message.text.strip() if message.text else ""
    if not custom_text:
        await message.answer("❌ Описание не может быть пустым. Введите текст:")
        return
    if len(custom_text) > 512:
        await message.answer(f"❌ Текст слишком длинный ({len(custom_text)} символов). Максимум 512:")
        return
    data = await state.get_data()
    await state.clear()
    await message.answer(
        f"✅ Описание принято. Обрабатываю обращение...\n\n"
        f"📝 <i>{custom_text[:100]}{'...' if len(custom_text) > 100 else ''}</i>",
        parse_mode="HTML"
    )
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


async def do_admin(message: Message):
    uid = message.from_user.id
    if not (await db.is_admin(uid) or uid in SUPERADMIN_IDS):
        await message.answer("❌ Нет доступа."); return
    buttons = [[InlineKeyboardButton(text="👥 Подписчики", callback_data="admin:subs"),
                InlineKeyboardButton(text="📋 Логи",       callback_data="admin:logs")]]
    if uid in SUPERADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="🔄 Вернуть основной бот", callback_data="backup:restore")])
    await message.answer("🔧 <b>Резервный бот — Админ-панель</b>", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def do_new_token(message: Message, state: FSMContext, bot: Bot):
    global backup_is_active, main_fail_count
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
        await db.set_backup_bot_active(False)
        backup_is_active = False
        main_fail_count = 0
        await state.clear()
        await message.answer(
            f"✅ Основной бот @{username} восстановлен!\n"
            f"Резервный бот переведён в режим ожидания.\n\n"
            f"⚠️ Обновите MAIN_BOT_TOKEN в backup_bot.py!"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ─── Промокод (пользователь) ────────────────────────────────

async def do_promo_activate(message: Message, state: FSMContext):
    await state.set_state(S.waiting_peer_username)  # re-use for temp; actually using a dedicated approach below
    # redirect to a simple text-state approach: ask for code via existing custom_text state isn't ideal,
    # so we'll just handle it inline with a flag
    await state.clear()
    await message.answer(
        "🎟 <b>Активация промокода</b>\n\nВведите ваш промокод:",
        parse_mode="HTML"
    )
    # We use waiting_peer_custom state with a flag for promo
    await state.set_state(S.waiting_peer_custom)
    await state.update_data(promo_mode=True)


# ─── Peer-репорты (канал / бот) ─────────────────────────────

def _parse_peer_username(text: str):
    text = text.strip()
    import re as _re
    m = _re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", text)
    if m:
        return m.group(1)
    if text.startswith("@"):
        uname = text[1:]
        if len(uname) >= 4:
            return uname
    if _re.match(r"^[A-Za-z0-9_]{4,}$", text):
        return text
    return None


async def do_got_peer_username(message: Message, state: FSMContext):
    uname = _parse_peer_username(message.text or "")
    if not uname:
        await message.answer("❌ Неверный формат. Отправьте @username или https://t.me/username:"); return
    data = await state.get_data()
    peer_type = data.get("peer_type", "channel")
    await state.update_data(peer_username=uname)
    await state.set_state(S.waiting_peer_reason)
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


async def do_got_peer_custom(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()

    # Режим промокода
    if data.get("promo_mode"):
        await state.clear()
        code = (message.text or "").strip().upper()
        promo = await db.get_promo_code(code)
        if not promo:
            await message.answer("❌ Промокод не найден."); return
        if promo["uses"] >= promo["max_uses"]:
            await message.answer("❌ Промокод уже использован максимальное количество раз."); return
        uid = message.from_user.id
        new_end = await db.grant_subscription(uid, promo["days"])
        await db.use_promo_code(code)
        end_str = new_end.strftime('%d.%m.%Y %H:%M') + " UTC" if new_end else ""
        await message.answer(
            f"✅ Промокод активирован!\n\n"
            f"🎁 Добавлено дней: <b>{promo['days']}</b>\n"
            f"📅 Подписка до: <b>{end_str}</b>",
            parse_mode="HTML"
        )
        return

    # Режим кастомного текста для peer-репорта
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым:"); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512:"); return
    await state.clear()
    await message.answer(
        f"✅ Описание принято. Обрабатываю...\n\n"
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


async def _send_peer_reports(bot: Bot, user_id: int, peer_username: str, peer_type: str,
                              reason_name: str, reason_obj, custom_text: str = "",
                              reply_target=None, call=None):
    """Отправка обращений на весь канал / бот через пул аккаунтов."""
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()
    icon = "📢" if peer_type == "channel" else "🤖"

    if not sessions:
        msg = "❌ Нет активных сессий. Обратитесь к администратору."
        if call:
            await call.message.edit_text(msg)
        elif reply_target:
            await reply_target.answer(msg)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))
        return

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
                    logger.warning(f"Peer-сессия {sess['id']}: {e}")
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
    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))


# ─── Отправка обращений ────────────────────────────────────

async def _send_reports(bot: Bot, user_id: int, chat_id_str: str, msg_id_str: str,
                        reason_name: str, reason_obj, custom_text: str = "",
                        reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()

    if not sessions:
        msg = "❌ Нет активных сессий. Обратитесь к администратору."
        if call:
            await call.message.edit_text(msg)
        elif reply_target:
            await reply_target.answer(msg)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))
        return

    total = len(sessions)
    if call:
        await call.message.edit_text(f"⏳ Обрабатываю обращение ({reason_name})...")
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
                    peer = await client.get_input_entity(
                        int(chat_id_str) if chat_id_str.lstrip("-").isdigit() else chat_id_str
                    )
                    await client(ReportRequest(
                        peer=peer,
                        id=[int(msg_id_str)],
                        reason=reason_obj,
                        message=custom_text  # ← свой текст или "" для стандартных причин
                    ))
                    success += 1
                except Exception as e:
                    logger.warning(f"Сессия {sess['id']}: {e}")
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
    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))


# ─── Callback-кнопки (активный режим) ──────────────────────

@router.callback_query(F.data == "reason:custom", StateFilter(S.waiting_report_reason))
async def cb_reason_custom(call: CallbackQuery, state: FSMContext):
    if not backup_is_active:
        await call.answer(); return
    await state.set_state(S.waiting_custom_text)
    await call.message.edit_text(
        "✏️ Опишите нарушение своими словами:\n\n"
        "<i>Например: «Этот аккаунт рассылает спам и обманывает людей»</i>",
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("reason:"), StateFilter(S.waiting_report_reason))
async def cb_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not backup_is_active:
        await call.answer(); return
    reason_key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[reason_key]
    d = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_reports(
        bot=bot,
        user_id=call.from_user.id,
        chat_id_str=d["chat_id"],
        msg_id_str=d["message_id"],
        reason_name=reason_name,
        reason_obj=reason_obj,
        call=call
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    if not backup_is_active:
        await call.answer(); return
    days = int(call.data.split(":")[1])
    amount = 10.0 if days == 30 else 100.0
    await call.message.edit_text("⏳ Создаю счёт...")
    try:
        inv = await create_invoice(amount, days, call.from_user.id)
        await db.add_payment(call.from_user.id, amount, days, inv["invoice_id"])
        label = "30 дней" if days == 30 else "навсегда"
        await call.message.edit_text(
            f"💳 Счёт создан!\n💰 <b>{amount} USDT</b> — {label}\n\nПосле оплаты активируется автоматически.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить", url=inv["pay_url"])]]))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")
    await call.answer()


@router.callback_query(F.data == "check_channels")
async def cb_check(call: CallbackQuery, bot: Bot):
    if not backup_is_active:
        await call.answer(); return
    not_ch = await check_channels(bot, call.from_user.id)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await call.message.edit_text(f"❌ Ещё не подписаны:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Снова", callback_data="check_channels")]]))
    else:
        await call.message.edit_text("✅ Все каналы подписаны!")
    await call.answer()


@router.callback_query(F.data == "admin:subs")
async def cb_subs(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    subs = await db.get_all_subscribers()
    text = "👥 Нет подписчиков." if not subs else "👥 <b>Подписчики:</b>\n\n" + "\n".join(
        f"• <code>{u['user_id']}</code> {u.get('first_name','—')} — " +
        ("♾️" if u.get("subscription_lifetime") else str(u.get("subscription_end","—"))[:16])
        for u in subs)
    await call.message.edit_text(text[:4000], parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin:logs")
async def cb_logs(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    logs = await db.get_admin_logs(20)
    text = "📋 Пусто." if not logs else "📋 <b>Логи:</b>\n\n" + "\n".join(
        f"• [{l['created_at'][:16]}] {l['admin_id']}: {l['action']}" for l in logs)
    await call.message.edit_text(text[:4000], parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("rtype:"), StateFilter(S.waiting_report_type))
async def cb_rtype(call: CallbackQuery, state: FSMContext):
    if not backup_is_active:
        await call.answer(); return
    rtype = call.data.split(":")[1]
    await state.update_data(peer_type=rtype)
    if rtype == "message":
        await state.set_state(S.waiting_report_link)
        await call.message.edit_text(
            "🔗 Отправьте ссылку на публикацию:\n\n"
            "• <code>https://t.me/username/123</code>\n"
            "• <code>https://t.me/c/1234567890/123</code>",
            parse_mode="HTML"
        )
    elif rtype == "channel":
        await state.set_state(S.waiting_peer_username)
        await call.message.edit_text(
            "📢 <b>Жалоба на канал / группу</b>\n\n"
            "Отправьте @юзернейм или ссылку:\n"
            "• <code>@durov</code>\n"
            "• <code>https://t.me/durov</code>",
            parse_mode="HTML"
        )
    else:
        await state.set_state(S.waiting_peer_username)
        await call.message.edit_text(
            "🤖 <b>Жалоба на бота</b>\n\n"
            "Отправьте @юзернейм бота:\n"
            "• <code>@somebot</code>",
            parse_mode="HTML"
        )
    await call.answer()


@router.callback_query(F.data == "preason:custom", StateFilter(S.waiting_peer_reason))
async def cb_preason_custom(call: CallbackQuery, state: FSMContext):
    if not backup_is_active:
        await call.answer(); return
    await state.set_state(S.waiting_peer_custom)
    await call.message.edit_text(
        "✏️ Опишите нарушение своими словами (до 512 символов):",
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("preason:"), StateFilter(S.waiting_peer_reason))
async def cb_preason(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not backup_is_active:
        await call.answer(); return
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


@router.callback_query(F.data == "backup:restore")
async def cb_restore(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    await state.set_state(S.waiting_new_token)
    await call.message.edit_text(
        "🔄 Введите токен нового основного бота:\n(Резервный перейдёт в режим ожидания)")
    await call.answer()


# ─── Запуск ────────────────────────────────────────────────

async def main():
    global backup_is_active
    await db.init_db()

    rec = await db.get_backup_bot()
    if rec and rec.get("is_active"):
        backup_is_active = True
        logger.info("Запуск в АКТИВНОМ режиме")
    else:
        logger.info("Запуск в режиме ОЖИДАНИЯ")

    bot = Bot(token=BACKUP_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(monitor_main_bot(bot))
    asyncio.create_task(poll_payments(bot))

    logger.info("Резервный бот запущен ✅")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
