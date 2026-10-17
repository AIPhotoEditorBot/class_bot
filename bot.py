import asyncio
import logging
import sys
import html
import re
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
import calendar
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import Message, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton as _InlineKeyboardButton,
    InputMediaPhoto,
    InputRichMessage,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockButtons,
    InputRichBlockPhoto,
    RichMessageButton,
    RichBlockCaption,
    ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder as _AiogramInlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from aiogram.types import ReactionTypeEmoji
from aiogram.methods import SendRichMessage
from aiogram.exceptions import TelegramBadRequest


def _infer_inline_button_style(text: str) -> str:
    """Assign a Bot API 9.4+ button style by intent without changing its action."""
    label = (text or "").casefold()
    danger_terms = (
        "отмена", "удалить", "удаление", "очистить", "сбросить", "стереть",
        "выключить", "отключить", "❌", "🗑", "🧹",
    )
    success_terms = (
        "сохранить", "готово", "загрузить", "добавить", "дополнить", "заполнить",
        "отправить", "подтвердить", "✅", "➕",
    )
    if any(term in label for term in danger_terms):
        return "danger"
    if any(term in label for term in success_terms):
        return "success"
    return "primary"


def InlineKeyboardButton(*args, **kwargs):
    """Backward-compatible factory that adds semantic color to existing buttons."""
    text_value = kwargs.get("text", args[0] if args else "")
    kwargs.setdefault("style", _infer_inline_button_style(str(text_value)))
    return _InlineKeyboardButton(*args, **kwargs)


class InlineKeyboardBuilder(_AiogramInlineKeyboardBuilder):
    """Color buttons created through the builder, too."""
    def button(self, *args, **kwargs):
        text_value = kwargs.get("text", args[0] if args else "")
        kwargs.setdefault("style", _infer_inline_button_style(str(text_value)))
        return super().button(*args, **kwargs)
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

HEART_MESSAGE_EFFECT_ID = "5159385139981059251"
RICH_BUTTON_STYLES = {"primary", "success", "danger", "link"}


def _rich_escape_attr(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _legacy_markdown_to_html(text: str) -> str:
    """Convert the small Telegram Markdown subset used by this bot to rich HTML."""
    raw = str(text or "")
    # MarkdownV2 escaped punctuation is literal text in the old message.
    raw = re.sub(r"\\([_*\[\]()~`>#+\-=|{}.!])", r"\1", raw)
    code_values = []

    def stash_code(match):
        code_values.append(html.escape(match.group(1), quote=False))
        return f"RICHCODEPLACEHOLDER{len(code_values) - 1}END"

    raw = re.sub(r"`([^`\n]+)`", stash_code, raw)
    result = html.escape(raw, quote=False)
    # Markdown links first, so formatting markers inside link labels remain intact.
    result = re.sub(
        r"\[([^\]]+)\]\((https?://[^ )]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        result,
    )
    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
    result = re.sub(r"(?<!\\)\*([^*\n]+)\*", r"<b>\1</b>", result)
    result = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", result)
    for index, code in enumerate(code_values):
        result = result.replace(f"RICHCODEPLACEHOLDER{index}END", f"<code>{code}</code>")
    return result


def _legacy_text_to_rich_html(text, parse_mode=None) -> str:
    value = str(text or "")
    # Rich Message HTML does not reliably render literal newline characters as
    # visible line breaks. Convert CRLF/CR/LF to explicit <br> tags after the
    # original formatting has been translated, preserving Markdown and HTML.
    def rich_line_breaks(rendered: str) -> str:
        return re.sub(r"\r\n|\r|\n", "<br>", rendered)

    mode = str(getattr(parse_mode, "value", parse_mode) or "").upper()
    if mode == "HTML":
        return rich_line_breaks(value)
    if mode.startswith("MARKDOWN"):
        return rich_line_breaks(_legacy_markdown_to_html(value))
    # Unformatted text must be escaped; otherwise angle brackets in homework
    # could unexpectedly become rich-message HTML tags.
    return rich_line_breaks(html.escape(value, quote=False))


def _legacy_button_to_rich_html(button):
    """Serialize supported legacy inline-button actions as Bot API 10.3 HTML."""
    label = html.escape(str(getattr(button, "text", "") or ""), quote=False)
    style = getattr(button, "style", None)
    if style not in RICH_BUTTON_STYLES:
        style = _infer_inline_button_style(str(getattr(button, "text", "") or ""))

    attrs = []
    action_type = None
    if getattr(button, "callback_data", None) is not None:
        action_type = "callback_data"
        attrs.append(("data", button.callback_data))
    elif getattr(button, "url", None) is not None:
        action_type = "url"
        attrs.append(("url", button.url))
    elif getattr(button, "switch_inline_query_current_chat", None) is not None:
        action_type = "switch_inline_query_current_chat"
        attrs.append(("query", button.switch_inline_query_current_chat))
    elif getattr(button, "switch_inline_query", None) is not None:
        action_type = "switch_inline_query"
        attrs.append(("query", button.switch_inline_query))
    elif getattr(button, "switch_inline_query_chosen_chat", None) is not None:
        action_type = "switch_inline_query_chosen_chat"
        chosen = button.switch_inline_query_chosen_chat
        attrs.append(("query", getattr(chosen, "query", "") or ""))
        for field, attr in (
            ("allow_user_chats", "allow-user-chats"),
            ("allow_bot_chats", "allow-bot-chats"),
            ("allow_group_chats", "allow-group-chats"),
            ("allow_channel_chats", "allow-channel-chats"),
        ):
            if getattr(chosen, field, False):
                attrs.append((attr, None))
    elif getattr(button, "web_app", None) is not None:
        action_type = "web_app"
        web_app = button.web_app
        attrs.append(("url", getattr(web_app, "url", "") or ""))
    elif getattr(button, "login_url", None) is not None:
        action_type = "login_url"
        login = button.login_url
        attrs.append(("url", getattr(login, "url", "") or ""))
        if getattr(login, "forward_text", None):
            attrs.append(("forward-text", login.forward_text))
        if getattr(login, "request_write_access", False):
            attrs.append(("request-write-access", None))
    elif getattr(button, "copy_text", None) is not None:
        action_type = "copy_text"
        attrs.append(("text", getattr(button.copy_text, "text", "") or ""))
    elif getattr(button, "disabled", None) is not None:
        action_type = "disabled"
    else:
        # This preserves an unsupported action instead of silently creating
        # a dead rich button. Current v8 buttons use callback, URL, or inline query.
        return None

    if style == "link" and action_type != "callback_data":
        style = "primary"
    attr_text = f' type="{action_type}"'
    if style:
        attr_text += f' style="{_rich_escape_attr(style)}"'
    for name, value in attrs:
        if value is None:
            attr_text += f" {name}"
        else:
            attr_text += f' {name}="{_rich_escape_attr(value)}"'
    return f"<tg-button{attr_text}>{label}</tg-button>"


def _legacy_markup_to_rich_message(text, reply_markup, parse_mode=None):
    """Return an InputRichMessage with the old inline-keyboard rows embedded in its body."""
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return None
    body = _legacy_text_to_rich_html(text, parse_mode)
    rich_rows = []
    for row in rows:
        # RichMessage button rows allow at most eight buttons per row.
        for start in range(0, len(row), 8):
            buttons = row[start:start + 8]
            serialized = [_legacy_button_to_rich_html(button) for button in buttons]
            if any(button_html is None for button_html in serialized):
                return None
            if serialized:
                rich_rows.append("<tg-button-row>" + "".join(serialized) + "</tg-button-row>")
    joined = body + ("\n" if body and rich_rows else "") + "\n".join(rich_rows)
    return InputRichMessage(html=joined)


def _legacy_button_to_rich_button(button):
    """Convert an existing inline button to a typed RichMessageButton for media blocks."""
    kwargs = {"text": str(getattr(button, "text", "") or "")}
    style = getattr(button, "style", None)
    if style not in RICH_BUTTON_STYLES:
        style = _infer_inline_button_style(kwargs["text"])
    if style == "link" and getattr(button, "callback_data", None) is None:
        style = "primary"
    if style:
        kwargs["style"] = style

    # Preserve the button's original action. RichMessageButton supports each of the
    # action types listed here; unsupported actions return None rather than becoming dead buttons.
    for field in (
        "callback_data", "url", "switch_inline_query_current_chat", "switch_inline_query",
        "switch_inline_query_chosen_chat", "web_app", "login_url", "copy_text", "disabled",
    ):
        value = getattr(button, field, None)
        if value is not None:
            kwargs[field] = value
            break
    else:
        return None

    try:
        return RichMessageButton(**kwargs)
    except Exception:
        logging.exception("Не удалось преобразовать inline-кнопку в RichMessageButton")
        return None


def _legacy_markup_to_rich_button_blocks(reply_markup):
    """Convert an inline keyboard into Rich Message button blocks, retaining row order."""
    rows = getattr(reply_markup, "inline_keyboard", None)
    if rows is None:
        return None
    blocks = []
    for row in rows:
        for start in range(0, len(row), 8):
            buttons = [_legacy_button_to_rich_button(button) for button in row[start:start + 8]]
            if any(button is None for button in buttons):
                return None
            if buttons:
                blocks.append(InputRichBlockButtons(buttons=buttons))
    return blocks


def _is_private_chat_id(chat_id) -> bool:
    # Telegram user/private chat IDs are positive integers; group/channel IDs are negative.
    if isinstance(chat_id, int):
        return chat_id > 0
    if isinstance(chat_id, str) and chat_id.isdigit():
        return int(chat_id) > 0
    # A @username target is ambiguous, so avoid applying a private-only effect.
    return False


def _add_private_heart_effect(method):
    fields = getattr(type(method), "model_fields", {})
    if "message_effect_id" not in fields:
        return method
    if not _is_private_chat_id(getattr(method, "chat_id", None)):
        return method
    if getattr(method, "message_effect_id", None):
        return method
    try:
        return method.model_copy(update={"message_effect_id": HEART_MESSAGE_EFFECT_ID})
    except Exception:
        # If a future aiogram version changes model-copy mechanics, sending the
        # message normally is safer than breaking unrelated handlers.
        logging.exception("Не удалось добавить эффект сердца к личному сообщению")
        return method


def _copy_rich_send_fields(source_method, rich_message, excluded_fields=(), preserve_reply_markup=False):
    """Build SendRichMessage while preserving all compatible original send options.

    Legacy inline keyboards are embedded in rich content. Other reply markup types
    remain as reply_markup because the official sendRichMessage method supports them.
    """
    target_fields = getattr(SendRichMessage, "model_fields", {})
    excluded = set(excluded_fields) | {
        "chat_id", "rich_message", "reply_markup", "text", "caption",
        "parse_mode", "entities", "caption_entities", "link_preview_options",
        "disable_web_page_preview", "photo", "has_spoiler", "show_caption_above_media",
        "reply_to_message_id", "allow_sending_without_reply",
    }
    kwargs = {
        "chat_id": getattr(source_method, "chat_id", None),
        "rich_message": rich_message,
    }
    for name in target_fields:
        if name in excluded:
            continue
        if hasattr(source_method, name):
            value = getattr(source_method, name)
            if value is not None:
                kwargs[name] = value

    if preserve_reply_markup and "reply_markup" in target_fields:
        reply_markup = getattr(source_method, "reply_markup", None)
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup

    # Preserve reply_to_message_id shorthand supported by SendMessage/SendPhoto.
    if not kwargs.get("reply_parameters"):
        reply_to_message_id = getattr(source_method, "reply_to_message_id", None)
        if reply_to_message_id is not None:
            reply_kwargs = {"message_id": reply_to_message_id}
            allow_without_reply = getattr(source_method, "allow_sending_without_reply", None)
            if allow_without_reply is not None:
                reply_kwargs["allow_sending_without_reply"] = allow_without_reply
            kwargs["reply_parameters"] = ReplyParameters(**reply_kwargs)
    return SendRichMessage(**kwargs)


def _convert_legacy_keyboard_method(method):
    """Central conversion boundary for the project's Rich Message-capable sends and edits.

    Message.answer()/Bot.send_message() and Message.edit_text()/Bot.edit_message_text()
    dispatch through Bot.__call__, so this layer covers the many existing handlers without
    rewriting their business logic. The source audit found only text, rich-message, and
    photo sends in use; no sendDocument/video/audio/voice/media-group/copy/forward calls exist.
    """
    method_name = type(method).__name__
    markup = getattr(method, "reply_markup", None)

    if method_name == "SendMessage":
        text = getattr(method, "text", None)
        if text is None:
            return method

        if isinstance(markup, InlineKeyboardMarkup):
            rich_message = _legacy_markup_to_rich_message(
                text, markup, getattr(method, "parse_mode", None)
            )
            if rich_message is None:
                logging.warning(
                    "SendMessage оставлен в исходном формате: тип кнопки не поддерживается rich-конвертером"
                )
                return method
            try:
                return _copy_rich_send_fields(method, rich_message)
            except Exception:
                logging.exception("Не удалось преобразовать SendMessage с inline-кнопками в Rich Message")
                return method

        # Plain text plus ReplyKeyboardMarkup/ReplyKeyboardRemove/ForceReply can still
        # use rich text while retaining the official reply_markup option.
        rich_message = InputRichMessage(
            html=_legacy_text_to_rich_html(text, getattr(method, "parse_mode", None))
        )
        try:
            return _copy_rich_send_fields(
                method, rich_message, preserve_reply_markup=markup is not None
            )
        except Exception:
            logging.exception("Не удалось преобразовать SendMessage в Rich Message; отправляю исходное сообщение")
            return method

    if method_name == "SendPhoto":
        photo = getattr(method, "photo", None)
        if photo is None:
            return method

        # The current source sends plain photo captions. If someone later adds an
        # explicit parse_mode/entities, retain the original method rather than silently
        # stripping caption formatting that this block-level converter cannot represent.
        parse_mode = getattr(method, "parse_mode", None)
        parse_mode_name = str(getattr(parse_mode, "value", parse_mode) or "").casefold()
        explicit_parse_mode = bool(parse_mode_name and not parse_mode_name.startswith("default("))
        caption_entities = getattr(method, "caption_entities", None)
        caption_text = getattr(method, "caption", None)
        if caption_text and (explicit_parse_mode or caption_entities):
            logging.info("SendPhoto оставлен в исходном формате для сохранения разметки подписи")
            return method

        try:
            media_kwargs = {"media": photo}
            has_spoiler = getattr(method, "has_spoiler", None)
            if has_spoiler is not None:
                media_kwargs["has_spoiler"] = has_spoiler
            media = InputMediaPhoto(**media_kwargs)
            caption = RichBlockCaption(text=caption_text) if caption_text else None
            blocks = [InputRichBlockPhoto(photo=media, caption=caption)]

            if isinstance(markup, InlineKeyboardMarkup):
                button_blocks = _legacy_markup_to_rich_button_blocks(markup)
                if button_blocks is None:
                    logging.warning(
                        "SendPhoto оставлен в исходном формате: тип кнопки не поддерживается rich-конвертером"
                    )
                    return method
                blocks.extend(button_blocks)
                return _copy_rich_send_fields(method, InputRichMessage(blocks=blocks))

            # sendRichMessage officially supports non-inline reply markup; preserve it.
            return _copy_rich_send_fields(
                method, InputRichMessage(blocks=blocks), preserve_reply_markup=markup is not None
            )
        except Exception:
            logging.exception("Не удалось преобразовать SendPhoto в Rich Message; отправляю исходное фото")
            return method

    if method_name == "EditMessageText":
        if getattr(method, "rich_message", None) is not None:
            return method
        text = getattr(method, "text", None)
        if text is None:
            return method
        if markup is not None and not isinstance(markup, InlineKeyboardMarkup):
            # editMessageText only accepts an inline keyboard in this API method.
            return method
        if isinstance(markup, InlineKeyboardMarkup):
            rich_message = _legacy_markup_to_rich_message(
                text, markup, getattr(method, "parse_mode", None)
            )
            if rich_message is None:
                logging.warning(
                    "EditMessageText оставлен в исходном формате: тип кнопки не поддерживается rich-конвертером"
                )
                return method
        else:
            rich_message = InputRichMessage(
                html=_legacy_text_to_rich_html(text, getattr(method, "parse_mode", None))
            )
        try:
            return method.model_copy(update={
                "text": None,
                "parse_mode": None,
                "entities": None,
                "link_preview_options": None,
                "disable_web_page_preview": None,
                "reply_markup": None,
                "rich_message": rich_message,
            })
        except Exception:
            logging.exception("Не удалось преобразовать EditMessageText в Rich Message; выполняю исходное редактирование")
            return method

    return method

class RichEffectBot(Bot):
    """Central dispatch boundary for rich-capable sends/edits and private heart effects.

    Message.answer() and Message.edit_text() shortcuts dispatch through this Bot instance.
    CallbackQuery.answer() is an acknowledgement rather than a visible message and remains
    on its official API method. Unsupported message kinds are not transformed speculatively.
    """

    async def __call__(self, method, request_timeout=None):
        method = _convert_legacy_keyboard_method(method)
        method = _add_private_heart_effect(method)
        try:
            return await super().__call__(method, request_timeout=request_timeout)
        except TelegramBadRequest as exc:
            # Retry only when Telegram explicitly rejects the effect field; this avoids
            # duplicate messages on unrelated/network errors.
            error_text = str(exc).casefold()
            is_effect_error = any(token in error_text for token in (
                "message_effect_id", "message effect", "effect_id", "effect id", "message effect is invalid"
            ))
            if is_effect_error and getattr(method, "message_effect_id", None) == HEART_MESSAGE_EFFECT_ID:
                retry_method = method.model_copy(update={"message_effect_id": None})
                return await super().__call__(retry_method, request_timeout=request_timeout)
            raise


bot = RichEffectBot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЯ (FSM) ---
class HWState(StatesGroup):
    choosing_subject = State()
    choosing_group = State()
    choosing_action = State()
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


class GalleryUploadState(StatesGroup):
    choosing_subject = State()
    choosing_date = State()
    waiting_for_photos = State()

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
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'homework'"
        )
        homework_exists = await cursor.fetchone() is not None

        if not homework_exists:
            await db.execute('''
                CREATE TABLE homework (
                    chat_id INTEGER,
                    date_str TEXT,
                    subject TEXT,
                    task TEXT,
                    group_number INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, date_str, subject, group_number)
                )
            ''')
        else:
            cursor = await db.execute("PRAGMA table_info(homework)")
            homework_columns = {row[1] for row in await cursor.fetchall()}
            if "group_number" not in homework_columns:
                await db.execute("ALTER TABLE homework RENAME TO homework_old")
                await db.execute('''
                    CREATE TABLE homework (
                        chat_id INTEGER,
                        date_str TEXT,
                        subject TEXT,
                        task TEXT,
                        group_number INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (chat_id, date_str, subject, group_number)
                    )
                ''')
                await db.execute('''
                    INSERT INTO homework (chat_id, date_str, subject, task, group_number)
                    SELECT chat_id, date_str, subject, task, 0
                    FROM homework_old
                ''')
                await db.execute("DROP TABLE homework_old")

        await db.execute('''
            CREATE TABLE IF NOT EXISTS subject_group_modes (
                chat_id INTEGER,
                subject TEXT,
                groups_enabled INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, subject)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS gallery_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                photo_date TEXT NOT NULL,
                subject TEXT NOT NULL,
                file_id TEXT NOT NULL,
                file_unique_id TEXT,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                media_group_id TEXT,
                uploader_id INTEGER,
                uploaded_at TEXT NOT NULL,
                UNIQUE(source_chat_id, source_message_id)
            )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gallery_chat_date "
            "ON gallery_photos(chat_id, photo_date, subject)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gallery_subject_date "
            "ON gallery_photos(chat_id, subject, photo_date)"
        )
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
        InlineKeyboardButton(text="📚 Группы предметов", callback_data="admin_subject_groups")
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

async def _get_subjects_for_group_settings(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT DISTINCT subject FROM schedule WHERE chat_id = ? AND subject != '—' ORDER BY subject COLLATE NOCASE",
            (chat_id,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def _is_subject_grouped(chat_id: int, subject: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT groups_enabled FROM subject_group_modes WHERE chat_id = ? AND subject = ?",
            (chat_id, subject),
        )
        row = await cursor.fetchone()
    return bool(row and row[0])


def _get_admin_subject_groups_keyboard(subjects, enabled_subjects=None):
    enabled_subjects = enabled_subjects or set()
    builder = InlineKeyboardBuilder()
    for index, subject in enumerate(subjects):
        mark = "🔀" if subject in enabled_subjects else "➖"
        builder.button(text=f"{mark} {subject}", callback_data=f"admin_subject_group:{index}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back"))
    return builder.as_markup()


async def _get_admin_subject_group_detail(subject: str, index: int):
    enabled = await _is_subject_grouped(ANON_CHAT_ID, subject)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="✅ Включить 2 группы" if not enabled else "🔀 2 группы включены",
        callback_data=f"admin_subject_group_set:{index}:1",
    ))
    builder.row(InlineKeyboardButton(
        text="⛔ Выключить 2 группы" if enabled else "➖ 2 группы выключены",
        callback_data=f"admin_subject_group_set:{index}:0",
    ))
    builder.row(InlineKeyboardButton(text="⬅️ К предметам", callback_data="admin_subject_groups"))
    text = (
        "📚 <b>Настройка предмета</b>\n\n"
        f"Предмет: <b>{html.escape(subject)}</b>\n"
        f"Режим 2 групп: <b>{'ВКЛ' if enabled else 'ВЫКЛ'}</b>"
    )
    return text, builder.as_markup()


@dp.callback_query(F.data == "admin_subject_groups")
async def admin_subject_groups(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    subjects = await _get_subjects_for_group_settings(ANON_CHAT_ID)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT subject FROM subject_group_modes WHERE chat_id = ? AND groups_enabled = 1",
            (ANON_CHAT_ID,),
        )
        enabled_subjects = {row[0] for row in await cursor.fetchall()}

    if not subjects:
        text = "📚 <b>Разделение предметов на группы</b>\n\nВ расписании пока нет предметов."
    else:
        text = (
            "📚 <b>Разделение предметов на группы</b>\n\n"
            "Выбери предмет. 🔀 — режим 2 групп включён, ➖ — выключен."
        )

    await callback.message.edit_text(
        text,
        reply_markup=_get_admin_subject_groups_keyboard(subjects, enabled_subjects),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_subject_group:"))
async def admin_subject_group_open(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректный предмет.", show_alert=True)
        return

    subjects = await _get_subjects_for_group_settings(ANON_CHAT_ID)
    if not 0 <= index < len(subjects):
        await callback.answer("❌ Предмет больше недоступен.", show_alert=True)
        return

    text, keyboard = await _get_admin_subject_group_detail(subjects[index], index)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_subject_group_set:"))
async def admin_subject_group_set(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return

    try:
        index = int(parts[1])
        enabled = int(parts[2])
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return

    subjects = await _get_subjects_for_group_settings(ANON_CHAT_ID)
    if not 0 <= index < len(subjects) or enabled not in (0, 1):
        await callback.answer("❌ Предмет больше недоступен.", show_alert=True)
        return

    subject = subjects[index]
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT groups_enabled FROM subject_group_modes WHERE chat_id=? AND subject=?", (ANON_CHAT_ID, subject))
        old_mode_row = await cur.fetchone()
        old_mode = int(old_mode_row[0]) if old_mode_row else 0
        await db.execute(
            "INSERT INTO subject_group_modes (chat_id, subject, groups_enabled) VALUES (?, ?, ?) ON CONFLICT(chat_id, subject) DO UPDATE SET groups_enabled = excluded.groups_enabled",
            (ANON_CHAT_ID, subject, enabled),
        )
        await _insert_audit_event_db(db, category="admin", event_type="subject_group_mode_changed",
                                     actor_id=callback.from_user.id, actor_name=_audit_user_name(callback.from_user),
                                     object_type="subject", object_id=subject, result="success",
                                     details={"old_enabled": old_mode, "new_enabled": enabled}, admin_id=ADMIN_ID,
                                     retention_days=AUDIT_DATA_RETENTION_DAYS)
        await db.commit()

    text, keyboard = await _get_admin_subject_group_detail(subject, index)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer("Режим 2 групп включён." if enabled else "Режим 2 групп выключен.")


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
        await _insert_audit_event_db(db, category="admin", event_type="reminder_deleted", actor_id=callback.from_user.id,
                                     actor_name=_audit_user_name(callback.from_user), object_type="reminder", object_id=str(reminder_id),
                                     admin_id=ADMIN_ID, retention_days=AUDIT_DATA_RETENTION_DAYS)
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

    try:
        await _create_database_backup("before_homework_cleanup", actor_id=callback.from_user.id)
    except Exception:
        logging.exception("Не удалось создать резервную копию перед очисткой старого ДЗ")
        await callback.answer("Не удалось создать резервную копию. Очистка отменена.", show_alert=True)
        return

    monday = datetime.now(MINSK_TZ).date() - timedelta(days=datetime.now(MINSK_TZ).weekday())
    monday_str = monday.strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM homework WHERE chat_id = ? AND date_str < ?",
            (ANON_CHAT_ID, monday_str)
        )
        count = (await cursor.fetchone())[0]
        # Snapshot each old homework row before the legacy bulk cleanup.
        cur_rows = await db.execute(
            "SELECT date_str, subject, task, group_number FROM homework WHERE chat_id = ? AND date_str < ?",
            (ANON_CHAT_ID, monday_str)
        )
        old_rows = await cur_rows.fetchall()
        for old_date, old_subject, old_task, old_group in old_rows:
            await _insert_homework_history_db(
                db, ANON_CHAT_ID, old_date, old_subject, old_group, "admin_bulk_delete", old_task, None,
                callback.from_user.id, _audit_user_name(callback.from_user), ADMIN_ID
            )
        await _insert_audit_event_db(
            db, category="data", event_type="homework_bulk_cleanup", actor_id=callback.from_user.id,
            actor_name=_audit_user_name(callback.from_user), object_type="homework", object_id=f"before:{monday_str}",
            result="success", details={"deleted_count": count, "before_date": monday_str}, admin_id=ADMIN_ID,
            retention_days=AUDIT_DATA_RETENTION_DAYS
        )
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
        "Отправь текст, фото или фото с подписью — бот сразу опубликует это в основном чате.",
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

    if message.photo:
        photo_file_id = message.photo[-1].file_id
        caption = (message.caption or "").strip() or None

        try:
            await bot.send_photo(
                chat_id=ANON_CHAT_ID,
                photo=photo_file_id,
                caption=caption
            )
        except Exception as e:
            logging.error(f"Не удалось отправить фото от бота: {e}")
            await message.answer("❌ Не удалось отправить фото в основной чат.")
            return

        await state.clear()
        await message.answer("✅ Фото отправлено в основной чат от имени бота.")
        return

    text_to_send = (message.text or "").strip()
    if not text_to_send:
        await message.answer("❌ Отправь текст, фото или фото с подписью.")
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


# --- ТЕГИ И СТАТУСЫ ДЗ ---
HW_TAGS = {
    "replace": "(✏️)",
    "append": "(➕)",
}
HW_FILLED_MARK = "🔸"
HW_EMPTY_MARK = "🔹"


def _normalize_homework_text(task_text: str) -> str:
    return (task_text or "").strip()


def _homework_is_filled(task_text) -> bool:
    return isinstance(task_text, str) and bool(task_text.strip())


def _replace_homework_text(task_text: str) -> str:
    # Не накапливаем несколько (✏️) в начале при повторной замене.
    cleaned = re.sub(r'^(?:\s*\(✏️\)\s*)+', '', _normalize_homework_text(task_text))
    return f"{HW_TAGS['replace']} {cleaned}" if cleaned else HW_TAGS['replace']


def _append_homework_text(old_task: str, new_task: str) -> str:
    old_task = _normalize_homework_text(old_task)
    new_task = _normalize_homework_text(new_task)
    if not old_task:
        return new_task
    if not new_task:
        return old_task
    return f"{old_task} {HW_TAGS['append']} {new_task}"


async def _get_homework_task(chat_id: int, date_str: str, subject: str, group_number: int = 0):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT task FROM homework WHERE chat_id = ? AND date_str = ? AND subject = ? AND group_number = ?",
            (chat_id, date_str, subject, group_number),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return row[0]


async def _get_homework_statuses(chat_id: int, date_str: str, subjects):
    subjects = list(subjects)
    if not subjects:
        return {}

    subject_set = set(subjects)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT subject, task, group_number FROM homework WHERE chat_id = ? AND date_str = ?",
            (chat_id, date_str),
        )
        hw_rows = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT subject FROM subject_group_modes WHERE chat_id = ? AND groups_enabled = 1",
            (chat_id,),
        )
        grouped_subjects = {row[0] for row in await cursor.fetchall()}

    status = {subject: False for subject in subjects}
    for subject, task, group_number in hw_rows:
        if subject not in subject_set or not _homework_is_filled(task):
            continue
        if subject in grouped_subjects:
            if group_number in (1, 2):
                status[subject] = True
        elif group_number == 0:
            status[subject] = True
    return status


def _format_subject_choice_text(index: int, subject: str, filled: bool) -> str:
    mark = HW_FILLED_MARK if filled else HW_EMPTY_MARK
    return f"{index}. {mark} {subject}"


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

        lesson_map = {lesson_num: subject for lesson_num, subject in lessons}

        cursor_hw = await db.execute(
            "SELECT subject, task, group_number FROM homework WHERE chat_id = ? AND date_str = ?",
            (chat_id, date_str)
        )
        hw_rows = await cursor_hw.fetchall()
        hw_map = {(subject, group_number): task for subject, task, group_number in hw_rows}

        cursor_groups = await db.execute(
            "SELECT subject FROM subject_group_modes WHERE chat_id = ? AND groups_enabled = 1",
            (chat_id,)
        )
        grouped_subjects = {row[0] for row in await cursor_groups.fetchall()}

        cursor_gallery = await db.execute(
            "SELECT DISTINCT subject FROM gallery_photos WHERE chat_id = ? AND photo_date = ?",
            (chat_id, date_str),
        )
        gallery_subjects = {row[0] for row in await cursor_gallery.fetchall()}

    lines = [header.rstrip("\n")]
    for lesson_num in range(1, 9):
        subject = lesson_map.get(lesson_num)
        subject_normalized = str(subject or "").strip()
        is_missing = subject is None or subject_normalized in {"", "—", "-"}

        # For lessons 7 and 8, a non-existing slot is omitted completely. For slots
        # 1-6 keep the existing dash placeholder behavior to avoid altering schedule setup.
        if lesson_num in (7, 8) and is_missing:
            continue

        if is_missing:
            line = f"{lesson_num}. —"
            lines.append(line)
            continue

        if subject in grouped_subjects:
            group1_task = hw_map.get((subject, 1))
            group2_task = hw_map.get((subject, 2))
            group1_filled = _homework_is_filled(group1_task)
            group2_filled = _homework_is_filled(group2_task)
            mark = HW_FILLED_MARK if (group1_filled or group2_filled) else HW_EMPTY_MARK

            # Show only groups that currently have homework; when neither group has
            # homework, show a single shared "Нет Д/з" line as requested.
            group_parts = []
            if group1_filled:
                group_parts.append(f"1️⃣: {group1_task}")
            if group2_filled:
                group_parts.append(f"2️⃣: {group2_task}")
            homework_display = "  ".join(group_parts) if group_parts else "Нет Д/з"
            if subject in gallery_subjects:
                homework_display += " (📸)"
            line = f"{lesson_num}. {mark} *{subject}*: {homework_display}"
        else:
            hw_task = hw_map.get((subject, 0))
            hw_filled = _homework_is_filled(hw_task)
            homework_display = hw_task if hw_filled else "Нет Д/з"
            if subject in gallery_subjects:
                homework_display += " (📸)"
            mark = HW_FILLED_MARK if hw_filled else HW_EMPTY_MARK
            line = f"{lesson_num}. {mark} *{subject}*: {homework_display}"

        lines.append(line)

    return "\n".join(lines) + "\n"

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
    builder.row(InlineKeyboardButton(text="🖼 Галерея", callback_data=f"gal_open:{date_str}"))
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

async def _refresh_anonymous_reaction_message(message_id: int, anonymous_id: int, likes: int, dislikes: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT text FROM anonymous_messages WHERE id = ?", (anonymous_id,))
        row = await cursor.fetchone()
    body = f"🕵️ Анонимное сообщение\n\n{row[0]}" if row and row[0] else "🕵️ Анонимное сообщение"
    return await bot.edit_message_text(
        chat_id=ANON_CHAT_ID,
        message_id=message_id,
        text=body,
        reply_markup=get_anonymous_reaction_keyboard(anonymous_id, likes, dislikes),
    )


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
                    await _refresh_anonymous_reaction_message(callback.message.message_id, anonymous_id, counts[0], counts[1])
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
            await _refresh_anonymous_reaction_message(callback.message.message_id, anonymous_id, counts[0], counts[1])
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
        "Отправь текст, фото или фото с подписью одним сообщением. Оно будет опубликовано в чате без ответа на другое сообщение.",
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

    data = await state.get_data()
    target_chat_id = data.get("target_chat_id", ANON_CHAT_ID)
    replied_to_message_id = data.get("replied_to_message_id")

    if message.photo:
        # Фото поддерживаем для обычной анонимки. Анонимные ответы оставляем
        # в прежнем текстовом формате: бот и так умеет отвечать на фото.
        if replied_to_message_id:
            await message.answer("❌ Для анонимного ответа пока можно отправлять только текст.")
            return

        photo_file_id = message.photo[-1].file_id
        photo_caption = (message.caption or "").strip()
        caption = "🕵️ Анонимное сообщение"
        if photo_caption:
            caption += f"\n\n{photo_caption}"

        try:
            await bot.send_photo(
                chat_id=target_chat_id,
                photo=photo_file_id,
                caption=caption
            )
        except Exception as e:
            logging.error(f"Не удалось отправить анонимное фото: {e}")
            await message.answer(
                "❌ Не удалось отправить фото в чат.\n"
                "Проверь, что бот находится в нужном чате и имеет права на отправку сообщений."
            )
            return

        await message.answer("✅ Фото отправлено анонимно.")
        await state.clear()
        return

    if not message.text or message.text.startswith("/"):
        await message.answer("❌ Отправь текст, фото или фото с подписью. Команды здесь не принимаются.")
        return

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
        await bot.edit_message_text(
            chat_id=target_chat_id,
            message_id=sent_message.message_id,
            text=sent_message.text or text,
            reply_markup=get_anonymous_reaction_keyboard(anonymous_id, 0, 0),
        )
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
        except Exception as e:
            logging.error(f"Не удалось отправить напоминание #{reminder_id}: {e}")
            await write_audit_event(category="system", event_type="reminder_send_failed", result="error",
                                    object_type="reminder", object_id=str(reminder_id),
                                    details={"error_type": type(e).__name__}, retention_days=AUDIT_DATA_RETENTION_DAYS)

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
        cur = await db.execute("INSERT INTO reminders (chat_id,user_id,text,time_str,weekdays,created_at) VALUES (?,?,?,?,?,?)", (ANON_CHAT_ID,message.from_user.id,message.text.strip(),data["reminder_time"],f"ONCE:{data['reminder_date']}",datetime.now(MINSK_TZ).strftime("%d.%m.%Y %H:%M:%S")))
        await _insert_audit_event_db(db, category="data", event_type="reminder_created", actor_id=message.from_user.id,
                                     actor_name=_audit_user_name(message.from_user), object_type="reminder", object_id=str(cur.lastrowid),
                                     result="success", details={"kind":"once","date":data["reminder_date"],"time":data["reminder_time"]},
                                     retention_days=AUDIT_DATA_RETENTION_DAYS)
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
    await callback.message.edit_text(
        "📅 Выбери дни недели. Можно выбрать несколько или все семь:",
        reply_markup=get_reminder_days_keyboard(selected),
    )
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
        cur = await db.execute(
            "INSERT INTO reminders (chat_id, user_id, text, time_str, weekdays, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ANON_CHAT_ID, callback.from_user.id, text, time_str, ",".join(map(str, selected)), datetime.now(MINSK_TZ).strftime("%d.%m.%Y %H:%M:%S"))
        )
        await _insert_audit_event_db(db, category="data", event_type="reminder_created", actor_id=callback.from_user.id,
                                     actor_name=_audit_user_name(callback.from_user), object_type="reminder", object_id=str(cur.lastrowid),
                                     result="success", details={"kind":"repeat","time":time_str,"weekdays":selected},
                                     retention_days=AUDIT_DATA_RETENTION_DAYS)
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
        cur = await db.execute("SELECT 1 FROM reminders WHERE id = ? AND user_id = ? AND chat_id = ?", (reminder_id, callback.from_user.id, ANON_CHAT_ID))
        exists = await cur.fetchone()
        await db.execute("DELETE FROM reminders WHERE id = ? AND user_id = ? AND chat_id = ?", (reminder_id, callback.from_user.id, ANON_CHAT_ID))
        if exists:
            await _insert_audit_event_db(db, category="data", event_type="reminder_deleted", actor_id=callback.from_user.id,
                                         actor_name=_audit_user_name(callback.from_user), object_type="reminder", object_id=str(reminder_id),
                                         retention_days=AUDIT_DATA_RETENTION_DAYS)
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
                    "SELECT lesson_num, subject FROM schedule WHERE chat_id = ? AND day_int = ?",
                    (chat_id, day_int)
                )
                subjects = await cursor.fetchall()
            
            valid_subjects = [s[1] for s in subjects if s[1] != "—"]
            
            if not valid_subjects:
                await message.answer("На этот день уроков нет или расписание не настроено.")
                return

            statuses = await _get_homework_statuses(chat_id, date_str, valid_subjects)
            builder = InlineKeyboardBuilder()
            for index, sub in enumerate(valid_subjects, 1):
                builder.button(
                    text=_format_subject_choice_text(index, sub, statuses.get(sub, False)),
                    callback_data=f"sethw_{sub}",
                )
            builder.adjust(2)
            
            builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
            await message.answer(f"Выбери предмет для записи ДЗ на {date_str}:", reply_markup=builder.as_markup())
            await state.set_state(HWState.choosing_subject)
            
        except Exception as e:
            logging.error(e)
            await message.answer("Ошибка ссылки.")


@dp.callback_query(HWState.choosing_subject, F.data.startswith("sethw_"))
async def subject_chosen(callback: types.CallbackQuery, state: FSMContext):
    subject = callback.data.split("_", 1)[1]
    data = await state.get_data()
    chat_id = data.get("target_chat_id", ANON_CHAT_ID)
    await state.update_data(subject=subject)

    if await _is_subject_grouped(chat_id, subject):
        target_date = data.get("target_date")
        group1_task = await _get_homework_task(chat_id, target_date, subject, 1)
        group2_task = await _get_homework_task(chat_id, target_date, subject, 2)
        group1_mark = HW_FILLED_MARK if _homework_is_filled(group1_task) else HW_EMPTY_MARK
        group2_mark = HW_FILLED_MARK if _homework_is_filled(group2_task) else HW_EMPTY_MARK
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text=f"{group1_mark} 1 группа", callback_data="sethw_group_1"),
            InlineKeyboardButton(text=f"{group2_mark} 2 группа", callback_data="sethw_group_2"),
        )
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
        await callback.message.edit_text(
            f"Выбран предмет: *{subject}*\n\nВыбери группу:",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )
        await state.set_state(HWState.choosing_group)
    else:
        await state.update_data(group_number=0)
        existing_task = await _get_homework_task(chat_id, data.get("target_date"), subject, 0)
        if _homework_is_filled(existing_task):
            await callback.message.edit_text(
                f"Выбран предмет: *{subject}*\n\n"
                "На этот день ДЗ уже записано. Что сделать?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="➕ Дополнить", callback_data="hw_action_append"),
                        InlineKeyboardButton(text="✏️ Заменить", callback_data="hw_action_replace"),
                    ],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel")],
                ]),
                parse_mode="Markdown",
            )
            await state.set_state(HWState.choosing_action)
        else:
            await callback.message.edit_text(
                f"Выбран предмет: *{subject}*\n\nНапиши задание одним сообщением:",
                reply_markup=get_cancel_keyboard(),
                parse_mode="Markdown",
            )
            await state.set_state(HWState.writing_task)
    await callback.answer()


@dp.callback_query(HWState.choosing_group, F.data.startswith("sethw_group_"))
async def homework_group_chosen(callback: types.CallbackQuery, state: FSMContext):
    try:
        group_number = int(callback.data.rsplit("_", 1)[1])
    except ValueError:
        await callback.answer("❌ Некорректная группа.", show_alert=True)
        return

    if group_number not in (1, 2):
        await callback.answer("❌ Некорректная группа.", show_alert=True)
        return

    data = await state.get_data()
    subject = data.get("subject")
    chat_id = data.get("target_chat_id", ANON_CHAT_ID)
    date_str = data.get("target_date")
    if not subject:
        await callback.answer("❌ Сначала выбери предмет.", show_alert=True)
        return

    if not await _is_subject_grouped(chat_id, subject):
        await state.update_data(group_number=0)
        group_number = 0
    else:
        await state.update_data(group_number=group_number)

    existing_task = await _get_homework_task(chat_id, date_str, subject, group_number)
    if _homework_is_filled(existing_task):
        await callback.message.edit_text(
            f"Выбран предмет: *{subject}*\n"
            f"Выбрана: *{group_number} группа*\n\n"
            "На этот день для этой группы ДЗ уже записано. Что сделать?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="➕ Дополнить", callback_data="hw_action_append"),
                    InlineKeyboardButton(text="✏️ Заменить", callback_data="hw_action_replace"),
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel")],
            ]),
            parse_mode="Markdown",
        )
        await state.set_state(HWState.choosing_action)
    else:
        await callback.message.edit_text(
            f"Выбран предмет: *{subject}*\n"
            f"Выбрана: *{group_number} группа*\n\n"
            "Напиши задание одним сообщением:",
            reply_markup=get_cancel_keyboard(),
            parse_mode="Markdown",
        )
        await state.set_state(HWState.writing_task)
    await callback.answer()


@dp.callback_query(HWState.choosing_action, F.data.in_(["hw_action_append", "hw_action_replace"]))
async def homework_action_chosen(callback: types.CallbackQuery, state: FSMContext):
    action = "append" if callback.data == "hw_action_append" else "replace"
    await state.update_data(homework_action=action)
    await callback.message.edit_text(
        "✍️ Теперь напиши новое домашнее задание одним сообщением:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    await state.set_state(HWState.writing_task)
    await callback.answer()


@dp.message(HWState.writing_task)
async def task_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("target_chat_id")
    date_str = data.get("target_date")
    subject = data.get("subject")
    group_number = int(data.get("group_number", 0) or 0)
    action = data.get("homework_action")

    if message.photo:
        if message.media_group_id:
            await _queue_homework_photo_album(message, state, data)
        else:
            await _process_homework_photo_messages([message], state, data)
        return

    task_text = _normalize_homework_text(message.text or "")
    if not task_text:
        await message.answer("❌ ДЗ не может быть пустым.", reply_markup=get_cancel_keyboard())
        return

    existing_task = None
    if action in HW_TAGS:
        existing_task = await _get_homework_task(chat_id, date_str, subject, group_number)
        if not _homework_is_filled(existing_task):
            action = None

    if action == "replace":
        final_task = _replace_homework_text(task_text)
    elif action == "append":
        final_task = _append_homework_text(existing_task, task_text)
    else:
        final_task = task_text

    await _save_homework_audited(
        chat_id=int(chat_id), date_str=date_str, subject=subject,
        group_number=group_number, new_task=final_task,
        action=(action if action in ("append", "replace") else "save"),
        actor_id=getattr(message.from_user, "id", None),
        actor_name=_audit_user_name(getattr(message, "from_user", None)),
        admin_id=(ADMIN_ID if getattr(getattr(message, "from_user", None), "id", None) == ADMIN_ID else None),
    )

    group_note = f" для {group_number} группы" if group_number in (1, 2) else ""
    await message.answer(f"✅ ДЗ по {subject}{group_note} сохранено! Вернись в чат и нажми 'Обновить'.")
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
        cur = await db.execute("INSERT INTO birthdays (chat_id, full_name, birth_day, birth_month) VALUES (?, ?, ?, ?)",
                               (ANON_CHAT_ID, name, day, month))
        await _insert_audit_event_db(db, category="data", event_type="birthday_created", actor_id=message.from_user.id,
                                     actor_name=_audit_user_name(message.from_user), object_type="birthday", object_id=str(cur.lastrowid),
                                     result="success", details={"operation":"record_created"}, admin_id=ADMIN_ID if message.from_user.id==ADMIN_ID else None,
                                     retention_days=AUDIT_DATA_RETENTION_DAYS)
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
        cur = await db.execute("SELECT full_name FROM birthdays WHERE id=? AND chat_id=?", (birthday_id, ANON_CHAT_ID))
        old_row = await cur.fetchone()
        await db.execute("UPDATE birthdays SET full_name = ? WHERE id = ? AND chat_id = ?", (message.text.strip(), birthday_id, ANON_CHAT_ID))
        await _insert_audit_event_db(db, category="data", event_type="birthday_name_changed", actor_id=message.from_user.id,
                                     actor_name=_audit_user_name(message.from_user), object_type="birthday", object_id=str(birthday_id),
                                     details={"operation":"name_changed"},
                                     admin_id=ADMIN_ID, retention_days=AUDIT_DATA_RETENTION_DAYS)
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
        cur = await db.execute("SELECT birth_day,birth_month FROM birthdays WHERE id=? AND chat_id=?", (birthday_id, ANON_CHAT_ID))
        old_row = await cur.fetchone()
        await db.execute("UPDATE birthdays SET birth_day = ?, birth_month = ? WHERE id = ? AND chat_id = ?", (day, month, birthday_id, ANON_CHAT_ID))
        await _insert_audit_event_db(db, category="data", event_type="birthday_date_changed", actor_id=message.from_user.id,
                                     actor_name=_audit_user_name(message.from_user), object_type="birthday", object_id=str(birthday_id),
                                     details={"operation":"date_changed"},
                                     admin_id=ADMIN_ID, retention_days=AUDIT_DATA_RETENTION_DAYS)
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
        await _insert_audit_event_db(db, category="data", event_type="birthday_deleted", actor_id=callback.from_user.id,
                                     actor_name=_audit_user_name(callback.from_user), object_type="birthday", object_id=str(birthday_id),
                                     details={"operation":"record_deleted"}, admin_id=ADMIN_ID, retention_days=AUDIT_DATA_RETENTION_DAYS)
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
        BotCommand(command="privacy", description="🔐 Журналирование и данные"),
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



# --- ДОБАВОК: ОБЩИЙ РАЗДЕЛ «ДЕЙСТВИЯ ОТ ИМЕНИ БОТА» ---
# Этот блок ничего не удаляет из существующего кода, а только добавляет
# новый раздел админ-панели и меняет отображение уже существующих функций.


def get_admin_bot_actions_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📢 Сообщение от бота", callback_data="admin_bot_message"),
        InlineKeyboardButton(text="↩️ Ответ от бота", callback_data="admin_bot_reply"),
    )
    builder.row(
        InlineKeyboardButton(text="😀 Реакция от бота", callback_data="admin_bot_reaction")
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")
    )
    return builder.as_markup()


# Сохраняем текущую клавиатуру админ-панели и только убираем из её отображения
# старую отдельную кнопку «Сообщение от бота», заменяя её общим разделом.
_previous_admin_keyboard = get_admin_keyboard


def get_admin_keyboard_grouped_bot_actions():
    original_markup = _previous_admin_keyboard()
    builder = InlineKeyboardBuilder()

    for row in original_markup.inline_keyboard:
        filtered_row = [
            button
            for button in row
            if button.callback_data not in {
                "admin_bot_message",
                "admin_bot_reply",
                "admin_bot_reaction",
            }
        ]
        if filtered_row:
            builder.row(*filtered_row)

    builder.row(
        InlineKeyboardButton(
            text="🤖 Действия от имени бота",
            callback_data="admin_bot_actions",
        )
    )
    return builder.as_markup()


get_admin_keyboard = get_admin_keyboard_grouped_bot_actions


@dp.callback_query(F.data == "admin_bot_actions")
async def admin_bot_actions(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "🤖 <b>Действия от имени бота</b>\n\nВыбери нужное действие:",
        reply_markup=get_admin_bot_actions_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


# --- ПАГИНАЦИЯ РЕАКЦИЙ: 12 НА СТРАНИЦЕ, 3 В РЯД ---

ADMIN_REACTIONS_PER_PAGE = 12


def get_admin_reaction_keyboard_paginated(page: int = 0):
    # Используем уже существующий словарь ADMIN_REACTIONS, не изменяя список
    # реакций пользователя и не добавляя новые записи в него.
    items = list(ADMIN_REACTIONS.items())
    total_pages = max(1, (len(items) + ADMIN_REACTIONS_PER_PAGE - 1) // ADMIN_REACTIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * ADMIN_REACTIONS_PER_PAGE
    page_items = items[start:start + ADMIN_REACTIONS_PER_PAGE]

    builder = InlineKeyboardBuilder()
    for key, emoji in page_items:
        builder.button(text=emoji, callback_data=f"admin_reaction_{key}")
    builder.adjust(3)

    # Стрелки находятся непосредственно над кнопкой отмены.
    prev_page = (page - 1) % total_pages
    next_page = (page + 1) % total_pages
    builder.row(
        InlineKeyboardButton(text="⬅️", callback_data=f"reaction_page_{prev_page}"),
        InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="reaction_page_info"),
        InlineKeyboardButton(text="➡️", callback_data=f"reaction_page_{next_page}"),
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="fsm_cancel"))
    return builder.as_markup()


# Переопределяем только отображение клавиатуры выбора реакции.
get_admin_reaction_keyboard = get_admin_reaction_keyboard_paginated


@dp.callback_query(F.data.startswith("reaction_page_"))
async def admin_reaction_page_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    if callback.data == "reaction_page_info":
        await callback.answer("Используй стрелки для переключения страниц.")
        return

    try:
        page = int(callback.data.removeprefix("reaction_page_"))
    except ValueError:
        await callback.answer("❌ Некорректная страница.", show_alert=True)
        return

    await callback.message.edit_text(
        "✅ Ссылка принята.\n\nВыбери реакцию, которую поставить от имени бота:",
        reply_markup=get_admin_reaction_keyboard(page),
    )
    await callback.answer()


async def _get_homework_subject_for_index(chat_id: int, date_obj, subject_index: int):
    day_int = date_obj.weekday()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT lesson_num, subject FROM schedule WHERE chat_id = ? AND day_int = ? AND subject != '—' ORDER BY lesson_num",
            (chat_id, day_int),
        )
        rows = await cursor.fetchall()
    if not 0 <= subject_index < len(rows):
        return None
    return rows[subject_index][1]


async def _delete_homework_record(chat_id: int, date_str: str, subject: str, group_number: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT task FROM homework WHERE chat_id = ? AND date_str = ? AND subject = ? AND group_number = ?",
            (chat_id, date_str, subject, group_number),
        )
        row = await cursor.fetchone()
        old_task = row[0] if row else None
        if row:
            await _insert_homework_history_db(db, chat_id, date_str, subject, group_number, "delete", old_task, None)
            await _insert_audit_event_db(db, category="data", event_type="homework_delete", object_type="homework",
                                         object_id=f"{date_str}|{subject}|{group_number}",
                                         details={"date": date_str, "subject": subject, "group_number": group_number},
                                         retention_days=AUDIT_DATA_RETENTION_DAYS)
        await db.execute(
            "DELETE FROM homework WHERE chat_id = ? AND date_str = ? AND subject = ? AND group_number = ?",
            (chat_id, date_str, subject, group_number),
        )
        await db.commit()


async def _show_homework_after_delete(callback: types.CallbackQuery, date_obj):
    await callback.message.edit_text(
        await generate_schedule_text(callback.message.chat.id, date_obj),
        reply_markup=get_keyboard(callback.message.chat.id, date_obj),
        parse_mode="Markdown",
    )


# --- INLINE-РЕЖИМ ---
# Быстрый доступ только к домашнему заданию и его заполнению.
# Обычный интерфейс бота не изменяется.

import secrets
import time


INLINE_PENDING_ACTIONS = {}
INLINE_PENDING_TTL = 30 * 60
INLINE_PENDING_LIMIT = 500


def _cleanup_inline_pending_actions():
    now = time.monotonic()
    expired = [
        token for token, data in INLINE_PENDING_ACTIONS.items()
        if data.get("expires_at", 0) <= now
    ]
    for token in expired:
        INLINE_PENDING_ACTIONS.pop(token, None)

    if len(INLINE_PENDING_ACTIONS) > INLINE_PENDING_LIMIT:
        oldest = sorted(
            INLINE_PENDING_ACTIONS.items(),
            key=lambda item: item[1].get("expires_at", 0)
        )
        for token, _ in oldest[:len(INLINE_PENDING_ACTIONS) - INLINE_PENDING_LIMIT]:
            INLINE_PENDING_ACTIONS.pop(token, None)


def _create_inline_pending_action(user_id: int, action: dict) -> str:
    _cleanup_inline_pending_actions()
    token = secrets.token_hex(5)
    action = dict(action)
    action["user_id"] = user_id
    action["expires_at"] = time.monotonic() + INLINE_PENDING_TTL
    INLINE_PENDING_ACTIONS[token] = action
    return token


def _get_inline_pending_action(token: str, user_id: int):
    _cleanup_inline_pending_actions()
    action = INLINE_PENDING_ACTIONS.get(token)
    if not action or action.get("user_id") != user_id:
        return None
    return action


def _parse_inline_date(value: str):
    value = value.strip()
    try:
        if not re.fullmatch(r"\d{2}/\d{2}", value):
            return None
        day, month = map(int, value.split("/"))
        year = datetime.now(MINSK_TZ).year
        if not (1 <= month <= 12):
            return None
        if not (1 <= day <= calendar.monthrange(year, month)[1]):
            return None
        return datetime(year, month, day).date()
    except (ValueError, TypeError):
        return None


def _inline_keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _inline_article(result_id, title, description, text, reply_markup=None, parse_mode="HTML"):
    # Inline results also use Rich Messages. When legacy buttons are present, embed
    # them in the rich body so the selected result retains the same actions.
    rich_message = None
    if reply_markup is not None:
        rich_message = _legacy_markup_to_rich_message(text, reply_markup, parse_mode)
        if rich_message is None:
            logging.warning("Inline-результат оставлен в совместимом текстовом формате: обнаружена неподдерживаемая кнопка")
    else:
        rich_message = InputRichMessage(
            html=_legacy_text_to_rich_html(text, parse_mode)
        )

    if rich_message is not None:
        return types.InlineQueryResultArticle(
            id=result_id,
            title=title,
            description=description,
            input_message_content=types.InputRichMessageContent(rich_message=rich_message),
        )

    # Fallback only for button action types that the compatibility serializer cannot
    # represent. Do not silently discard their behavior.
    return types.InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=types.InputTextMessageContent(
            message_text=text,
            parse_mode=parse_mode,
        ),
        reply_markup=reply_markup,
    )


def _inline_switch_button(text, query):
    return InlineKeyboardButton(
        text=text,
        switch_inline_query_current_chat=query,
    )


def _inline_homework_keyboard(date_obj):
    # Та же раскладка, что и в обычном интерфейсе «дз»:
    # навигация + обновление, затем «Заполнить ДЗ».
    prev_date = date_obj - timedelta(days=1)
    next_date = date_obj + timedelta(days=1)
    date_str = date_obj.isoformat()

    rows = [
        [
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"inline_hw:{prev_date.isoformat()}",
            ),
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=f"inline_hw:{date_str}",
            ),
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"inline_hw:{next_date.isoformat()}",
            ),
        ]
    ]

    if bot_info and bot_info.username:
        bot_link = f"https://t.me/{bot_info.username}?start=fill_{ANON_CHAT_ID}_{date_str}"
        rows.append([
            InlineKeyboardButton(text="✍️ Заполнить ДЗ", url=bot_link)
        ])

    return _inline_keyboard(rows)


async def _get_inline_homework_text(date_obj):
    # Используем ту же генерацию, что и обычная команда «дз»,
    # чтобы содержимое inline совпадало с обычным интерфейсом.
    return await generate_schedule_text(ANON_CHAT_ID, date_obj)


def _inline_homework_result_id(date_obj):
    return f"inline_hw_{date_obj.isoformat()}"


async def _get_inline_subjects(date_obj):
    day_int = date_obj.weekday()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """SELECT lesson_num, subject
               FROM schedule
               WHERE chat_id = ? AND day_int = ? AND subject != '—'
               ORDER BY lesson_num""",
            (ANON_CHAT_ID, day_int),
        )
        rows = await cursor.fetchall()
    return [subject for _, subject in rows]



@dp.inline_query()
async def inline_query_handler(inline_query: types.InlineQuery):
    _cleanup_inline_pending_actions()

    query_text = (inline_query.query or "").strip()
    parts = query_text.split()
    command = parts[0].lower() if parts else ""

    results = []

    # Пустой запрос: сразу показываем сегодняшнее ДЗ.
    if not query_text:
        today = datetime.now(MINSK_TZ).date()
        hw_text = await _get_inline_homework_text(today)
        results = [
            _inline_article(
                _inline_homework_result_id(today),
                "📚 Домашнее задание",
                "ДЗ на сегодня",
                hw_text,
                _inline_homework_keyboard(today),
                parse_mode="Markdown",
            )
        ]

    elif command == "hw":
        # Дополнительный прямой вызов: @hwlsbot hw
        # тоже сразу показывает сегодняшний день.
        today = datetime.now(MINSK_TZ).date()
        hw_text = await _get_inline_homework_text(today)
        results = [
            _inline_article(
                _inline_homework_result_id(today),
                "📚 Домашнее задание",
                "ДЗ на сегодня",
                hw_text,
                _inline_homework_keyboard(today),
                parse_mode="Markdown",
            )
        ]

    else:
        # Дата распознаётся автоматически: ДД/ММ [текст ДЗ]
        selected_date = _parse_inline_date(parts[0]) if parts else None

        if selected_date is not None:
            if len(parts) == 1:
                results = [
                    _inline_article(
                        "inline_add_enter_task",
                        f"📅 {selected_date.strftime('%d/%m')} — введи ДЗ",
                        "После даты напиши текст задания",
                        (
                            "📝 <b>Теперь введи домашнее задание</b>\n\n"
                            f"Дата: <code>{selected_date.strftime('%d.%m.%Y')}</code>\n\n"
                            "После даты просто допиши текст задания."
                        ),
                    )
                ]
            else:
                task = " ".join(parts[1:]).strip()
                if not task:
                    results = [
                        _inline_article(
                            "inline_add_empty_task",
                            "❌ Нет текста задания",
                            "После даты нужен текст ДЗ",
                            "❌ <b>Нет текста задания</b>\n\nНапиши после даты само домашнее задание.",
                        )
                    ]
                else:
                    subjects = await _get_inline_subjects(selected_date)
                    if not subjects:
                        results = [
                            _inline_article(
                                "inline_add_no_subjects",
                                "📭 Нет предметов",
                                "На эту дату нет уроков в расписании",
                                (
                                    "📭 <b>Не удалось выбрать предмет</b>\n\n"
                                    f"На <code>{selected_date.strftime('%d.%m.%Y')}</code> в расписании нет предметов."
                                ),
                            )
                        ]
                    else:
                        token = _create_inline_pending_action(
                            inline_query.from_user.id,
                            {
                                "type": "homework",
                                "date": selected_date.isoformat(),
                                "task": task,
                            },
                        )

                        statuses = await _get_homework_statuses(ANON_CHAT_ID, selected_date.isoformat(), subjects)
                        builder = InlineKeyboardBuilder()
                        for index, subject in enumerate(subjects, 1):
                            subject_index = index - 1
                            builder.button(
                                text=_format_subject_choice_text(index, subject, statuses.get(subject, False)),
                                callback_data=f"inline_add_subject:{token}:{subject_index}",
                            )
                        builder.adjust(2)

                        results = [
                            _inline_article(
                                "inline_add_subjects_" + token,
                                f"📚 Выбери предмет — {selected_date.strftime('%d/%m')}",
                                "Выбери предмет из расписания",
                                (
                                    "📚 <b>Выбери предмет</b>\n\n"
                                    f"📅 {selected_date.strftime('%d.%m.%Y')}\n"
                                    f"📝 {html.escape(task)}"
                                ),
                                builder.as_markup(),
                            )
                        ]
        else:
            results = [
                _inline_article(
                    "inline_menu_fallback",
                    "📚 Домашнее задание",
                    "Введи дату в формате ДД/ММ",
                    "📚 <b>Inline-режим</b>\n\nВведи дату в формате <code>ДД/ММ</code>, чтобы добавить ДЗ.",
                )
            ]

    await inline_query.answer(
        results=results,
        cache_time=0,
        is_personal=True,
    )


@dp.callback_query(F.data.startswith("inline_hw:"))
async def inline_homework_navigation(callback: types.CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer("Эта кнопка работает в inline-сообщении.", show_alert=True)
        return

    try:
        date_obj = datetime.strptime(callback.data.split(":", 1)[1], "%Y-%m-%d").date()
    except ValueError:
        await callback.answer("❌ Некорректная дата.", show_alert=True)
        return

    try:
        text = await _get_inline_homework_text(date_obj)
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=text,
            reply_markup=_inline_homework_keyboard(date_obj),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as e:
        error_text = str(e).lower()
        if "message is not modified" in error_text:
            await callback.answer("Данные актуальны")
        else:
            logging.error(f"Не удалось обновить inline-ДЗ: {e}")
            await callback.answer("❌ Не удалось обновить ДЗ.", show_alert=True)


async def _inline_show_homework_preview(callback: types.CallbackQuery, token: str):
    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    date_obj = datetime.strptime(action["date"], "%Y-%m-%d").date()
    subject = action.get("subject")
    group_number = int(action.get("group_number", 0) or 0)
    try:
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=(
                "📝 <b>Проверь домашнее задание</b>\n\n"
                f"📅 {date_obj.strftime('%d.%m.%Y')}\n"
                f"📖 <b>{html.escape(subject)}</b>\n"
                + (f"👥 <b>{group_number} группа</b>\n" if group_number in (1, 2) else "")
                + f"📝 {html.escape(action['task'])}"
            ),
            reply_markup=_inline_keyboard([
                [
                    InlineKeyboardButton(text="❌ Отмена", callback_data=f"inline_add_cancel:{token}"),
                    InlineKeyboardButton(text="✅ Сохранить", callback_data=f"inline_add_save:{token}"),
                ]
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception as e:
        logging.error(f"Не удалось показать inline-предпросмотр ДЗ: {e}")
        await callback.answer("❌ Не удалось открыть подтверждение.", show_alert=True)


async def _inline_show_homework_action_choice(callback: types.CallbackQuery, token: str, existing_task: str):
    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    date_obj = datetime.strptime(action["date"], "%Y-%m-%d").date()
    subject = action.get("subject")
    group_number = int(action.get("group_number", 0) or 0)
    group_note = f"\n👥 <b>{group_number} группа</b>" if group_number in (1, 2) else ""
    text = (
        "📚 <b>ДЗ уже записано</b>\n\n"
        f"📅 {date_obj.strftime('%d.%m.%Y')}\n"
        f"📖 <b>{html.escape(subject)}</b>{group_note}\n"
        f"📌 <b>Сейчас:</b> {html.escape(existing_task)}\n"
        f"📝 <b>Новое:</b> {html.escape(action['task'])}\n\n"
        "Что сделать?"
    )
    try:
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=text,
            reply_markup=_inline_keyboard([
                [
                    InlineKeyboardButton(text="➕ Дополнить", callback_data=f"inline_add_action:{token}:append"),
                    InlineKeyboardButton(text="✏️ Заменить", callback_data=f"inline_add_action:{token}:replace"),
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"inline_add_cancel:{token}")],
            ]),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception as e:
        logging.error(f"Не удалось показать выбор изменения inline-ДЗ: {e}")
        await callback.answer("❌ Не удалось открыть выбор действия.", show_alert=True)


async def _inline_prepare_homework_action(callback: types.CallbackQuery, token: str):
    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    date_str = action["date"]
    subject = action.get("subject")
    group_number = int(action.get("group_number", 0) or 0)
    existing_task = await _get_homework_task(ANON_CHAT_ID, date_str, subject, group_number)
    if _homework_is_filled(existing_task):
        await _inline_show_homework_action_choice(callback, token, existing_task)
    else:
        await _inline_show_homework_preview(callback, token)


@dp.callback_query(F.data.startswith("inline_add_subject:"))
async def inline_add_homework_subject(callback: types.CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer("Эта кнопка работает в inline-сообщении.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return

    token = parts[1]
    try:
        subject_index = int(parts[2])
    except ValueError:
        await callback.answer("❌ Некорректный предмет.", show_alert=True)
        return

    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    date_obj = datetime.strptime(action["date"], "%Y-%m-%d").date()
    subjects = await _get_inline_subjects(date_obj)
    if not 0 <= subject_index < len(subjects):
        await callback.answer("❌ Предмет больше недоступен.", show_alert=True)
        return

    action["subject"] = subjects[subject_index]
    subject = action["subject"]

    if await _is_subject_grouped(ANON_CHAT_ID, subject):
        try:
            group1_task = await _get_homework_task(ANON_CHAT_ID, action["date"], subject, 1)
            group2_task = await _get_homework_task(ANON_CHAT_ID, action["date"], subject, 2)
            group1_mark = HW_FILLED_MARK if _homework_is_filled(group1_task) else HW_EMPTY_MARK
            group2_mark = HW_FILLED_MARK if _homework_is_filled(group2_task) else HW_EMPTY_MARK
            await bot.edit_message_text(
                inline_message_id=callback.inline_message_id,
                text=(
                    "📚 <b>Выбери группу</b>\n\n"
                    f"📅 {date_obj.strftime('%d.%m.%Y')}\n"
                    f"📖 <b>{html.escape(subject)}</b>\n"
                    f"📝 {html.escape(action['task'])}"
                ),
                reply_markup=_inline_keyboard([
                    [
                        InlineKeyboardButton(text=f"{group1_mark} 1 группа", callback_data=f"inline_add_group:{token}:1"),
                        InlineKeyboardButton(text=f"{group2_mark} 2 группа", callback_data=f"inline_add_group:{token}:2"),
                    ],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"inline_add_cancel:{token}")],
                ]),
                parse_mode="HTML",
            )
            await callback.answer()
        except Exception as e:
            logging.error(f"Не удалось показать выбор группы в inline-ДЗ: {e}")
            await callback.answer("❌ Не удалось открыть выбор группы.", show_alert=True)
        return

    action["group_number"] = 0
    await _inline_prepare_homework_action(callback, token)


@dp.callback_query(F.data.startswith("inline_add_group:"))
async def inline_add_homework_group(callback: types.CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer("Эта кнопка работает в inline-сообщении.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return

    token = parts[1]
    try:
        group_number = int(parts[2])
    except ValueError:
        await callback.answer("❌ Некорректная группа.", show_alert=True)
        return

    if group_number not in (1, 2):
        await callback.answer("❌ Некорректная группа.", show_alert=True)
        return

    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    subject = action.get("subject")
    if not subject or not await _is_subject_grouped(ANON_CHAT_ID, subject):
        await callback.answer("❌ Предмет больше не разделён на группы.", show_alert=True)
        return

    action["group_number"] = group_number
    await _inline_prepare_homework_action(callback, token)


@dp.callback_query(F.data.startswith("inline_add_action:"))
async def inline_add_homework_action(callback: types.CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer("Эта кнопка работает в inline-сообщении.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3 or parts[2] not in ("append", "replace"):
        await callback.answer("❌ Некорректное действие.", show_alert=True)
        return

    token = parts[1]
    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    action["homework_action"] = parts[2]
    await inline_add_homework_save(callback)


@dp.callback_query(F.data.startswith("inline_add_cancel:"))
async def inline_add_homework_cancel(callback: types.CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer("Эта кнопка работает в inline-сообщении.", show_alert=True)
        return

    token = callback.data.split(":", 1)[1]
    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели.", show_alert=True)
        return

    INLINE_PENDING_ACTIONS.pop(token, None)
    try:
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=(
                "➕ <b>Заполнение ДЗ отменено</b>\n\n"
                "Чтобы начать заново, введи дату <code>ДД/ММ</code> и текст задания через inline-режим."
            ),
            parse_mode="HTML",
        )
        await callback.answer("Отменено.")
    except Exception as e:
        logging.error(f"Не удалось отменить inline-добавление ДЗ: {e}")
        await callback.answer("❌ Не удалось отменить действие.", show_alert=True)


@dp.callback_query(F.data.startswith("inline_add_save:"))
async def inline_add_homework_save(callback: types.CallbackQuery):
    if not callback.inline_message_id:
        await callback.answer("Эта кнопка работает в inline-сообщении.", show_alert=True)
        return

    token = callback.data.split(":", 1)[1]
    action = _get_inline_pending_action(token, callback.from_user.id)
    if action is None or action.get("type") != "homework":
        await callback.answer("❌ Данные устарели. Начни заполнение заново.", show_alert=True)
        return

    subject = action.get("subject")
    if not subject:
        await callback.answer("❌ Сначала выбери предмет.", show_alert=True)
        return

    try:
        date_obj = datetime.strptime(action["date"], "%Y-%m-%d").date()
        group_number = int(action.get("group_number", 0) or 0)
        homework_action = action.get("homework_action")
        existing_task = await _get_homework_task(ANON_CHAT_ID, action["date"], subject, group_number)
        if homework_action == "replace":
            final_task = _replace_homework_text(action["task"])
        elif homework_action == "append" and _homework_is_filled(existing_task):
            final_task = _append_homework_text(existing_task, action["task"])
        else:
            final_task = action["task"]

        await _save_homework_audited(
            chat_id=int(ANON_CHAT_ID), date_str=action["date"], subject=subject,
            group_number=group_number, new_task=final_task,
            action=(homework_action if homework_action in ("append", "replace") else "save"),
            actor_id=callback.from_user.id,
            actor_name=_audit_user_name(callback.from_user),
            admin_id=(ADMIN_ID if callback.from_user.id == ADMIN_ID else None),
        )

        INLINE_PENDING_ACTIONS.pop(token, None)
        await bot.edit_message_text(
            inline_message_id=callback.inline_message_id,
            text=(
                "✅ <b>Домашнее задание сохранено</b>\n\n"
                f"📅 {date_obj.strftime('%d.%m.%Y')}\n"
                f"📖 <b>{html.escape(subject)}</b>\n"
                + (f"👥 <b>{group_number} группа</b>\n" if group_number in (1, 2) else "")
                + f"📝 {html.escape(final_task)}"
            ),
            parse_mode="HTML",
        )
        await callback.answer("ДЗ сохранено.")
    except Exception as e:
        logging.error(f"Не удалось сохранить inline-ДЗ: {e}")
        await callback.answer("❌ Не удалось сохранить ДЗ.", show_alert=True)

# --- ШКОЛЬНАЯ ГАЛЕРЕЯ (Bot API 10.3 Rich Messages) ---

GALLERY_PHOTOS_PER_PAGE = 4
GALLERY_DATE_HORIZON_DAYS = 42
GALLERY_UPLOAD_BUFFER = {}
HW_PHOTO_ALBUM_BUFFERS = {}


def _gallery_current_week_start():
    today = datetime.now(MINSK_TZ).date()
    return today - timedelta(days=today.weekday())


def _gallery_normalize_week(value):
    try:
        week_start = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        week_start = _gallery_current_week_start()
    week_start = week_start - timedelta(days=week_start.weekday())
    current_start = _gallery_current_week_start()
    if week_start < current_start:
        week_start = current_start
    return week_start


def _gallery_week_label(week_start):
    week_end = week_start + timedelta(days=6)
    return f"{week_start.strftime('%d.%m')}–{week_end.strftime('%d.%m.%Y')}"


def _rich_button(text, callback_data, style="primary"):
    return RichMessageButton(text=text, callback_data=callback_data, style=style)


def _rich_url_button(text, url, style="primary"):
    return RichMessageButton(text=text, url=url, style=style)


def _rich_paragraph(text):
    return InputRichBlockParagraph(text=text)


def _rich_button_rows(buttons, row_size=2):
    """Split RichMessageButtons into valid 1-8-button rows."""
    blocks = []
    row_size = max(1, min(8, row_size))
    for start in range(0, len(buttons), row_size):
        row = buttons[start:start + row_size]
        if row:
            blocks.append(InputRichBlockButtons(buttons=row))
    return blocks


def _rich_gallery_heading(text):
    return InputRichBlockSectionHeading(text=text, size=3)


async def _edit_rich_message(chat_id: int, message_id: int, blocks):
    rich_message = InputRichMessage(blocks=blocks)
    return await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        rich_message=rich_message,
    )


async def _send_rich_message(chat_id: int, blocks):
    return await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=InputRichMessage(blocks=blocks),
    )


async def _gallery_get_subjects(chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT DISTINCT subject FROM schedule "
            "WHERE chat_id = ? AND subject IS NOT NULL AND TRIM(subject) != '' AND subject != '—' "
            "ORDER BY subject COLLATE NOCASE",
            (chat_id,),
        )
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def _gallery_subject_for_index(chat_id: int, index: int):
    subjects = await _gallery_get_subjects(chat_id)
    if index < 0 or index >= len(subjects):
        return None
    return subjects[index]


async def _gallery_subject_index(chat_id: int, subject: str):
    subjects = await _gallery_get_subjects(chat_id)
    try:
        return subjects.index(subject)
    except ValueError:
        return None


async def _gallery_get_photos(chat_id: int, week_start, subject=None):
    week_start = _gallery_normalize_week(week_start.isoformat() if hasattr(week_start, "isoformat") else week_start)
    week_end = week_start + timedelta(days=6)
    query = (
        "SELECT id, photo_date, subject, file_id, uploaded_at "
        "FROM gallery_photos WHERE chat_id = ? AND photo_date BETWEEN ? AND ?"
    )
    params = [chat_id, week_start.isoformat(), week_end.isoformat()]
    if subject is not None:
        query += " AND subject = ?"
        params.append(subject)
    query += " ORDER BY photo_date, subject COLLATE NOCASE, uploaded_at, id"
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(query, tuple(params))
        return await cursor.fetchall()


async def _gallery_menu_blocks(home_date: str):
    return [
        _rich_gallery_heading("🖼 Школьная галерея"),
        _rich_paragraph("Выбери, как смотреть фотографии."),
        InputRichBlockButtons(buttons=[_rich_button("🗓 Вся галерея по неделям", f"gal_all:{_gallery_current_week_start().isoformat()}:0:{home_date}")]),
        InputRichBlockButtons(buttons=[_rich_button("📚 Галерея по предметам", f"gal_subjects:{home_date}")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ Назад к ДЗ", f"gal_back_hw:{home_date}")]),
    ]


async def _gallery_subjects_blocks(chat_id: int, home_date: str):
    subjects = await _gallery_get_subjects(chat_id)
    blocks = [
        _rich_gallery_heading("📚 Галерея по предметам"),
        _rich_paragraph("Выбери предмет, чтобы посмотреть фотографии по неделям."),
    ]
    if not subjects:
        blocks.append(_rich_paragraph("В расписании пока нет предметов."))
    else:
        buttons = [
            _rich_button(f"📖 {subject}", f"gal_subject:{index}:{_gallery_current_week_start().isoformat()}:0:{home_date}")
            for index, subject in enumerate(subjects)
        ]
        blocks.extend(_rich_button_rows(buttons, row_size=2))
    blocks.extend([
        InputRichBlockButtons(buttons=[_rich_button("📸 Загрузить фото", f"gal_upload:subjects:{home_date}", "success")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ Назад", f"gal_menu:{home_date}")]),
    ])
    return blocks


async def _gallery_view_blocks(chat_id: int, week_value: str, page: int, home_date: str, subject_index=None):
    week_start = _gallery_normalize_week(week_value)
    current_week = _gallery_current_week_start()
    selected_subject = None
    subjects = None
    if subject_index is not None:
        subjects = await _gallery_get_subjects(chat_id)
        if subject_index < 0 or subject_index >= len(subjects):
            return [
                _rich_gallery_heading("📚 Галерея по предметам"),
                _rich_paragraph("Предмет больше не найден в расписании."),
                InputRichBlockButtons(buttons=[_rich_button("⬅️ К предметам", f"gal_subjects:{home_date}")]),
            ]
        selected_subject = subjects[subject_index]

    photos = await _gallery_get_photos(chat_id, week_start, selected_subject)
    total_pages = max(1, (len(photos) + GALLERY_PHOTOS_PER_PAGE - 1) // GALLERY_PHOTOS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * GALLERY_PHOTOS_PER_PAGE
    visible_photos = photos[start:start + GALLERY_PHOTOS_PER_PAGE]

    title = "🖼 Галерея" if selected_subject is None else f"📚 {selected_subject}"
    blocks = [
        _rich_gallery_heading(title),
        _rich_paragraph(f"Неделя: {_gallery_week_label(week_start)}"),
    ]

    if not visible_photos:
        if selected_subject is None:
            blocks.append(_rich_paragraph("На этой неделе пока нет фотографий."))
        else:
            blocks.append(_rich_paragraph("Для этого предмета на выбранной неделе фотографий нет."))
    else:
        for _, photo_date, subject, file_id, uploaded_at in visible_photos:
            caption = f"{datetime.strptime(photo_date, '%Y-%m-%d').strftime('%d.%m.%Y')} · {subject}"
            blocks.append(
                InputRichBlockPhoto(
                    photo=InputMediaPhoto(media=file_id),
                    caption=RichBlockCaption(text=caption),
                )
            )

    # Pagination for weeks containing many photos.
    if total_pages > 1:
        page_buttons = []
        if page > 0:
            if selected_subject is None:
                page_buttons.append(_rich_button("⬅️ Фото", f"gal_all:{week_start.isoformat()}:{page - 1}:{home_date}"))
            else:
                page_buttons.append(_rich_button("⬅️ Фото", f"gal_subject:{subject_index}:{week_start.isoformat()}:{page - 1}:{home_date}"))
        page_buttons.append(_rich_button(f"Фото {page + 1}/{total_pages}", "gal_page_info", "primary"))
        if page + 1 < total_pages:
            if selected_subject is None:
                page_buttons.append(_rich_button("Фото ➡️", f"gal_all:{week_start.isoformat()}:{page + 1}:{home_date}"))
            else:
                page_buttons.append(_rich_button("Фото ➡️", f"gal_subject:{subject_index}:{week_start.isoformat()}:{page + 1}:{home_date}"))
        blocks.extend(_rich_button_rows(page_buttons, row_size=3))

    week_buttons = []
    if week_start > current_week:
        prev_week = week_start - timedelta(days=7)
        if selected_subject is None:
            week_buttons.append(_rich_button("⬅️ Неделя", f"gal_all:{prev_week.isoformat()}:0:{home_date}"))
        else:
            week_buttons.append(_rich_button("⬅️ Неделя", f"gal_subject:{subject_index}:{prev_week.isoformat()}:0:{home_date}"))
    next_week = week_start + timedelta(days=7)
    if selected_subject is None:
        week_buttons.append(_rich_button("Неделя ➡️", f"gal_all:{next_week.isoformat()}:0:{home_date}"))
    else:
        week_buttons.append(_rich_button("Неделя ➡️", f"gal_subject:{subject_index}:{next_week.isoformat()}:0:{home_date}"))
    if week_buttons:
        blocks.extend(_rich_button_rows(week_buttons, row_size=2))

    if selected_subject is None:
        upload_callback = f"gal_upload:all:{week_start.isoformat()}:{page}:{home_date}"
        back_callback = f"gal_menu:{home_date}"
        back_label = "⬅️ Меню галереи"
    else:
        upload_callback = f"gal_upload:subject:{subject_index}:{week_start.isoformat()}:{page}:{home_date}"
        back_callback = f"gal_subjects:{home_date}"
        back_label = "⬅️ К предметам"
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("📸 Загрузить фото", upload_callback, "success")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button(back_label, back_callback)]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ К домашнему заданию", f"gal_back_hw:{home_date}")]))
    return blocks


async def _gallery_upload_subject_picker_blocks(chat_id: int, home_date: str):
    subjects = await _gallery_get_subjects(chat_id)
    blocks = [
        _rich_gallery_heading("📸 Загрузка фотографий"),
        _rich_paragraph("Сначала выбери предмет."),
    ]
    if not subjects:
        blocks.append(_rich_paragraph("Не удалось найти предметы в расписании."))
    else:
        blocks.extend(_rich_button_rows([
            _rich_button(subject, f"galup_sub:{index}")
            for index, subject in enumerate(subjects)
        ], row_size=2))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("❌ Отмена", "gal_upload_cancel", "danger")]))
    return blocks


async def _gallery_upload_date_picker_blocks(chat_id: int, subject: str):
    today = datetime.now(MINSK_TZ).date()
    current_monday = _gallery_current_week_start()
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT DISTINCT day_int FROM schedule WHERE chat_id = ? AND subject = ?",
            (chat_id, subject),
        )
        subject_days = {int(row[0]) for row in await cursor.fetchall()}
    dates = []
    for offset in range(GALLERY_DATE_HORIZON_DAYS):
        day = current_monday + timedelta(days=offset)
        if day.weekday() in subject_days:
            dates.append(day)
    blocks = [
        _rich_gallery_heading("📅 Выбор даты"),
        _rich_paragraph(f"Предмет: {subject}\nВыбери дату, к которой относятся фотографии."),
    ]
    if not dates:
        blocks.append(_rich_paragraph("Для этого предмета нет занятий в доступном периоде."))
    else:
        labels = [
            _rich_button(f"{day.strftime('%d.%m')} · {get_day_name(day)}", f"galup_date:{day.isoformat()}")
            for day in dates
        ]
        blocks.extend(_rich_button_rows(labels, row_size=2))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("❌ Отмена", "gal_upload_cancel", "danger")]))
    return blocks


async def _gallery_upload_prompt_blocks(subject: str, photo_date: str):
    date_obj = datetime.strptime(photo_date, "%Y-%m-%d").date()
    return [
        _rich_gallery_heading("📸 Загрузка фотографий"),
        _rich_paragraph(
            f"Предмет: {subject}\nДата: {date_obj.strftime('%d.%m.%Y')}\n\n"
            "Отправь одну фотографию или несколько фотографий/альбомов. "
            "Когда закончишь, нажми «✅ Завершить загрузку»."
        ),
        InputRichBlockButtons(buttons=[_rich_button("✅ Завершить загрузку", "gal_upload_finish", "success")]),
        InputRichBlockButtons(buttons=[_rich_button("❌ Отмена", "gal_upload_cancel", "danger")]),
    ]


async def _insert_gallery_photos(db, gallery_chat_id: int, photo_date: str, subject: str, messages):
    inserted = 0
    for message in messages:
        if not getattr(message, "photo", None):
            continue
        photo = message.photo[-1]
        cursor = await db.execute(
            """INSERT OR IGNORE INTO gallery_photos
               (chat_id, photo_date, subject, file_id, file_unique_id,
                source_chat_id, source_message_id, media_group_id, uploader_id, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(gallery_chat_id), photo_date, subject, photo.file_id,
                getattr(photo, "file_unique_id", None), int(message.chat.id), int(message.message_id),
                getattr(message, "media_group_id", None),
                getattr(getattr(message, "from_user", None), "id", None),
                datetime.now(MINSK_TZ).isoformat(timespec="seconds"),
            ),
        )
        if cursor.rowcount > 0:
            inserted += cursor.rowcount
            await _insert_audit_event_db(
                db, category="data", event_type="gallery_photo_uploaded",
                actor_id=getattr(getattr(message, "from_user", None), "id", None),
                actor_name=_audit_user_name(getattr(message, "from_user", None)),
                object_type="gallery_photo", object_id=str(cursor.lastrowid),
                result="success", details={"date": photo_date, "subject": subject},
                admin_id=ADMIN_ID if getattr(getattr(message, "from_user", None), "id", None) == ADMIN_ID else None,
                retention_days=AUDIT_DATA_RETENTION_DAYS,
            )
    return inserted


async def _save_gallery_photos(gallery_chat_id: int, photo_date: str, subject: str, messages):
    async with aiosqlite.connect(DB_NAME) as db:
        inserted = await _insert_gallery_photos(db, gallery_chat_id, photo_date, subject, messages)
        await db.commit()
    return inserted


async def cleanup_gallery_old_photos():
    """Remove gallery entries older than the current Minsk calendar week."""
    current_monday = _gallery_current_week_start().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM gallery_photos WHERE photo_date < ?", (current_monday,))
        await db.commit()


async def _gallery_render_context_blocks(chat_id: int, context: dict):
    mode = context.get("mode", "menu")
    home_date = context.get("home_date") or datetime.now(MINSK_TZ).date().isoformat()
    if mode == "all":
        return await _gallery_view_blocks(
            chat_id, context.get("week", _gallery_current_week_start().isoformat()),
            int(context.get("page", 0)), home_date,
        )
    if mode == "subjects":
        return await _gallery_subjects_blocks(chat_id, home_date)
    if mode == "subject":
        idx = context.get("subject_index")
        if idx is None:
            idx = await _gallery_subject_index(chat_id, context.get("subject", ""))
        if idx is None:
            return await _gallery_subjects_blocks(chat_id, home_date)
        return await _gallery_view_blocks(
            chat_id, context.get("week", _gallery_current_week_start().isoformat()),
            int(context.get("page", 0)), home_date, int(idx),
        )
    return await _gallery_menu_blocks(home_date)


async def _gallery_restore_return_view(callback: types.CallbackQuery, return_context: dict):
    blocks = await _gallery_render_context_blocks(callback.message.chat.id, return_context)
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)


async def _gallery_open_upload(callback: types.CallbackQuery, state: FSMContext, return_context: dict, subject=None):
    await state.clear()
    await state.update_data(
        gallery_target_chat_id=callback.message.chat.id,
        gallery_return_context=return_context,
        gallery_homework_date=return_context.get("home_date") or datetime.now(MINSK_TZ).date().isoformat(),
    )
    if subject is not None:
        await state.update_data(gallery_upload_subject=subject)
        await state.set_state(GalleryUploadState.choosing_date)
        blocks = await _gallery_upload_date_picker_blocks(callback.message.chat.id, subject)
    else:
        await state.set_state(GalleryUploadState.choosing_subject)
        blocks = await _gallery_upload_subject_picker_blocks(callback.message.chat.id, return_context.get("home_date"))
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)


@dp.callback_query(F.data.startswith("gal_open:"))
async def gallery_open(callback: types.CallbackQuery):
    home_date = callback.data.split(":", 1)[1]
    try:
        datetime.strptime(home_date, "%Y-%m-%d")
    except ValueError:
        home_date = datetime.now(MINSK_TZ).date().isoformat()
    try:
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _gallery_menu_blocks(home_date))
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось открыть школьную галерею: %s", e)
        await callback.answer("Не удалось открыть галерею. Проверь версию aiogram и Bot API.", show_alert=True)


@dp.callback_query(F.data.startswith("gal_back_hw:"))
async def gallery_back_to_homework(callback: types.CallbackQuery):
    try:
        date_str = callback.data.split(":", 1)[1]
        current_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, IndexError):
        current_date = datetime.now(MINSK_TZ).date()
    try:
        await callback.message.edit_text(
            await generate_schedule_text(callback.message.chat.id, current_date),
            reply_markup=get_keyboard(callback.message.chat.id, current_date),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось вернуться к ДЗ из галереи: %s", e)
        await callback.answer("Не удалось вернуться к домашнему заданию.", show_alert=True)


@dp.callback_query(F.data.startswith("gal_menu:"))
async def gallery_menu_callback(callback: types.CallbackQuery):
    home_date = callback.data.split(":", 1)[1]
    try:
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _gallery_menu_blocks(home_date))
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось показать меню галереи: %s", e)
        await callback.answer("Не удалось открыть меню галереи.", show_alert=True)


@dp.callback_query(F.data.startswith("gal_subjects:"))
async def gallery_subjects_callback(callback: types.CallbackQuery):
    home_date = callback.data.split(":", 1)[1]
    try:
        blocks = await _gallery_subjects_blocks(callback.message.chat.id, home_date)
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось показать предметы галереи: %s", e)
        await callback.answer("Не удалось загрузить список предметов.", show_alert=True)


@dp.callback_query(F.data == "gal_page_info")
async def gallery_page_info(callback: types.CallbackQuery):
    await callback.answer("Используй стрелки, чтобы переключать фотографии.")


@dp.callback_query(F.data.startswith("gal_all:"))
async def gallery_all_week_callback(callback: types.CallbackQuery):
    try:
        _, week_value, page_value, home_date = callback.data.split(":", 3)
        week_start = _gallery_normalize_week(week_value)
        page = int(page_value)
        blocks = await _gallery_view_blocks(callback.message.chat.id, week_start.isoformat(), page, home_date)
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
        await callback.answer()
    except Exception as e:
        logging.exception("Ошибка навигации общей галереи: %s", e)
        await callback.answer("Не удалось открыть выбранную неделю.", show_alert=True)


@dp.callback_query(F.data.startswith("gal_subject:"))
async def gallery_subject_week_callback(callback: types.CallbackQuery):
    try:
        _, index_value, week_value, page_value, home_date = callback.data.split(":", 4)
        subject_index = int(index_value)
        week_start = _gallery_normalize_week(week_value)
        page = int(page_value)
        blocks = await _gallery_view_blocks(
            callback.message.chat.id, week_start.isoformat(), page, home_date, subject_index
        )
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
        await callback.answer()
    except Exception as e:
        logging.exception("Ошибка навигации галереи предмета: %s", e)
        await callback.answer("Не удалось открыть фотографии предмета.", show_alert=True)


@dp.callback_query(F.data.startswith("gal_upload:"))
async def gallery_upload_start_callback(callback: types.CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        if len(parts) == 3 and parts[1] == "subjects":
            _, _, home_date = parts
            return_context = {"mode": "subjects", "home_date": home_date}
            await _gallery_open_upload(callback, state, return_context)
        elif len(parts) == 5 and parts[1] == "all":
            _, _, week, page, home_date = parts
            return_context = {"mode": "all", "week": _gallery_normalize_week(week).isoformat(), "page": int(page), "home_date": home_date}
            await _gallery_open_upload(callback, state, return_context)
        elif len(parts) == 6 and parts[1] == "subject":
            _, _, index_value, week, page, home_date = parts
            subject_index = int(index_value)
            subject = await _gallery_subject_for_index(callback.message.chat.id, subject_index)
            if subject is None:
                await callback.answer("Предмет больше не найден в расписании.", show_alert=True)
                return
            return_context = {"mode": "subject", "subject_index": subject_index, "subject": subject,
                              "week": _gallery_normalize_week(week).isoformat(), "page": int(page), "home_date": home_date}
            await _gallery_open_upload(callback, state, return_context, subject=subject)
        else:
            await callback.answer("Некорректные данные загрузки.", show_alert=True)
            return
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось начать загрузку фотографий: %s", e)
        await callback.answer("Не удалось начать загрузку фотографий.", show_alert=True)


@dp.callback_query(GalleryUploadState.choosing_subject, F.data.startswith("galup_sub:"))
async def gallery_upload_subject_chosen(callback: types.CallbackQuery, state: FSMContext):
    try:
        index = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        chat_id = int(data.get("gallery_target_chat_id", callback.message.chat.id))
        subject = await _gallery_subject_for_index(chat_id, index)
        if subject is None:
            await callback.answer("Предмет больше не найден.", show_alert=True)
            return
        await state.update_data(gallery_upload_subject=subject)
        await state.set_state(GalleryUploadState.choosing_date)
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id,
                                 await _gallery_upload_date_picker_blocks(chat_id, subject))
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось выбрать предмет для галереи: %s", e)
        await callback.answer("Не удалось выбрать предмет.", show_alert=True)


@dp.callback_query(GalleryUploadState.choosing_date, F.data.startswith("galup_date:"))
async def gallery_upload_date_chosen(callback: types.CallbackQuery, state: FSMContext):
    try:
        date_str = callback.data.split(":", 1)[1]
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if selected_date < _gallery_current_week_start():
            await callback.answer("Нельзя загружать фотографии за прошлые недели.", show_alert=True)
            return
        data = await state.get_data()
        chat_id = int(data.get("gallery_target_chat_id", callback.message.chat.id))
        subject = data.get("gallery_upload_subject")
        if not subject or not await _gallery_subject_scheduled_on_date(chat_id, subject, selected_date):
            await callback.answer("Этот предмет не стоит в расписании на выбранную дату.", show_alert=True)
            return
        await state.update_data(gallery_photo_date=date_str)
        await state.set_state(GalleryUploadState.waiting_for_photos)
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id,
                                 await _gallery_upload_prompt_blocks(subject, date_str))
        await callback.answer()
    except Exception as e:
        logging.exception("Не удалось выбрать дату фотографий: %s", e)
        await callback.answer("Не удалось выбрать дату.", show_alert=True)


async def _gallery_subject_scheduled_on_date(chat_id: int, subject: str, selected_date):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT 1 FROM schedule WHERE chat_id = ? AND day_int = ? AND subject = ? LIMIT 1",
            (chat_id, selected_date.weekday(), subject),
        )
        return await cursor.fetchone() is not None


@dp.message(GalleryUploadState.waiting_for_photos, F.photo)
async def gallery_upload_photo_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = (int(message.chat.id), int(message.from_user.id))
    session = GALLERY_UPLOAD_BUFFER.get(key)
    if session is None:
        session = {
            "messages": {},
            "gallery_chat_id": int(data.get("gallery_target_chat_id", message.chat.id)),
            "photo_date": data.get("gallery_photo_date"),
            "subject": data.get("gallery_upload_subject"),
        }
        GALLERY_UPLOAD_BUFFER[key] = session
    if message.message_id not in session["messages"]:
        session["messages"][message.message_id] = message


@dp.message(GalleryUploadState.waiting_for_photos)
async def gallery_upload_non_photo_received(message: types.Message, state: FSMContext):
    await message.answer(
        "Пришли фотографию или альбом. Когда закончишь, нажми «✅ Завершить загрузку» в сообщении галереи."
    )


@dp.callback_query(F.data == "gal_upload_finish", GalleryUploadState.waiting_for_photos)
async def gallery_upload_finish_callback(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    key = (int(callback.message.chat.id), int(callback.from_user.id))
    session = GALLERY_UPLOAD_BUFFER.get(key)
    if not session or not session.get("messages"):
        await callback.answer("Сначала отправь хотя бы одну фотографию.", show_alert=True)
        return
    messages = list(session["messages"].values())
    try:
        inserted = await _save_gallery_photos(
            int(data.get("gallery_target_chat_id", session["gallery_chat_id"])),
            data.get("gallery_photo_date", session["photo_date"]),
            data.get("gallery_upload_subject", session["subject"]),
            messages,
        )
        GALLERY_UPLOAD_BUFFER.pop(key, None)
        return_context = data.get("gallery_return_context", {"mode": "menu"})
        await state.clear()
        await _edit_rich_message(
            callback.message.chat.id, callback.message.message_id,
            await _gallery_render_context_blocks(callback.message.chat.id, return_context),
        )
        if inserted:
            await callback.answer(f"Добавлено фото: {inserted}")
            await bot.send_message(callback.message.chat.id, f"✅ В галерею добавлено фотографий: {inserted}.")
        else:
            await callback.answer("Эти фотографии уже были сохранены.")
    except Exception as e:
        logging.exception("Не удалось сохранить фотографии в галерее: %s", e)
        try:
            await callback.answer("Не удалось сохранить фотографии. Попробуй ещё раз.", show_alert=True)
        except Exception:
            pass


@dp.callback_query(F.data == "gal_upload_cancel")
async def gallery_upload_cancel_callback(callback: types.CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current not in {
        GalleryUploadState.choosing_subject.state,
        GalleryUploadState.choosing_date.state,
        GalleryUploadState.waiting_for_photos.state,
    }:
        await callback.answer("Загрузка уже завершена или устарела.", show_alert=True)
        return
    data = await state.get_data()
    key = (int(callback.message.chat.id), int(callback.from_user.id))
    GALLERY_UPLOAD_BUFFER.pop(key, None)
    return_context = data.get("gallery_return_context", {"mode": "menu"})
    await state.clear()
    try:
        await _edit_rich_message(
            callback.message.chat.id, callback.message.message_id,
            await _gallery_render_context_blocks(callback.message.chat.id, return_context),
        )
        await callback.answer("Загрузка отменена.")
    except Exception as e:
        logging.exception("Не удалось отменить загрузку галереи: %s", e)
        await callback.answer("Загрузка отменена.")


async def _process_homework_photo_messages(messages, state: FSMContext, data: dict):
    chat_id = int(data.get("target_chat_id", ANON_CHAT_ID))
    date_str = data.get("target_date")
    subject = data.get("subject")
    group_number = int(data.get("group_number", 0) or 0)
    action = data.get("homework_action")
    if not date_str or not subject:
        await messages[0].answer("Не удалось определить предмет или дату. Начни заполнение ДЗ заново.")
        await state.clear()
        return

    captions = [(_normalize_homework_text(getattr(message, "caption", ""))) for message in messages]
    task_text = next((caption for caption in captions if caption), "")
    try:
        existing_task = None
        if task_text:
            existing_task = await _get_homework_task(chat_id, date_str, subject, group_number)
            if action in HW_TAGS and not _homework_is_filled(existing_task):
                action = None
        async with aiosqlite.connect(DB_NAME) as db:
            inserted_photos = await _insert_gallery_photos(db, chat_id, date_str, subject, messages)
            if task_text:
                if action == "replace":
                    final_task = _replace_homework_text(task_text)
                elif action == "append":
                    final_task = _append_homework_text(existing_task, task_text)
                else:
                    final_task = task_text
                hw_cursor = await db.execute(
                    "SELECT task FROM homework WHERE chat_id = ? AND date_str = ? AND subject = ? AND group_number = ?",
                    (chat_id, date_str, subject, group_number),
                )
                hw_old_row = await hw_cursor.fetchone()
                hw_old_task = hw_old_row[0] if hw_old_row else None
                await _insert_homework_history_db(
                    db, chat_id, date_str, subject, group_number, action or ("edit" if hw_old_task else "create"),
                    hw_old_task, final_task,
                    getattr(getattr(messages[0], "from_user", None), "id", None),
                    _audit_user_name(getattr(messages[0], "from_user", None)),
                    ADMIN_ID if getattr(getattr(messages[0], "from_user", None), "id", None) == ADMIN_ID else None,
                )
                await db.execute(
                    """INSERT OR REPLACE INTO homework (chat_id, date_str, subject, task, group_number)
                       VALUES (?, ?, ?, ?, ?)""",
                    (chat_id, date_str, subject, final_task, group_number),
                )
                await _insert_audit_event_db(
                    db, category="data", event_type=f"homework_{action or ('edit' if hw_old_task else 'create')}",
                    actor_id=getattr(getattr(messages[0], "from_user", None), "id", None),
                    actor_name=_audit_user_name(getattr(messages[0], "from_user", None)),
                    object_type="homework", object_id=f"{date_str}|{subject}|{group_number}",
                    result="success", details={"date": date_str, "subject": subject, "group_number": group_number,
                                                  "old_length": len(hw_old_task or ""), "new_length": len(final_task)},
                    admin_id=ADMIN_ID if getattr(getattr(messages[0], "from_user", None), "id", None) == ADMIN_ID else None,
                    retention_days=AUDIT_DATA_RETENTION_DAYS,
                )
            await db.commit()

        if task_text:
            group_note = f" для {group_number} группы" if group_number in (1, 2) else ""
            await messages[0].answer(
                f"✅ Домашнее задание по {subject}{group_note} заполнено.\n"
                f"📸 В галерею добавлено фотографий: {inserted_photos}."
            )
            await state.clear()
        else:
            await messages[0].answer(
                f"📸 Фотографии сохранены в галерее ({inserted_photos}).\n"
                "Чтобы записать ДЗ автоматически, добавь текст задания в подпись к фото/альбому "
                "или отправь текст отдельным сообщением."
            )
    except Exception as e:
        logging.exception("Не удалось сохранить фото при заполнении ДЗ: %s", e)
        await messages[0].answer("❌ Не удалось сохранить фото/ДЗ. Попробуй ещё раз.")


async def _queue_homework_photo_album(message: types.Message, state: FSMContext, data: dict):
    key = (int(message.chat.id), int(message.from_user.id), str(message.media_group_id))
    album = HW_PHOTO_ALBUM_BUFFERS.get(key)
    if album is None:
        album = {"messages": {}, "state": state, "data": dict(data), "task": None}
        HW_PHOTO_ALBUM_BUFFERS[key] = album
    album["messages"][message.message_id] = message
    old_task = album.get("task")
    if old_task and not old_task.done():
        old_task.cancel()
    album["task"] = asyncio.create_task(_flush_homework_photo_album_after_delay(key))


async def _flush_homework_photo_album_after_delay(key):
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        return
    album = HW_PHOTO_ALBUM_BUFFERS.pop(key, None)
    if not album:
        return
    messages = sorted(album["messages"].values(), key=lambda item: item.message_id)
    await _process_homework_photo_messages(messages, album["state"], album["data"])



# =============================================================================
# ЦЕНТР КОНТРОЛЯ (v12): аудит, история версий, менеджеры ДЗ/галереи, backups
# =============================================================================

AUDIT_NAV_RETENTION_DAYS = 14
AUDIT_DATA_RETENTION_DAYS = 90
AUDIT_MAX_PAGE_SIZE = 8
AUDIT_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "school_bot_backups")
AUDIT_BACKUP_MAX_FILES = 5


class ControlState(StatesGroup):
    waiting_for_homework_text = State()
    waiting_for_audit_filter = State()


def _audit_now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit_user_name(user):
    if user is None:
        return None
    username = getattr(user, "username", None)
    if username:
        return str(username)[:128]
    full_name = getattr(user, "full_name", None)
    return str(full_name)[:128] if full_name else None


def _audit_safe_json(value):
    try:
        return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))[:4000]
    except Exception:
        return "{}"


async def _insert_audit_event_db(
    db, *, category, event_type, actor_id=None, actor_name=None, object_type=None,
    object_id=None, result="success", details=None, admin_id=None, retention_days=None,
):
    if retention_days is None:
        retention_days = AUDIT_NAV_RETENTION_DAYS if category in ("navigation", "anonymous_aggregate") else AUDIT_DATA_RETENTION_DAYS
    await db.execute(
        """INSERT INTO audit_events
           (timestamp_utc, category, event_type, actor_id, actor_name, object_type,
            object_id, result, details_json, admin_id, retention_days)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_audit_now_utc(), str(category)[:40], str(event_type)[:100], actor_id,
         actor_name, object_type, str(object_id)[:250] if object_id is not None else None,
         str(result)[:30], _audit_safe_json(details), admin_id, int(retention_days)),
    )


async def write_audit_event(**kwargs):
    """Best-effort audit logging: logging failures never block core bot actions."""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await _insert_audit_event_db(db, **kwargs)
            await db.commit()
    except Exception:
        logging.exception("Не удалось записать audit event")


async def _insert_homework_history_db(
    db, chat_id, date_str, subject, group_number, action, old_task, new_task,
    actor_id=None, actor_name=None, admin_id=None,
):
    await db.execute(
        """INSERT INTO homework_history
           (timestamp_utc, chat_id, date_str, subject, group_number, action,
            actor_id, actor_name, old_task, new_task, admin_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_audit_now_utc(), int(chat_id), str(date_str), str(subject), int(group_number or 0),
         str(action)[:40], actor_id, actor_name, old_task, new_task, admin_id),
    )


async def _save_homework_audited(
    *, chat_id, date_str, subject, group_number, new_task, action, actor_id=None,
    actor_name=None, admin_id=None,
):
    """Persist an HW change, snapshot its previous value, and audit it atomically."""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT task FROM homework WHERE chat_id = ? AND date_str = ? AND subject = ? AND group_number = ?",
            (int(chat_id), str(date_str), str(subject), int(group_number or 0)),
        )
        row = await cursor.fetchone()
        old_task = row[0] if row else None
        actual_action = action if action in ("append", "replace", "admin_edit", "remove_tag") else ("edit" if old_task is not None else "create")
        stored_task = None if actual_action == "remove_tag" and not str(new_task or "").strip() else str(new_task)
        await _insert_homework_history_db(
            db, chat_id, date_str, subject, group_number, actual_action,
            old_task, stored_task, actor_id, actor_name, admin_id,
        )
        if stored_task is None:
            await db.execute(
                "DELETE FROM homework WHERE chat_id=? AND date_str=? AND subject=? AND group_number=?",
                (int(chat_id), str(date_str), str(subject), int(group_number or 0)),
            )
        else:
            await db.execute(
                """INSERT OR REPLACE INTO homework (chat_id, date_str, subject, task, group_number)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(chat_id), str(date_str), str(subject), stored_task, int(group_number or 0)),
            )
        await _insert_audit_event_db(
            db, category="data", event_type=f"homework_{actual_action}", actor_id=actor_id,
            actor_name=actor_name, object_type="homework",
            object_id=f"{date_str}|{subject}|{int(group_number or 0)}", result="success",
            details={"date": str(date_str), "subject": str(subject), "group_number": int(group_number or 0),
                     "old_length": len(old_task or ""), "new_length": len(stored_task or "")},
            admin_id=admin_id, retention_days=AUDIT_DATA_RETENTION_DAYS,
        )
        await db.commit()
    return old_task


async def _audit_cleanup_expired():
    now = datetime.now(timezone.utc)
    data_cutoff = (now - timedelta(days=AUDIT_DATA_RETENTION_DAYS)).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "DELETE FROM audit_events WHERE julianday(timestamp_utc) + (retention_days * 1.0) < julianday('now')"
        )
        deleted_events = cursor.rowcount if cursor.rowcount >= 0 else 0
        cursor = await db.execute("DELETE FROM homework_history WHERE timestamp_utc < ?", (data_cutoff,))
        deleted_history = cursor.rowcount if cursor.rowcount >= 0 else 0
        await db.execute(
            "INSERT OR REPLACE INTO control_status (key, value) VALUES ('last_audit_cleanup_utc', ?)",
            (_audit_now_utc(),),
        )
        await db.commit()
    return deleted_events, deleted_history


_original_init_db_v12 = init_db
async def init_db():
    await _original_init_db_v12()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            category TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor_id INTEGER,
            actor_name TEXT,
            object_type TEXT,
            object_id TEXT,
            result TEXT NOT NULL DEFAULT 'success',
            details_json TEXT NOT NULL DEFAULT '{}',
            admin_id INTEGER,
            retention_days INTEGER NOT NULL DEFAULT 14
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp_utc DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_category_time ON audit_events(category, timestamp_utc DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor_time ON audit_events(actor_id, timestamp_utc DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_events(object_type, object_id)")
        await db.execute("""CREATE TABLE IF NOT EXISTS homework_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            date_str TEXT NOT NULL,
            subject TEXT NOT NULL,
            group_number INTEGER NOT NULL DEFAULT 0,
            action TEXT NOT NULL,
            actor_id INTEGER,
            actor_name TEXT,
            old_task TEXT,
            new_task TEXT,
            admin_id INTEGER
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_hw_history_key ON homework_history(chat_id, date_str, subject, group_number, timestamp_utc DESC)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_hw_history_time ON homework_history(timestamp_utc DESC)")
        await db.execute("""CREATE TABLE IF NOT EXISTS audit_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS control_status (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        await db.execute("INSERT OR IGNORE INTO audit_settings(key, value) VALUES ('navigation_enabled', '1')")
        await db.execute("INSERT OR IGNORE INTO audit_settings(key, value) VALUES ('navigation_retention_days', '14')")
        await db.execute("INSERT OR IGNORE INTO audit_settings(key, value) VALUES ('data_retention_days', '90')")
        await db.commit()
        cur = await db.execute("SELECT key, value FROM audit_settings WHERE key IN ('navigation_retention_days','data_retention_days')")
        retention_rows = dict(await cur.fetchall())
    global AUDIT_NAV_RETENTION_DAYS, AUDIT_DATA_RETENTION_DAYS
    try:
        AUDIT_NAV_RETENTION_DAYS = max(1, min(365, int(retention_rows.get('navigation_retention_days', '14'))))
        AUDIT_DATA_RETENTION_DAYS = max(1, min(365, int(retention_rows.get('data_retention_days', '90'))))
    except (TypeError, ValueError):
        AUDIT_NAV_RETENTION_DAYS, AUDIT_DATA_RETENTION_DAYS = 14, 90
    # Apply retention once on startup, idempotently.
    await _audit_cleanup_expired()


async def _audit_get_setting(key, default=None):
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute("SELECT value FROM audit_settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return row[0] if row else default
    except Exception:
        return default


async def _audit_set_setting(key, value):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO audit_settings(key, value) VALUES (?, ?)", (key, str(value)))
        await db.commit()


class AuditMiddleware(BaseMiddleware):
    """Record received callbacks and relevant FSM/command inputs without message bodies."""
    async def __call__(self, handler, event, data):
        try:
            user = getattr(event, "from_user", None)
            user_id = getattr(user, "id", None)
            actor_name = _audit_user_name(user)
            state_obj = data.get("state")
            try:
                state_name = await state_obj.get_state() if state_obj else None
            except Exception:
                state_name = None

            is_callback = isinstance(event, types.CallbackQuery)
            is_inline_query = isinstance(event, types.InlineQuery)
            callback_data = getattr(event, "data", None) if is_callback else None
            message_text = getattr(event, "text", None) if isinstance(event, types.Message) else None
            chat = getattr(event, "chat", None)
            if chat is None and getattr(event, "message", None) is not None:
                chat = getattr(event.message, "chat", None)

            is_anonymous_context = (
                (state_name and "AnonymousState" in str(state_name))
                or (isinstance(callback_data, str) and "anon" in callback_data.casefold())
                or (isinstance(message_text, str) and message_text.strip().split(" ", 1)[0].casefold() in ("/anon", "/anonymous"))
            )

            should_log = is_callback or is_inline_query
            if isinstance(event, types.Message):
                is_command = bool(message_text and message_text.lstrip().startswith("/"))
                is_shortcut = bool(message_text and message_text.strip().casefold() in ("дз", "др", "рм", "меню"))
                should_log = is_command or is_shortcut or bool(state_name)

            result = "success"
            try:
                response = await handler(event, data)
            except Exception:
                result = "error"
                if should_log:
                    if is_anonymous_context:
                        await write_audit_event(category="anonymous_aggregate", event_type="anonymous_flow_event",
                                                result=result, details={"kind": "interaction"}, retention_days=AUDIT_NAV_RETENTION_DAYS)
                    else:
                        safe_action = callback_data[:180] if isinstance(callback_data, str) else None
                        event_type = "callback_click" if is_callback else ("inline_query" if is_inline_query else ("command" if message_text and message_text.strip().startswith("/") else "fsm_input"))
                        category = "admin" if user_id == ADMIN_ID and ((safe_action and safe_action.startswith(("admin_", "ctrl_", "rem_", "dr_"))) or (state_name and "ControlState" in str(state_name))) else "navigation"
                        await write_audit_event(category=category, event_type=event_type, actor_id=user_id,
                                                actor_name=actor_name, object_type="callback" if is_callback else "interaction",
                                                object_id=safe_action, result=result,
                                                details={"state": state_name, "chat_type": getattr(chat, "type", None)},
                                                admin_id=user_id if user_id == ADMIN_ID else None)
                raise

            if should_log:
                if is_anonymous_context:
                    # Never include actor IDs, anonymous message IDs, callback payloads, or content here.
                    await write_audit_event(category="anonymous_aggregate", event_type="anonymous_flow_event",
                                            result=result, details={"kind": "interaction"},
                                            retention_days=AUDIT_NAV_RETENTION_DAYS)
                else:
                    safe_action = callback_data[:180] if isinstance(callback_data, str) else None
                    if message_text and message_text.strip().startswith("/"):
                        safe_action = message_text.strip().split()[0][:60]
                    event_type = "callback_click" if is_callback else ("inline_query" if is_inline_query else ("command" if message_text and message_text.strip().startswith("/") else "fsm_input"))
                    is_admin_action = user_id == ADMIN_ID and (((safe_action or "").startswith(("admin_", "ctrl_", "dr_", "rem_"))) or (state_name and "ControlState" in str(state_name)))
                    category = "admin" if is_admin_action else ("data" if state_name and any(x in str(state_name) for x in ("HWState", "GalleryUploadState", "ReminderState", "SetupDrState")) else "navigation")
                    nav_enabled = (await _audit_get_setting("navigation_enabled", "1")) == "1"
                    if category != "navigation" or nav_enabled:
                        await write_audit_event(
                            category=category,
                            event_type=event_type,
                            actor_id=user_id,
                            actor_name=actor_name,
                            object_type="callback" if is_callback else ("inline_query" if is_inline_query else "interaction"),
                            object_id=safe_action,
                            result=result,
                            details={"state": state_name, "chat_type": getattr(chat, "type", None)},
                            admin_id=user_id if user_id == ADMIN_ID else None,
                            retention_days=AUDIT_NAV_RETENTION_DAYS if category == "navigation" else AUDIT_DATA_RETENTION_DAYS,
                        )
            return response
        except Exception:
            # Auditing must never mask a handler's result or exception.
            raise


# Attach audit middleware to callbacks and messages. Text content is never stored by this middleware.
dp.message.outer_middleware(AuditMiddleware())
dp.callback_query.outer_middleware(AuditMiddleware())
dp.inline_query.outer_middleware(AuditMiddleware())


async def _require_control_admin(callback: types.CallbackQuery) -> bool:
    # Audit, personal data, backups and restore UI must never be rendered into a group chat.
    if not callback.from_user or callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return False
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None) if message is not None else None
    if chat is None or getattr(chat, "type", None) != "private":
        await callback.answer("Центр контроля доступен только в личном чате с ботом.", show_alert=True)
        return False
    return True


async def _control_center_blocks():
    return [
        _rich_gallery_heading("🛡 Центр контроля"),
        _rich_paragraph("Журнал действий, управление данными, история изменений и восстановление."),
        InputRichBlockButtons(buttons=[_rich_button("📊 Обзор", "ctrl_overview")]),
        InputRichBlockButtons(buttons=[_rich_button("📋 Журнал действий", "ctrl_audit:0")]),
        InputRichBlockButtons(buttons=[_rich_button("📚 Управление ДЗ", "ctrl_hwdates:0")]),
        InputRichBlockButtons(buttons=[_rich_button("🖼 Управление галереей", "ctrl_gallery")]),
        InputRichBlockButtons(buttons=[_rich_button("🏷 Управление тегами", "ctrl_tags")]),
        InputRichBlockButtons(buttons=[_rich_button("🕓 История изменений", "ctrl_history:0")]),
        InputRichBlockButtons(buttons=[_rich_button("💾 Резервные копии", "ctrl_backups")]),
        InputRichBlockButtons(buttons=[_rich_button("⚙️ Состояние и настройки", "ctrl_status")]),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Назад", "ctrl_back")]),
    ]


_previous_get_admin_keyboard_v12 = get_admin_keyboard
def get_admin_keyboard():
    builder = InlineKeyboardBuilder.from_markup(_previous_get_admin_keyboard_v12())
    builder.row(InlineKeyboardButton(text="🛡 Центр контроля", callback_data="ctrl_center"))
    return builder.as_markup()


@dp.callback_query(F.data == "ctrl_center")
async def ctrl_center(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    try:
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _control_center_blocks())
    except Exception:
        logging.exception("Не удалось открыть центр контроля")
        await callback.answer("Не удалось открыть центр контроля.", show_alert=True)
        return
    await callback.answer()


@dp.callback_query(F.data == "ctrl_back")
async def ctrl_back(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, [
        _rich_gallery_heading("🛠 Админ-панель"), _rich_paragraph("Выбери нужный раздел."),
        *_rich_button_rows([
            _rich_button("🕵️ Анонимные сообщения", "admin_anon"),
            _rich_button("🎂 Дни рождения", "admin_birthdays"),
            _rich_button("⏰ Напоминания", "admin_reminders"),
            _rich_button("🧹 Старое ДЗ", "admin_hw_cleanup", "danger"),
            _rich_button("📚 Группы предметов", "admin_subject_groups"),
            _rich_button("🤖 Действия от имени бота", "admin_bot_actions"),
            _rich_button("🛡 Центр контроля", "ctrl_center"),
        ], row_size=2),
    ])
    await callback.answer()


@dp.callback_query(F.data == "ctrl_privacy")
async def ctrl_privacy_callback(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    await callback.answer("Команда /privacy показывает, какие события регистрируются.", show_alert=True)


@dp.message(Command("privacy"))
async def cmd_privacy(message: types.Message):
    nav_days = await _audit_get_setting("navigation_retention_days", "14")
    data_days = await _audit_get_setting("data_retention_days", "90")
    await message.answer(
        "<b>Информация о журналировании</b>\n\n"
        "Бот может сохранять время и тип команд, нажатий кнопок, изменений домашнего задания, загрузок фотографий и административных операций. "
        "Это нужно для исправления ошибок и восстановления данных. Доступ к подробному журналу есть только у администратора.\n\n"
        f"События навигации хранятся до {html.escape(str(nav_days))} дней; значимые операции и история изменений — до {html.escape(str(data_days))} дней. "
        "Обычный текст сообщений не записывается общим журналом. Для анонимных публикаций новый журнал не сохраняет ID автора вместе с ID публикации или её содержимым.\n\n"
        "Бот регистрирует только те действия, о которых получает обновления от Telegram; открытия внешних ссылок без callback-события ему недоступны.",
        parse_mode="HTML",
    )


async def _control_overview_blocks():
    today_utc = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM audit_events WHERE substr(timestamp_utc,1,10)=?", (today_utc,))
        events_today = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM audit_events WHERE result='error' AND timestamp_utc >= ?", ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
        errors_24h = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM homework WHERE chat_id=?", (ANON_CHAT_ID,))
        hw_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM gallery_photos WHERE chat_id=?", (ANON_CHAT_ID,))
        photo_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT value FROM control_status WHERE key='last_gallery_cleanup_utc'")
        cleanup_row = await cur.fetchone()
        cur = await db.execute("SELECT value FROM control_status WHERE key='last_backup_path'")
        backup_row = await cur.fetchone()
    return [
        _rich_gallery_heading("📊 Обзор"),
        _rich_paragraph(f"Событий за сегодня (UTC): {events_today}"),
        _rich_paragraph(f"Ошибок за последние 24 часа: {errors_24h}"),
        _rich_paragraph(f"Записей ДЗ: {hw_count}"),
        _rich_paragraph(f"Фотографий в галерее: {photo_count}"),
        _rich_paragraph(f"Последняя очистка галереи: {cleanup_row[0] if cleanup_row else 'не записана'}"),
        _rich_paragraph(f"Последняя резервная копия: {os.path.basename(backup_row[0]) if backup_row else 'не создавалась'}"),
        InputRichBlockButtons(buttons=[_rich_button("📋 Журнал", "ctrl_audit:0")]),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля", "ctrl_center")]),
    ]


@dp.callback_query(F.data == "ctrl_overview")
async def ctrl_overview(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _control_overview_blocks())
    await callback.answer()


def _audit_display_time(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MINSK_TZ).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(value)


async def _control_audit_page_blocks(page: int, filter_values: dict):
    page = max(0, int(page))
    offset = page * AUDIT_MAX_PAGE_SIZE
    clauses = []
    params = []
    category = filter_values.get("audit_filter_category")
    actor_id = filter_values.get("audit_filter_user")
    subject = filter_values.get("audit_filter_subject")
    date_value = filter_values.get("audit_filter_date")
    hw_date_value = filter_values.get("audit_filter_hwdate")
    result_value = filter_values.get("audit_filter_result")
    admin_only = bool(filter_values.get("audit_filter_admin"))
    if category and category != "all":
        clauses.append("category = ?"); params.append(category)
    if actor_id is not None:
        clauses.append("actor_id = ?"); params.append(int(actor_id))
    if subject:
        clauses.append("(details_json LIKE ? OR object_id LIKE ?)")
        params.extend([f"%{subject}%", f"%{subject}%"])
    if hw_date_value:
        clauses.append("(details_json LIKE ? OR object_id LIKE ?)")
        params.extend([f"%{hw_date_value}%", f"{hw_date_value}|%"])
    if date_value:
        # The stored timestamp is UTC. Convert the requested Minsk calendar day into a UTC interval.
        local_day = datetime.strptime(date_value, "%Y-%m-%d").replace(tzinfo=MINSK_TZ)
        next_local_day = local_day + timedelta(days=1)
        utc_start = local_day.astimezone(timezone.utc).isoformat(timespec="seconds")
        utc_end = next_local_day.astimezone(timezone.utc).isoformat(timespec="seconds")
        clauses.append("timestamp_utc >= ? AND timestamp_utc < ?")
        params.extend([utc_start, utc_end])
    if result_value and result_value != "all":
        clauses.append("result = ?"); params.append(result_value)
    if admin_only:
        clauses.append("admin_id IS NOT NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM audit_events" + where, tuple(params))
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT id,timestamp_utc,category,event_type,actor_id,actor_name,object_type,object_id,result,details_json "
            "FROM audit_events" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params + [AUDIT_MAX_PAGE_SIZE, offset]),
        )
        rows = await cur.fetchall()
    filter_labels=[]
    if category and category != "all": filter_labels.append(f"категория: {category}")
    if actor_id is not None: filter_labels.append(f"пользователь ID: {actor_id}")
    if subject: filter_labels.append(f"предмет/объект: {subject}")
    if date_value: filter_labels.append(f"дата события Минск: {date_value}")
    if hw_date_value: filter_labels.append(f"дата ДЗ: {hw_date_value}")
    if result_value and result_value != "all": filter_labels.append(f"результат: {result_value}")
    if admin_only: filter_labels.append("только админские операции")
    blocks = [_rich_gallery_heading("📋 Журнал действий"), _rich_paragraph(f"Найдено записей: {total}. Страница {page+1}."),
              _rich_paragraph("Фильтры: " + ("; ".join(filter_labels) if filter_labels else "не применены"))]
    if not rows:
        blocks.append(_rich_paragraph("По выбранным фильтрам записей нет."))
    for row in rows:
        (_id, ts, row_category, event_type, row_actor_id, actor_name, object_type, object_id, result, details_json) = row
        who = "Агрегированное событие без идентификатора" if row_actor_id is None else f"{actor_name or 'Пользователь'} (ID {row_actor_id})"
        details = {}
        try: details = json.loads(details_json or "{}")
        except Exception: pass
        fields = [f"🕒 {_audit_display_time(ts)}", f"👤 {who}", f"⚙️ {event_type} · {row_category}", f"Объект: {object_type or '—'} {object_id or ''}", f"Результат: {result}"]
        if details.get("date"): fields.append(f"Дата: {details['date']}")
        if details.get("subject"): fields.append(f"Предмет: {details['subject']}")
        blocks.append(_rich_paragraph("\n".join(fields)))
    nav=[]
    if page > 0: nav.append(_rich_button("⬅️ Назад", f"ctrl_audit:{page-1}"))
    if offset + len(rows) < total: nav.append(_rich_button("Далее ➡️", f"ctrl_audit:{page+1}"))
    if nav: blocks.extend(_rich_button_rows(nav,2))
    blocks.extend([
        InputRichBlockButtons(buttons=[_rich_button("🔍 Фильтры", "ctrl_audit_filters")]),
        InputRichBlockButtons(buttons=[_rich_button("⚙️ Настройки журналирования", "ctrl_audit_settings")]),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля", "ctrl_center")]),
    ])
    return blocks


@dp.callback_query(F.data.startswith("ctrl_audit:"))
async def ctrl_audit_page(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback):
        return
    try: page = max(0, int(callback.data.split(":", 1)[1]))
    except Exception: page = 0
    filters = await state.get_data()
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _control_audit_page_blocks(page, filters))
    await callback.answer()


@dp.callback_query(F.data == "ctrl_audit_filters")
async def ctrl_audit_filters(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    blocks=[_rich_gallery_heading("🔍 Фильтры журнала"),_rich_paragraph("Выбери категорию или поле для поиска. Введённые условия будут применены к журналу.")]
    for label,key in (("Все категории","all"),("Навигация","navigation"),("Изменения данных","data"),("Админские действия","admin"),("Система и ошибки","system"),("Агрегированные анонимные события","anonymous_aggregate")):
        blocks.append(InputRichBlockButtons(buttons=[_rich_button(label,f"ctrl_audit_cat:{key}")]))
    blocks.extend([
        InputRichBlockButtons(buttons=[_rich_button("👤 Фильтр по ID пользователя","ctrl_audit_ask:user")]),
        InputRichBlockButtons(buttons=[_rich_button("📚 Фильтр по предмету/объекту","ctrl_audit_ask:subject")]),
        InputRichBlockButtons(buttons=[_rich_button("📅 Фильтр по дате события (ДД.ММ.ГГГГ)","ctrl_audit_ask:date")]),
        InputRichBlockButtons(buttons=[_rich_button("📚 Фильтр по дате ДЗ","ctrl_audit_ask:hwdate")]),
        InputRichBlockButtons(buttons=[_rich_button("✅ Фильтр по результату","ctrl_audit_ask:result")]),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Только административные операции","ctrl_audit_admin_only")]),
        InputRichBlockButtons(buttons=[_rich_button("🧹 Очистить фильтры","ctrl_audit_clear","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ К журналу","ctrl_audit:0")]),
    ])
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,blocks)
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_audit_cat:"))
async def ctrl_audit_category_filter(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    await state.update_data(audit_filter_pending=None)
    await state.set_state(None)
    category=callback.data.split(":",1)[1]
    allowed={"all","navigation","data","admin","system","anonymous_aggregate"}
    if category not in allowed:
        await callback.answer("Неизвестная категория.",show_alert=True);return
    await state.update_data(audit_filter_category=category)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,
                             await _control_audit_page_blocks(0,await state.get_data()))
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_audit_ask:"))
async def ctrl_audit_ask(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    field=callback.data.split(":",1)[1]
    if field not in ("user","subject","date","hwdate","result"):
        await callback.answer("Некорректный фильтр.",show_alert=True);return
    prompts={"user":"Введи числовой Telegram ID пользователя:",
             "subject":"Введи название предмета или часть объекта:",
             "date":"Введи дату события в формате ДД.ММ.ГГГГ:",
             "hwdate":"Введи дату домашнего задания в формате ДД.ММ.ГГГГ:",
             "result":"Введи success или error:"}
    await state.update_data(audit_filter_pending=field)
    await state.set_state(ControlState.waiting_for_audit_filter)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("🔍 Фильтр журнала"),_rich_paragraph(prompts[field]+"\nОтмена: /cancel"),
        InputRichBlockButtons(buttons=[_rich_button("❌ Отмена","ctrl_audit_filter_cancel","danger")])])
    await callback.answer()


@dp.callback_query(F.data == "ctrl_audit_filter_cancel")
async def ctrl_audit_filter_cancel(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    await state.update_data(audit_filter_pending=None)
    await state.set_state(None)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("🔍 Фильтры журнала"),
        _rich_paragraph("Ввод отменён. Сохранённые фильтры остались без изменений."),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Назад к журналу","ctrl_audit:0")]),
        InputRichBlockButtons(buttons=[_rich_button("🔍 Фильтры","ctrl_audit_filters")]),
    ])
    await callback.answer("Отменено")


@dp.callback_query(F.data == "ctrl_audit_admin_only")
async def ctrl_audit_admin_only(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    await state.update_data(audit_filter_pending=None)
    await state.set_state(None)
    data=await state.get_data()
    await state.update_data(audit_filter_admin=not bool(data.get("audit_filter_admin")))
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,
                             await _control_audit_page_blocks(0,await state.get_data()))
    await callback.answer()


@dp.callback_query(F.data == "ctrl_audit_clear")
async def ctrl_audit_clear(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data=await state.get_data()
    for key in ("audit_filter_category","audit_filter_user","audit_filter_subject","audit_filter_date","audit_filter_hwdate","audit_filter_result","audit_filter_admin","audit_filter_pending"):
        data.pop(key,None)
    await state.set_data(data)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,await _control_audit_page_blocks(0,await state.get_data()))
    await callback.answer("Фильтры очищены")


@dp.message(ControlState.waiting_for_audit_filter)
async def ctrl_audit_filter_input(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID or message.chat.type != "private": return
    if (message.text or "").strip().casefold()=="/cancel":
        await state.update_data(audit_filter_pending=None)
        await state.set_state(None)
        await message.answer("Ввод фильтра отменён. Открой фильтры журнала повторно через центр контроля.")
        return
    data=await state.get_data(); field=data.get("audit_filter_pending"); value=(message.text or "").strip()
    if not value:
        await message.answer("Фильтр не может быть пустым. Попробуй ещё раз или /cancel.");return
    if field=="user":
        try: value=int(value)
        except ValueError:
            await message.answer("ID должен быть числом.");return
        await state.update_data(audit_filter_user=value)
    elif field=="subject":
        await state.update_data(audit_filter_subject=value[:100])
    elif field in ("date","hwdate"):
        try: parsed=datetime.strptime(value,"%d.%m.%Y").date()
        except ValueError:
            await message.answer("Дата должна быть в формате ДД.ММ.ГГГГ.");return
        await state.update_data(**({"audit_filter_date":parsed.isoformat()} if field == "date" else {"audit_filter_hwdate":parsed.isoformat()}))
    elif field=="result":
        normalized=value.casefold()
        if normalized not in ("success","error","cancelled","canceled"):
            await message.answer("Допустимые значения: success, error или cancelled.");return
        await state.update_data(audit_filter_result="cancelled" if normalized in ("cancelled","canceled") else normalized)
    else:
        await message.answer("Не удалось определить тип фильтра.");return
    await state.update_data(audit_filter_pending=None)
    await state.set_state(None)
    await message.answer("✅ Фильтр применён. Результаты журнала ниже.")
    await _send_rich_message(message.chat.id, await _control_audit_page_blocks(0, await state.get_data()))


@dp.callback_query(F.data == "ctrl_audit_settings")
async def ctrl_audit_settings(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    nav_enabled = (await _audit_get_setting("navigation_enabled", "1")) == "1"
    nav_days = await _audit_get_setting("navigation_retention_days", "14")
    data_days = await _audit_get_setting("data_retention_days", "90")
    blocks = [
        _rich_gallery_heading("⚙️ Журналирование"),
        _rich_paragraph(f"Подробные события навигации: {'ВКЛ' if nav_enabled else 'ВЫКЛ'}"),
        _rich_paragraph(f"Хранение навигации: {nav_days} дней. История изменений: {data_days} дней."),
        InputRichBlockButtons(buttons=[_rich_button("🔄 Включить/выключить навигацию", "ctrl_audit_toggle_nav")]),
        InputRichBlockButtons(buttons=[_rich_button("14 дней навигации", "ctrl_nav_retention:14")]),
        InputRichBlockButtons(buttons=[_rich_button("30 дней навигации", "ctrl_nav_retention:30")]),
        InputRichBlockButtons(buttons=[_rich_button("90 дней навигации", "ctrl_nav_retention:90")]),
        InputRichBlockButtons(buttons=[_rich_button("90 дней истории", "ctrl_data_retention:90")]),
        InputRichBlockButtons(buttons=[_rich_button("180 дней истории", "ctrl_data_retention:180")]),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Назад", "ctrl_audit:0")]),
    ]
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
    await callback.answer()


@dp.callback_query(F.data == "ctrl_audit_toggle_nav")
async def ctrl_audit_toggle_nav(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    current = (await _audit_get_setting("navigation_enabled", "1")) == "1"
    await _audit_set_setting("navigation_enabled", "0" if current else "1")
    await write_audit_event(category="admin", event_type="audit_navigation_toggled", actor_id=callback.from_user.id,
                            actor_name=_audit_user_name(callback.from_user), result="success",
                            details={"enabled": not current}, admin_id=ADMIN_ID)
    await ctrl_audit_settings(callback)


@dp.callback_query(F.data.startswith("ctrl_nav_retention:"))
async def ctrl_nav_retention(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    days = int(callback.data.split(":",1)[1])
    if days not in (14,30,90):
        await callback.answer("Недопустимый срок хранения.", show_alert=True); return
    global AUDIT_NAV_RETENTION_DAYS
    AUDIT_NAV_RETENTION_DAYS = days
    await _audit_set_setting("navigation_retention_days", days)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE audit_events SET retention_days=? WHERE category IN ('navigation','anonymous_aggregate')", (days,))
        await db.commit()
    await _audit_cleanup_expired()
    await write_audit_event(category="admin", event_type="audit_nav_retention_changed", actor_id=callback.from_user.id,
                            actor_name=_audit_user_name(callback.from_user), details={"days": days}, admin_id=ADMIN_ID)
    await ctrl_audit_settings(callback)


@dp.callback_query(F.data.startswith("ctrl_data_retention:"))
async def ctrl_data_retention(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    days = int(callback.data.split(":",1)[1])
    if days not in (90,180):
        await callback.answer("Недопустимый срок хранения.", show_alert=True); return
    global AUDIT_DATA_RETENTION_DAYS
    AUDIT_DATA_RETENTION_DAYS = days
    await _audit_set_setting("data_retention_days", days)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE audit_events SET retention_days=? WHERE category NOT IN ('navigation','anonymous_aggregate')", (days,))
        await db.commit()
    await _audit_cleanup_expired()
    await write_audit_event(category="admin", event_type="audit_data_retention_changed", actor_id=callback.from_user.id,
                            actor_name=_audit_user_name(callback.from_user), details={"days": days}, admin_id=ADMIN_ID)
    await ctrl_audit_settings(callback)


async def _control_hw_dates_blocks(page: int = 0):
    offset = max(0, page) * 8
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(DISTINCT date_str) FROM homework WHERE chat_id=?", (ANON_CHAT_ID,))
        total = (await cur.fetchone())[0]
        cur = await db.execute("SELECT DISTINCT date_str FROM homework WHERE chat_id=? ORDER BY date_str DESC LIMIT 8 OFFSET ?", (ANON_CHAT_ID, offset))
        dates = [r[0] for r in await cur.fetchall()]
    blocks = [_rich_gallery_heading("📚 Управление ДЗ"), _rich_paragraph("Выбери дату.")]
    for date_str in dates:
        try:
            label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
        except Exception:
            label = date_str
        blocks.append(InputRichBlockButtons(buttons=[_rich_button(label, f"ctrl_hwdate:{date_str}")]))
    if not dates:
        blocks.append(_rich_paragraph("Записей ДЗ пока нет."))
    nav = []
    if page > 0:
        nav.append(_rich_button("⬅️", f"ctrl_hwdates:{page-1}"))
    if offset + len(dates) < total:
        nav.append(_rich_button("➡️", f"ctrl_hwdates:{page+1}"))
    if nav:
        blocks.extend(_rich_button_rows(nav, 2))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля", "ctrl_center")]))
    return blocks


@dp.callback_query(F.data == "ctrl_tags")
async def ctrl_tags(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, [
        _rich_gallery_heading("🏷 Управление тегами ДЗ"),
        _rich_paragraph("Выбери дату и предмет, затем конкретную группу. Удаляется только выбранный текстовый тег: (✏️) или (➕). Тег (📸) вычисляется автоматически и исчезает после удаления связанных фотографий."),
        InputRichBlockButtons(buttons=[_rich_button("📅 Выбрать дату", "ctrl_hwdates:0")]),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля", "ctrl_center")]),
    ])
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_hwdates:"))
async def ctrl_hwdates(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    try: page = max(0, int(callback.data.split(":",1)[1]))
    except Exception: page = 0
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _control_hw_dates_blocks(page))
    await callback.answer()


async def _control_subjects_for_date(date_str: str):
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT DISTINCT subject FROM schedule WHERE chat_id=? AND day_int=? AND subject IS NOT NULL AND TRIM(subject)!='' AND subject NOT IN ('—','-')",
            (ANON_CHAT_ID, date_obj.weekday()),
        )
        scheduled = {r[0] for r in await cur.fetchall()}
        cur = await db.execute("SELECT DISTINCT subject FROM homework WHERE chat_id=? AND date_str=?", (ANON_CHAT_ID, date_str))
        stored = {r[0] for r in await cur.fetchall()}
    return sorted(scheduled | stored, key=str.casefold)


@dp.callback_query(F.data.startswith("ctrl_hwdate:"))
async def ctrl_hwdate(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    date_str = callback.data.split(":",1)[1]
    try: datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        await callback.answer("Некорректная дата.", show_alert=True); return
    subjects = await _control_subjects_for_date(date_str)
    blocks = [_rich_gallery_heading("📚 ДЗ · выбор предмета"), _rich_paragraph(f"Дата: {date_str}")]
    if not subjects: blocks.append(_rich_paragraph("На эту дату предметы не найдены."))
    for idx, subject in enumerate(subjects):
        blocks.append(InputRichBlockButtons(buttons=[_rich_button(subject, f"ctrl_hwsub:{date_str}:{idx}")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("⬅️ К датам", "ctrl_hwdates:0")]))
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_hwsub:"))
async def ctrl_hwsub(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text = callback.data.split(":",2); idx=int(idx_text)
        subjects = await _control_subjects_for_date(date_str)
        subject = subjects[idx]
    except Exception:
        await callback.answer("Предмет или дата больше не найдены.", show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT groups_enabled FROM subject_group_modes WHERE chat_id=? AND subject=?", (ANON_CHAT_ID, subject))
        row = await cur.fetchone()
        grouped = bool(row and row[0])
        cur = await db.execute("SELECT group_number FROM homework WHERE chat_id=? AND date_str=? AND subject=?", (ANON_CHAT_ID,date_str,subject))
        saved_groups = {int(r[0]) for r in await cur.fetchall()}
    blocks = [_rich_gallery_heading(subject), _rich_paragraph(f"Дата: {date_str}\nВыбери конкретную запись.")]
    if grouped:
        if 0 in saved_groups:
            blocks.append(InputRichBlockButtons(buttons=[_rich_button("⚠️ Старая общая запись", f"ctrl_hwitem:{date_str}:{idx}:0")]))
        for group in (1,2):
            task = await _get_homework_task(ANON_CHAT_ID, date_str, subject, group)
            mark = "🔸" if _homework_is_filled(task) else "🔹"
            blocks.append(InputRichBlockButtons(buttons=[_rich_button(f"{mark} {group} группа", f"ctrl_hwitem:{date_str}:{idx}:{group}")]))
    else:
        blocks.append(InputRichBlockButtons(buttons=[_rich_button("📝 Обычное ДЗ", f"ctrl_hwitem:{date_str}:{idx}:0")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("⬅️ К предметам", f"ctrl_hwdate:{date_str}")]))
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
    await callback.answer()


async def _control_load_hw(date_str: str, subject_index: int, group_number: int):
    subjects = await _control_subjects_for_date(date_str)
    if not 0 <= subject_index < len(subjects):
        return None, None
    subject = subjects[subject_index]
    task = await _get_homework_task(ANON_CHAT_ID, date_str, subject, group_number)
    return subject, task


@dp.callback_query(F.data.startswith("ctrl_hwitem:"))
async def ctrl_hwitem(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text, group_text = callback.data.split(":",3)
        idx, group_number = int(idx_text), int(group_text)
        subject, task = await _control_load_hw(date_str, idx, group_number)
        if subject is None: raise ValueError
    except Exception:
        await callback.answer("Запись не найдена.", show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM gallery_photos WHERE chat_id=? AND photo_date=? AND subject=?", (ANON_CHAT_ID,date_str,subject))
        photo_count=(await cur.fetchone())[0]
    group_label = {0:"обычный режим",1:"1 группа",2:"2 группа"}.get(group_number, "группа")
    blocks = [_rich_gallery_heading("📚 Запись ДЗ"),
              _rich_paragraph(f"Дата: {date_str}\nПредмет: {subject}\nРежим: {group_label}\nФотографий: {photo_count}"),
              _rich_paragraph("ДЗ: " + (task if _homework_is_filled(task) else "Нет Д/з"))]
    tags = []
    if task:
        if "(✏️)" in task: tags.append("replace")
        if "(➕)" in task: tags.append("append")
    blocks.extend([
        InputRichBlockButtons(buttons=[_rich_button("✏️ Редактировать", f"ctrl_hwedit:{date_str}:{idx}:{group_number}", "success")]),
        InputRichBlockButtons(buttons=[_rich_button("🗑 Удалить это ДЗ", f"ctrl_hwdel:{date_str}:{idx}:{group_number}", "danger")]),
    ])
    for tag_key, tag_label in (("replace","(✏️) удалить"),("append","(➕) удалить")):
        if tag_key in tags:
            blocks.append(InputRichBlockButtons(buttons=[_rich_button(f"🏷 {tag_label}", f"ctrl_hwtag:{date_str}:{idx}:{group_number}:{tag_key}", "danger")]))
    blocks.extend([
        InputRichBlockButtons(buttons=[_rich_button("🕓 История изменений", f"ctrl_hwhist:{date_str}:{idx}:{group_number}")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ К предметам", f"ctrl_hwdate:{date_str}")]),
    ])
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_hwedit:"))
async def ctrl_hwedit(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text, group_text = callback.data.split(":",3)
        idx, group_number = int(idx_text), int(group_text)
        subject, task = await _control_load_hw(date_str, idx, group_number)
        if subject is None: raise ValueError
    except Exception:
        await callback.answer("Запись не найдена.", show_alert=True); return
    await state.clear()
    await state.update_data(ctrl_hw_date=date_str, ctrl_hw_subject=subject, ctrl_hw_group=group_number, ctrl_hw_old_task=task)
    await state.set_state(ControlState.waiting_for_homework_text)
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, [
        _rich_gallery_heading("✏️ Редактирование ДЗ"),
        _rich_paragraph(f"Дата: {date_str}\nПредмет: {subject}\nТекущий текст: {task or 'Нет Д/з'}\n\nОтправь новый текст одним сообщением. Отправь /cancel для отмены."),
    ])
    await callback.answer()


@dp.message(ControlState.waiting_for_homework_text)
async def ctrl_hwedit_text(message: types.Message, state: FSMContext):
    if not message.from_user or message.from_user.id != ADMIN_ID or message.chat.type != "private":
        return
    if (message.text or "").strip().casefold() == "/cancel":
        await state.clear(); await message.answer("Редактирование отменено."); return
    new_task = (message.text or "").strip()
    if not new_task:
        await message.answer("Текст ДЗ не может быть пустым. Отправь текст или /cancel."); return
    data = await state.get_data()
    old = data.get("ctrl_hw_old_task")
    await _save_homework_audited(chat_id=ANON_CHAT_ID, date_str=data["ctrl_hw_date"],
                                 subject=data["ctrl_hw_subject"], group_number=int(data["ctrl_hw_group"]),
                                 new_task=new_task, action="admin_edit", actor_id=message.from_user.id,
                                 actor_name=_audit_user_name(message.from_user), admin_id=ADMIN_ID)
    await state.clear()
    await message.answer("✅ Домашнее задание обновлено. История изменений сохранена.")


@dp.callback_query(F.data.startswith("ctrl_hwdel:"))
async def ctrl_hwdel(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text, group_text = callback.data.split(":",3)
        idx, group_number = int(idx_text), int(group_text)
        subject, task = await _control_load_hw(date_str,idx,group_number)
        if subject is None: raise ValueError
    except Exception:
        await callback.answer("Запись не найдена.", show_alert=True); return
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, [
        _rich_gallery_heading("🗑 Подтверждение удаления"),
        _rich_paragraph(f"Удалить ДЗ?\nДата: {date_str}\nПредмет: {subject}\nГруппа: {group_number if group_number else 'обычная'}\n\n{task or 'Нет Д/з'}"),
        InputRichBlockButtons(buttons=[_rich_button("🗑 Да, удалить", f"ctrl_hwdel_yes:{date_str}:{idx}:{group_number}", "danger")]),
        InputRichBlockButtons(buttons=[_rich_button("❌ Отмена", f"ctrl_hwitem:{date_str}:{idx}:{group_number}", "danger")]),
    ])
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_hwdel_yes:"))
async def ctrl_hwdel_yes(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text, group_text = callback.data.split(":",3)
        idx, group_number = int(idx_text), int(group_text)
        subject, _ = await _control_load_hw(date_str,idx,group_number)
        if subject is None: raise ValueError
    except Exception:
        await callback.answer("Запись не найдена.", show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT task FROM homework WHERE chat_id=? AND date_str=? AND subject=? AND group_number=?", (ANON_CHAT_ID,date_str,subject,group_number))
        row = await cur.fetchone()
        if not row:
            await callback.answer("ДЗ уже отсутствует.", show_alert=True); return
        await _insert_homework_history_db(db, ANON_CHAT_ID,date_str,subject,group_number,"admin_delete",row[0],None,
                                          callback.from_user.id,_audit_user_name(callback.from_user),ADMIN_ID)
        await _insert_audit_event_db(db,category="data",event_type="homework_delete",actor_id=callback.from_user.id,
                                     actor_name=_audit_user_name(callback.from_user),object_type="homework",
                                     object_id=f"{date_str}|{subject}|{group_number}",details={"date":date_str,"subject":subject,"group_number":group_number},
                                     admin_id=ADMIN_ID,retention_days=AUDIT_DATA_RETENTION_DAYS)
        await db.execute("DELETE FROM homework WHERE chat_id=? AND date_str=? AND subject=? AND group_number=?", (ANON_CHAT_ID,date_str,subject,group_number))
        await db.commit()
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("✅ ДЗ удалено"), _rich_paragraph(f"Удалена только выбранная запись: {subject} · {date_str} · группа {group_number or 'обычная'}"),
        InputRichBlockButtons(buttons=[_rich_button("↩️ К предметам", f"ctrl_hwdate:{date_str}")]),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Центр контроля", "ctrl_center")]),
    ])
    await callback.answer("Удалено")


@dp.callback_query(F.data.startswith("ctrl_hwtag:"))
async def ctrl_hwtag_remove(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text, group_text, tag_key = callback.data.split(":",4)
        idx, group_number = int(idx_text), int(group_text)
        subject, task = await _control_load_hw(date_str, idx, group_number)
        if subject is None or not task: raise ValueError
    except Exception:
        await callback.answer("Запись или тег не найдены.", show_alert=True); return
    tag = HW_TAGS.get(tag_key)
    if not tag or tag not in task:
        await callback.answer("Тег не найден.", show_alert=True); return
    new_task = task.replace(tag, "")
    new_task = re.sub(r"[ \t]{2,}", " ", new_task)
    new_task = re.sub(r"\s+([,.;])", r"\1", new_task).strip()
    new_task = re.sub(r"\s{2,}", " ", new_task)
    await _save_homework_audited(chat_id=ANON_CHAT_ID, date_str=date_str, subject=subject,
                                 group_number=group_number, new_task=new_task, action="remove_tag",
                                 actor_id=callback.from_user.id, actor_name=_audit_user_name(callback.from_user), admin_id=ADMIN_ID)
    await callback.answer("Тег удалён")
    await ctrl_hwitem_view_by_args(callback, date_str, idx, group_number)


async def ctrl_hwitem_view_by_args(callback, date_str, idx, group_number):
    subject, _ = await _control_load_hw(date_str, idx, group_number)
    if subject is None:
        await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _control_hw_dates_blocks(0)); return
    # Re-run the item renderer through the same guarded screen logic without reusing stale callback data.
    task = await _get_homework_task(ANON_CHAT_ID, date_str, subject, group_number)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM gallery_photos WHERE chat_id=? AND photo_date=? AND subject=?", (ANON_CHAT_ID,date_str,subject))
        photo_count=(await cur.fetchone())[0]
    mark_task = task if task else "Нет Д/з"
    blocks=[_rich_gallery_heading("📚 Запись ДЗ"),_rich_paragraph(f"Дата: {date_str}\nПредмет: {subject}\nГруппа: {group_number or 'обычная'}\nФотографий: {photo_count}"),_rich_paragraph(f"ДЗ: {mark_task}")]
    if task and "(✏️)" in task:
        blocks.append(InputRichBlockButtons(buttons=[_rich_button("🏷 (✏️) удалить","ctrl_hwtag:"+f"{date_str}:{idx}:{group_number}:replace","danger")]))
    if task and "(➕)" in task:
        blocks.append(InputRichBlockButtons(buttons=[_rich_button("🏷 (➕) удалить","ctrl_hwtag:"+f"{date_str}:{idx}:{group_number}:append","danger")]))
    blocks.extend([
        InputRichBlockButtons(buttons=[_rich_button("✏️ Редактировать",f"ctrl_hwedit:{date_str}:{idx}:{group_number}","success")]),
        InputRichBlockButtons(buttons=[_rich_button("🗑 Удалить это ДЗ",f"ctrl_hwdel:{date_str}:{idx}:{group_number}","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("🕓 История изменений",f"ctrl_hwhist:{date_str}:{idx}:{group_number}")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ К предметам",f"ctrl_hwdate:{date_str}")]),
    ])
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,blocks)


@dp.callback_query(F.data.startswith("ctrl_hwhist:"))
async def ctrl_hwhist(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        _, date_str, idx_text, group_text = callback.data.split(":",3); idx=int(idx_text); group_number=int(group_text)
        subject, _ = await _control_load_hw(date_str,idx,group_number)
        if subject is None: raise ValueError
    except Exception:
        await callback.answer("Запись не найдена.", show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT id,timestamp_utc,action,old_task,new_task,actor_id FROM homework_history WHERE chat_id=? AND date_str=? AND subject=? AND group_number=? ORDER BY id DESC LIMIT 8",
                             (ANON_CHAT_ID,date_str,subject,group_number))
        rows=await cur.fetchall()
    blocks=[_rich_gallery_heading("🕓 История изменений"),_rich_paragraph(f"{subject} · {date_str} · группа {group_number or 'обычная'}")]
    if not rows: blocks.append(_rich_paragraph("История для этой записи пока не накоплена."))
    for hid,ts,action,old_task,new_task,actor_id in rows:
        blocks.append(_rich_paragraph(f"{_audit_display_time(ts)} · {action}\nДо: {old_task if old_task is not None else 'записи не было'}\nПосле: {new_task if new_task is not None else 'запись удалена'}\nID: {actor_id if actor_id is not None else 'не установлен'}"))
        blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ Восстановить состояние до этого действия",f"ctrl_restore:{hid}","success")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("⬅️ К записи",f"ctrl_hwitem:{date_str}:{idx}:{group_number}")]))
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,blocks)
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_history:"))
async def ctrl_history(callback: types.CallbackQuery):
    if not await _require_control_admin(callback):
        return
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except Exception:
        page = 0
    offset = page * 6
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM homework_history")
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT id,timestamp_utc,date_str,subject,group_number,action,old_task,new_task,actor_id "
            "FROM homework_history ORDER BY id DESC LIMIT 6 OFFSET ?", (offset,)
        )
        rows = await cur.fetchall()
    blocks = [_rich_gallery_heading("🕓 История изменений"), _rich_paragraph(f"Записей истории: {total}")]
    if not rows:
        blocks.append(_rich_paragraph("История изменений пока пуста."))
    for hid, ts, date_str, subject, group_number, action, old_task, new_task, actor_id in rows:
        blocks.append(_rich_paragraph(
            f"{_audit_display_time(ts)} · {action}\n{date_str} · {subject} · группа {group_number or 'обычная'}\n"
            f"До: {old_task if old_task is not None else 'записи не было'}\n"
            f"После: {new_task if new_task is not None else 'запись удалена'}\n"
            f"Кто: {actor_id if actor_id is not None else 'не установлен'}"
        ))
        blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ Восстановить состояние до действия", f"ctrl_restore:{hid}", "success")]))
    nav = []
    if page > 0:
        nav.append(_rich_button("⬅️ Назад", f"ctrl_history:{page-1}"))
    if offset + len(rows) < total:
        nav.append(_rich_button("Далее ➡️", f"ctrl_history:{page+1}"))
    if nav:
        blocks.extend(_rich_button_rows(nav, 2))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля", "ctrl_center")]))
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, blocks)
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_restore:"))
async def ctrl_restore_confirm(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try: hid=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректная версия.", show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT chat_id,date_str,subject,group_number,old_task FROM homework_history WHERE id=?", (hid,))
        row=await cur.fetchone()
    if not row:
        await callback.answer("Версия не найдена.", show_alert=True); return
    chat_id,date_str,subject,group_number,old_task=row
    blocks=[_rich_gallery_heading("↩️ Подтверждение восстановления"),
            _rich_paragraph(f"Дата: {date_str}\nПредмет: {subject}\nГруппа: {group_number or 'обычная'}\nБудет восстановлено: {old_task if old_task is not None else 'отсутствие записи'}"),
            InputRichBlockButtons(buttons=[_rich_button("↩️ Да, восстановить",f"ctrl_restore_yes:{hid}","success")]),
            InputRichBlockButtons(buttons=[_rich_button("❌ Отмена",f"ctrl_hwhist:{date_str}:{(await _control_subjects_for_date(date_str)).index(subject)}:{group_number}","danger")])]
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,blocks)
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_restore_yes:"))
async def ctrl_restore_yes(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try: hid=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректная версия.", show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT chat_id,date_str,subject,group_number,old_task FROM homework_history WHERE id=?",(hid,))
        row=await cur.fetchone()
        if not row:
            await callback.answer("Версия не найдена.",show_alert=True); return
        chat_id,date_str,subject,group_number,target_task=row
        cur=await db.execute("SELECT task FROM homework WHERE chat_id=? AND date_str=? AND subject=? AND group_number=?",(chat_id,date_str,subject,group_number))
        current_row=await cur.fetchone(); current_task=current_row[0] if current_row else None
        await _insert_homework_history_db(db,chat_id,date_str,subject,group_number,"restore",current_task,target_task,
                                          callback.from_user.id,_audit_user_name(callback.from_user),ADMIN_ID)
        if target_task is None:
            await db.execute("DELETE FROM homework WHERE chat_id=? AND date_str=? AND subject=? AND group_number=?",(chat_id,date_str,subject,group_number))
        else:
            await db.execute("INSERT OR REPLACE INTO homework(chat_id,date_str,subject,task,group_number) VALUES(?,?,?,?,?)",(chat_id,date_str,subject,target_task,group_number))
        await _insert_audit_event_db(db,category="data",event_type="homework_restore",actor_id=callback.from_user.id,
                                     actor_name=_audit_user_name(callback.from_user),object_type="homework",
                                     object_id=f"{date_str}|{subject}|{group_number}",details={"history_id":hid},admin_id=ADMIN_ID,
                                     retention_days=AUDIT_DATA_RETENTION_DAYS)
        await db.commit()
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("✅ Версия восстановлена"),_rich_paragraph(f"{subject} · {date_str} · группа {group_number or 'обычная'}"),
        InputRichBlockButtons(buttons=[_rich_button("🕓 История",f"ctrl_hwhist:{date_str}:{(await _control_subjects_for_date(date_str)).index(subject)}:{group_number}")]),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Центр контроля","ctrl_center")])])
    await callback.answer("Восстановлено")


async def _control_gallery_blocks(page=0, selected_ids=None):
    selected_ids = {int(x) for x in (selected_ids or []) if str(x).isdigit()}
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT COUNT(*) FROM gallery_photos WHERE chat_id=?",(ANON_CHAT_ID,)); total=(await cur.fetchone())[0]
        cur=await db.execute("SELECT id,photo_date,subject,uploader_id,uploaded_at FROM gallery_photos WHERE chat_id=? ORDER BY photo_date DESC,id DESC LIMIT 8 OFFSET ?",(ANON_CHAT_ID,max(0,page)*8)); rows=await cur.fetchall()
    blocks=[_rich_gallery_heading("🖼 Управление галереей"),_rich_paragraph(f"Фотографий: {total}. Выбрано для массового удаления: {len(selected_ids)}.")]
    for pid,pdate,subject,uploader,uploaded in rows:
        is_selected = pid in selected_ids
        toggle_label = ("✅ Выбрано" if is_selected else "☐ Выбрать") + f" #{pid}"
        blocks.append(InputRichBlockButtons(buttons=[
            _rich_button(f"{pdate} · {subject} · фото #{pid}",f"ctrl_gallery_photo:{pid}"),
            _rich_button(toggle_label,f"ctrl_gallery_toggle:{pid}:{page}","success" if is_selected else "primary"),
        ]))
    if not rows:
        blocks.append(_rich_paragraph("В галерее пока нет фотографий."))
    nav=[]
    if page>0: nav.append(_rich_button("⬅️",f"ctrl_gallery_page:{page-1}"))
    if (page+1)*8<total: nav.append(_rich_button("➡️",f"ctrl_gallery_page:{page+1}"))
    if nav: blocks.extend(_rich_button_rows(nav,2))
    if selected_ids:
        blocks.append(InputRichBlockButtons(buttons=[_rich_button(f"🗑 Удалить выбранные ({len(selected_ids)})","ctrl_gallery_bulk_review","danger")]))
        blocks.append(InputRichBlockButtons(buttons=[_rich_button("✖️ Снять выбор","ctrl_gallery_clear_selection","danger")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля","ctrl_center")]))
    return blocks


@dp.callback_query(F.data == "ctrl_gallery")
async def ctrl_gallery(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    await state.update_data(ctrl_gallery_selected_ids=[], ctrl_gallery_current_page=0)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,await _control_gallery_blocks(0, [])); await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_gallery_page:"))
async def ctrl_gallery_page(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    try: page=max(0,int(callback.data.split(":",1)[1]))
    except Exception: page=0
    selected=(await state.get_data()).get("ctrl_gallery_selected_ids",[])
    await state.update_data(ctrl_gallery_current_page=page)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,await _control_gallery_blocks(page,selected)); await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_gallery_toggle:"))
async def ctrl_gallery_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    try:
        _, pid_text, page_text = callback.data.split(":",2)
        pid, page = int(pid_text), max(0,int(page_text))
    except Exception:
        await callback.answer("Некорректная запись.",show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT 1 FROM gallery_photos WHERE id=? AND chat_id=?",(pid,ANON_CHAT_ID))
        if not await cur.fetchone():
            await callback.answer("Фотография уже отсутствует.",show_alert=True); return
    data=await state.get_data()
    selected={int(x) for x in data.get("ctrl_gallery_selected_ids",[]) if str(x).isdigit()}
    if pid in selected:
        selected.remove(pid)
    else:
        if len(selected) >= 100:
            await callback.answer("За одну операцию можно выбрать не больше 100 фотографий.", show_alert=True); return
        selected.add(pid)
    await state.update_data(ctrl_gallery_selected_ids=sorted(selected),ctrl_gallery_current_page=page)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,await _control_gallery_blocks(page,selected))
    await callback.answer("Выбор обновлён")


@dp.callback_query(F.data == "ctrl_gallery_clear_selection")
async def ctrl_gallery_clear_selection(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data=await state.get_data(); page=max(0,int(data.get("ctrl_gallery_current_page",0)))
    await state.update_data(ctrl_gallery_selected_ids=[])
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,await _control_gallery_blocks(page,[]))
    await callback.answer("Выбор снят")


@dp.callback_query(F.data == "ctrl_gallery_bulk_review")
async def ctrl_gallery_bulk_review(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data=await state.get_data()
    selected=sorted({int(x) for x in data.get("ctrl_gallery_selected_ids",[]) if str(x).isdigit()})
    if not selected:
        await callback.answer("Сначала выбери фотографии.",show_alert=True); return
    placeholders=",".join("?" for _ in selected)
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute(f"SELECT id,photo_date,subject FROM gallery_photos WHERE chat_id=? AND id IN ({placeholders}) ORDER BY photo_date,subject,id",[ANON_CHAT_ID,*selected])
        rows=await cur.fetchall()
    if not rows:
        await state.update_data(ctrl_gallery_selected_ids=[])
        await callback.answer("Выбранные фотографии уже отсутствуют.",show_alert=True); return
    detail="\n".join(f"#{pid} · {date_str} · {subject}" for pid,date_str,subject in rows[:15])
    if len(rows)>15: detail+=f"\n… и ещё {len(rows)-15} фото"
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("⚠️ Массовое удаление фотографий"),
        _rich_paragraph(f"Будет удалено записей: {len(rows)}. Перед удалением бот создаст резервную копию БД.\n\n{detail}"),
        InputRichBlockButtons(buttons=[_rich_button(f"🗑 Подтвердить удаление ({len(rows)})","ctrl_gallery_bulk_delete_yes","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("❌ Отмена","ctrl_gallery_bulk_delete_cancel","danger")]),
    ])
    await callback.answer()


@dp.callback_query(F.data == "ctrl_gallery_bulk_delete_cancel")
async def ctrl_gallery_bulk_delete_cancel(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data=await state.get_data(); selected=data.get("ctrl_gallery_selected_ids",[]); page=max(0,int(data.get("ctrl_gallery_current_page",0)))
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,await _control_gallery_blocks(page,selected))
    await callback.answer("Удаление отменено")


@dp.callback_query(F.data == "ctrl_gallery_bulk_delete_yes")
async def ctrl_gallery_bulk_delete_yes(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data=await state.get_data()
    selected=sorted({int(x) for x in data.get("ctrl_gallery_selected_ids",[]) if str(x).isdigit()})
    if not selected:
        await callback.answer("Нет выбранных фотографий.",show_alert=True); return
    try:
        placeholders=",".join("?" for _ in selected)
        async with aiosqlite.connect(DB_NAME) as db:
            cur=await db.execute(f"SELECT COUNT(*) FROM gallery_photos WHERE chat_id=? AND id IN ({placeholders})",[ANON_CHAT_ID,*selected])
            existing_count=(await cur.fetchone())[0]
        if not existing_count:
            await state.update_data(ctrl_gallery_selected_ids=[])
            await callback.answer("Выбранные фотографии уже отсутствуют.",show_alert=True); return
        await _create_database_backup("before_gallery_bulk_delete", actor_id=callback.from_user.id)
        deleted=[]
        async with aiosqlite.connect(DB_NAME) as db:
            cur=await db.execute(f"SELECT id,photo_date,subject FROM gallery_photos WHERE chat_id=? AND id IN ({placeholders}) ORDER BY id",[ANON_CHAT_ID,*selected])
            rows=await cur.fetchall()
            for pid,date_str,subject in rows:
                await db.execute("DELETE FROM gallery_photos WHERE id=? AND chat_id=?",(pid,ANON_CHAT_ID))
                await _insert_audit_event_db(db,category="admin",event_type="gallery_photo_deleted",actor_id=callback.from_user.id,
                                             actor_name=_audit_user_name(callback.from_user),object_type="gallery_photo",object_id=str(pid),
                                             details={"date":date_str,"subject":subject,"bulk":True},admin_id=ADMIN_ID,
                                             retention_days=AUDIT_DATA_RETENTION_DAYS)
                deleted.append((pid,date_str,subject))
            await db.commit()
        await state.update_data(ctrl_gallery_selected_ids=[])
    except Exception:
        logging.exception("Не удалось удалить выбранные фотографии")
        await write_audit_event(category="admin",event_type="gallery_bulk_delete_failed",actor_id=callback.from_user.id,
                                actor_name=_audit_user_name(callback.from_user),result="error",details={"selected_count":len(selected)},admin_id=ADMIN_ID,
                                retention_days=AUDIT_DATA_RETENTION_DAYS)
        await callback.answer("Удаление не выполнено. Проверь логи.",show_alert=True); return
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("✅ Массовое удаление завершено"),
        _rich_paragraph(f"Удалено записей фотографий: {len(deleted)}. Динамические теги (📸) обновятся при следующем отображении ДЗ."),
        InputRichBlockButtons(buttons=[_rich_button("🖼 К галерее","ctrl_gallery")]),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Центр контроля","ctrl_center")]),
    ])
    await callback.answer(f"Удалено: {len(deleted)}")


@dp.callback_query(F.data.startswith("ctrl_gallery_photo:"))
async def ctrl_gallery_photo(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try: photo_id=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректное фото.",show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT photo_date,subject,file_id,uploader_id,uploaded_at FROM gallery_photos WHERE id=? AND chat_id=?",(photo_id,ANON_CHAT_ID)); row=await cur.fetchone()
    if not row:
        await callback.answer("Фотография не найдена.",show_alert=True); return
    photo_date,subject,file_id,uploader,uploaded=row
    caption=RichBlockCaption(text=f"{photo_date} · {subject} · загрузка {uploaded} · пользователь {uploader if uploader is not None else 'не установлен'}")
    block=InputRichBlockPhoto(photo=InputMediaPhoto(media=file_id),caption=caption)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("🖼 Фотография"),block,
        InputRichBlockButtons(buttons=[_rich_button("🗑 Удалить фотографию",f"ctrl_gallery_delete:{photo_id}","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ К галерее","ctrl_gallery_return")]),
    ])
    await callback.answer()


@dp.callback_query(F.data == "ctrl_gallery_return")
async def ctrl_gallery_return(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data = await state.get_data()
    page = max(0, int(data.get("ctrl_gallery_current_page", 0)))
    selected = data.get("ctrl_gallery_selected_ids", [])
    await _edit_rich_message(callback.message.chat.id, callback.message.message_id, await _control_gallery_blocks(page, selected))
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_gallery_delete:"))
async def ctrl_gallery_delete_confirm(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try: pid=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректный ID.",show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT photo_date,subject,file_id FROM gallery_photos WHERE id=? AND chat_id=?",(pid,ANON_CHAT_ID)); row=await cur.fetchone()
    if not row:
        await callback.answer("Фотография не найдена.",show_alert=True); return
    date_str,subject,file_id=row
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("🗑 Удаление фотографии"),_rich_paragraph(f"Удалить запись фото?\nДата: {date_str}\nПредмет: {subject}"),
        InputRichBlockButtons(buttons=[_rich_button("🗑 Да, удалить",f"ctrl_gallery_delete_yes:{pid}","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("❌ Отмена",f"ctrl_gallery_photo:{pid}","danger")])])
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_gallery_delete_yes:"))
async def ctrl_gallery_delete_yes(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try: pid=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректный ID.",show_alert=True); return
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT photo_date,subject FROM gallery_photos WHERE id=? AND chat_id=?",(pid,ANON_CHAT_ID)); row=await cur.fetchone()
        if not row:
            await callback.answer("Фотография уже удалена.",show_alert=True); return
        photo_date,subject=row
        await db.execute("DELETE FROM gallery_photos WHERE id=? AND chat_id=?",(pid,ANON_CHAT_ID))
        await _insert_audit_event_db(db,category="data",event_type="gallery_photo_deleted",actor_id=callback.from_user.id,
                                     actor_name=_audit_user_name(callback.from_user),object_type="gallery_photo",object_id=str(pid),
                                     details={"date":photo_date,"subject":subject},admin_id=ADMIN_ID,retention_days=AUDIT_DATA_RETENTION_DAYS)
        await db.commit()
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("✅ Фотография удалена"),_rich_paragraph(f"Удалена запись фото: {subject} · {photo_date}. Тег (📸) обновится при следующем отображении ДЗ."),
        InputRichBlockButtons(buttons=[_rich_button("↩️ К галерее","ctrl_gallery")]),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Центр контроля","ctrl_center")])])
    await callback.answer("Запись галереи удалена")


async def _create_database_backup(reason="manual", actor_id=None):
    os.makedirs(AUDIT_BACKUP_DIR, exist_ok=True)
    filename = f"school_data_{datetime.now(MINSK_TZ).strftime('%Y%m%d_%H%M%S_%f')}_{re.sub(r'[^a-zA-Z0-9_-]', '_', reason)[:20]}.sqlite3"
    backup_path = os.path.join(AUDIT_BACKUP_DIR, filename)
    def _backup_sync():
        source = sqlite3.connect(DB_NAME, timeout=20)
        target = sqlite3.connect(backup_path, timeout=20)
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError(f"integrity_check failed: {check}")
        finally:
            target.close(); source.close()
    await asyncio.to_thread(_backup_sync)
    files = sorted([os.path.join(AUDIT_BACKUP_DIR,n) for n in os.listdir(AUDIT_BACKUP_DIR) if n.endswith(".sqlite3")], key=os.path.getmtime, reverse=True)
    for obsolete in files[AUDIT_BACKUP_MAX_FILES:]:
        try: os.remove(obsolete)
        except OSError: logging.exception("Не удалось удалить старую резервную копию")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO control_status(key,value) VALUES('last_backup_path',?)",(backup_path,))
        await _insert_audit_event_db(db,category="admin",event_type="database_backup_created",actor_id=actor_id,
                                     object_type="database_backup",object_id=filename,result="success",
                                     details={"reason":reason},admin_id=ADMIN_ID if actor_id == ADMIN_ID else None,
                                     retention_days=AUDIT_DATA_RETENTION_DAYS)
        await db.commit()
    return backup_path


@dp.callback_query(F.data == "ctrl_backups")
async def ctrl_backups(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    await state.update_data(ctrl_backup_restore_filename=None)
    os.makedirs(AUDIT_BACKUP_DIR,exist_ok=True)
    files=sorted([n for n in os.listdir(AUDIT_BACKUP_DIR) if n.endswith(".sqlite3")],reverse=True)
    blocks=[_rich_gallery_heading("💾 Резервные копии"),_rich_paragraph(f"Хранится копий: {len(files)}. Максимум: {AUDIT_BACKUP_MAX_FILES}.")]
    for index,name in enumerate(files[:AUDIT_BACKUP_MAX_FILES]):
        blocks.append(InputRichBlockButtons(buttons=[_rich_button(name[:55],f"ctrl_backup_detail:{index}")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("➕ Создать копию сейчас","ctrl_backup_create","success")]))
    blocks.append(InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля","ctrl_center")]))
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,blocks); await callback.answer()


@dp.callback_query(F.data == "ctrl_backup_create")
async def ctrl_backup_create(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try:
        path=await _create_database_backup("manual", actor_id=callback.from_user.id)
    except Exception as exc:
        logging.exception("Не удалось создать резервную копию")
        await callback.answer("Не удалось создать резервную копию.",show_alert=True); return
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("✅ Резервная копия создана"),_rich_paragraph(os.path.basename(path)),
        InputRichBlockButtons(buttons=[_rich_button("💾 К резервным копиям","ctrl_backups")]),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Центр контроля","ctrl_center")])])
    await callback.answer("Копия создана")


@dp.callback_query(F.data.startswith("ctrl_backup_detail:"))
async def ctrl_backup_detail(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    try: index=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректная копия.",show_alert=True); return
    files=sorted([n for n in os.listdir(AUDIT_BACKUP_DIR) if n.endswith(".sqlite3")],reverse=True)
    if not 0<=index<len(files):
        await callback.answer("Копия не найдена.",show_alert=True); return
    name=files[index]
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("💾 Резервная копия"),_rich_paragraph(f"Файл: {name}\nВосстановление заменит текущее содержимое базы данных содержимым этой копии. Перед этим будет создана копия текущей БД."),
        InputRichBlockButtons(buttons=[_rich_button("⚠️ Подготовить восстановление",f"ctrl_backup_restore:{index}","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("⬅️ К копиям","ctrl_backups")])])
    await callback.answer()


@dp.callback_query(F.data.startswith("ctrl_backup_restore:"))
async def ctrl_backup_restore_confirm(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    try: index=int(callback.data.split(":",1)[1])
    except Exception:
        await callback.answer("Некорректная копия.",show_alert=True); return
    files=sorted([n for n in os.listdir(AUDIT_BACKUP_DIR) if n.endswith(".sqlite3")],reverse=True)
    if not 0<=index<len(files):
        await callback.answer("Копия не найдена.",show_alert=True); return
    selected_name = files[index]
    await state.update_data(ctrl_backup_restore_filename=selected_name)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("⚠️ Подтверждение восстановления"),_rich_paragraph(f"Восстановить {selected_name}? Все записи БД изменятся на состояние этой копии. Текущее состояние предварительно будет сохранено."),
        InputRichBlockButtons(buttons=[_rich_button("⚠️ Да, восстановить БД","ctrl_backup_restore_yes","danger")]),
        InputRichBlockButtons(buttons=[_rich_button("❌ Отмена","ctrl_backups","danger")])])
    await callback.answer()


@dp.callback_query(F.data == "ctrl_backup_restore_yes")
async def ctrl_backup_restore_yes(callback: types.CallbackQuery, state: FSMContext):
    if not await _require_control_admin(callback): return
    data = await state.get_data()
    selected_name = data.get("ctrl_backup_restore_filename")
    if not selected_name or os.path.basename(str(selected_name)) != str(selected_name) or not str(selected_name).endswith(".sqlite3"):
        await callback.answer("Выбор резервной копии устарел. Открой список копий заново.",show_alert=True); return
    backup_path=os.path.join(AUDIT_BACKUP_DIR,str(selected_name))
    if not os.path.isfile(backup_path):
        await callback.answer("Копия не найдена. Открой список копий заново.",show_alert=True); return
    try:
        # Snapshot the current DB first. Use SQLite Backup API instead of replacing the file handle.
        current_path=await _create_database_backup("before_restore", actor_id=callback.from_user.id)
        def _restore_sync():
            source=sqlite3.connect(backup_path,timeout=30)
            target=sqlite3.connect(DB_NAME,timeout=30)
            try:
                check=source.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok": raise RuntimeError("Backup integrity check failed")
                source.backup(target)
            finally:
                target.close(); source.close()
        await asyncio.to_thread(_restore_sync)
        # Ensure the restored database receives idempotent current-version table/index migrations.
        await init_db()
        await write_audit_event(category="admin",event_type="database_backup_restored",actor_id=callback.from_user.id,
                                actor_name=_audit_user_name(callback.from_user),object_type="database_backup",
                                object_id=str(selected_name),details={"safety_backup":os.path.basename(current_path)},admin_id=ADMIN_ID,
                                retention_days=AUDIT_DATA_RETENTION_DAYS)
    except Exception:
        logging.exception("Не удалось восстановить БД")
        await callback.answer("Не удалось восстановить базу. Проверь логи.",show_alert=True); return
    await state.update_data(ctrl_backup_restore_filename=None)
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("✅ Восстановление завершено"),_rich_paragraph(f"Восстановлена копия {selected_name}. Перед операцией создана резервная копия текущей БД."),
        InputRichBlockButtons(buttons=[_rich_button("🛡 Центр контроля","ctrl_center")])])
    await callback.answer("База восстановлена")


@dp.callback_query(F.data == "ctrl_status")
async def ctrl_status(callback: types.CallbackQuery):
    if not await _require_control_admin(callback): return
    db_ok=True; db_error=None
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            quick_check = await db.execute("PRAGMA quick_check")
            quick_row = await quick_check.fetchone()
            if not quick_row or quick_row[0] != "ok":
                raise RuntimeError("SQLite quick_check failed")
            cur=await db.execute("SELECT value FROM control_status WHERE key='last_audit_cleanup_utc'")
            cleanup=(await cur.fetchone())
            cur=await db.execute("SELECT COUNT(*) FROM audit_events WHERE result='error' AND timestamp_utc >= ?",((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
            errors=(await cur.fetchone())[0]
    except Exception as exc:
        db_ok=False; db_error=type(exc).__name__; cleanup=None; errors=-1
    nav_enabled=(await _audit_get_setting("navigation_enabled","1"))=="1"
    await _edit_rich_message(callback.message.chat.id,callback.message.message_id,[
        _rich_gallery_heading("⚙️ Состояние системы"),
        _rich_paragraph(f"База данных: {'✅ доступна' if db_ok else '❌ ошибка: '+str(db_error)}"),
        _rich_paragraph(f"Планировщик: {'запущен' if scheduler.running else 'не запущен'}"),
        _rich_paragraph(f"Ошибок за 24 часа: {errors if errors >= 0 else 'не удалось получить'}"),
        _rich_paragraph(f"Последняя очистка журналов: {cleanup[0] if cleanup else 'не записана'}"),
        _rich_paragraph(f"События навигации: {'ВКЛ' if nav_enabled else 'ВЫКЛ'}"),
        InputRichBlockButtons(buttons=[_rich_button("⚙️ Настройки журналирования","ctrl_audit_settings")]),
        InputRichBlockButtons(buttons=[_rich_button("↩️ Центр контроля","ctrl_center")])])
    await callback.answer()


# Wrap the existing gallery cleanup so audit/history retention is cleaned on the same schedule.
_original_cleanup_gallery_old_photos_v12 = cleanup_gallery_old_photos
async def cleanup_gallery_old_photos():
    current_week_start = _gallery_current_week_start().isoformat()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT COUNT(*) FROM gallery_photos WHERE photo_date < ?", (current_week_start,))
            photos_before = (await cur.fetchone())[0]
        await _original_cleanup_gallery_old_photos_v12()
        deleted_events, deleted_history = await _audit_cleanup_expired()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR REPLACE INTO control_status(key,value) VALUES('last_gallery_cleanup_utc',?)",(_audit_now_utc(),))
            await _insert_audit_event_db(db,category="system",event_type="gallery_cleanup_completed",result="success",
                                         details={"current_week_start":current_week_start,"photos_removed":photos_before,
                                                  "expired_audit_events_removed":deleted_events,"expired_history_rows_removed":deleted_history},
                                         retention_days=AUDIT_DATA_RETENTION_DAYS)
            await db.commit()
    except Exception:
        logging.exception("Не удалось выполнить/записать статус очистки галереи и журнала")
        await write_audit_event(category="system",event_type="gallery_cleanup_failed",result="error",
                                details={"current_week_start":current_week_start},retention_days=AUDIT_DATA_RETENTION_DAYS)


async def main():
    global scheduler
    await init_db()
    await write_audit_event(category="system", event_type="bot_started", result="success", details={"version": "v12"}, retention_days=AUDIT_DATA_RETENTION_DAYS)
    await cleanup_gallery_old_photos()
    # --- ЗАПУСКАЕМ ПЛАНИРОВЩИК ---
    scheduler = AsyncIOScheduler(timezone=MINSK_TZ)
    # Ставим проверку каждый день в 08:00 утра
    scheduler.add_job(check_birthdays_job, 'cron', hour=6, minute=0)
    scheduler.add_job(send_reminder_job, 'cron', minute='*')
    scheduler.add_job(
        cleanup_gallery_old_photos,
        'cron', day_of_week='mon', hour=0, minute=10,
        id='gallery_weekly_cleanup', replace_existing=True,
    )
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
      
