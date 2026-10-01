import asyncio
import logging
import sys
import html
import re
import re
from datetime import datetime, timedelta
import calendar
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from aiogram.types import ReactionTypeEmoji
MINSK_TZ = ZoneInfo("Europe/Minsk")
scheduler = AsyncIOScheduler(timezone=MINSK_TZ)

# Используем минское время и для времени в логах.
def minsk_log_converter(timestamp):
    return datetime.fromtimestamp(timestamp, MINSK_TZ).timetuple()

logging.Formatter.converter = minsk_log_converter

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "8293585417:AAG0yaEWy-FJcI6UIm67ODlUSQfTKNFAsC4" 
ADMIN_ID = 6774558397
ANON_CHAT_ID = -1002720925459
DB_NAME = "school_data.db"

# Включаем логирование
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЯ (FSM) ---
class HWState(StatesGroup):
    choosing_subject = State()
    writing_task = State()

class SetupState(StatesGroup):
    choosing_day = State()      # Выбор дня для настройки
    waiting_for_lessons = State() # Ожидание списка уроков
    
class SetupDrState(StatesGroup):
    waiting_for_name = State()
    waiting_for_date = State()
    editing_name = State()
    editing_date = State()

class AnonymousState(StatesGroup):
    waiting_for_choice = State()
    waiting_for_link = State()
    waiting_for_text = State()

class AnonymousAdminState(StatesGroup):
    waiting_for_search = State()

class AdminMessageState(StatesGroup):
    waiting_for_text = State()

class AdminBotReplyState(StatesGroup):
    waiting_for_link = State()
    waiting_for_text = State()

class AdminBotReactionState(StatesGroup):
    waiting_for_link = State()
    choosing_reaction = State()

class ReminderState(StatesGroup):
    waiting_for_text = State()
    choosing_type = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_once_text = State()
    choosing_days = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS schedule (
                chat_id INTEGER,
                day_int INTEGER,
                lesson_num INTEGER,
                subject TEXT,
                PRIMARY KEY (chat_id, day_int, lesson_num)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS birthdays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                full_name TEXT,
                birth_day INTEGER,
                birth_month INTEGER
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS homework (
                chat_id INTEGER,
                date_str TEXT,
                subject TEXT,
                task TEXT,
                PRIMARY KEY (chat_id, date_str, subject)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS anonymous_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                text TEXT,
                sent_message_id INTEGER,
                replied_to_message_id INTEGER,
                created_at TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                text TEXT,
                time_str TEXT,
                weekdays TEXT,
                created_at TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS anonymous_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                anonymous_message_id INTEGER,
                user_id INTEGER,
                reaction INTEGER,
                UNIQUE(anonymous_message_id, user_id)
            )
        ''')
        cursor = await db.execute("PRAGMA table_info(anonymous_messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "likes" not in columns:
            await db.execute("ALTER TABLE anonymous_messages ADD COLUMN likes INTEGER NOT NULL DEFAULT 0")
        if "dislikes" not in columns:
            await db.execute("ALTER TABLE anonymous_messages ADD COLUMN dislikes INTEGER NOT NULL DEFAULT 0")

        # Чистим только сиротские реакции, для которых уже нет анонимного сообщения.
        await db.execute(
            "DELETE FROM anonymous_reactions "
            "WHERE anonymous_message_id NOT IN "
            "(SELECT id FROM anonymous_messages)"
        )
        await db.commit()

# --- АДМИН-ПАНЕЛЬ ---

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🕵️ Анонимные сообщения", callback_data="admin_anon"),
        InlineKeyboardButton(text="🎂 Дни рождения", callback_data="admin_birthdays")
    )
    builder.row(
        InlineKeyboardButton(text="⏰ Напоминания", callback_data="admin_reminders")
    )
    builder.row(
        InlineKeyboardButton(text="🧹 Старое ДЗ", callback_data="admin_hw_cleanup")
    )
    builder.row(
        InlineKeyboardButton(text="📢 Сообщение от бота", callback_data="admin_bot_message")
    )
    return builder.as_markup()


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return

    await message.answer(
        "🛠 *Админ-панель*\n\n"
        "Панель создана. Функции управления добавим сюда позже.",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_birthdays")
async def admin_birthdays(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    text, kb = await get_birthdays_manage_text(ANON_CHAT_ID)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

async def get_anonymous_messages(page: int = 0):
    per_page = 5
    offset = page * per_page

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM anonymous_messages WHERE chat_id = ?",
            (ANON_CHAT_ID,)
        )
        total = (await cursor.fetchone())[0]

        cursor = await db.execute(
            """SELECT id, user_id, username, display_name, text, sent_message_id,
                      replied_to_message_id, created_at, likes, dislikes
               FROM anonymous_messages
               WHERE chat_id = ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (ANON_CHAT_ID, per_page, offset)
        )
        rows = await cursor.fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return rows, total_pages

def get_anonymous_admin_keyboard(page: int, total_pages: int):
    builder = InlineKeyboardBuilder()

    if total_pages > 1:
        prev_page = (page - 1) % total_pages
        next_page = (page + 1) % total_pages
        builder.row(
            InlineKeyboardButton(text="⬅️", callback_data=f"anonpage_{prev_page}"),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="anon_page_info"),
            InlineKeyboardButton(text="➡️", callback_data=f"anonpage_{next_page}")
        )

    builder.row(
        InlineKeyboardButton(text="🔎 Поиск", callback_data="anon_search"),
        InlineKeyboardButton(text="🗑 Очистить историю", callback_data="anon_clear")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back"))
    return builder.as_markup()

def format_anonymous_admin_text(rows, page: int, total_pages: int):
    if not rows:
        return "🕵️ <b>Анонимные сообщения</b>\n\nИстория пока пуста."

    text = "🕵️ <b>Последние анонимные сообщения</b>\n〰️〰️〰️〰️〰️〰️〰️\n"

    for row in rows:
        msg_id, user_id, username, display_name, msg_text, sent_id, replied_id, created_at, likes, dislikes = row
        author = html.escape(display_name or "Без имени")
        if username:
            author += f" (@{html.escape(username)})"
        safe_text = html.escape(msg_text)
        reply_info = f"\n↪️ Ответ на сообщение: <code>{replied_id}</code>" if replied_id else ""
        text += (
            f"<b>#{msg_id}</b> — {author}\n"
            f"ID: <code>{user_id}</code>\n"
            f"🕐 {html.escape(created_at)}\n"
            f"💬 {safe_text}\n"
            f"📨 Сообщение бота: <code>{sent_id}</code>{reply_info}\n"
            f"👍 {likes}  👎 {dislikes}\n\n"
        )

    text += f"<i>Страница {page + 1} из {total_pages}</i>"
    return text

@dp.callback_query(F.data == "admin_anon")
async def admin_anonymous_messages(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    rows, total_pages = await get_anonymous_messages(0)
    await callback.message.edit_text(
        format_anonymous_admin_text(rows, 0, total_pages),
        reply_markup=get_anonymous_admin_keyboard(0, total_pages),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("anonpage_"))
async def anonymous_page_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    page = int(callback.data.split("_")[1])
    rows, total_pages = await get_anonymous_messages(page)
    await callback.message.edit_text(
        format_anonymous_admin_text(rows, page, total_pages),
        reply_markup=get_anonymous_admin_keyboard(page, total_pages),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "anon_page_info")
async def anonymous_page_info(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Используй стрелки для переключения страниц.")
        return
    await callback.answer("Используй стрелки для переключения страниц.")

@dp.callback_query(F.data == "anon_search")
async def anonymous_search_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AnonymousAdminState.waiting_for_search)
    await callback.message.answer(
        "🔎 Введи текст для поиска по анонимным сообщениям.\n"
        "Можно искать по тексту, имени, username или Telegram ID.\n\n"
        "Для отмены напиши /cancel.",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()

@dp.message(Command("cancel"), AnonymousAdminState.waiting_for_search)
async def anonymous_search_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Поиск отменён.")

@dp.message(AnonymousAdminState.waiting_for_search)
async def anonymous_search_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ Введи текст для поиска.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """SELECT id, user_id, username, display_name, text, sent_message_id,
                      replied_to_message_id, created_at, likes, dislikes
               FROM anonymous_messages
               WHERE chat_id = ? AND (
                   text LIKE ? OR display_name LIKE ? OR username LIKE ? OR CAST(user_id AS TEXT) LIKE ?
               )
               ORDER BY id DESC LIMIT 20""",
            (ANON_CHAT_ID, f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
        )
        rows = await cursor.fetchall()

    await state.clear()

    if not rows:
        await message.answer("🔎 Ничего не найдено.")
        return

    text = "🔎 <b>Результаты поиска</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
    for row in rows:
        msg_id, user_id, username, display_name, msg_text, sent_id, replied_id, created_at, likes, dislikes = row
        author = html.escape(display_name or "Без имени")
        if username:
            author += f" (@{html.escape(username)})"
        text += (
            f"<b>#{msg_id}</b> — {author}\n"
            f"ID: <code>{user_id}</code>\n"
            f"🕐 {html.escape(created_at)}\n"
            f"💬 {html.escape(msg_text)}\n"
            f"📨 <code>{sent_id}</code>\n👍 {likes}  👎 {dislikes}"
        )
        if replied_id:
            text += f" | ↪️ <code>{replied_id}</code>"
        text += "\n\n"

    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "anon_clear")
async def anonymous_clear_start(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Нет", callback_data="anon_clear_no"),
        InlineKeyboardButton(text="✅ Да, очистить", callback_data="anon_clear_yes")
    )
    await callback.message.edit_text(
        "⚠️ *Очистить всю историю анонимных сообщений?*\n\n"
        "Это удалит только журнал в базе данных. Уже отправленные сообщения в чате не удалятся.\n\nЭто действие нельзя отменить.",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "anon_clear_no")
async def anonymous_clear_no(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    rows, total_pages = await get_anonymous_messages(0)
    await callback.message.edit_text(
        format_anonymous_admin_text(rows, 0, total_pages),
        reply_markup=get_anonymous_admin_keyboard(0, total_pages),
        parse_mode="HTML"
    )
    await callback.answer("Очистка отменена")

@dp.callback_query(F.data == "anon_clear_yes")
async def anonymous_clear_yes(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM anonymous_reactions "
            "WHERE anonymous_message_id IN "
            "(SELECT id FROM anonymous_messages WHERE chat_id = ?)",
            (ANON_CHAT_ID,)
        )
        await db.execute("DELETE FROM anonymous_messages WHERE chat_id = ?", (ANON_CHAT_ID,))
        await db.commit()

    rows, total_pages = await get_anonymous_messages(0)
    await callback.message.edit_text(
        format_anonymous_admin_text(rows, 0, total_pages),
        reply_markup=get_anonymous_admin_keyboard(0, total_pages),
        parse_mode="HTML"
    )
    await callback.answer("История очищена")


async def get_all_reminders():
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """SELECT id, user_id, text, time_str, weekdays, created_at
               FROM reminders WHERE chat_id = ? ORDER BY time_str, id""",
            (ANON_CHAT_ID,)
        )
        return await cursor.fetchall()


def format_admin_reminders(rows):
    if not rows:
        return "⏰ <b>Напоминания</b>\n\nНапоминаний пока нет."

    text = "⏰ <b>Все напоминания</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
    for rid, user_id, reminder_text, time_str, weekdays, created_at in rows:
        text += (
            f"<b>#{rid}</b> — ⏰ {time_str} — 📅 {html.escape(format_weekdays(weekdays))}\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"📝 {html.escape(reminder_text)}\n"
            f"🕐 {html.escape(created_at)}\n\n"
        )
    return text


def get_admin_reminders_keyboard(rows):
    builder = InlineKeyboardBuilder()
    for rid, user_id, reminder_text, time_str, weekdays, created_at in rows:
        short_text = reminder_text.replace("\n", " ").strip()
        if len(short_text) > 22:
            short_text = short_text[:22] + "…"
        builder.row(
            InlineKeyboardButton(
                text=f"🗑 #{rid} {time_str} — {short_text}",
                callback_data=f"admin_rem_delete_{rid}"
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back"))
    return builder.as_markup()


@dp.callback_query(F.data == "admin_reminders")
async def admin_reminders(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    rows = await get_all_reminders()
    await callback.message.edit_text(
        format_admin_reminders(rows),
        reply_markup=get_admin_reminders_keyboard(rows),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_rem_delete_"))
async def admin_reminder_delete(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    reminder_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM reminders WHERE id = ? AND chat_id = ?",
            (reminder_id, ANON_CHAT_ID)
        )
        await db.commit()
    rows = await get_all_reminders()
    await callback.message.edit_text(
        format_admin_reminders(rows),
        reply_markup=get_admin_reminders_keyboard(rows),
        parse_mode="HTML"
    )
    await callback.answer("Напоминание удалено")


@dp.callback_query(F.data == "admin_hw_cleanup")
async def admin_hw_cleanup_start(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    monday = datetime.now(MINSK_TZ).date() - timedelta(days=datetime.now(MINSK_TZ).weekday())
    monday_str = monday.strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM homework WHERE chat_id = ? AND date_str < ?",
            (ANON_CHAT_ID, monday_str)
        )
        count = (await cursor.fetchone())[0]

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin_hw_cleanup_no"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data="admin_hw_cleanup_yes")
    )

    await callback.message.edit_text(
        "🧹 <b>Очистка старого ДЗ</b>\n\n"
        f"Удалить все записи ДЗ до <b>{monday.strftime('%d.%m.%Y')}</b>?\n"
        "ДЗ текущей недели останется.\n\n"
        f"Найдено старых записей: <b>{count}</b>.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_hw_cleanup_no")
async def admin_hw_cleanup_no(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>\n\nВыбери нужный раздел:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer("Очистка отменена")


@dp.callback_query(F.data == "admin_hw_cleanup_yes")
async def admin_hw_cleanup_yes(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    monday = datetime.now(MINSK_TZ).date() - timedelta(days=datetime.now(MINSK_TZ).weekday())
    monday_str = monday.strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM homework WHERE chat_id = ? AND date_str < ?",
            (ANON_CHAT_ID, monday_str)
        )
        count = (await cursor.fetchone())[0]
        await db.execute(
            "DELETE FROM homework WHERE chat_id = ? AND date_str < ?",
            (ANON_CHAT_ID, monday_str)
        )
        await db.commit()

    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>\n\nВыбери нужный раздел:",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer(f"Удалено записей ДЗ: {count}")


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "🛠 *Админ-панель*\n\nВыбери нужный раздел:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_bot_message")
async def admin_bot_message_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminMessageState.waiting_for_text)
    await callback.message.answer(
        "📢 <b>Сообщение от бота</b>\n\n"
        "Напиши текст, который бот отправит в основной чат без подписи и без отметки об анонимном отправителе.",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("cancel"), AdminMessageState.waiting_for_text)
async def admin_bot_message_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Отправка сообщения отменена.")


@dp.message(AdminMessageState.waiting_for_text)
async def admin_bot_message_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    text_to_send = (message.text or "").strip()
    if not text_to_send:
        await message.answer("❌ Нужно отправить обычный текст.")
        return

    try:
        await bot.send_message(
            chat_id=ANON_CHAT_ID,
            text=text_to_send
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение от бота: {e}")
        await message.answer("❌ Не удалось отправить сообщение в основной чат.")
        return

    await state.clear()
    await message.answer("✅ Сообщение отправлено в основной чат от имени бота.")


def get_day_name(date_obj):

    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[date_obj.weekday()]

def get_day_name_by_int(day_int):
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
    if 0 <= day_int < 6:
        return days[day_int]
    return "Неизвестно"

async def generate_schedule_text(chat_id: int, date_obj: datetime.date):
    day_int = date_obj.weekday()
    date_str = date_obj.strftime("%Y-%m-%d")
    
    header = f"📅 *{date_obj.strftime('%d.%m.%Y')}* ({get_day_name(date_obj)})\n"
    header += "〰️〰️〰️〰️〰️〰️〰️\n"
    
    if day_int == 6:
        return header + "🏖 *Сегодня выходной!* Уроков нет."

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT lesson_num, subject FROM schedule WHERE chat_id = ? AND day_int = ? ORDER BY lesson_num",
            (chat_id, day_int)
        )
        lessons = await cursor.fetchall()
        
        if not lessons:
            return header + "📂 *Расписание пусто.*\nИспользуйте /setup для настройки."

        lesson_map = {l[0]: l[1] for l in lessons}

        cursor_hw = await db.execute(
            "SELECT subject, task FROM homework WHERE chat_id = ? AND date_str = ?",
            (chat_id, date_str)
        )
        hw_rows = await cursor_hw.fetchall()
        hw_map = {h[0]: h[1] for h in hw_rows}

    text = header
    has_lessons = False
    # Выводим до 8 уроков, или до последнего заполненного, если их меньше 8, но хотя бы 1 есть
    max_lesson = max(lesson_map.keys()) if lesson_map else 8
    limit = 8 # Всегда 8 строк, как в дневнике, или можно limit = max_lesson
    
    for i in range(1, 9):
        subject = lesson_map.get(i, "—")
        
        # Красивое отображение: если предмета нет, просто прочерк
        if subject == "—":
            line = f"{i}. —"
        else:
            has_lessons = True
            hw_text = hw_map.get(subject, "Нет Д/з")
            line = f"{i}. *{subject}*: {hw_text}"
        
        text += line + "\n"
    
    return text

def get_keyboard(chat_id, date_obj):
    date_str = date_obj.strftime("%Y-%m-%d")
    prev_date = (date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
    next_date = (date_obj + timedelta(days=1)).strftime("%Y-%m-%d")
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⬅️", callback_data=f"nav_{prev_date}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_{date_str}"),
        InlineKeyboardButton(text="➡️", callback_data=f"nav_{next_date}")
    )
    if bot_info and bot_info.username:
        bot_link = f"https://t.me/{bot_info.username}?start=fill_{chat_id}_{date_str}"
        builder.row(InlineKeyboardButton(text="✍️ Заполнить ДЗ", url=bot_link))
    
    return builder.as_markup()

# Клавиатура для настройки дней недели
def get_setup_keyboard():
    builder = InlineKeyboardBuilder()
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    for i, day in enumerate(days):
        builder.button(text=day, callback_data=f"setup_day_{i}")
    builder.adjust(3) # по 3 кнопки в ряд
    builder.row(InlineKeyboardButton(text="✅ Готово / Выход", callback_data="setup_done"))
    return builder.as_markup()

def get_cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
    return builder.as_markup()


@dp.callback_query(F.data == "fsm_cancel")
async def fsm_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "❌ Действие отменено.",
        reply_markup=get_main_menu_keyboard(callback.from_user.id == ADMIN_ID),
        parse_mode="HTML"
    )
    await callback.answer("Отменено")


# --- ХЕНДЛЕРЫ НАСТРОЙКИ РАСПИСАНИЯ (/setup) ---

@dp.message(Command("setup"))
async def cmd_setup(message: types.Message, state: FSMContext):
    # В идеале здесь стоит добавить проверку на админа: 
    # if message.chat.type != 'private' and ... (проверка прав)
    
    await message.answer(
        "🛠 *Режим настройки расписания*\n"
        "Выберите день недели для редактирования:",
        reply_markup=get_setup_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(SetupState.choosing_day)

@dp.callback_query(SetupState.choosing_day, F.data.startswith("setup_day_"))
async def setup_day_chosen(callback: types.CallbackQuery, state: FSMContext):
    day_int = int(callback.data.split("_")[2])
    day_name = get_day_name_by_int(day_int)
    
    await state.update_data(setup_day_int=day_int)
    
    await callback.message.edit_text(
        f"Редактируем: *{day_name}*.\n\n"
        "Напишите список предметов *через запятую* или с новой строки.\n"
        "Если урока нет, поставьте прочерк или минус.\n\n"
        "_Пример:_\n"
        "Алгебра, Геометрия, -, Физика, Английский",
        parse_mode="Markdown",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(SetupState.waiting_for_lessons)

@dp.message(SetupState.waiting_for_lessons)
async def setup_receive_lessons(message: types.Message, state: FSMContext):
    data = await state.get_data()
    day_int = data.get('setup_day_int')
    chat_id = message.chat.id
    raw_text = message.text
    
    # Парсинг текста (разбиваем по запятым или переносам строк)
    text = raw_text.replace('\n', ',')
    lessons_list = [s.strip() for s in text.split(',') if s.strip()]
    
    # Обрезаем до 8 уроков
    lessons_list = lessons_list[:8]
    
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Удаляем старое расписание на этот день
        await db.execute(
            "DELETE FROM schedule WHERE chat_id = ? AND day_int = ?", 
            (chat_id, day_int)
        )
        
        # 2. Записываем новое
        records = []
        for i, subj in enumerate(lessons_list):
            # Если пользователь написал "-" или "нет", сохраняем как прочерк
            if subj in ["-", "нет", "окно"]:
                subj = "—"
            records.append((chat_id, day_int, i + 1, subj))
        
        if records:
            await db.executemany("INSERT INTO schedule VALUES (?, ?, ?, ?)", records)
            await db.commit()
    
    await message.answer(
        f"✅ Расписание на {get_day_name_by_int(day_int)} сохранено!\n"
        "Выберите другой день или нажмите Готово.",
        reply_markup=get_setup_keyboard()
    )
    # Возвращаемся к выбору дня
    await state.set_state(SetupState.choosing_day)

@dp.callback_query(SetupState.choosing_day, F.data == "setup_done")
async def setup_finish(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("✅ Настройка расписания завершена.\nНапишите 'дз', чтобы проверить.")
# --- ХЕНДЛЕРЫ ДЗ И ЛОГИКА (Остальное без изменений) ---

def get_anonymous_reaction_keyboard(anonymous_id: int, likes: int = 0, dislikes: int = 0):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=f"👍 {likes}", callback_data=f"anon_like_{anonymous_id}"),
        InlineKeyboardButton(text=f"👎 {dislikes}", callback_data=f"anon_dislike_{anonymous_id}")
    )
    return builder.as_markup()


@dp.callback_query(F.data.startswith("anon_like_") | F.data.startswith("anon_dislike_"))
async def anonymous_reaction(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    anonymous_id = int(parts[-1])
    reaction = 1 if parts[1] == "like" else -1
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT reaction FROM anonymous_reactions WHERE anonymous_message_id = ? AND user_id = ?", (anonymous_id, user_id))
        existing = await cur.fetchone()
        if existing and existing[0] == reaction:
            await db.execute("DELETE FROM anonymous_reactions WHERE anonymous_message_id = ? AND user_id = ?", (anonymous_id, user_id))
            if reaction == 1:
                await db.execute("UPDATE anonymous_messages SET likes = MAX(0, likes - 1) WHERE id = ?", (anonymous_id,))
            else:
                await db.execute("UPDATE anonymous_messages SET dislikes = MAX(0, dislikes - 1) WHERE id = ?", (anonymous_id,))
            await db.commit()
            cur = await db.execute("SELECT likes, dislikes FROM anonymous_messages WHERE id = ?", (anonymous_id,))
            counts = await cur.fetchone()
            if counts:
                try:
                    await bot.edit_message_reply_markup(chat_id=ANON_CHAT_ID, message_id=callback.message.message_id, reply_markup=get_anonymous_reaction_keyboard(anonymous_id, counts[0], counts[1]))
                except Exception as e:
                    logging.error(f"Не удалось обновить кнопки реакций: {e}")
            await callback.answer("Голос убран.")
            return
        if existing:
            await db.execute("UPDATE anonymous_reactions SET reaction = ? WHERE anonymous_message_id = ? AND user_id = ?", (reaction, anonymous_id, user_id))
            if reaction == 1:
                await db.execute("UPDATE anonymous_messages SET likes = likes + 1, dislikes = MAX(0, dislikes - 1) WHERE id = ?", (anonymous_id,))
            else:
                await db.execute("UPDATE anonymous_messages SET dislikes = dislikes + 1, likes = MAX(0, likes - 1) WHERE id = ?", (anonymous_id,))
        else:
            await db.execute("INSERT INTO anonymous_reactions (anonymous_message_id, user_id, reaction) VALUES (?, ?, ?)", (anonymous_id, user_id, reaction))
            await db.execute("UPDATE anonymous_messages SET likes = likes + ? , dislikes = dislikes + ? WHERE id = ?", (1 if reaction == 1 else 0, 1 if reaction == -1 else 0, anonymous_id))
        await db.commit()
        cur = await db.execute("SELECT likes, dislikes FROM anonymous_messages WHERE id = ?", (anonymous_id,))
        counts = await cur.fetchone()
    if counts:
        try:
            await bot.edit_message_reply_markup(chat_id=ANON_CHAT_ID, message_id=callback.message.message_id, reply_markup=get_anonymous_reaction_keyboard(anonymous_id, counts[0], counts[1]))
        except Exception as e:
            logging.error(f"Не удалось обновить кнопки реакций: {e}")
    await callback.answer("👍 Голос учтён." if reaction == 1 else "👎 Голос учтён.")


def get_anonymous_choice_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="↩️ В ответ на сообщение", callback_data="anon_choice_reply")
    )
    builder.row(
        InlineKeyboardButton(text="✉️ Просто анонимное сообщение", callback_data="anon_choice_plain")
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
    return builder.as_markup()


@dp.message(Command("anon"))
async def cmd_anon(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        await message.answer("ℹ️ Анонимные сообщения отправляются через личный чат с ботом.")
        return

    await state.set_state(AnonymousState.waiting_for_choice)
    await message.answer(
        "🕵️ *Анонимное сообщение*\n\n"
        "Выбери, как отправить сообщение:",
        reply_markup=get_anonymous_choice_keyboard(),
        parse_mode="Markdown"
    )


@dp.callback_query(AnonymousState.waiting_for_choice, F.data == "anon_choice_reply")
async def anonymous_choice_reply(callback: types.CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer()
        return

    await state.set_state(AnonymousState.waiting_for_link)
    await callback.message.edit_text(
        "↩️ *Анонимный ответ*\n\n"
        "Скопируй ссылку на нужное сообщение из нашего чата и отправь её сюда.\n\n"
        "Например:\n"
        "`https://t.me/c/2720925459/12345`\n\n"
        "После проверки ссылки я попрошу написать текст ответа.",
        parse_mode="Markdown",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()


@dp.callback_query(AnonymousState.waiting_for_choice, F.data == "anon_choice_plain")
async def anonymous_choice_plain(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(
        target_chat_id=ANON_CHAT_ID,
        replied_to_message_id=None
    )
    await state.set_state(AnonymousState.waiting_for_text)
    await callback.message.edit_text(
        "✉️ *Анонимное сообщение*\n\n"
        "Напиши текст одним сообщением. Он будет опубликован в чате без ответа на другое сообщение.\n\n"
        "Пока поддерживаются только текстовые сообщения.",
        parse_mode="Markdown",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()


def parse_message_link(link: str):
    """Возвращает (chat_id, message_id) из ссылки Telegram на сообщение."""
    link = link.strip().rstrip("/")

    # Ссылки вида https://t.me/c/1234567890/123
    match = re.fullmatch(r"https?://t\.me/c/(\d+)/(\d+)(?:\?[^\s]+)?", link)
    if match:
        internal_chat_id = int(match.group(1))
        message_id = int(match.group(2))
        chat_id = int(f"-100{internal_chat_id}")
        return chat_id, message_id

    # Публичные ссылки вида https://t.me/channel_name/123
    match = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]+)/([0-9]+)(?:\?[^\s]+)?", link)
    if match:
        username = match.group(1)
        message_id = int(match.group(2))
        return f"@{username}", message_id

    return None, None


@dp.message(AnonymousState.waiting_for_link)
async def anonymous_link_received(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return

    link = (message.text or "").strip()
    target_chat_id, message_id = parse_message_link(link)

    if target_chat_id is None or message_id is None:
        await message.answer(
            "❌ Не удалось распознать ссылку.\n\n"
            "Скопируй ссылку именно на сообщение из нужного чата и отправь её сюда."
        )
        return

    # Для приватного основного чата Telegram-ссылка содержит внутренний ID без -100.
    if isinstance(target_chat_id, int) and target_chat_id != ANON_CHAT_ID:
        await message.answer("❌ Эта ссылка ведёт не на сообщение из настроенного чата.")
        return

    if isinstance(target_chat_id, str):
        # Для публичной ссылки проверяем, что она действительно указывает на наш чат.
        try:
            chat = await bot.get_chat(target_chat_id)
        except Exception:
            await message.answer("❌ Не удалось проверить эту ссылку. Убедись, что она ведёт в наш чат.")
            return
        if chat.id != ANON_CHAT_ID:
            await message.answer("❌ Эта ссылка ведёт не на сообщение из настроенного чата.")
            return
        target_chat_id = chat.id

    # Сохраняем ссылку. Само существование сообщения окончательно проверим
    # в момент публикации: Telegram Bot API не предоставляет отдельного
    # getMessage для проверки произвольного message_id.
    await state.update_data(
        target_chat_id=target_chat_id,
        replied_to_message_id=message_id
    )
    await state.set_state(AnonymousState.waiting_for_text)

    await message.answer(
        "✅ Ссылка принята.\n\n"
        "Теперь напиши текст анонимного ответа одним сообщением.\n\n"
        "Пока поддерживаются только текстовые сообщения.",
        reply_markup=get_cancel_keyboard()
    )


@dp.message(AnonymousState.waiting_for_text)
async def anonymous_text_received(message: types.Message, state: FSMContext):
    if message.chat.type != "private":
        return

    if not message.text or message.text.startswith("/"):
        await message.answer("❌ Пока можно отправлять только обычный текст. Напиши сообщение без команды.")
        return

    data = await state.get_data()
    target_chat_id = data.get("target_chat_id", ANON_CHAT_ID)
    replied_to_message_id = data.get("replied_to_message_id")

    text = f"🕵️ Анонимное сообщение\n\n{message.text}"

    try:
        if replied_to_message_id:
            sent_message = await bot.send_message(
                chat_id=target_chat_id,
                text=text,
                reply_to_message_id=replied_to_message_id
            )
        else:
            sent_message = await bot.send_message(
                chat_id=target_chat_id,
                text=text
            )
    except Exception as e:
        logging.error(f"Не удалось отправить анонимное сообщение: {e}")
        await message.answer(
            "❌ Не удалось отправить сообщение в чат.\n"
            "Проверь, что бот находится в нужном чате и имеет права на отправку сообщений."
        )
        return

    username = message.from_user.username if message.from_user else None
    display_name = message.from_user.full_name if message.from_user else "Неизвестный пользователь"
    created_at = datetime.now(MINSK_TZ).strftime("%d.%m.%Y %H:%M:%S")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO anonymous_messages
               (chat_id, user_id, username, display_name, text, sent_message_id, replied_to_message_id, created_at, likes, dislikes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
            (
                target_chat_id,
                message.from_user.id,
                username,
                display_name,
                message.text,
                sent_message.message_id,
                replied_to_message_id,
                created_at
            )
        )
        cursor = await db.execute("SELECT last_insert_rowid()")
        anonymous_id = (await cursor.fetchone())[0]
        await db.commit()

    try:
        await bot.edit_message_reply_markup(chat_id=target_chat_id, message_id=sent_message.message_id, reply_markup=get_anonymous_reaction_keyboard(anonymous_id, 0, 0))
    except Exception as e:
        logging.error(f"Не удалось добавить кнопки реакций: {e}")

    await message.answer("✅ Сообщение отправлено анонимно.")
    await state.clear()



# --- НАПОМИНАНИЯ ---

def get_reminder_days_keyboard(selected=None):
    selected = set(selected or [])
    days = [
        (0, "Пн"), (1, "Вт"), (2, "Ср"),
        (3, "Чт"), (4, "Пт"), (5, "Сб"), (6, "Вс")
    ]
    builder = InlineKeyboardBuilder()
    for day, name in days:
        mark = "✅ " if day in selected else ""
        builder.button(text=f"{mark}{name}", callback_data=f"rem_day_{day}")
    builder.adjust(4)
    builder.row(InlineKeyboardButton(text="💾 Сохранить", callback_data="rem_days_save"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
    return builder.as_markup()

async def get_user_reminders(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, text, time_str, weekdays FROM reminders WHERE chat_id = ? AND user_id = ? ORDER BY time_str, id",
            (ANON_CHAT_ID, user_id)
        )
        return await cursor.fetchall()


def format_weekdays(days_str: str):
    if days_str.startswith("ONCE:"):
        try: return "однократно, " + datetime.strptime(days_str[5:], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError: return "однократно"
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    try:
        days = sorted(int(x) for x in days_str.split(',') if x != '')
    except ValueError:
        return ""
    if days == list(range(7)):
        return "каждый день"
    return ", ".join(names[d] for d in days if 0 <= d <= 6)

async def send_reminder_job():
    now=datetime.now(MINSK_TZ); current_day=now.weekday(); current_time=now.strftime("%H:%M"); current_date=now.strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT id,text,weekdays FROM reminders WHERE chat_id=? AND time_str=?",(ANON_CHAT_ID,current_time)); rows=await cur.fetchall()
    for reminder_id,text,weekdays in rows:
        if weekdays.startswith("ONCE:"): should_send=weekdays == f"ONCE:{current_date}"
        else:
            try: should_send=current_day in {int(x) for x in weekdays.split(',') if x}
            except ValueError: continue
        if not should_send: continue
        try:
            await bot.send_message(ANON_CHAT_ID,f"⏰ <b>Напоминание</b>\n\n{html.escape(text)}",parse_mode="HTML")
            if weekdays.startswith("ONCE:"):
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("DELETE FROM reminders WHERE id=?",(reminder_id,)); await db.commit()
        except Exception as e: logging.error(f"Не удалось отправить напоминание #{reminder_id}: {e}")

@dp.callback_query(F.data == "menu_reminders")
async def menu_reminders(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "⏰ <b>Напоминания</b>\n\n"
        "Ты можешь создать одноразовое напоминание (дата → время → текст) или повторяющееся по выбранным дням недели.",
        reply_markup=get_reminders_keyboard(), parse_mode="HTML"
    )
    await callback.answer()


def get_reminders_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить напоминание", callback_data="rem_add"))
    builder.row(InlineKeyboardButton(text="📋 Мои напоминания", callback_data="rem_list"))
    return builder.as_markup()

def get_reminder_type_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📅 Одноразовое", callback_data="rem_type_once"), InlineKeyboardButton(text="🔁 Повторяющееся", callback_data="rem_type_repeat"))
    return builder.as_markup()

@dp.callback_query(F.data == "rem_add")
async def reminder_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ReminderState.choosing_type)
    await callback.message.answer("📌 Какое напоминание создать?", reply_markup=get_reminder_type_keyboard())
    await callback.answer()

@dp.message(ReminderState.waiting_for_text)
async def reminder_text_received(message: types.Message, state: FSMContext):
    if message.chat.type != "private" or not message.text:
        await message.answer("❌ Нужно отправить обычный текст.")
        return
    data = await state.get_data()
    await state.update_data(reminder_text=message.text.strip())
    if data.get("reminder_type") == "repeat":
        await state.set_state(ReminderState.waiting_for_time)
        await message.answer("⏰ Во сколько отправлять? Напиши время в формате ЧЧ:ММ, например 18:30:")
    else:
        await state.set_state(ReminderState.choosing_type)
        await message.answer("📌 Какое это напоминание?", reply_markup=get_reminder_type_keyboard())

@dp.callback_query(ReminderState.choosing_type, F.data == "rem_type_once")
async def reminder_type_once(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(reminder_type="once")
    await state.set_state(ReminderState.waiting_for_date)
    await callback.message.edit_text(
        "📅 Введи дату напоминания в формате ДД.ММ.ГГГГ, например 25.09.2026:",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()

@dp.callback_query(ReminderState.choosing_type, F.data == "rem_type_repeat")
async def reminder_type_repeat(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(reminder_type="repeat")
    await state.set_state(ReminderState.waiting_for_text)
    await callback.message.edit_text(
        "📝 Напиши текст повторяющегося напоминания одним сообщением:",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()

@dp.message(ReminderState.waiting_for_date)
async def reminder_date_received(message: types.Message, state: FSMContext):
    try:
        d=datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
        if d < datetime.now(MINSK_TZ).date(): raise ValueError
    except ValueError:
        await message.answer("❌ Неверная дата. Используй ДД.ММ.ГГГГ и сегодняшнюю или будущую дату.")
        return
    await state.update_data(reminder_date=d.isoformat(), reminder_one_off=True)
    await state.set_state(ReminderState.waiting_for_time)
    await message.answer(
        "⏰ Во сколько отправить? Напиши время в формате ЧЧ:ММ, например 18:30:",
        reply_markup=get_cancel_keyboard()
    )

@dp.message(ReminderState.waiting_for_time)
async def reminder_time_received(message: types.Message, state: FSMContext):
    if message.chat.type != "private": return
    try:
        time_str=datetime.strptime((message.text or "").strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        await message.answer("❌ Неверное время. Используй формат ЧЧ:ММ, например 18:30.")
        return
    data=await state.get_data()
    if data.get("reminder_one_off"):
        await state.update_data(reminder_time=time_str)
        await state.set_state(ReminderState.waiting_for_once_text)
        await message.answer(
            "📝 Теперь напиши текст одноразового напоминания:",
            reply_markup=get_cancel_keyboard()
        )
        return
    await state.update_data(reminder_time=time_str, reminder_days=[])
    await state.set_state(ReminderState.choosing_days)
    await message.answer(
        "📅 Выбери дни недели. Можно выбрать несколько или все семь:",
        reply_markup=get_reminder_days_keyboard([])
    )

@dp.message(ReminderState.waiting_for_once_text)
async def reminder_once_text_received(message: types.Message, state: FSMContext):
    if message.chat.type != "private" or not message.text:
        await message.answer("❌ Нужно отправить обычный текст.")
        return
    data=await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO reminders (chat_id,user_id,text,time_str,weekdays,created_at) VALUES (?,?,?,?,?,?)", (ANON_CHAT_ID,message.from_user.id,message.text.strip(),data["reminder_time"],f"ONCE:{data['reminder_date']}",datetime.now(MINSK_TZ).strftime("%d.%m.%Y %H:%M:%S")))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Одноразовое напоминание создано!\n\n📅 {datetime.strptime(data['reminder_date'], '%Y-%m-%d').strftime('%d.%m.%Y')}\n⏰ {data['reminder_time']}\n📝 {html.escape(message.text.strip())}", parse_mode="HTML", reply_markup=get_reminders_keyboard())


@dp.callback_query(ReminderState.choosing_days, F.data.startswith("rem_day_"))
async def reminder_day_toggle(callback: types.CallbackQuery, state: FSMContext):
    day = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected = set(data.get("reminder_days", []))
    if day in selected:
        selected.remove(day)
    else:
        selected.add(day)
    selected = sorted(selected)
    await state.update_data(reminder_days=selected)
    await callback.message.edit_reply_markup(reply_markup=get_reminder_days_keyboard(selected))
    await callback.answer()

@dp.callback_query(ReminderState.choosing_days, F.data == "rem_days_save")
async def reminder_days_save(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = sorted(set(data.get("reminder_days", [])))
    if not selected:
        await callback.answer("Выбери хотя бы один день.", show_alert=True)
        return
    text = data.get("reminder_text")
    time_str = data.get("reminder_time")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO reminders (chat_id, user_id, text, time_str, weekdays, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ANON_CHAT_ID, callback.from_user.id, text, time_str, ",".join(map(str, selected)), datetime.now(MINSK_TZ).strftime("%d.%m.%Y %H:%M:%S"))
        )
        await db.commit()
    await state.clear()
    await callback.message.answer(
        f"✅ Напоминание создано!\n\n⏰ {time_str}\n📅 {format_weekdays(','.join(map(str, selected)))}\n📝 {html.escape(text)}",
        parse_mode="HTML", reply_markup=get_reminders_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "rem_list")
async def reminder_list(callback: types.CallbackQuery):
    rows = await get_user_reminders(callback.from_user.id)
    if not rows:
        await callback.message.answer("⏰ У тебя пока нет напоминаний.", reply_markup=get_reminders_keyboard())
        await callback.answer()
        return
    builder = InlineKeyboardBuilder()
    text = "⏰ <b>Мои напоминания</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
    for rid, reminder_text, time_str, weekdays in rows:
        text += f"<b>#{rid}</b> — {time_str}, {html.escape(format_weekdays(weekdays))}\n📝 {html.escape(reminder_text)}\n\n"
        builder.row(InlineKeyboardButton(text=f"🗑 Удалить #{rid}", callback_data=f"rem_delete_{rid}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rem_back"))
    await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("rem_delete_"))
async def reminder_delete(callback: types.CallbackQuery):
    reminder_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM reminders WHERE id = ? AND user_id = ? AND chat_id = ?", (reminder_id, callback.from_user.id, ANON_CHAT_ID))
        await db.commit()
    await callback.answer("Напоминание удалено")
    rows = await get_user_reminders(callback.from_user.id)
    if not rows:
        await callback.message.edit_text("⏰ У тебя больше нет напоминаний.", reply_markup=get_reminders_keyboard())
        return
    builder = InlineKeyboardBuilder()
    text = "⏰ <b>Мои напоминания</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
    for rid, reminder_text, time_str, weekdays in rows:
        text += f"<b>#{rid}</b> — {time_str}, {html.escape(format_weekdays(weekdays))}\n📝 {html.escape(reminder_text)}\n\n"
        builder.row(InlineKeyboardButton(text=f"🗑 Удалить #{rid}", callback_data=f"rem_delete_{rid}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="rem_back"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "rem_back")
async def reminder_back(callback: types.CallbackQuery):
    await callback.message.edit_text("⏰ <b>Напоминания</b>\n\nВыбери действие:", reply_markup=get_reminders_keyboard(), parse_mode="HTML")
    await callback.answer()

def get_weekday_reminders_keyboard(day: int):
    builder = InlineKeyboardBuilder()
    prev_day = (day - 1) % 7
    next_day = (day + 1) % 7
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    builder.row(
        InlineKeyboardButton(text="⬅️", callback_data=f"rm_day_{prev_day}"),
        InlineKeyboardButton(text=names[day], callback_data="rm_day_info"),
        InlineKeyboardButton(text="➡️", callback_data=f"rm_day_{next_day}")
    )
    return builder.as_markup()


async def get_all_reminders_for_day(day: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, user_id, text, time_str, weekdays FROM reminders WHERE chat_id = ? ORDER BY time_str, id",
            (ANON_CHAT_ID,)
        )
        rows = await cursor.fetchall()
    result=[]
    week_start = datetime.now(MINSK_TZ).date() - timedelta(days=datetime.now(MINSK_TZ).weekday())
    target_date = week_start + timedelta(days=day)
    target_date_str = target_date.strftime("%Y-%m-%d")
    for row in rows:
        s=row[4] or ""
        if s.startswith("ONCE:"):
            if s == f"ONCE:{target_date_str}":
                result.append(row)
        elif str(day) in {x for x in s.split(',') if x}:
            result.append(row)
    return result


async def build_public_reminders_text(day: int):
    names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    rows = await get_all_reminders_for_day(day)
    text = f"⏰ <b>Напоминания на {names[day]}</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
    if not rows:
        text += "На этот день напоминаний нет."
        return text
    for rid, user_id, reminder_text, time_str, weekdays in rows:
        text += f"⏰ <b>{time_str}</b>\n📝 {html.escape(reminder_text)}\n\n"
    return text


@dp.message(F.text.lower() == "рм")
async def show_reminders_command(message: types.Message):
    # В общем чате показываем напоминания всех пользователей, начиная с текущего дня.
    if message.chat.id != ANON_CHAT_ID:
        rows = await get_user_reminders(message.from_user.id)
        if not rows:
            await message.answer("⏰ У тебя пока нет напоминаний.")
            return
        text = "⏰ <b>Мои напоминания</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
        for rid, reminder_text, time_str, weekdays in rows:
            text += f"<b>#{rid}</b> — {time_str}, {html.escape(format_weekdays(weekdays))}\n📝 {html.escape(reminder_text)}\n\n"
        await message.answer(text, parse_mode="HTML")
        return

    day = datetime.now(MINSK_TZ).weekday()
    await message.answer(
        await build_public_reminders_text(day),
        reply_markup=get_weekday_reminders_keyboard(day),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("rm_day_"))
async def reminders_day_callback(callback: types.CallbackQuery):
    day = int(callback.data.split("_")[-1])
    await callback.message.edit_text(
        await build_public_reminders_text(day),
        reply_markup=get_weekday_reminders_keyboard(day),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "rm_day_info")
async def reminders_day_info(callback: types.CallbackQuery):
    await callback.answer("Листай стрелками по дням недели.")


@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    args = command.args
    if not args:
        await message.answer(
            "👋 <b>Привет!</b>\n\nВыбери нужный раздел:",
            reply_markup=get_main_menu_keyboard(message.from_user.id == ADMIN_ID),
            parse_mode="HTML"
        )
        return

    if args.startswith("fill_"):
        try:
            _, chat_id_str, date_str = args.split("_")
            chat_id = int(chat_id_str)
            await state.update_data(target_chat_id=chat_id, target_date=date_str)
            
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_int = dt.weekday()
            
            async with aiosqlite.connect(DB_NAME) as db:
                cursor = await db.execute(
                    "SELECT subject FROM schedule WHERE chat_id = ? AND day_int = ?",
                    (chat_id, day_int)
                )
                subjects = await cursor.fetchall()
                
            # Фильтруем пустые уроки (прочерки)
            valid_subjects = [s[0] for s in subjects if s[0] != "—"]
            
            if not valid_subjects:
                await message.answer("На этот день уроков нет или расписание не настроено.")
                return

            builder = InlineKeyboardBuilder()
            for sub in valid_subjects:
                builder.button(text=sub, callback_data=f"sethw_{sub}")
            builder.adjust(2)
            
            builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
            await message.answer(f"Выбери предмет для записи ДЗ на {date_str}:", reply_markup=builder.as_markup())
            await state.set_state(HWState.choosing_subject)
            
        except Exception as e:
            logging.error(e)
            await message.answer("Ошибка ссылки.")

@dp.callback_query(HWState.choosing_subject, F.data.startswith("sethw_"))
async def subject_chosen(callback: types.CallbackQuery, state: FSMContext):
    subject = callback.data.split("_")[1]
    await state.update_data(subject=subject)
    await callback.message.edit_text(
        f"Выбран предмет: *{subject}*\n\nНапиши задание одним сообщением:",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(HWState.writing_task)

@dp.message(HWState.writing_task)
async def task_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get('target_chat_id')
    date_str = data.get('target_date')
    subject = data.get('subject')
    task_text = message.text

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            INSERT OR REPLACE INTO homework (chat_id, date_str, subject, task)
            VALUES (?, ?, ?, ?)
        ''', (chat_id, date_str, subject, task_text))
        await db.commit()

    await message.answer(f"✅ ДЗ по {subject} сохранено! Вернись в чат и нажми 'Обновить'.")
    await state.clear()

@dp.message(F.text.lower() == "дз")
async def show_hw_command(message: types.Message):
    now = datetime.now(MINSK_TZ)
    text = await generate_schedule_text(message.chat.id, now.date())
    kb = get_keyboard(message.chat.id, now.date())
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("nav_") | F.data.startswith("refresh_"))
async def nav_callback(callback: types.CallbackQuery):
    action, date_str = callback.data.split("_")
    current_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    
    new_text = await generate_schedule_text(callback.message.chat.id, current_date)
    new_kb = get_keyboard(callback.message.chat.id, current_date)
    
    try:
        await callback.message.edit_text(new_text, reply_markup=new_kb, parse_mode="Markdown")
    except Exception:
        await callback.answer("Данные актуальны")
    else:
        await callback.answer()
        
# --- ЛОГИКА ДНЕЙ РОЖДЕНИЯ ---

async def get_birthdays_text(chat_id: int, page: int = 0):
    now = datetime.now(MINSK_TZ)
    today = now.date()
    header = f"🎂 *Дни рождения* ({today.strftime('%d.%m.%Y')})\n〰️〰️〰️〰️〰️〰️〰️\n"

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT full_name, birth_day, birth_month FROM birthdays WHERE chat_id = ?", (chat_id,))
        rows = await cursor.fetchall()

    if not rows:
        return header + "Список дней рождения пуст. Админ может добавить их командой /add_dr", 0

    upcoming = []
    for name, b_day, b_month in rows:
        try:
            next_bday = datetime(today.year, b_month, b_day).date()
        except ValueError: # Для високосных годов
            next_bday = datetime(today.year, b_month, b_day - 1).date()

        if next_bday < today:
            try:
                next_bday = datetime(today.year + 1, b_month, b_day).date()
            except ValueError:
                next_bday = datetime(today.year + 1, b_month, b_day - 1).date()

        days_left = (next_bday - today).days
        upcoming.append({
            'name': name,
            'date': f"{b_day:02d}.{b_month:02d}",
            'days_left': days_left
        })

    # Сортируем по количеству оставшихся дней
    upcoming.sort(key=lambda x: x['days_left'])

    total_pages = (len(upcoming) - 1) // 10 + 1
    start_idx = page * 10
    page_items = upcoming[start_idx : start_idx + 10]

    text = header
    for item in page_items:
        if item['days_left'] == 0:
            days_str = "*(СЕГОДНЯ!)* 🎉"
        else:
            # Склонение слова "день"
            d = item['days_left']
            if d % 10 == 1 and d % 100 != 11:
                word = "день"
            elif 2 <= d % 10 <= 4 and (d % 100 < 10 or d % 100 >= 20):
                word = "дня"
            else:
                word = "дней"
            days_str = f"(через {d} {word})"
            
        text += f"🎈 *{item['name']}* — {item['date']} {days_str}\n"

    text += f"\n_Страница {page + 1} из {total_pages}_"
    return text, total_pages

def get_dr_keyboard(page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    prev_p = page - 1 if page > 0 else total_pages - 1
    next_p = page + 1 if page < total_pages - 1 else 0

    builder.row(
        InlineKeyboardButton(text="⬅️", callback_data=f"navdr_{prev_p}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refreshdr_{page}"),
        InlineKeyboardButton(text="➡️", callback_data=f"navdr_{next_p}")
    )
    return builder.as_markup()

@dp.message(F.text.lower() == "др")
async def show_dr_command(message: types.Message):
    text, total_pages = await get_birthdays_text(message.chat.id, 0)
    kb = get_dr_keyboard(0, total_pages) if total_pages > 0 else None
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("navdr_") | F.data.startswith("refreshdr_"))
async def nav_dr_callback(callback: types.CallbackQuery):
    action, page_str = callback.data.split("_")
    page = int(page_str)
    text, total_pages = await get_birthdays_text(callback.message.chat.id, page)
    kb = get_dr_keyboard(page, total_pages) if total_pages > 0 else None

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.answer("Данные актуальны")
    else:
        await callback.answer()

# --- ДОБАВЛЕНИЕ ДНЕЙ РОЖДЕНИЯ (АДМИН) ---

@dp.message(Command("add_dr"))
async def cmd_add_dr(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только администратор может изменять дни рождения.")
        return
    await message.answer("📝 Введи Имя и Фамилию ученика:", reply_markup=get_cancel_keyboard())
    await state.set_state(SetupDrState.waiting_for_name)

@dp.message(SetupDrState.waiting_for_name)
async def dr_name_received(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer(
        "📅 Теперь введи дату его рождения в формате ДД.ММ (например, 15.04 или 05.11):",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(SetupDrState.waiting_for_date)

@dp.message(SetupDrState.waiting_for_date)
async def dr_date_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data.get('name')
    date_text = message.text

    try:
        day, month = map(int, date_text.split('.'))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверный формат! Напиши дату строго в формате ДД.ММ (например, 15.04):")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO birthdays (chat_id, full_name, birth_day, birth_month) VALUES (?, ?, ?, ?)",
                         (ANON_CHAT_ID, name, day, month))
        await db.commit()

    await message.answer(f"✅ День рождения для *{name}* ({date_text}) успешно сохранен!", parse_mode="Markdown")
    await state.clear()

def get_birthdays_manage_keyboard(chat_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data="dr_add"))
    return builder.as_markup()

async def get_birthdays_manage_text(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, full_name, birth_day, birth_month FROM birthdays WHERE chat_id = ? ORDER BY birth_month, birth_day, full_name",
            (chat_id,)
        )
        rows = await cursor.fetchall()

    if not rows:
        return "🎂 <b>Управление днями рождения</b>\n\nСписок пуст.", get_birthdays_manage_keyboard(chat_id)

    builder = InlineKeyboardBuilder()
    text = "🎂 <b>Управление днями рождения</b>\n〰️〰️〰️〰️〰️〰️〰️\n"
    for birthday_id, name, day, month in rows:
        text += f"🎈 <b>{html.escape(name)}</b> — {day:02d}.{month:02d}\n"
        builder.row(
            InlineKeyboardButton(text=f"✏️ {name}", callback_data=f"dr_edit_{birthday_id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"dr_delete_{birthday_id}")
        )
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data="dr_add"))
    return text, builder.as_markup()

@dp.callback_query(F.data == "dr_manage")
async def dr_manage(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    text, kb = await get_birthdays_manage_text(ANON_CHAT_ID)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "dr_add")
async def dr_add_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    await callback.message.answer("📝 Введи Имя и Фамилию ученика:", reply_markup=get_cancel_keyboard())
    await state.set_state(SetupDrState.waiting_for_name)
    await callback.answer()

@dp.callback_query(F.data.startswith("dr_edit_"))
async def dr_edit_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    birthday_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT full_name, birth_day, birth_month FROM birthdays WHERE id = ? AND chat_id = ?", (birthday_id, ANON_CHAT_ID))
        row = await cursor.fetchone()
    if not row:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    await state.update_data(edit_birthday_id=birthday_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить имя", callback_data="dr_edit_name"),
        InlineKeyboardButton(text="📅 Изменить дату", callback_data="dr_edit_date")
    )
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="dr_manage"))
    await callback.message.answer(
        f"🎂 <b>{html.escape(row[0])}</b> — {row[1]:02d}.{row[2]:02d}\n\nЧто изменить?",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "dr_edit_name")
async def dr_edit_name(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    await callback.message.answer("✏️ Введи новое имя и фамилию:", reply_markup=get_cancel_keyboard())
    await state.set_state(SetupDrState.editing_name)
    await callback.answer()

@dp.message(SetupDrState.editing_name)
async def dr_edit_name_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    birthday_id = data.get("edit_birthday_id")
    if not birthday_id or not message.text:
        await message.answer("❌ Не удалось изменить запись.")
        await state.clear()
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE birthdays SET full_name = ? WHERE id = ? AND chat_id = ?", (message.text.strip(), birthday_id, ANON_CHAT_ID))
        await db.commit()
    await state.clear()
    await message.answer("✅ Имя изменено.")
    text, kb = await get_birthdays_manage_text(ANON_CHAT_ID)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "dr_edit_date")
async def dr_edit_date(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    await callback.message.answer("📅 Введи новую дату в формате ДД.ММ:", reply_markup=get_cancel_keyboard())
    await state.set_state(SetupDrState.editing_date)
    await callback.answer()

@dp.message(SetupDrState.editing_date)
async def dr_edit_date_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    birthday_id = data.get("edit_birthday_id")
    try:
        day, month = map(int, message.text.strip().split('.'))
        datetime(2000, month, day)
    except (ValueError, TypeError):
        await message.answer("❌ Неверная дата. Используй формат ДД.ММ, например 15.04.")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE birthdays SET birth_day = ?, birth_month = ? WHERE id = ? AND chat_id = ?", (day, month, birthday_id, ANON_CHAT_ID))
        await db.commit()
    await state.clear()
    await message.answer("✅ Дата изменена.")
    text, kb = await get_birthdays_manage_text(ANON_CHAT_ID)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("dr_delete_"))
async def dr_delete_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    birthday_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT full_name FROM birthdays WHERE id = ? AND chat_id = ?", (birthday_id, ANON_CHAT_ID))
        row = await cursor.fetchone()
        if not row:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        await db.execute("DELETE FROM birthdays WHERE id = ? AND chat_id = ?", (birthday_id, ANON_CHAT_ID))
        await db.commit()
    await callback.answer("Удалено")
    text, kb = await get_birthdays_manage_text(ANON_CHAT_ID)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

# --- АВТОМАТИЧЕСКОЕ ПОЗДРАВЛЕНИЕ ПРИ НАСТУПЛЕНИИ ДНЯ РОЖДЕНИЯ ---

async def check_birthdays_job():
    now = datetime.now(MINSK_TZ)
    day = now.day
    month = now.month

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT chat_id, full_name FROM birthdays WHERE birth_day = ? AND birth_month = ?", (day, month))
        rows = await cursor.fetchall()

    chat_birthdays = {}
    for chat_id, name in rows:
        if chat_id not in chat_birthdays:
            chat_birthdays[chat_id] = []
        chat_birthdays[chat_id].append(name)

    for chat_id, names in chat_birthdays.items():
        names_str = ", ".join(names)
        text = f"🎉 *С ДНЕМ РОЖДЕНИЯ!* 🎉\n\nСегодня свой день рождения празднует: *{names_str}*! 🎂🎁\nЖелаем успехов в учебе, классных оценок и отличного настроения!"
        try:
            await bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось отправить поздравление: {e}")

def get_main_menu_keyboard(is_admin: bool = False):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📅 Расписание", callback_data="menu_schedule"),
        InlineKeyboardButton(text="📝 Домашнее задание", callback_data="menu_hw")
    )
    builder.row(
        InlineKeyboardButton(text="🎂 Дни рождения", callback_data="menu_birthdays"),
        InlineKeyboardButton(text="🕵️ Анонимка", callback_data="menu_anon")
    )
    builder.row(InlineKeyboardButton(text="⏰ Напоминания", callback_data="menu_reminders"))
    builder.row(InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="menu_bells"))
    builder.row(InlineKeyboardButton(text="📖 Документация", callback_data="menu_docs"))
    if is_admin:
        builder.row(InlineKeyboardButton(text="🛠 Админ-панель", callback_data="menu_admin"))
    return builder.as_markup()

@dp.message(Command("schedule"))
async def cmd_schedule(message: types.Message, state: FSMContext):
    await state.clear()
    now = datetime.now(MINSK_TZ)
    text = await generate_schedule_text(ANON_CHAT_ID, now.date())
    await message.answer(text, reply_markup=get_keyboard(ANON_CHAT_ID, now.date()), parse_mode="Markdown")


@dp.message(Command("hw"))
async def cmd_hw(message: types.Message, state: FSMContext):
    await state.clear()
    now = datetime.now(MINSK_TZ)
    text = await generate_schedule_text(ANON_CHAT_ID, now.date())
    await message.answer(text, reply_markup=get_keyboard(ANON_CHAT_ID, now.date()), parse_mode="Markdown")


@dp.message(Command("birthdays"))
async def cmd_birthdays(message: types.Message, state: FSMContext):
    await state.clear()
    text, total_pages = await get_birthdays_text(ANON_CHAT_ID, 0)
    kb = get_dr_keyboard(0, total_pages) if total_pages > 0 else None
    if message.from_user.id == ADMIN_ID:
        builder = InlineKeyboardBuilder()
        if kb:
            builder.attach(InlineKeyboardBuilder.from_markup(kb))
        builder.row(InlineKeyboardButton(text="⚙️ Управление днями рождения", callback_data="dr_manage"))
        kb = builder.as_markup()
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⏰ <b>Напоминания</b>\n\n"
        "Ты можешь создать одноразовое напоминание (дата → время → текст) или повторяющееся по выбранным дням недели.",
        reply_markup=get_reminders_keyboard(), parse_mode="HTML"
    )


@dp.message(Command("bells"))
async def cmd_bells(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(get_bell_schedule_text(), parse_mode="HTML")


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏠 <b>Главное меню</b>\n\nВыбери нужный раздел:",
        reply_markup=get_main_menu_keyboard(message.from_user.id == ADMIN_ID),
        parse_mode="HTML"
    )


def get_bell_schedule_text():
    """Расписание звонков: 8 уроков, по 45 минут, перемены по 15 минут.
    После 4-го урока перемена является обедом.
    """
    start = datetime.strptime("08:30", "%H:%M")
    lines = ["🔔 <b>Расписание звонков</b>", "〰️〰️〰️〰️〰️〰️〰️", ""]
    current = start

    for lesson in range(1, 9):
        end = current + timedelta(minutes=45)
        lines.append(f"<b>{lesson}. {current.strftime('%H:%M')}–{end.strftime('%H:%M')}</b>")

        if lesson < 8:
            if lesson == 4:
                lines.append("🍽️ <b>Обед — 15 минут</b>")
            else:
                lines.append("〰️ Перемена — 15 минут")
            current = end + timedelta(minutes=15)

    return "\n".join(lines)


@dp.callback_query(F.data == "menu_bells")
async def menu_bells(callback: types.CallbackQuery):
    await callback.message.answer(get_bell_schedule_text(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu_schedule")
async def menu_schedule(callback: types.CallbackQuery):
    now = datetime.now(MINSK_TZ)
    text = await generate_schedule_text(ANON_CHAT_ID, now.date())
    await callback.message.answer(text, reply_markup=get_keyboard(ANON_CHAT_ID, now.date()), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_hw")
async def menu_hw(callback: types.CallbackQuery):
    now = datetime.now(MINSK_TZ)
    text = await generate_schedule_text(ANON_CHAT_ID, now.date())
    await callback.message.answer(text, reply_markup=get_keyboard(ANON_CHAT_ID, now.date()), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_birthdays")
async def menu_birthdays(callback: types.CallbackQuery):
    text, total_pages = await get_birthdays_text(ANON_CHAT_ID, 0)
    kb = get_dr_keyboard(0, total_pages) if total_pages > 0 else None
    if callback.from_user.id == ADMIN_ID:
        admin_kb = get_birthdays_manage_keyboard(callback.message.chat.id)
        if kb:
            # add management button under navigation
            builder = InlineKeyboardBuilder()
            builder.attach(InlineKeyboardBuilder.from_markup(kb))
            builder.row(InlineKeyboardButton(text="⚙️ Управление днями рождения", callback_data="dr_manage"))
            kb = builder.as_markup()
        else:
            kb = admin_kb
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "menu_anon")
async def menu_anon(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("🕵️ <b>Анонимное сообщение</b>\n\nКак отправить сообщение?", reply_markup=get_anonymous_choice_keyboard(), parse_mode="HTML")
    await state.set_state(AnonymousState.waiting_for_choice)
    await callback.answer()

@dp.callback_query(F.data == "menu_docs")
async def menu_docs(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "📖 <b>Документация бота</b>\n\n"
        "📅 <b>Расписание</b>\n"
        "/setup — настроить расписание.\n"
        "Напиши «дз», чтобы посмотреть расписание и домашнее задание.\n"
        "🔔 «Расписание звонков» — время 8 уроков, перемен и обеда.\n\n"
        "📝 <b>Домашнее задание</b>\n"
        "В расписании нажми «✍️ Заполнить ДЗ», выбери предмет и отправь задание.\n\n"
        "🎂 <b>Дни рождения</b>\n"
        "«др» — посмотреть список дней рождения.\n"
        "/add_dr — добавить день рождения (только админ).\nАдминистратор также может поштучно изменить имя, изменить дату или удалить запись через меню «🎂 Дни рождения».\n\n"
        "🕵️ <b>Анонимка</b>\n"
        "Нажми «🕵️ Анонимка» в меню бота. Затем выбери обычное сообщение или ответ на сообщение из чата.\n\n"
        "Для анонимного ответа скопируй ссылку на нужное сообщение из чата и отправь её боту.\n\n"
        "⏰ <b>Напоминания</b>\n"
        "Нажми «⏰ Напоминания» в главном меню. Можно создать одноразовое напоминание на дату или повторяющееся по дням недели.\n"
        "Напиши только «рм» в общем чате, чтобы посмотреть напоминания всех учеников. Можно листать дни недели стрелками. В личке «рм» показывает только твои напоминания.\n\n"
        "🛠 <b>Админ-панель</b>\n"
        "/admin — доступна только администратору. В админ-панели также можно удалить ДЗ за предыдущие недели.\n\n"
        "Пока анонимка поддерживает только текстовые сообщения. Под опубликованными анонимками доступны 👍/👎; один пользователь может иметь только один голос, повторное нажатие снимает голос, а другая реакция переключает его.",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_admin")
async def menu_admin(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return
    await callback.message.answer("🛠 <b>Админ-панель</b>", reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.message(Command("docs"))
async def cmd_docs(message: types.Message, state: FSMContext):
    # Команда документации всегда должна срабатывать, даже если пользователь
    # случайно остался в одном из состояний FSM.
    await state.clear()
    await message.answer(
        "📖 <b>За получением документации @eeclover</b>\n\n"
       
    )


async def setup_bot_menu():
    commands = [
        BotCommand(command="menu", description="🏠 Главное меню"),
        BotCommand(command="schedule", description="📅 Расписание"),
        BotCommand(command="hw", description="📝 Домашнее задание"),
        BotCommand(command="birthdays", description="🎂 Дни рождения"),
        BotCommand(command="anon", description="🕵️ Анонимка"),
        BotCommand(command="reminders", description="⏰ Напоминания"),
        BotCommand(command="bells", description="🔔 Расписание звонков"),
        BotCommand(command="docs", description="📖 Документация"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

    # /admin показываем только администратору, а не всем пользователям.
    await bot.set_my_commands(
        commands + [BotCommand(command="admin", description="🛠 Админ-панель")],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )


# --- НОВЫЕ ФУНКЦИИ АДМИНА: ОТВЕТ ОТ БОТА И РЕАКЦИИ ---

# Сохраняем исходную функцию клавиатуры и только добавляем новые кнопки.
_original_get_admin_keyboard = get_admin_keyboard

def get_admin_keyboard_with_bot_tools():
    builder = InlineKeyboardBuilder.from_markup(_original_get_admin_keyboard())
    builder.row(InlineKeyboardButton(text="↩️ Ответ от бота", callback_data="admin_bot_reply"))
    builder.row(InlineKeyboardButton(text="😀 Реакция от бота", callback_data="admin_bot_reaction"))
    return builder.as_markup()

get_admin_keyboard = get_admin_keyboard_with_bot_tools


def get_admin_reaction_keyboard():
    builder = InlineKeyboardBuilder()
    reactions = [
        ("👍", "thumbs_up"),
        ("❤️", "red_heart"),
        ("🔥", "fire"),
        ("🎉", "party"),
        ("😢", "sad"),
        ("👍", "thumbs_up"),
        ("👎", "thumbs_down"),
        ("❤️", "red_heart"),
        ("🔥", "fire"),
        ("🥰", "smiling_face_with_hearts"),
        ("👏", "clapping_hands"),
        ("😁", "beaming_face_with_smiling_eyes"),
        ("🤔", "thinking_face"),
        ("🤯", "exploding_head"),
        ("😱", "screaming_in_fear"),
        ("🤬", "face_with_symbols_on_mouth"),
        ("😢", "sad"),
        ("🎉", "party"),
        ("🤩", "star_struck"),
        ("🤮", "vomiting"),
        ("💩", "poop"),
        ("🙏", "folded_hands"),
        ("👌", "ok_hand"),
        ("🕊", "dove"),
        ("🤡", "clown_face"),
        ("🥱", "yawning_face"),
        ("🥴", "woozy_face"),
        ("😍", "heart_eyes"),
        ("🐳", "whale"),
        ("❤️‍🔥", "heart_on_fire"),
        ("🌚", "new_moon_face"),
        ("🌭", "hot_dog"),
        ("💯", "hundred_points"),
        ("🤣", "rofl"),
        ("⚡", "high_voltage"),
        ("🍌", "banana"),
        ("🏆", "trophy"),
        ("💔", "broken_heart"),
        ("🤨", "face_with_raised_eyebrow"),
        ("😐", "neutral_face"),
        ("🍓", "strawberry"),
        ("🍾", "champagne"),
        ("💋", "kiss_mark"),
        ("🖕", "middle_finger"),
        ("😈", "smiling_face_with_horns"),
        ("😴", "sleeping_face"),
        ("😭", "loudly_crying_face"),
        ("🤓", "nerd_face"),
        ("👻", "ghost"),
        ("👨‍💻", "technologist"),
        ("👀", "eyes"),
        ("🎃", "jack_o_lantern"),
        ("🙈", "see_no_evil_monkey"),
        ("😇", "smiling_face_with_halo"),
        ("😨", "fearful_face"),
        ("🤝", "handshake"),
        ("✍️", "writing_hand"),
        ("🤗", "hugging_face"),
        ("🫡", "saluting_face"),
        ("🎅", "santa_claus"),
        ("🎄", "christmas_tree"),
        ("☃️", "snowman"),
        ("💅", "nail_polish"),
        ("🤪", "zany_face"),
        ("🗿", "moai"),
        ("🆒", "cool_button"),
        ("💘", "heart_with_arrow"),
        ("🙉", "hear_no_evil_monkey"),
        ("🦄", "unicorn"),
        ("😘", "blowing_a_kiss"),
        ("💊", "pill"),
        ("🙊", "speak_no_evil_monkey"),
        ("😎", "smiling_face_with_sunglasses"),
        ("👾", "alien_monster"),
        ("🤷‍♂️", "man_shrugging"),
        ("🤷", "person_shrugging"),
        ("🤷‍♀️", "woman_shrugging"),
        ("😡", "enraged_face")
    ]
    for emoji, key in reactions:
        builder.button(text=emoji, callback_data=f"admin_reaction_{key}")
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
    return builder.as_markup()


ADMIN_REACTIONS = {
    "thumbs_up": "👍",
    "red_heart": "❤️",
    "fire": "🔥",
    "party": "🎉",
    "sad": "😢",
    "thumbs_up": "👍",
    "thumbs_down": "👎",
    "red_heart": "❤️",
    "fire": "🔥",
    "smiling_face_with_hearts": "🥰",
    "clapping_hands": "👏",
    "beaming_face_with_smiling_eyes": "😁",
    "thinking_face": "🤔",
    "exploding_head": "🤯",
    "screaming_in_fear": "😱",
    "face_with_symbols_on_mouth": "🤬",
    "sad": "😢",
    "party": "🎉",
    "star_struck": "🤩",
    "vomiting": "🤮",
    "poop": "💩",
    "folded_hands": "🙏",
    "ok_hand": "👌",
    "dove": "🕊",
    "clown_face": "🤡",
    "yawning_face": "🥱",
    "woozy_face": "🥴",
    "heart_eyes": "😍",
    "whale": "🐳",
    "heart_on_fire": "❤️‍🔥",
    "new_moon_face": "🌚",
    "hot_dog": "🌭",
    "hundred_points": "💯",
    "rofl": "🤣",
    "high_voltage": "⚡",
    "banana": "🍌",
    "trophy": "🏆",
    "broken_heart": "💔",
    "face_with_raised_eyebrow": "🤨",
    "neutral_face": "😐",
    "strawberry": "🍓",
    "champagne": "🍾",
    "kiss_mark": "💋",
    "middle_finger": "🖕",
    "smiling_face_with_horns": "😈",
    "sleeping_face": "😴",
    "loudly_crying_face": "😭",
    "nerd_face": "🤓",
    "ghost": "👻",
    "technologist": "👨‍💻",
    "eyes": "👀",
    "jack_o_lantern": "🎃",
    "see_no_evil_monkey": "🙈",
    "smiling_face_with_halo": "😇",
    "fearful_face": "😨",
    "handshake": "🤝",
    "writing_hand": "✍️",
    "hugging_face": "🤗",
    "saluting_face": "🫡",
    "santa_claus": "🎅",
    "christmas_tree": "🎄",
    "snowman": "☃️",
    "nail_polish": "💅",
    "zany_face": "🤪",
    "moai": "🗿",
    "cool_button": "🆒",
    "heart_with_arrow": "💘",
    "hear_no_evil_monkey": "🙉",
    "unicorn": "🦄",
    "blowing_a_kiss": "😘",
    "pill": "💊",
    "speak_no_evil_monkey": "🙊",
    "smiling_face_with_sunglasses": "😎",
    "alien_monster": "👾",
    "man_shrugging": "🤷‍♂️",
    "person_shrugging": "🤷",
    "woman_shrugging": "🤷‍♀️",
    "enraged_face": "😡"
}


async def validate_admin_message_link(link: str):
    """Проверяет ссылку сообщения и возвращает (chat_id, message_id)."""
    target_chat_id, message_id = parse_message_link(link)

    if target_chat_id is None or message_id is None:
        return None, None

    if isinstance(target_chat_id, int):
        if target_chat_id != ANON_CHAT_ID:
            return None, None
        return target_chat_id, message_id

    try:
        chat = await bot.get_chat(target_chat_id)
    except Exception:
        return None, None

    if chat.id != ANON_CHAT_ID:
        return None, None

    return chat.id, message_id


@dp.callback_query(F.data == "admin_bot_reply")
async def admin_bot_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminBotReplyState.waiting_for_link)
    await callback.message.answer(
        "↩️ <b>Ответ от бота</b>\n\n"
        "Скопируй ссылку на сообщение из основного чата и отправь её сюда.\n\n"
        "Например:\n"
        "<code>https://t.me/c/2720925459/12345</code>\n\n"
        "После этого я попрошу текст ответа.",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(AdminBotReplyState.waiting_for_link)
async def admin_bot_reply_link_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    target_chat_id, message_id = await validate_admin_message_link((message.text or "").strip())
    if target_chat_id is None or message_id is None:
        await message.answer(
            "❌ Не удалось распознать ссылку или она ведёт не на сообщение из основного чата.\n\n"
            "Отправь корректную ссылку на сообщение из нашего чата."
        )
        return

    await state.update_data(
        admin_reply_chat_id=target_chat_id,
        admin_reply_message_id=message_id,
    )
    await state.set_state(AdminBotReplyState.waiting_for_text)
    await message.answer(
        "✅ Ссылка принята.\n\n"
        "Теперь напиши текст ответа. Он будет опубликован от имени бота без подписи.",
        reply_markup=get_cancel_keyboard(),
    )


@dp.message(AdminBotReplyState.waiting_for_text)
async def admin_bot_reply_text_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    text_to_send = (message.text or "").strip()
    if not text_to_send:
        await message.answer("❌ Нужно отправить обычный текст.")
        return

    data = await state.get_data()
    target_chat_id = data.get("admin_reply_chat_id")
    target_message_id = data.get("admin_reply_message_id")

    if target_chat_id is None or target_message_id is None:
        await state.clear()
        await message.answer("❌ Не удалось определить сообщение для ответа.")
        return

    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=text_to_send,
            reply_to_message_id=target_message_id,
        )
    except Exception as e:
        logging.error(f"Не удалось отправить ответ от бота: {e}")
        await message.answer(
            "❌ Не удалось отправить ответ.\n"
            "Проверь ссылку и права бота на отправку сообщений в чате."
        )
        return

    await state.clear()
    await message.answer("✅ Ответ отправлен от имени бота.")


@dp.message(Command("cancel"), AdminBotReplyState.waiting_for_link)
async def admin_bot_reply_cancel_link(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Ответ от бота отменён.")


@dp.message(Command("cancel"), AdminBotReplyState.waiting_for_text)
async def admin_bot_reply_cancel_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Ответ от бота отменён.")


@dp.callback_query(F.data == "admin_bot_reaction")
async def admin_bot_reaction_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await state.set_state(AdminBotReactionState.waiting_for_link)
    await callback.message.answer(
        "😀 <b>Реакция от бота</b>\n\n"
        "Скопируй ссылку на сообщение из основного чата и отправь её сюда.\n\n"
        "Например:\n"
        "<code>https://t.me/c/2720925459/12345</code>",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(AdminBotReactionState.waiting_for_link)
async def admin_bot_reaction_link_received(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    target_chat_id, message_id = await validate_admin_message_link((message.text or "").strip())
    if target_chat_id is None or message_id is None:
        await message.answer(
            "❌ Не удалось распознать ссылку или она ведёт не на сообщение из основного чата.\n\n"
            "Отправь корректную ссылку на сообщение из нашего чата."
        )
        return

    await state.update_data(
        reaction_chat_id=target_chat_id,
        reaction_message_id=message_id,
    )
    await state.set_state(AdminBotReactionState.choosing_reaction)
    await message.answer(
        "✅ Ссылка принята.\n\n"
        "Выбери реакцию, которую поставить от имени бота:",
        reply_markup=get_admin_reaction_keyboard(),
    )


@dp.callback_query(AdminBotReactionState.choosing_reaction, F.data.startswith("admin_reaction_"))
async def admin_bot_reaction_chosen(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    key = callback.data.removeprefix("admin_reaction_")
    emoji = ADMIN_REACTIONS.get(key)
    if emoji is None:
        await callback.answer("❌ Неизвестная реакция.", show_alert=True)
        return

    data = await state.get_data()
    target_chat_id = data.get("reaction_chat_id")
    target_message_id = data.get("reaction_message_id")

    if target_chat_id is None or target_message_id is None:
        await state.clear()
        await callback.answer("❌ Не удалось определить сообщение.", show_alert=True)
        return

    try:
        await bot.set_message_reaction(
            chat_id=target_chat_id,
            message_id=target_message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logging.error(f"Не удалось поставить реакцию от бота: {e}")
        await callback.answer(
            "❌ Не удалось поставить реакцию. Проверь права бота и доступность реакции в чате.",
            show_alert=True,
        )
        return

    await state.clear()
    await callback.message.edit_text(f"✅ Реакция {emoji} поставлена от имени бота.")
    await callback.answer()


@dp.message(Command("cancel"), AdminBotReactionState.waiting_for_link)
async def admin_bot_reaction_cancel_link(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Установка реакции отменена.")


@dp.message(Command("cancel"), AdminBotReactionState.choosing_reaction)
async def admin_bot_reaction_cancel_choice(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Установка реакции отменена.")


async def main():
    await init_db()
    # --- ЗАПУСКАЕМ ПЛАНИРОВЩИК ---
    scheduler = AsyncIOScheduler(timezone=MINSK_TZ)
    # Ставим проверку каждый день в 08:00 утра
    scheduler.add_job(check_birthdays_job, 'cron', hour=6, minute=0)
    scheduler.add_job(send_reminder_job, 'cron', minute='*')
    scheduler.start()
    # -----------------------------
    global bot_info
    bot_info = await bot.get_me()
    await setup_bot_menu()
    print("Бот запущен...")
    await dp.start_polling(bot)
if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
      
