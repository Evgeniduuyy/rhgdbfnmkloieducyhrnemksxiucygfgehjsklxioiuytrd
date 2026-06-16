import asyncio
import logging
import re
import random
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
    FSInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup
)
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, UsernameNotOccupiedError, UsernameInvalidError
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.functions.account import ReportPeerRequest
from telethon.tl.types import (
    InputReportReasonSpam, InputReportReasonViolence,
    InputReportReasonPornography, InputReportReasonOther,
    Channel
)
from telethon.sessions import StringSession

import database as db

# ============================================================
#                        НАСТРОЙКИ
# ============================================================

BACKUP_BOT_TOKEN  = "ТОКЕН_РЕЗЕРВНОГО_БОТА"
MAIN_BOT_TOKEN    = "ТОКЕН_ОСНОВНОГО_БОТА"

CRYPTOBOT_TOKEN   = "ТОКЕН_CRYPTOBOT"
SUPERADMIN_IDS    = {853173723, 1090307552}
MAIN_BOT_USERNAME = "Pizza_FenixBot"

TELETHON_API_ID   = 35989820
TELETHON_API_HASH = "18cec00c9bef93d0dd475baba4e6c3f4"

DB_PATH = "bot_database.db"

# Кулдаун между жалобами — 30 минут
REPORT_COOLDOWN_SECONDS = 1800

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

TYPE_LABELS = {
    "user":    "👤 Пользователь",
    "bot":     "🤖 Бот",
    "channel": "📢 Канал",
    "group":   "👥 Группа",
}


class S(StatesGroup):
    waiting_report_link    = State()
    waiting_report_reason  = State()
    waiting_custom_text    = State()
    waiting_new_token      = State()
    waiting_report_type    = State()
    waiting_peer_username  = State()
    waiting_peer_reason    = State()
    waiting_peer_custom    = State()
    waiting_sess_string    = State()
    waiting_wl_target      = State()
    waiting_wl_type        = State()
    waiting_log_group_id   = State()
    waiting_revoke_uid     = State()
    waiting_revoke_reason  = State()


# ─── Утилиты ───────────────────────────────────────────────

def parse_tg_link(link: str) -> tuple[str | None, str | None, bool]:
    link = link.strip()
    m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if m:
        return "-100" + m.group(1), m.group(2), True
    m = re.match(r"https?://t\.me/([^/]+)/(\d+)", link)
    if m:
        return m.group(1), m.group(2), False
    return None, None, False


def _parse_peer_username(text: str):
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


async def _get_telethon_client() -> TelegramClient | None:
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


async def check_peer_accessible(username: str) -> tuple[bool, str]:
    client = await _get_telethon_client()
    if client is None:
        return True, ""
    try:
        entity = await client.get_entity(username)
        if isinstance(entity, Channel) and not entity.username:
            return False, "private"
        return True, ""
    except ChannelPrivateError:
        return False, "private"
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return False, "not_found"
    except Exception:
        return True, ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _wl_type_label(wtype: str) -> str:
    return TYPE_LABELS.get(wtype, wtype)


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


async def _buy_keyboard() -> InlineKeyboardMarkup:
    plans = await db.get_subscription_plans()
    if not plans:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 30 дней — 10 USD",  callback_data="buy:30:10.0")],
            [InlineKeyboardButton(text="♾️ Навсегда — 100 USD", callback_data="buy:0:100.0")],
        ])
    rows = []
    for p in plans:
        label = f"{'♾️ Навсегда' if p['days'] == 0 else f'📅 {p[\"days\"]} дней'} — {p['price']:.0f} USD"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"buy:{p['days']}:{p['price']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    raise Exception(f"CryptoBot: {data}")


# ─── Фоновые задачи ────────────────────────────────────────

async def monitor_main_bot(bot: Bot):
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
                for sid in SUPERADMIN_IDS:
                    try:
                        await bot.send_message(
                            sid,
                            "🚨 Основной бот недоступен!\n"
                            "Резервный бот автоматически активирован.\n\n"
                            "Когда основной бот восстановлен — используйте кнопку "
                            "<b>🔄 Вернуть основной бот</b> в админ-панели.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass


async def poll_payments(bot: Bot):
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


# ─── Режим ожидания ─────────────────────────────────────────

@router.message()
async def handle_any(message: Message, bot: Bot, state: FSMContext):
    if not backup_is_active:
        await message.answer(
            f"⚠️ Данный бот временно не активен.\n"
            f"Используйте основной бот: @{MAIN_BOT_USERNAME}"
        )
        return
    await _dispatch(message, bot, state)


async def _dispatch(message: Message, bot: Bot, state: FSMContext):
    cur = await state.get_state()
    text = message.text or ""

    if text.startswith("/start"):   await _do_start(message, bot, state); return
    if text == "/support":
        links = " | ".join(f'<a href="tg://user?id={sid}">{sid}</a>' for sid in SUPERADMIN_IDS)
        await message.answer(f"📞 Супер-администраторы: {links}", parse_mode="HTML"); return

    # FSM-состояния
    if cur == S.waiting_report_link:    await _do_got_link(message, state, bot); return
    if cur == S.waiting_custom_text:    await _do_got_custom(message, state, bot); return
    if cur == S.waiting_new_token:      await _do_new_token(message, state, bot); return
    if cur == S.waiting_peer_username:  await _do_got_peer_username(message, state, bot); return
    if cur == S.waiting_peer_custom:    await _do_got_peer_custom(message, state, bot); return
    if cur == S.waiting_sess_string:    await _do_got_sess_string(message, state, bot); return
    if cur == S.waiting_wl_target:      await _do_got_wl_target(message, state); return
    if cur == S.waiting_log_group_id:   await _do_got_log_group(message, state, bot); return
    if cur == S.waiting_revoke_uid:     await _do_got_revoke_uid(message, state); return
    if cur == S.waiting_revoke_reason:  await _do_got_revoke_reason(message, state, bot); return

    # Кнопки меню
    if text == "📜 Правила":
        url = await db.get_setting("rules_url")
        if url:
            await message.answer(await db.get_setting("rules_text") or "Правила:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Открыть", url=url)]]))
        else:
            await message.answer("❌ Правила не заданы.")
        return
    if text == "📄 Моя подписка":     await _do_my_sub(message); return
    if text == "💎 Купить подписку":  await _do_buy_menu(message, bot); return
    if text == "📨 Подать обращение": await _do_report_start(message, bot, state); return
    if text == "🎟 Промокод":         await _do_promo(message, state); return
    if text == "🔧 Админ панель":     await _do_admin(message); return


# ─── Обработчики ───────────────────────────────────────────

async def _do_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = message.from_user
    await db.upsert_user(u.id, u.username or "", u.first_name or "")
    if u.id in SUPERADMIN_IDS:
        await db.set_admin(u.id, True)
    is_adm = await db.is_admin(u.id) or u.id in SUPERADMIN_IDS
    has_sub = is_adm or await db.has_active_subscription(u.id)
    not_ch = [] if is_adm else await check_channels(bot, u.id)
    status = "✅ Подписка активна." if has_sub else "⚠️ Нет подписки — нажмите <b>💎 Купить подписку</b>."
    ch_text = ("\n\n📢 Подпишитесь: " + ", ".join(f"@{c['channel_username']}" for c in not_ch)) if not_ch else ""
    await message.answer(
        f"👋 Привет, <b>{u.first_name}</b>!\n\n"
        f"⚠️ Сейчас работает <b>резервный бот</b>.\n\n"
        f"🛡 <b>Сервис верификации и модерации контента.</b>\n\n"
        f"{status}{ch_text}",
        parse_mode="HTML",
        reply_markup=await get_main_kb(u.id, is_adm)
    )


async def _do_my_sub(message: Message):
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
                await message.answer(
                    f"✅ Подписка активна.\n📅 До: <b>{end.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
                    f"⏳ Осталось: <b>{(end - now).days} дн.</b>", parse_mode="HTML")
            else:
                await message.answer("❌ Подписка истекла.")
        except Exception:
            await message.answer("❌ Ошибка данных.")
    else:
        await message.answer("❌ Нет подписки.")


async def _do_buy_menu(message: Message, bot: Bot):
    uid = message.from_user.id
    if await db.is_admin(uid) or uid in SUPERADMIN_IDS:
        await message.answer("👑 Вы администратор — подписка уже бессрочная!"); return
    not_ch = await check_channels(bot, uid)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await message.answer(f"📢 Подпишитесь:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]])); return
    await message.answer("💎 Выберите тариф:", reply_markup=await _buy_keyboard())


async def _do_report_start(message: Message, bot: Bot, state: FSMContext):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    if not is_adm:
        now = datetime.now(timezone.utc)
        last = user_last_report.get(uid)
        if last and (now - last).total_seconds() < REPORT_COOLDOWN_SECONDS:
            remain = int(REPORT_COOLDOWN_SECONDS - (now - last).total_seconds())
            mins, secs = divmod(remain, 60)
            await message.answer(
                f"⏳ Следующее обращение доступно через <b>{mins} мин. {secs} сек.</b>\n\n"
                f"Это защита от злоупотреблений.", parse_mode="HTML"); return
        if not await db.has_active_subscription(uid):
            await message.answer("❌ Нет подписки.", reply_markup=await _buy_keyboard()); return
        not_ch = await check_channels(bot, uid)
        if not_ch:
            names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
            await message.answer(f"📢 Подпишитесь:\n{names}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_channels")]])); return
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


async def _do_got_link(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    chat_id, msg_id, is_private = parse_tg_link(message.text.strip())

    if not chat_id:
        await message.answer("❌ Неверный формат. Пример: https://t.me/username/123"); return

    if is_private and not is_adm:
        await state.clear()
        await message.answer(
            "⛔ <b>Обращение отклонено.</b>\n\n"
            "Репорты на приватные каналы и группы не принимаются.",
            parse_mode="HTML",
            reply_markup=await get_main_kb(uid, False)); return

    wl_entry = await db.is_whitelisted(chat_id)
    if wl_entry and not is_adm:
        await state.clear()
        await message.answer(
            f"⛔ <b>Обращение отклонено.</b>\n\n"
            f"Публикация из <b>@{wl_entry['target']}</b> находится в белом списке.",
            parse_mode="HTML",
            reply_markup=await get_main_kb(uid, False)); return

    if await db.has_reported_before(uid, chat_id, msg_id):
        if is_adm:
            await message.answer("⚠️ Вы уже жаловались — для администраторов разрешено.")
        else:
            await db.revoke_subscription(uid)
            await state.clear()
            await message.answer(
                "❌ <b>Повторное обращение на ту же публикацию недопустимо.</b>\n\n"
                "Ваша подписка аннулирована. Оформите новую подписку.",
                parse_mode="HTML",
                reply_markup=await get_main_kb(uid, False)); return

    await state.update_data(chat_id=chat_id, message_id=msg_id)
    await state.set_state(S.waiting_report_reason)
    await message.answer("📋 Выберите категорию нарушения:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Спам",              callback_data="reason:spam")],
            [InlineKeyboardButton(text="🔪 Насилие",           callback_data="reason:violence")],
            [InlineKeyboardButton(text="🔞 Порнография",       callback_data="reason:porn")],
            [InlineKeyboardButton(text="❓ Другое",            callback_data="reason:other")],
            [InlineKeyboardButton(text="✏️ Описать нарушение", callback_data="reason:custom")],
        ]))


async def _do_got_custom(message: Message, state: FSMContext, bot: Bot):
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


async def _do_got_peer_username(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    is_adm = await db.is_admin(uid) or uid in SUPERADMIN_IDS
    uname = _parse_peer_username(message.text or "")
    if not uname:
        await message.answer("❌ Неверный формат. Отправьте @username или https://t.me/username:"); return

    data = await state.get_data()
    peer_type = data.get("peer_type", "channel")

    wl_entry = await db.is_whitelisted(uname)
    if wl_entry and not is_adm:
        await state.clear()
        ttype = {"channel": "Канал", "bot": "Бот", "user": "Пользователь", "group": "Группа"}.get(
            wl_entry.get("target_type", ""), "Объект")
        await message.answer(
            f"⛔ <b>Обращение отклонено.</b>\n\n"
            f"{ttype} <b>@{wl_entry['target']}</b> находится в белом списке.",
            parse_mode="HTML",
            reply_markup=await get_main_kb(uid, False)); return

    if peer_type == "channel" and not is_adm:
        wait_msg = await message.answer("🔍 Проверяю доступность...")
        ok, err = await check_peer_accessible(uname)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if not ok:
            await state.clear()
            if err == "private":
                await message.answer(
                    f"⛔ <b>Обращение отклонено.</b>\n\n"
                    f"Канал/группа <b>@{uname}</b> является приватным.\n"
                    f"Репорты принимаются только на публичные каналы.",
                    parse_mode="HTML",
                    reply_markup=await get_main_kb(uid, False))
            else:
                await message.answer(
                    f"❌ Канал/группа <b>@{uname}</b> не найден(а).",
                    parse_mode="HTML",
                    reply_markup=await get_main_kb(uid, False))
            return

    if await db.has_peer_reported_before(uid, uname) and not is_adm:
        await db.revoke_subscription(uid)
        await state.clear()
        icon = "📢" if peer_type == "channel" else "🤖"
        await message.answer(
            f"❌ <b>Повторное обращение на {icon} @{uname} недопустимо.</b>\n\n"
            f"Ваша подписка аннулирована. Оформите новую подписку.",
            parse_mode="HTML",
            reply_markup=await get_main_kb(uid, False)); return

    await state.update_data(peer_username=uname)
    await state.set_state(S.waiting_peer_reason)
    icon = "📢" if peer_type == "channel" else "🤖"
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


async def _do_got_peer_custom(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("promo_mode"):
        await state.clear()
        code = (message.text or "").strip().upper()
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
            parse_mode="HTML"); return

    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Описание не может быть пустым."); return
    if len(text) > 512:
        await message.answer(f"❌ Слишком длинно ({len(text)} симв.). Максимум 512."); return
    await state.clear()
    await message.answer(f"✅ Принято. Обрабатываю...\n\n📝 <i>{text[:100]}...</i>", parse_mode="HTML")
    await _send_peer_reports(bot=bot, user_id=message.from_user.id,
        peer_username=data["peer_username"], peer_type=data.get("peer_type", "channel"),
        reason_name="✏️ Свой текст", reason_obj=InputReportReasonOther(),
        custom_text=text, reply_target=message)


async def _do_admin(message: Message):
    uid = message.from_user.id
    if not (await db.is_admin(uid) or uid in SUPERADMIN_IDS):
        await message.answer("❌ Нет доступа."); return
    buttons = [
        [InlineKeyboardButton(text="👥 Подписчики",    callback_data="admin:subs"),
         InlineKeyboardButton(text="📋 Логи",          callback_data="admin:logs")],
        [InlineKeyboardButton(text="📂 Сессии",        callback_data="admin:sessions")],
        [InlineKeyboardButton(text="🛡 Белый список",  callback_data="admin:whitelist")],
        [InlineKeyboardButton(text="📊 Группа логов",  callback_data="admin:log_group"),
         InlineKeyboardButton(text="❌ Снять подписку", callback_data="admin:revoke_sub")],
        [InlineKeyboardButton(text="📤 Выгрузить БД",  callback_data="admin:export_db")],
    ]
    if uid in SUPERADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="🔄 Вернуть основной бот", callback_data="backup:restore")])
    await message.answer("🔧 <b>Резервный бот — Админ-панель</b>", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def _do_new_token(message: Message, state: FSMContext, bot: Bot):
    global backup_is_active, main_fail_count
    if message.from_user.id not in SUPERADMIN_IDS:
        await state.clear(); return
    token = (message.text or "").strip()
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
            f"⚠️ Обновите MAIN_BOT_TOKEN в backup_bot.py!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


async def _do_promo(message: Message, state: FSMContext):
    await state.set_state(S.waiting_peer_custom)
    await state.update_data(promo_mode=True)
    await message.answer("🎟 <b>Активация промокода</b>\n\nВведите ваш промокод:", parse_mode="HTML")


async def _do_got_sess_string(message: Message, state: FSMContext, bot: Bot):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено."); return
    session_str = (message.text or "").strip()
    if not session_str:
        await message.answer("❌ Строка пустая."); return
    status_msg = await message.answer("🔄 Проверяю сессию...")
    try:
        client = TelegramClient(StringSession(session_str), TELETHON_API_ID, TELETHON_API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            await client.disconnect()
            await state.clear()
            sess_id = await db.add_session(session_str)
            await db.log_admin_action(message.from_user.id, "add_session", f"id={sess_id} user={me.username or me.id}")
            await status_msg.edit_text(
                f"✅ <b>Сессия добавлена!</b>\n\n"
                f"👤 Аккаунт: <code>{me.first_name or ''} {me.last_name or ''}</code>\n"
                f"📱 @{me.username or '—'}\n🆔 ID: <code>{sess_id}</code>",
                parse_mode="HTML")
        else:
            await client.disconnect()
            await status_msg.edit_text(
                "❌ <b>Сессия недействительна.</b>\n\nАккаунт не авторизован.", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка:</b>\n\n<code>{e}</code>", parse_mode="HTML")


async def _do_got_wl_target(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    raw = (message.text or "").strip()
    m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})", raw)
    target = m.group(1) if m else (raw[1:] if raw.startswith("@") else raw)
    if not target:
        await message.answer("❌ Не могу распознать."); return
    await state.update_data(wl_target=target)
    await state.set_state(S.waiting_wl_type)
    await message.answer(f"✅ Объект: <code>{target}</code>\n\nВыберите тип:", parse_mode="HTML",
                         reply_markup=whitelist_type_kb())


async def _do_got_log_group(message: Message, state: FSMContext, bot: Bot):
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
        await message.answer("✅ Группа логов отключена."); return
    try:
        await bot.send_message(gid, "✅ Группа логов успешно настроена! Сюда будут приходить все жалобы.")
        await db.set_setting("log_group_id", str(gid))
        await state.clear()
        await message.answer(
            f"✅ Группа логов настроена!\n\n🆔 ID: <code>{gid}</code>",
            parse_mode="HTML")
    except Exception as e:
        await message.answer(
            f"❌ Не удалось отправить сообщение в группу <code>{gid}</code>.\n\n"
            f"Убедитесь что бот добавлен в группу и имеет право писать.\n\n"
            f"Ошибка: <code>{e}</code>",
            parse_mode="HTML")


async def _do_got_revoke_uid(message: Message, state: FSMContext):
    if not (await db.is_admin(message.from_user.id) or message.from_user.id in SUPERADMIN_IDS):
        await state.clear(); return
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введите числовой ID:"); return
    user = await db.get_user(uid)
    if not user or not (user.get("subscription_lifetime") or user.get("subscription_end")):
        await message.answer(f"❌ У пользователя <code>{uid}</code> нет активной подписки.", parse_mode="HTML"); return
    await state.update_data(revoke_uid=uid)
    await state.set_state(S.waiting_revoke_reason)
    name = user.get("first_name") or str(uid)
    await message.answer(
        f"👤 Пользователь: <b>{name}</b> (<code>{uid}</code>)\n\n"
        f"Напишите причину снятия подписки (будет отправлена пользователю):",
        parse_mode="HTML")


async def _do_got_revoke_reason(message: Message, state: FSMContext, bot: Bot):
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
        parse_mode="HTML")
    try:
        await bot.send_message(
            uid,
            f"⚠️ <b>Ваша подписка была снята администратором.</b>\n\n"
            f"📝 Причина: <i>{reason}</i>\n\n"
            f"Если считаете это ошибкой — обратитесь к поддержке.",
            parse_mode="HTML")
    except Exception:
        pass


# ─── Отправка репортов ──────────────────────────────────────

async def _send_peer_reports(bot: Bot, user_id: int, peer_username: str, peer_type: str,
                              reason_name: str, reason_obj, custom_text: str = "",
                              reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()
    icon = "📢" if peer_type == "channel" else "🤖"

    if not sessions:
        msg = "❌ Нет активных сессий. Обратитесь к администратору."
        if call: await call.message.edit_text(msg)
        elif reply_target: await reply_target.answer(msg)
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
                    await client(ReportPeerRequest(peer=peer, reason=reason_obj, message=custom_text))
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
        f"{'📢' if peer_type == 'channel' else '🤖'} <b>Жалоба на {'канал/группу' if peer_type == 'channel' else 'бота'}</b>\n\n"
        f"👤 От: {uname_str} (<code>{user_id}</code>)\n"
        f"🎯 Цель: <code>@{peer_username}</code>\n"
        f"📌 Причина: {reason_name}{extra}\n"
        f"✅ Принято: <b>{success}</b> / {total}"
    )
    await _send_to_log_group(bot, log_text)

    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))


async def _send_reports(bot: Bot, user_id: int, chat_id_str: str, msg_id_str: str,
                        reason_name: str, reason_obj, custom_text: str = "",
                        reply_target=None, call=None):
    is_adm = await db.is_admin(user_id) or user_id in SUPERADMIN_IDS
    sessions = await db.get_all_sessions()

    if not sessions:
        msg = "❌ Нет активных сессий. Обратитесь к администратору."
        if call: await call.message.edit_text(msg)
        elif reply_target: await reply_target.answer(msg)
        await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))
        return

    total = len(sessions)
    if call:
        await call.message.edit_text(f"⏳ Обрабатываю ({reason_name})...")
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
                        int(chat_id_str) if chat_id_str.lstrip("-").isdigit() else chat_id_str)
                    await client(ReportRequest(peer=peer, id=[int(msg_id_str)], reason=reason_obj, message=custom_text))
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

    await bot.send_message(user_id, "Главное меню:", reply_markup=await get_main_kb(user_id, is_adm))


# ─── Callback-кнопки ───────────────────────────────────────

@router.callback_query(F.data == "reason:custom", StateFilter(S.waiting_report_reason))
async def cb_reason_custom(call: CallbackQuery, state: FSMContext):
    if not backup_is_active: await call.answer(); return
    await state.set_state(S.waiting_custom_text)
    await call.message.edit_text("✏️ Опишите нарушение своими словами:", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("reason:"), StateFilter(S.waiting_report_reason))
async def cb_reason(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not backup_is_active: await call.answer(); return
    key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[key]
    d = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_reports(bot=bot, user_id=call.from_user.id,
        chat_id_str=d["chat_id"], msg_id_str=d["message_id"],
        reason_name=reason_name, reason_obj=reason_obj, call=call)


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery):
    if not backup_is_active: await call.answer(); return
    parts = call.data.split(":")
    days = int(parts[1])
    amount = float(parts[2])
    await call.message.edit_text("⏳ Создаю счёт...")
    try:
        inv = await create_invoice(amount, days, call.from_user.id)
        await db.add_payment(call.from_user.id, amount, days, inv["invoice_id"])
        label = "навсегда" if days == 0 else f"{days} дней"
        await call.message.edit_text(
            f"💳 Счёт создан!\n💰 <b>{amount:.0f} USDT</b> — {label}\n\nПосле оплаты активируется автоматически.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Оплатить {amount:.0f} USDT", url=inv["pay_url"])]]))
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}")
    await call.answer()


@router.callback_query(F.data == "check_channels")
async def cb_check(call: CallbackQuery, bot: Bot):
    if not backup_is_active: await call.answer(); return
    not_ch = await check_channels(bot, call.from_user.id)
    if not_ch:
        names = "\n".join(f"• @{c['channel_username']}" for c in not_ch)
        await call.message.edit_text(f"❌ Ещё не подписаны:\n{names}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Снова", callback_data="check_channels")]]))
    else:
        await call.message.edit_text("✅ Все каналы подписаны!")
    await call.answer()


@router.callback_query(F.data == "admin:back")
async def cb_admin_back(call: CallbackQuery):
    if not backup_is_active: await call.answer(); return
    uid = call.from_user.id
    buttons = [
        [InlineKeyboardButton(text="👥 Подписчики",   callback_data="admin:subs"),
         InlineKeyboardButton(text="📋 Логи",         callback_data="admin:logs")],
        [InlineKeyboardButton(text="📂 Сессии",       callback_data="admin:sessions")],
        [InlineKeyboardButton(text="🛡 Белый список", callback_data="admin:whitelist")],
        [InlineKeyboardButton(text="📤 Выгрузить БД", callback_data="admin:export_db")],
    ]
    if uid in SUPERADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="🔄 Вернуть основной бот", callback_data="backup:restore")])
    await call.message.edit_text("🔧 <b>Резервный бот — Админ-панель</b>", parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
    await call.message.edit_text(text[:4000], parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
    await call.answer()


@router.callback_query(F.data == "admin:logs")
async def cb_logs(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    logs = await db.get_admin_logs(20)
    text = "📋 Пусто." if not logs else "📋 <b>Логи:</b>\n\n" + "\n".join(
        f"• [{l['created_at'][:16]}] {l['admin_id']}: {l['action']}" for l in logs)
    await call.message.edit_text(text[:4000], parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
    await call.answer()


@router.callback_query(F.data == "admin:export_db")
async def cb_export_db(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
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


@router.callback_query(F.data == "admin:sessions")
async def cb_sessions(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    n = len(await db.get_all_sessions())
    await call.message.edit_text(
        f"📂 <b>Управление сессиями</b>\n\nАктивных сессий: <b>{n}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Добавить StringSession", callback_data="sess:upload")],
            [InlineKeyboardButton(text="🗑 Удалить сессию",         callback_data="sess:delete_menu")],
            [InlineKeyboardButton(text="✅ Проверить все",          callback_data="sess:check")],
            [InlineKeyboardButton(text="◀️ Назад",                  callback_data="admin:back")],
        ]))
    await call.answer()


@router.callback_query(F.data == "sess:upload")
async def cb_sess_upload(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(S.waiting_sess_string)
    await call.message.edit_text(
        "📝 <b>Добавление StringSession</b>\n\n"
        "Вставьте строку сессии Telethon.\n\n"
        "⚠️ Бот проверит валидность перед сохранением.\n\n/cancel — отмена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:sessions")]]))
    await call.answer()


@router.callback_query(F.data == "sess:check")
async def cb_sess_check(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await call.message.edit_text("🔄 Проверяю все сессии...")
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("📭 Нет сессий.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")]]))
        await call.answer(); return
    results = []
    for sess in sessions:
        try:
            client = TelegramClient(StringSession(sess["session_data"]), TELETHON_API_ID, TELETHON_API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                results.append(f"✅ #{sess['id']} — @{me.username or me.id}")
            else:
                results.append(f"❌ #{sess['id']} — не авторизован")
            await client.disconnect()
        except Exception as e:
            results.append(f"⚠️ #{sess['id']} — {str(e)[:40]}")
        await asyncio.sleep(0.3)
    text = "📋 <b>Статус сессий:</b>\n\n" + "\n".join(results)
    await call.message.edit_text(text[:4000], parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")]]))
    await call.answer()


@router.callback_query(F.data == "sess:delete_menu")
async def cb_sess_delete_menu(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("📭 Нет сессий.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")]]))
        await call.answer(); return
    buttons = [[InlineKeyboardButton(text=f"🗑 Сессия #{s['id']}", callback_data=f"sess:del:{s['id']}")] for s in sessions]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")])
    await call.message.edit_text("Выберите сессию для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data.startswith("sess:del:"))
async def cb_sess_del(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    sess_id = int(call.data.split(":")[2])
    await db.delete_session(sess_id)
    await call.answer(f"✅ Сессия #{sess_id} удалена", show_alert=True)
    sessions = await db.get_all_sessions()
    if not sessions:
        await call.message.edit_text("📭 Нет сессий.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")]])); return
    buttons = [[InlineKeyboardButton(text=f"🗑 Сессия #{s['id']}", callback_data=f"sess:del:{s['id']}")] for s in sessions]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:sessions")])
    await call.message.edit_text("Выберите сессию для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("rtype:"), StateFilter(S.waiting_report_type))
async def cb_rtype(call: CallbackQuery, state: FSMContext):
    if not backup_is_active: await call.answer(); return
    rtype = call.data.split(":")[1]
    await state.update_data(peer_type=rtype)
    if rtype == "message":
        await state.set_state(S.waiting_report_link)
        await call.message.edit_text(
            "🔗 Отправьте ссылку на публикацию:\n\n"
            "• <code>https://t.me/username/123</code>\n\n"
            "⚠️ Ссылки t.me/c/... (приватные каналы) не принимаются.",
            parse_mode="HTML")
    elif rtype == "channel":
        await state.set_state(S.waiting_peer_username)
        await call.message.edit_text(
            "📢 <b>Жалоба на канал / группу</b>\n\nОтправьте @юзернейм:\n• <code>@durov</code>",
            parse_mode="HTML")
    else:
        await state.set_state(S.waiting_peer_username)
        await call.message.edit_text(
            "🤖 <b>Жалоба на бота</b>\n\nОтправьте @юзернейм:\n• <code>@somebot</code>",
            parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "preason:custom", StateFilter(S.waiting_peer_reason))
async def cb_preason_custom(call: CallbackQuery, state: FSMContext):
    if not backup_is_active: await call.answer(); return
    await state.set_state(S.waiting_peer_custom)
    await call.message.edit_text("✏️ Опишите нарушение (до 512 символов):", parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("preason:"), StateFilter(S.waiting_peer_reason))
async def cb_preason(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not backup_is_active: await call.answer(); return
    key = call.data.split(":")[1]
    reason_name, reason_obj = REPORT_REASONS[key]
    data = await state.get_data()
    await state.clear()
    await call.answer()
    await _send_peer_reports(bot=bot, user_id=call.from_user.id,
        peer_username=data["peer_username"], peer_type=data.get("peer_type", "channel"),
        reason_name=reason_name, reason_obj=reason_obj, call=call)


# ─── Белый список callbacks ─────────────────────────────────

@router.callback_query(F.data == "admin:whitelist")
async def cb_whitelist(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    count = len(await db.get_whitelist())
    await call.message.edit_text(
        f"🛡 <b>Белый список</b>\n\nОбъектов: <b>{count}</b>",
        parse_mode="HTML", reply_markup=whitelist_main_kb())
    await call.answer()


@router.callback_query(F.data == "wl:add")
async def cb_wl_add(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(S.waiting_wl_target)
    await call.message.edit_text(
        "🛡 <b>Добавить в белый список</b>\n\nОтправьте @username или ID:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:whitelist")]]))
    await call.answer()


@router.callback_query(F.data.startswith("wltype:"), StateFilter(S.waiting_wl_type))
async def cb_wl_type(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    wtype = call.data.split(":")[1]
    data = await state.get_data()
    target = data["wl_target"]
    await state.clear()
    added = await db.add_to_whitelist(target, wtype, call.from_user.id)
    if added:
        await db.log_admin_action(call.from_user.id, "whitelist_add", f"target={target} type={wtype}")
        await call.message.edit_text(
            f"✅ <b>Добавлено!</b>\n\n🎯 <code>@{target}</code> — {_wl_type_label(wtype)}",
            parse_mode="HTML", reply_markup=whitelist_main_kb())
    else:
        await call.message.edit_text(
            f"⚠️ <code>@{target}</code> уже в списке.", parse_mode="HTML",
            reply_markup=whitelist_main_kb())
    await call.answer()


@router.callback_query(F.data.startswith("wl:list:"))
async def cb_wl_list(call: CallbackQuery):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    page = int(call.data.split(":")[2])
    per_page = 8
    wl = await db.get_whitelist()
    if not wl:
        await call.message.edit_text("🛡 <b>Пусто.</b>", parse_mode="HTML", reply_markup=whitelist_main_kb())
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
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    wl_id = int(call.data.split(":")[2])
    await db.remove_from_whitelist(wl_id)
    await call.answer("✅ Удалено", show_alert=True)
    wl = await db.get_whitelist()
    if not wl:
        await call.message.edit_text("🛡 <b>Пусто.</b>", parse_mode="HTML", reply_markup=whitelist_main_kb()); return
    lines = ["🛡 <b>Белый список:</b>\n"]
    buttons = []
    for item in wl[:8]:
        lines.append(f"• <code>@{item['target']}</code> — {_wl_type_label(item.get('target_type',''))}")
        buttons.append([InlineKeyboardButton(text=f"🗑 @{item['target']}", callback_data=f"wl:del:{item['id']}")])
    if len(wl) > 8: buttons.append([InlineKeyboardButton(text="▶️", callback_data="wl:list:1")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:whitelist")])
    await call.message.edit_text("\n".join(lines), parse_mode="HTML",
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "admin:log_group")
async def cb_log_group(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    current = await db.get_setting("log_group_id") or "не настроена"
    await state.set_state(S.waiting_log_group_id)
    await call.message.edit_text(
        f"📊 <b>Группа логов</b>\n\n"
        f"Текущий ID: <code>{current}</code>\n\n"
        f"Отправьте числовой ID группы (например <code>-1001234567890</code>).\n\n"
        f"ℹ️ Узнать ID — переслать сообщение боту @userinfobot\n\n"
        f"Введите <code>0</code> чтобы отключить.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:back")]
        ]))
    await call.answer()


@router.callback_query(F.data == "admin:revoke_sub")
async def cb_revoke_sub(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    subs = await db.get_all_subscribers()
    if not subs:
        await call.message.edit_text(
            "❌ Нет активных подписчиков.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
        await call.answer(); return
    buttons = []
    for u in subs[:10]:
        name = u.get("first_name") or str(u["user_id"])
        end = "♾️" if u.get("subscription_lifetime") else str(u.get("subscription_end",""))[:10]
        buttons.append([InlineKeyboardButton(
            text=f"{name} (#{u['user_id']}) до {end}",
            callback_data=f"revoke:{u['user_id']}")])
    if len(subs) > 10:
        buttons.append([InlineKeyboardButton(text="✏️ Ввести ID вручную", callback_data="revoke:manual")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")])
    await call.message.edit_text(
        "❌ <b>Снятие подписки</b>\n\nВыберите пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data == "revoke:manual")
async def cb_revoke_manual(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    await state.set_state(S.waiting_revoke_uid)
    await call.message.edit_text(
        "❌ <b>Снятие подписки</b>\n\nВведите Telegram ID пользователя:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:revoke_sub")]]))
    await call.answer()


@router.callback_query(F.data.startswith("revoke:"), lambda c: c.data != "revoke:manual")
async def cb_revoke_selected(call: CallbackQuery, state: FSMContext):
    if not backup_is_active or not (await db.is_admin(call.from_user.id) or call.from_user.id in SUPERADMIN_IDS):
        await call.answer("❌ Нет доступа", show_alert=True); return
    uid = int(call.data.split(":")[1])
    user = await db.get_user(uid)
    if not user:
        await call.message.edit_text("❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin:back")]]))
        await call.answer(); return
    await state.update_data(revoke_uid=uid)
    await state.set_state(S.waiting_revoke_reason)
    name = user.get("first_name") or str(uid)
    await call.message.edit_text(
        f"👤 Пользователь: <b>{name}</b> (<code>{uid}</code>)\n\n"
        f"Напишите причину снятия подписки (будет отправлена пользователю):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:revoke_sub")]]))
    await call.answer()


@router.callback_query(F.data == "backup:restore")
async def cb_restore(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SUPERADMIN_IDS:
        await call.answer("❌ Только супер-админ", show_alert=True); return
    await state.set_state(S.waiting_new_token)
    await call.message.edit_text("🔄 Введите токен нового основного бота:")
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
