import html
import logging
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import config
from database import Database, access_end
from lead_filters import is_start_command, looks_like_bot_token, parse_keywords

logger = logging.getLogger(__name__)

# Диспетчер бота-админки
dp = Dispatcher()
db: Database = None
admin_bot: Bot = None

# States for editing
class EditNichePrompt(StatesGroup):
    waiting_for_prompt = State()

class AddDefaultChat(StatesGroup):
    waiting_for_chat_info = State()

class DelDefaultChat(StatesGroup):
    waiting_for_chat_id = State()

class AddDefaultKeyword(StatesGroup):
    waiting_for_keyword = State()

class DelDefaultKeyword(StatesGroup):
    waiting_for_keyword = State()

# --- Middleware / Check ---
def stranger_greeting() -> str:
    """Ответ постороннему на /start: как подключиться или что регистрация закрыта (Telegram HTML)."""
    if config.SELF_REGISTRATION:
        return (
            "Привет! Это сервис LeadHunter: он находит заявки в Telegram-чатах и присылает готовые ответы.\n\n"
            "Чтобы подключиться, создайте своего бота у @BotFather и пришлите мне его токен одним сообщением. "
            "Вы получите пробный период."
        )
    return (
        "Привет! Это сервис LeadHunter. Регистрация клиентов закрыта.\n\n"
        f"Чтобы подключиться, напишите администратору: {html.escape(config.ADMIN_CONTACT)}"
    )


@dp.message.outer_middleware()
async def check_admin_msg(handler, event: Message, data: dict):
    user = event.from_user
    if user is None:
        return  # сообщение без отправителя
    if user.id in config.admin_ids_list:
        return await handler(event, data)

    # Постороннему отвечаем только на токен бота (регистрация, если она открыта) и на /start, остальное молча игнорируем
    if looks_like_bot_token(event.text):
        return await handler(event, data)
    if is_start_command(event.text):
        await event.answer(stranger_greeting(), parse_mode="HTML")


@dp.callback_query.outer_middleware()
async def check_admin_cb(handler, event: CallbackQuery, data: dict):
    if event.from_user is None or event.from_user.id not in config.admin_ids_list:
        return
    return await handler(event, data)


# --- Keyboards ---
def persistent_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Меню")]],
        resize_keyboard=True,
        is_persistent=True,
    )

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Клиенты", callback_data="admin_clients")],
        [InlineKeyboardButton(text="📨 Запросы чатов", callback_data="admin_requests")],
        [InlineKeyboardButton(text="🧠 Промпт ниши", callback_data="admin_prompt")],
        [InlineKeyboardButton(text="⚙️ Дефолтный набор", callback_data="admin_defaults")],
        [InlineKeyboardButton(text="🌐 Все чаты", callback_data="admin_all_chats")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔄 Статус системы", callback_data="admin_sys_status")],
    ])

# --- Main Menu handlers ---
@dp.message(Command("start", "menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 <b>Главное меню Admin</b>", reply_markup=persistent_menu_kb(), parse_mode="HTML")
    await message.answer("🔧 <b>АДМИНКА LeadHunter</b>", reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.message(F.text == "🏠 Меню")
async def btn_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔧 <b>АДМИНКА LeadHunter</b>", reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🔧 <b>АДМИНКА LeadHunter</b>", reply_markup=main_menu_kb(), parse_mode="HTML")

# --- Commands ---
@dp.message(Command("add_client"))
async def cmd_add_client(message: Message, command: CommandObject):
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Использование: <code>/add_client &lt;telegram_id&gt; &lt;bot_token&gt; [имя]</code>", parse_mode="HTML")
        return

    tg_id_str, bot_token = args[0], args[1]
    display_name = " ".join(args[2:]) if len(args) > 2 else f"Client {tg_id_str}"

    try:
        tg_id = int(tg_id_str)
    except ValueError:
        await message.answer("❌ telegram_id должен быть числом.")
        return

    try:
        client_id = await db.add_client(tg_id, bot_token, display_name=display_name)
        await db.copy_default_chats_to_client(client_id)
        await db.copy_default_keywords_to_client(client_id)

        try:
            from client_bots import register_new_client_bot
            client_record = await db.get_client_by_telegram_id(tg_id)
            if client_record:
                await register_new_client_bot(db, client_record)
        except Exception as e:
            logger.error(f"Failed to start client bot: {e}")
            await message.answer(f"⚠️ Ошибка запуска бота клиента: {e}")

        await message.answer(f"✅ Клиент <b>{html.escape(display_name)}</b> добавлен (ID={client_id})!\nТриал 7 дней активирован. Дефолты скопированы.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("extend"))
async def cmd_extend(message: Message, command: CommandObject):
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Использование: <code>/extend &lt;telegram_id&gt; &lt;дни&gt;</code>", parse_mode="HTML")
        return
    try:
        tg_id, days = int(args[0]), int(args[1])
        await db.extend_subscription(tg_id, days)
        await message.answer(f"✅ Подписка для {tg_id} продлена на {days} дней.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("deactivate"))
async def cmd_deactivate(message: Message, command: CommandObject):
    args = command.args
    if not args:
        await message.answer("Использование: <code>/deactivate &lt;telegram_id&gt;</code>", parse_mode="HTML")
        return
    try:
        tg_id = int(args.strip())
        await db.deactivate_client(tg_id)
        await message.answer(f"✅ Клиент {tg_id} деактивирован.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("list_clients"))
async def cmd_list_clients(message: Message):
    clients = await db.get_all_clients()
    if not clients:
        await message.answer("Клиентов нет.")
        return

    now = datetime.now(timezone.utc)
    res = ["👥 <b>Все клиенты:</b>\n"]
    for c in clients:
        status = "🟢" if c['is_active'] else "🔴"
        end_date = access_end(c)
        if end_date and end_date > now:
            delta = end_date - now
            left = delta.days
            hours = delta.seconds // 3600
            s_end = f"до {end_date.strftime('%Y-%m-%d')} ({left} дн. {hours} ч.)"
        else:
            s_end = "❌ истек"
            status = "🔴"

        username = c.get('username')
        display_name = html.escape(str(c['display_name'] or c['telegram_id']))
        client_link = f"<a href='https://t.me/{username}'>{display_name}</a>" if username else display_name

        res.append(f"{status} <b>{client_link}</b> (TG: <code>{c['telegram_id']}</code>)\n  Доступ: {s_end}\n")

    await message.answer("\n".join(res), parse_mode="HTML")

# --- Clients Menu ---
@dp.callback_query(F.data == "admin_clients")
async def cb_admin_clients(callback: CallbackQuery):
    await callback.message.edit_text(
        "👥 <b>Управление клиентами</b>\n\n"
        "Используйте команды чата:\n"
        "• <code>/add_client &lt;telegram_id&gt; &lt;bot_token&gt; [имя]</code>\n"
        "• <code>/list_clients</code> - список всех\n"
        "• <code>/extend &lt;telegram_id&gt; &lt;дни&gt;</code> - продлить\n"
        "• <code>/deactivate &lt;telegram_id&gt;</code> - отключить",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]),
        parse_mode="HTML"
    )

# --- Chat Requests ---
@dp.callback_query(F.data == "admin_requests")
async def cb_admin_requests(callback: CallbackQuery):
    reqs = await db.get_pending_requests()
    if not reqs:
        await callback.message.edit_text("📨 Нет ожидающих запросов.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]), parse_mode="HTML")
        return

    first = reqs[0]
    chat_link = first['chat_link']
    link_html = f'<a href="{html.escape(chat_link, quote=True)}">{html.escape(chat_link)}</a>' if chat_link.startswith("http") else f"<code>{html.escape(chat_link)}</code>"

    # Кликабельный профиль клиента
    client_username = first.get('client_username')
    client_name = html.escape(str(first['display_name'] or ""))
    client_link = f"<a href='https://t.me/{client_username}'>{client_name}</a>" if client_username else client_name

    text = (
        f"🆕 <b>Запрос #{first['id']} на добавление чата</b>\n\n"
        f"Клиент: {client_link}\n"
        f"Ссылка: {link_html}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"req_approve_{first['id']}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"req_reject_{first['id']}")
        ],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="admin_back")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("req_approve_"))
async def cb_req_approve(callback: CallbackQuery):
    req_id = int(callback.data.replace("req_approve_", ""))

    reqs = await db.get_pending_requests()
    req = next((r for r in reqs if r['id'] == req_id), None)
    if not req:
        await callback.answer("Запрос не найден или уже обработан.")
        return

    await callback.message.edit_text("⏳ <i>Юзербот пытается вступить в чат... Возможна задержка.</i>", parse_mode="HTML")

    try:
        from userbot import join_chat_via_userbot
        chat_id, title = await join_chat_via_userbot(req['chat_link'])

        # Save chat title in chats table
        await db.get_or_create_chat(chat_id, title)

        await db.resolve_request(req_id, 'approved', chat_id=chat_id, title=title, note="Одобрено Admin Bot")
        await db.add_client_chat(req['client_id'], chat_id, is_approved=True)

        # --- Нотификация клиента ---
        try:
            client_info = await db.get_client_info(req['client_id'])
            if client_info and client_info.get('bot_token'):
                from aiogram import Bot as ClientBot
                c_bot = ClientBot(token=client_info['bot_token'])
                await c_bot.send_message(
                    client_info['telegram_id'],
                    f"✅ Чат <b>{html.escape(title)}</b> одобрен и добавлен в работу!",
                    parse_mode="HTML"
                )
                await c_bot.session.close()
        except Exception as e:
            logger.error(f"Failed to notify client on approval: {e}")

        await callback.answer(f"Чат успешно привязан! ID: {chat_id}", show_alert=True)

        # Обновляем сообщение: вместо ожидания - результат
        new_text = f"✅ <b>Запрос #{req_id} ОДОБРЕН</b>\nЧат: {html.escape(title)}\nКлиент: {html.escape(str(req['display_name']))}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Следующий запрос", callback_data="admin_requests")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="admin_back")]
        ])
        await callback.message.edit_text(new_text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        await callback.message.answer(f"❌ <b>Ошибка вступления:</b> {html.escape(str(e))}\n\nПопробуйте позже или добавьте чат вручную.", parse_mode="HTML")
        await callback.answer("Ошибка вступления.")

@dp.callback_query(F.data.startswith("req_reject_"))
async def cb_req_reject(callback: CallbackQuery):
    req_id = int(callback.data.replace("req_reject_", ""))
    await db.resolve_request(req_id, 'rejected')
    await callback.answer("Запрос отклонен.")

    new_text = f"❌ <b>Запрос #{req_id} ОТКЛОНЕН</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Следующий запрос", callback_data="admin_requests")],
        [InlineKeyboardButton(text="◀️ Меню", callback_data="admin_back")]
    ])
    try:
        await callback.message.edit_text(new_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        # If it was a notification from client_bot, edit might fail if message is too old or already deleted
        await callback.message.answer(new_text, reply_markup=kb, parse_mode="HTML")


# --- Niche Prompt ---
@dp.callback_query(F.data == "admin_prompt")
async def cb_admin_prompt(callback: CallbackQuery):
    prompt = await db.get_niche_prompt()
    escaped_prompt = html.escape(prompt)
    text = f"🧠 <b>Промпт ниши</b>\n\n<code>{escaped_prompt}</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить промпт", callback_data="prompt_edit")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "prompt_edit")
async def cb_prompt_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(EditNichePrompt.waiting_for_prompt)
    await callback.message.edit_text("Отправьте новый промпт ниши:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_prompt")]]))

@dp.message(EditNichePrompt.waiting_for_prompt)
async def process_new_prompt(message: Message, state: FSMContext):
    await db.set_setting("niche_prompt", message.text.strip())
    await state.clear()
    await message.answer("✅ Промпт ниши обновлен!", reply_markup=main_menu_kb())

# --- Defaults ---
@dp.callback_query(F.data == "admin_defaults")
async def cb_admin_defaults(callback: CallbackQuery):
    chats = await db.get_default_chats()
    keywords = await db.get_default_keywords()

    text = f"⚙️ <b>Дефолтный набор ниши</b>\n\n<b>Чаты ({len(chats)}):</b>\n"
    for c in chats:
        c_val = html.escape(c['value'])
        c_extra = html.escape(c['extra'] or "")
        text += f"• {c_extra or c_val} (<code>{c_val}</code>)\n"

    text += f"\n<b>Ключевые слова ({len(keywords)}):</b>\n"
    kw_text = ", ".join([html.escape(k) for k in keywords[:40]])
    text += kw_text + ("..." if len(keywords) > 40 else "")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить чат", callback_data="defaults_add_chat"),
         InlineKeyboardButton(text="❌ Удалить чат", callback_data="defaults_del_chat")],
        [InlineKeyboardButton(text="➕ Добавить KW", callback_data="defaults_add_kw"),
         InlineKeyboardButton(text="❌ Удалить KW", callback_data="defaults_del_kw")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")



@dp.callback_query(F.data == "defaults_add_chat")
async def cb_defaults_add_chat(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddDefaultChat.waiting_for_chat_info)
    await callback.message.edit_text(
        "📝 <b>Добавление дефолтного чата</b>\n\n"
        "Пришлите ссылку на чат (t.me/...) и, если нужно, название через пробел.\n"
        "Пример: <code>t.me/chat_link Название</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_defaults")]]),
        parse_mode="HTML"
    )

@dp.message(AddDefaultChat.waiting_for_chat_info)
async def process_add_default_chat(message: Message, state: FSMContext):
    parts = message.text.strip().split(None, 1)
    link = parts[0]
    title = parts[1] if len(parts) > 1 else ""

    if "t.me/" not in link and "@" not in link and not link.lstrip('-').isdigit():
        await message.answer("❌ Похоже, это не ссылка на Telegram чат и не числовой ID. Попробуйте еще раз.")
        return

    msg = await message.answer("⏳ <i>Юзербот проверяет и вступает в чат...</i>", parse_mode="HTML")

    try:
        from userbot import join_chat_via_userbot
        chat_id, resolved_title = await join_chat_via_userbot(link)
        final_title = title or resolved_title

        await db.add_default_chat(str(chat_id), final_title)
        # Сразу добавляем чат всем активным клиентам
        synced = await db.sync_default_chat_to_all_clients(chat_id, final_title)
        await state.clear()
        await msg.edit_text(
            f"✅ Чат <b>{html.escape(final_title)}</b> добавлен и <b>синхронизирован</b> с {synced} клиентами.",
            reply_markup=main_menu_kb(), parse_mode="HTML"
        )
    except Exception as e:
        await state.clear()
        await msg.edit_text(f"❌ <b>Ошибка вступления юзербота:</b> {html.escape(str(e))}\n\nВозможно, ссылка недействительна или юзербот забанен в этом чате. Вы можете вступить в него вручную с аккаунта юзербота и отправить сюда <b>числовой ID</b>: по числовому id юзербот не вступает в чат, а находит тот, в котором уже состоит.", reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "defaults_del_chat")
async def cb_defaults_del_chat(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DelDefaultChat.waiting_for_chat_id)
    chats = await db.get_default_chats()
    if not chats:
        await callback.answer("Дефолтных чатов нет.")
        return

    text = "🗑 <b>Удаление дефолтного чата</b>\n\nПришлите ссылку/ID чата из списка:\n"
    for c in chats:
        text += f"• <code>{html.escape(c['value'])}</code> ({html.escape(c['extra'] or '??')})\n"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_defaults")]]),
        parse_mode="HTML"
    )

@dp.message(DelDefaultChat.waiting_for_chat_id)
async def process_del_default_chat(message: Message, state: FSMContext):
    await db.remove_default_chat(message.text.strip())
    await state.clear()
    await message.answer(f"✅ Чат <code>{html.escape(message.text.strip())}</code> удален из дефолтов.", reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "defaults_add_kw")
async def cb_defaults_add_kw(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddDefaultKeyword.waiting_for_keyword)
    await callback.message.edit_text("Введите ключевое слово (или несколько через запятую):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_defaults")]]))

@dp.message(AddDefaultKeyword.waiting_for_keyword)
async def process_add_default_kw(message: Message, state: FSMContext):
    keywords = parse_keywords(message.text or "")
    for kw in keywords:
        await db.add_default_keyword(kw)
    await state.clear()
    await message.answer(f"✅ Добавлено {len(keywords)} дефолтных keywords.", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "defaults_del_kw")
async def cb_defaults_del_kw(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DelDefaultKeyword.waiting_for_keyword)
    keywords = await db.get_default_keywords()
    text = "🗑 <b>Удаление ключевого слова</b>\n\nВведите слово из списка:\n"
    text += ", ".join([html.escape(k) for k in keywords[:100]])
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_defaults")]]),
        parse_mode="HTML"
    )

@dp.message(DelDefaultKeyword.waiting_for_keyword)
async def process_del_default_kw(message: Message, state: FSMContext):
    await db.remove_default_keyword(message.text.strip())
    await state.clear()
    await message.answer(f"✅ Ключевое слово <code>{html.escape(message.text.strip())}</code> удалено.", reply_markup=main_menu_kb(), parse_mode="HTML")

# --- Global Chats ---
@dp.callback_query(F.data == "admin_all_chats")
async def cb_admin_all_chats(callback: CallbackQuery):
    async with db.pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM chats")
        chats = await conn.fetch("SELECT chat_id, title FROM chats ORDER BY created_at DESC LIMIT 20")

    text = f"🌐 <b>Все чаты в системе ({count})</b>\n\n"
    for c in chats:
        t_esc = html.escape(c['title'] or "??")
        id_esc = html.escape(str(c['chat_id']))
        text += f"• <code>{id_esc}</code> - {t_esc}\n"

    if count > 20:
        text += "\n<i>(Показаны последние 20)</i>"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]),
        parse_mode="HTML"
    )

# --- Stats & Sys ---
@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    clients = await db.get_all_clients()
    today_leads = await db.get_today_leads_count()

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"Всего клиентов: {len(clients)}\n"
        f"Активных: {sum(1 for c in clients if c['is_active'])}\n"
        f"Лидов сегодня (по всем): {today_leads}\n"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]]), parse_mode="HTML")

# --- Саморегистрация клиента: он присылает токен своего бота ---
@dp.message(F.text.func(looks_like_bot_token))
async def cb_auto_register(message: Message):
    # Токен пропускается middleware от любого пользователя (не только админа)
    if not config.can_register(message.from_user.id):
        await message.answer(f"Регистрация закрыта. Напишите администратору: {config.ADMIN_CONTACT}")
        return
    token = message.text.strip()
    tg_id = message.from_user.id
    username = message.from_user.username
    display_name = message.from_user.full_name

    try:
        from client_bots import register_new_client_bot, unregister_client_bot

        client = await db.get_client_by_telegram_id(tg_id)
        if client:
            if client['bot_token'] != token:
                await unregister_client_bot(client['bot_token'])
            await db.update_client(client['id'], bot_token=token, username=username, display_name=display_name)
            reply = "✅ Ваш токен обновлен. Бот мониторинга теперь активен с новыми данными."
        else:
            client_id = await db.add_client(tg_id, token, display_name, username)
            # Сразу копируем дефолтный набор
            await db.copy_default_keywords_to_client(client_id)
            await db.copy_default_chats_to_client(client_id)
            reply = (
                "🎉 <b>Регистрация успешна!</b>\n\n"
                "Я добавил вас в базу как нового клиента. Ваши дефолтные чаты и ключевые слова уже подключены.\n\n"
                "<b>Ваш следующий шаг:</b>\n"
                "1. Откройте созданного вами бота (его юзернейм вам прислал @BotFather).\n"
                "2. Нажмите /start.\n"
                "3. Мониторинг начнется автоматически!"
            )
            await _notify_admins(
                f"🆕 <b>Новая авто-регистрация!</b>\nКлиент: {html.escape(display_name)} "
                f"(@{html.escape(username or 'no_user')})\nID: {tg_id}"
            )

        # Запускаем бота клиента сразу, без перезапуска приложения
        await register_new_client_bot(db, await db.get_client_by_telegram_id(tg_id))
        await message.answer(reply, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Auto-registration failed: {e}")
        await message.answer(f"⚠️ Ошибка при регистрации. Проверьте правильность токена или обратитесь к {config.ADMIN_CONTACT}.")

@dp.message()
async def cmd_echo(message: Message):
    # Сюда доходят только администраторы: посторонним отвечает check_admin_msg
    await message.answer("Команда не распознана. Используйте меню или /start.")

async def _notify_admins(text: str):
    for aid in config.admin_ids_list:
        try:
            await admin_bot.send_message(aid, text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not notify admin {aid}: {e}")

@dp.callback_query(F.data == "admin_sys_status")
async def cb_admin_sys_status(callback: CallbackQuery):
    act = await db.get_setting("monitoring_active") == "true"
    status_icon = "🟢 Включен" if act else "🔴 Остановлен"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Остановить" if act else "▶️ Запустить", callback_data="admin_toggle_sys")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(f"🔄 <b>Статус системы:</b> {status_icon}\n\n(Выключает парсинг для всех)", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_toggle_sys")
async def cb_admin_toggle_sys(callback: CallbackQuery):
    act = await db.get_setting("monitoring_active") == "true"
    await db.set_setting("monitoring_active", "false" if act else "true")
    await cb_admin_sys_status(callback)


# --- Startup ---
async def start_admin_bot(db_instance: Database):
    global db, admin_bot
    db = db_instance
    admin_bot = Bot(token=config.ADMIN_BOT_TOKEN.get_secret_value())
    logger.info("Starting admin_bot polling...")
    await dp.start_polling(admin_bot, drop_pending_updates=True)
