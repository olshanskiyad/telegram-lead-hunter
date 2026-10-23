import html
import logging
import os
from datetime import datetime, timezone

from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import config
from database import Database, access_end
from lead_filters import looks_like_chat_link, normal_form, parse_keywords

logger = logging.getLogger(__name__)

router = Router()

# Общая справка клиента в формате Telegram HTML (если у клиента нет своей в базе)
HELP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_instructions.html")

# База подставляется при запуске клиентских ботов (client_bots.init_client_bots)
db: Database = None

# Все тексты бота идут с parse_mode="HTML", значения из базы и от пользователей экранируются через html.escape

# States for FSM (client side)
class ClientAddKeyword(StatesGroup):
    waiting_for_keyword = State()

class ClientAddChat(StatesGroup):
    waiting_for_link = State()

# --- Keyboards ---
def persistent_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Меню")]],
        resize_keyboard=True,
        is_persistent=True,
    )

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статус", callback_data="client_status")],
        [InlineKeyboardButton(text="💬 Мои чаты", callback_data="client_chats")],
        [InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="client_keywords")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="client_help")],
    ])

# --- Middleware: чужих не пускаем, потом проверка подписки ---
async def is_owner_or_admin(client_id: int, user) -> bool:
    """Бот клиента отвечает только его владельцу и администраторам из ADMIN_IDS, остальных не замечает."""
    if user is None:
        return False
    if user.id in config.admin_ids_list:
        return True
    client = await db.get_client_info(client_id)
    return client is not None and client["telegram_id"] == user.id


@router.message.outer_middleware()
async def check_sub_msg(handler, event: Message, data: dict):
    client_id = data.get("client_id")
    if client_id is None or not await is_owner_or_admin(client_id, event.from_user):
        return  # нет контекста клиента или пишет посторонний: молча игнорируем

    if not await db.is_subscription_active(client_id):
        await event.answer("⚠️ Ваш триал/подписка истекли.\nПожалуйста, обратитесь к администратору для продления.")
        return
    return await handler(event, data)

@router.callback_query.outer_middleware()
async def check_sub_cb(handler, event: CallbackQuery, data: dict):
    client_id = data.get("client_id")
    if client_id is None or not await is_owner_or_admin(client_id, event.from_user):
        return

    if not await db.is_subscription_active(client_id):
        await event.answer("⚠️ Ваша подписка истекла.", show_alert=True)
        return
    return await handler(event, data)


# --- Commands Menu ---
@router.message(Command("start", "menu"))
async def cmd_start(message: Message, state: FSMContext, client_id: int):
    await state.clear()
    is_admin = message.from_user.id in config.admin_ids_list

    await message.answer("🏠 <b>Главное меню LeadHunter</b>", reply_markup=persistent_menu_kb(), parse_mode="HTML")

    # Владелец бота: обновляем ник и имя в базе. Администратор в чужом боте ничего не меняет
    client = await db.get_client_info(client_id)
    if client and client["telegram_id"] == message.from_user.id:
        if client.get("username") != message.from_user.username or client.get("display_name") != message.from_user.full_name:
            await db.update_client(client_id, username=message.from_user.username, display_name=message.from_user.full_name)

    welcome_text = f"Добро пожаловать, {html.escape(message.from_user.full_name)}!\nВыберите нужный раздел:"
    if is_admin:
        welcome_text = f"⚡️ <b>Режим администратора</b>\n{welcome_text}"

    await message.answer(welcome_text, reply_markup=main_menu_kb(), parse_mode="HTML")

@router.message(F.text == "🏠 Меню")
async def btn_menu(message: Message, state: FSMContext, client_id: int):
    await state.clear()
    await message.answer("🏠 <b>Главное меню LeadHunter</b>", reply_markup=main_menu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "client_back")
async def cb_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 <b>Главное меню LeadHunter</b>", reply_markup=main_menu_kb(), parse_mode="HTML")


# --- Status ---
@router.callback_query(F.data == "client_status")
async def cb_status(callback: CallbackQuery, client_id: int):
    client = await db.get_client_info(client_id)
    if not client:
        return

    now = datetime.now(timezone.utc)
    end_date = access_end(client)
    if end_date and end_date > now:
        left = (end_date - now).days + 1
        sub_text = f"Активна (осталось {left} дн.)"
    else:
        sub_text = "Истекла"

    chats = await db.get_client_chats(client_id)
    keywords = await db.get_client_keywords(client_id)
    leads_today = await db.get_today_leads_count(client_id)

    text = (
        f"📊 <b>Ваш статус</b>\n\n"
        f"Подписка: {sub_text}\n"
        f"Чатов отслеживается: {len([c for c in chats if c['is_approved']])}\n"
        f"Ключевых слов: {len(keywords)}\n"
        f"Лидов найдено сегодня: {leads_today}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="client_back")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


# --- Chats ---
@router.callback_query(F.data == "client_chats")
async def cb_chats(callback: CallbackQuery, client_id: int):
    # Убираем жесткую автосинхронизацию, чтобы клиент мог удалять дефолтные чаты
    chats = await db.get_client_chats(client_id)
    if not chats:
        await db.copy_default_chats_to_client(client_id)
        chats = await db.get_client_chats(client_id)

    buttons = []
    if not chats:
        text = "💬 <b>Мои чаты</b>\n\nСписок пуст."
    else:
        approved = sum(1 for c in chats if c['is_approved'])
        pending = len(chats) - approved
        lines = [
            f"💬 <b>Мои чаты</b> (Активно: {approved}, Ожидают: {pending})\n",
            "💡 <i>Базовые чаты уже подключены автоматически.</i>\n"
        ]
        for c in chats:
            status = "🟢" if c['is_approved'] else "⏳"
            lines.append(f"{status} {html.escape(c['title'])} (<code>{c['chat_id']}</code>)")
        text = "\n".join(lines)

        if len(chats) <= 15:
            for c in chats:
                buttons.append([InlineKeyboardButton(text=f"❌ Удалить {c['title'][:15]}", callback_data=f"del_chat_{c['chat_id']}")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить чат", callback_data="add_chat")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="client_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("del_chat_"))
async def cb_del_chat(callback: CallbackQuery, client_id: int):
    chat_id = int(callback.data.replace("del_chat_", ""))
    await db.remove_client_chat(client_id, chat_id)
    await cb_chats(callback, client_id)

@router.callback_query(F.data == "add_chat")
async def cb_add_chat(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ClientAddChat.waiting_for_link)
    await callback.message.edit_text(
        "🔗 <b>Добавление чата</b>\n\n"
        "Отправьте ссылку на чат (t.me/...) или @username.\n"
        "<i>(Новые чаты отправляются администратору на проверку)</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="client_chats")]])
    )

@router.message(ClientAddChat.waiting_for_link)
async def process_chat_link(message: Message, state: FSMContext, client_id: int):
    link = (message.text or "").strip()
    if not looks_like_chat_link(link):
        # Состояние не сбрасываем: можно прислать ссылку ещё раз или нажать «Меню»
        await message.answer("Отправьте одним сообщением ссылку на чат (t.me/...) или @username.")
        return

    warning = ""
    rejection = await db.check_rejection_history(link)
    if rejection:
        warning = (
            f"⚠️ <b>ВНИМАНИЕ:</b> Этот чат уже отклонялся ранее "
            f"(запрос #{rejection['id']} от {html.escape(str(rejection['display_name']))})!\n\n"
        )

    req_id = await db.create_pending_request(client_id, link)

    safe_link = html.escape(link, quote=True)
    link_html = f'<a href="{safe_link}">{safe_link}</a>' if link.startswith("http") else f"<code>{safe_link}</code>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"req_approve_{req_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req_reject_{req_id}")
        ]
    ])

    await message.answer(f"⏳ Ссылка <code>{safe_link}</code> отправлена на модерацию.", parse_mode="HTML", reply_markup=main_menu_kb())
    await _notify_admins(
        f"{warning}🆕 <b>Новый запрос на чат!</b>\n"
        f"От клиента ID: {client_id}\n"
        f"Ссылка: {link_html}\n\n"
        f"Проверьте чат и решите:",
        reply_markup=kb
    )

    await state.clear()


async def _notify_admins(text: str, reply_markup: InlineKeyboardMarkup = None):
    """Отправка уведомления всем администраторам."""
    try:
        bot = Bot(token=config.ADMIN_BOT_TOKEN.get_secret_value())
        for adm in config.admin_ids_list:
            try:
                await bot.send_message(adm, text, parse_mode="HTML", reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Failed to notify admin {adm}: {e}")
        await bot.session.close()
    except Exception as e:
        logger.error(f"Error in _notify_admins: {e}")


# --- Keywords ---
@router.callback_query(F.data == "client_keywords")
async def cb_keywords(callback: CallbackQuery, client_id: int):
    keywords = await db.get_client_keywords(client_id)
    # Авто-синхронизация если пусто
    if not keywords:
        await db.copy_default_keywords_to_client(client_id)
        keywords = await db.get_client_keywords(client_id)

    if not keywords:
        text = "🔑 <b>Ключевые слова</b>\n\nСписок пуст."
    else:
        lines = [
            f"🔑 <b>Мои ключевые слова</b> ({len(keywords)})\n",
            "💡 <i>Базовый набор слов уже добавлен при регистрации.</i>\n"
        ]
        for kw in keywords:
            lines.append(f"• <code>{html.escape(kw['keyword'])}</code>")
        text = "\n".join(lines)

    buttons = [[InlineKeyboardButton(text="➕ Добавить", callback_data="add_keyword")]]
    if keywords:
        buttons.append([InlineKeyboardButton(text="❌ Выборочное удаление", callback_data="list_kw_del")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="client_back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "list_kw_del")
async def cb_list_kw_del(callback: CallbackQuery, client_id: int):
    keywords = await db.get_client_keywords(client_id)
    buttons = []
    # Show max 20 for simple deletion
    for kw in keywords[:20]:
        buttons.append([InlineKeyboardButton(text=f"❌ {kw['keyword']}", callback_data=f"del_kw_{kw['id']}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="client_keywords")])
    await callback.message.edit_text("Выберите слово для удаления (последние 20):", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("del_kw_"))
async def cb_del_kw(callback: CallbackQuery, client_id: int):
    kw_id = int(callback.data.replace("del_kw_", ""))
    await db.remove_client_keyword(client_id, kw_id)
    await cb_list_kw_del(callback, client_id)

@router.callback_query(F.data == "add_keyword")
async def cb_add_keyword(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ClientAddKeyword.waiting_for_keyword)
    await callback.message.edit_text(
        "Введите ключевые слова:\n\n"
        "<i>Можно несколько через запятую, точку с запятой или перенос строки.</i>\n"
        "Пример: <code>фотограф, репетитор, ремонт квартиры</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="client_keywords")]])
    )

@router.message(ClientAddKeyword.waiting_for_keyword)
async def process_keyword(message: Message, state: FSMContext, client_id: int):
    keywords = parse_keywords(message.text or "")
    added = []
    for kw in keywords:
        if await db.add_client_keyword(client_id, kw):
            added.append(kw)
    await state.clear()

    if added:
        kw_parts = []
        for k in added:
            lemma = normal_form(k)
            if lemma != k:
                kw_parts.append(f"<b>{html.escape(k)}</b> (→{html.escape(lemma)})")
            else:
                kw_parts.append(f"<b>{html.escape(k)}</b>")
        kw_list = ", ".join(kw_parts)

        text = f"✅ Добавлено ({len(added)}): {kw_list}\n💡 Совпадают все склонения автоматически"
    else:
        text = "⚠️ Все переданные слова уже существуют."

    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


# --- Help ---
def default_help_text() -> str:
    """Общая справка из файла: в ней вместо {admin_contact} подставляется контакт администратора."""
    try:
        with open(HELP_FILE, "r", encoding="utf-8") as f:
            return f.read().replace("{admin_contact}", html.escape(config.ADMIN_CONTACT))
    except Exception as e:
        logger.error(f"Error reading help file: {e}")
        return "❓ <b>Инструкция временно недоступна.</b> Добавьте чаты и ключевые слова в меню."


@router.callback_query(F.data == "client_help")
async def cb_help(callback: CallbackQuery, client_id: int):
    # Свой текст клиента из базы (формат Telegram HTML), если нет - общая справка из файла
    client = await db.get_client_info(client_id)
    text = (client.get('custom_help') if client else None) or default_help_text()

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="client_back")]]),
        parse_mode="HTML"
    )

def setup_client_router():
    return router
