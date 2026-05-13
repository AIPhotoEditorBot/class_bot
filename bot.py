import asyncio
import logging
import sys
from datetime import datetime, timedelta
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "8293585417:AAHsbZv7_ChcIPdMjwRegILTixwZOjdIt1Y" 
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
        await db.commit()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
# CHAT_ID — id вашего чата, OLD_MESSAGE_ID — то, что мы узнали в пункте 1
@dp.message(Command("go"))
async def send_old_reply(message: Message):
    chat_id = -1002720925459  # твой чат
    text = "фрик"

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_to_message_id=10002
    )

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
        parse_mode="Markdown"
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

@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject, state: FSMContext):
    args = command.args
    if not args:
        await message.answer("Привет! Добавь меня в чат и введи /setup для настройки расписания.")
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
            
            await message.answer(f"Выбери предмет для записи ДЗ на {date_str}:", reply_markup=builder.as_markup())
            await state.set_state(HWState.choosing_subject)
            
        except Exception as e:
            logging.error(e)
            await message.answer("Ошибка ссылки.")

@dp.callback_query(HWState.choosing_subject, F.data.startswith("sethw_"))
async def subject_chosen(callback: types.CallbackQuery, state: FSMContext):
    subject = callback.data.split("_")[1]
    await state.update_data(subject=subject)
    await callback.message.edit_text(f"Выбран предмет: *{subject}*\n\nНапиши задание одним сообщением:")
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

@dp.message(F.text.lower().contains("дз"))
async def show_hw_command(message: types.Message):
    now = datetime.now()
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
    now = datetime.now()
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

@dp.message(F.text.lower().contains("др"))
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
    await message.answer("📝 Введи Имя и Фамилию ученика:")
    await state.set_state(SetupDrState.waiting_for_name)

@dp.message(SetupDrState.waiting_for_name)
async def dr_name_received(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("📅 Теперь введи дату его рождения в формате ДД.ММ (например, 15.04 или 05.11):")
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
                         (message.chat.id, name, day, month))
        await db.commit()

    await message.answer(f"✅ День рождения для *{name}* ({date_text}) успешно сохранен!", parse_mode="Markdown")
    await state.clear()

# --- АВТОМАТИЧЕСКОЕ ПОЗДРАВЛЕНИЕ ПРИ НАСТУПЛЕНИИ ДНЯ РОЖДЕНИЯ ---

async def check_birthdays_job():
    now = datetime.now()
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

async def main():
    await init_db()
    # --- ЗАПУСКАЕМ ПЛАНИРОВЩИК ---
    scheduler = AsyncIOScheduler()
    # Ставим проверку каждый день в 08:00 утра
    scheduler.add_job(check_birthdays_job, 'cron', hour=6, minute=0)
    scheduler.start()
    # -----------------------------
    global bot_info
    bot_info = await bot.get_me()
    print("Бот запущен...")
    await dp.start_polling(bot)
if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
      
