import asyncio
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOG_PATH = os.getenv("BOT_LOG_PATH", "bot.log")
ANALYTICS_DB_PATH = os.getenv("ANALYTICS_DB_PATH", "analytics.sqlite")
SUPPORT_REMINDER_SECONDS = int(os.getenv("SUPPORT_REMINDER_SECONDS", "600"))
SUPPORT_REMINDER_MAX = int(os.getenv("SUPPORT_REMINDER_MAX", "3"))
TEXTS_PATH = os.getenv("BOT_TEXTS_PATH", "texts.json")
DEFAULT_LANG = os.getenv("BOT_DEFAULT_LANG", "ru").lower()
TEXTS_EN_PATH = os.getenv("BOT_TEXTS_EN_PATH", "texts.en.json")
TEXTS_KZ_PATH = os.getenv("BOT_TEXTS_KZ_PATH", "texts.kz.json")
TEXTS_AZ_PATH = os.getenv("BOT_TEXTS_AZ_PATH", "texts.az.json")
TEXTS_UZ_PATH = os.getenv("BOT_TEXTS_UZ_PATH", "texts.uz.json")


def load_texts(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_optional_texts(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    return load_texts(path)


TEXTS_BY_LANG: dict[str, dict] = {"ru": load_texts(TEXTS_PATH)}
texts_en = load_optional_texts(TEXTS_EN_PATH)
if texts_en:
    TEXTS_BY_LANG["en"] = texts_en
texts_kz = load_optional_texts(TEXTS_KZ_PATH)
if texts_kz:
    TEXTS_BY_LANG["kz"] = texts_kz

texts_az = load_optional_texts(TEXTS_AZ_PATH)
if texts_az:
    TEXTS_BY_LANG["az"] = texts_az

texts_uz = load_optional_texts(TEXTS_UZ_PATH)
if texts_uz:
    TEXTS_BY_LANG["uz"] = texts_uz

if DEFAULT_LANG not in TEXTS_BY_LANG:
    DEFAULT_LANG = "ru"


def configure_logging(log_path: str) -> None:
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)


def init_analytics_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                subject TEXT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                chat_id INTEGER,
                data TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_event(
    db_path: str,
    event_type: str,
    user,
    chat_id: Optional[int],
    subject: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    payload = json.dumps(data or {})
    user_id = user.id if user else None
    username = user.username if user else None
    full_name = user.full_name if user else None
    ts = datetime.now(timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO events (ts, event_type, subject, user_id, username, full_name, chat_id, data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, event_type, subject, user_id, username, full_name, chat_id, payload),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to record analytics event: %s", event_type)


def get_stats(db_path: str, days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        total = cursor.execute(
            "SELECT COUNT(*) FROM events WHERE ts >= ?",
            (since,),
        ).fetchone()[0]
        unique_users = cursor.execute(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ? AND user_id IS NOT NULL",
            (since,),
        ).fetchone()[0]
        by_event = cursor.execute(
            """
            SELECT event_type, COUNT(*)
            FROM events
            WHERE ts >= ?
            GROUP BY event_type
            ORDER BY COUNT(*) DESC
            """,
            (since,),
        ).fetchall()
        top_faq = cursor.execute(
            """
            SELECT subject, COUNT(*)
            FROM events
            WHERE event_type = ? AND ts >= ?
            GROUP BY subject
            ORDER BY COUNT(*) DESC
            LIMIT 5
            """,
            (EVENT_FAQ_ANSWER, since),
        ).fetchall()
        top_install = cursor.execute(
            """
            SELECT subject, COUNT(*)
            FROM events
            WHERE event_type = ? AND ts >= ?
            GROUP BY subject
            ORDER BY COUNT(*) DESC
            LIMIT 5
            """,
            (EVENT_INSTALL_ANSWER, since),
        ).fetchall()
        helpful = cursor.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ? AND ts >= ?",
            (EVENT_FEEDBACK_HELPFUL, since),
        ).fetchone()[0]
        unhelpful = cursor.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ? AND ts >= ?",
            (EVENT_FEEDBACK_UNHELPFUL, since),
        ).fetchone()[0]
    finally:
        conn.close()

    return {
        "since": since,
        "total": total,
        "unique_users": unique_users,
        "by_event": by_event,
        "top_faq": top_faq,
        "top_install": top_install,
        "helpful": helpful,
        "unhelpful": unhelpful,
    }


configure_logging(LOG_PATH)


class Support(StatesGroup):
    waiting_message = State()


@dataclass
class Answer:
    text: str
    media_path: Optional[str] = None
    media_type: Optional[str] = None  # "photo" or "video"


def build_inline_keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dataclass
class Localized:
    texts: dict
    menus: dict[str, InlineKeyboardMarkup]
    answers: dict[str, Answer]
    install_answers: dict[str, Answer]
    subject_labels: dict[str, str]
    messages: dict[str, str]


def build_localized(texts: dict) -> Localized:
    menus = {
        "main": build_inline_keyboard(texts["menus"]["main"]),
        "install": build_inline_keyboard(texts["menus"]["install"]),
        "support": build_inline_keyboard(texts["menus"]["support"]),
        "support_reminder": build_inline_keyboard(texts["menus"]["support_reminder"]),
        "answer": build_inline_keyboard(texts["menus"]["answer"]),
    }
    answers = {key: Answer(**value) for key, value in texts["answers"].items()}
    install_answers = {
        key: Answer(**value) for key, value in texts["install_answers"].items()
    }
    return Localized(
        texts=texts,
        menus=menus,
        answers=answers,
        install_answers=install_answers,
        subject_labels=texts["subject_labels"],
        messages=texts["messages"],
    )


LOCALIZED_CACHE: dict[str, Localized] = {}
USER_LANG: dict[int, str] = {}


def detect_language_code(user) -> str:
    if not user or not user.language_code:
        return DEFAULT_LANG
    code = user.language_code.lower()
    for prefix in ("kk", "en", "ru"):
        if code.startswith(prefix):
            return prefix
    return DEFAULT_LANG


def get_user_lang(user) -> str:
    if user and user.id in USER_LANG:
        return USER_LANG[user.id]
    return detect_language_code(user)


def get_localized_by_lang(lang: str) -> Localized:
    normalized = lang if lang in TEXTS_BY_LANG else DEFAULT_LANG
    if normalized not in LOCALIZED_CACHE:
        LOCALIZED_CACHE[normalized] = build_localized(TEXTS_BY_LANG[normalized])
    return LOCALIZED_CACHE[normalized]


def get_localized_for_user(user) -> Localized:
    return get_localized_by_lang(get_user_lang(user))


def build_answer_menu(localized: Localized, subject: str) -> InlineKeyboardMarkup:
    return build_inline_keyboard(
        [
            [
                (
                    localized.messages["feedback_helpful_button"],
                    f"{FEEDBACK_HELPFUL_PREFIX}{subject}",
                ),
                (
                    localized.messages["feedback_unhelpful_button"],
                    f"{FEEDBACK_UNHELPFUL_PREFIX}{subject}",
                ),
            ],
            [(localized.messages["back_to_menu_button"], MAIN_MENU_OPEN)],
        ]
    )


MAIN_KEYS = "main:keys"
MAIN_INSTALL = "main:install"
MAIN_RENEW = "main:renew"
MAIN_INVITE = "main:invite"
MAIN_MENU_OPEN = "main:menu"
SUPPORT_START = "support:start"
SUPPORT_CANCEL = "support:cancel"
SUPPORT_RESOLVED = "support:resolved"
FEEDBACK_HELPFUL_PREFIX = "feedback:yes:"
FEEDBACK_UNHELPFUL_PREFIX = "feedback:no:"
INSTALL_BACK = "install:back"

EVENT_START = "start"
EVENT_FAQ_ANSWER = "faq_answer"
EVENT_INSTALL_ANSWER = "install_answer"
EVENT_SUPPORT_START = "support_start"
EVENT_SUPPORT_CANCEL = "support_cancel"
EVENT_SUPPORT_SUBMIT = "support_submit"
EVENT_SUPPORT_NON_TEXT = "support_non_text"
EVENT_INSTALL_MENU = "install_menu"
EVENT_FALLBACK_MESSAGE = "fallback_message"
EVENT_FALLBACK_CALLBACK = "fallback_callback"
EVENT_STATS = "stats_request"
EVENT_MAIN_MENU_OPEN = "main_menu_open"
EVENT_SUPPORT_REMINDER = "support_reminder"
EVENT_SUPPORT_RESOLVED = "support_resolved"
EVENT_FEEDBACK_HELPFUL = "feedback_helpful"
EVENT_FEEDBACK_UNHELPFUL = "feedback_unhelpful"

CANCEL_TRIGGERS = {
    texts["messages"]["support_cancel_trigger"].casefold()
    for texts in TEXTS_BY_LANG.values()
    if "messages" in texts and "support_cancel_trigger" in texts["messages"]
}
if not CANCEL_TRIGGERS:
    CANCEL_TRIGGERS = {"cancel"}

ANSWER_KEYS = set(TEXTS_BY_LANG[DEFAULT_LANG]["answers"].keys())
INSTALL_ANSWER_KEYS = set(TEXTS_BY_LANG[DEFAULT_LANG]["install_answers"].keys())

LANG_SELECT_PREFIX = "lang:"
LANGUAGE_MENU_ROWS = [
    [("Русский", f"{LANG_SELECT_PREFIX}ru"), ("Қазақша", f"{LANG_SELECT_PREFIX}kk")],
    [("English", f"{LANG_SELECT_PREFIX}en"), ("O'zbek tili", f"{LANG_SELECT_PREFIX}uz")],
    [("Azərbaycan",f"{LANG_SELECT_PREFIX}az")],
]
LANGUAGE_MENU = build_inline_keyboard(LANGUAGE_MENU_ROWS)
LANGUAGE_PROMPT = "Выберите язык / Choose language / Тілді таңдаңыз / Tilni tanlang / Dili seçin"


def subject_label(localized: Localized, subject: Optional[str]) -> str:
    if not subject:
        return localized.messages["unknown_subject"]
    return localized.subject_labels.get(subject, subject)


LAST_BOT_MESSAGE_ID: dict[int, int] = {}
SUPPORT_PENDING: set[tuple[int, int]] = set()
SUPPORT_REMINDER_TASKS: dict[tuple[int, int], asyncio.Task] = {}
SUPPORT_REMINDER_COUNTS: dict[tuple[int, int], int] = {}
SUPPORT_LANGS: dict[tuple[int, int], str] = {}


async def cleanup_previous_message(bot: Bot, chat_id: int) -> None:
    if not chat_id:
        return
    message_id = LAST_BOT_MESSAGE_ID.get(chat_id)
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        logger.warning("Failed to delete message %s in chat %s", message_id, chat_id)
    finally:
        LAST_BOT_MESSAGE_ID.pop(chat_id, None)


async def send_text(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Message:
    if message.chat:
        await cleanup_previous_message(message.bot, message.chat.id)
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    if message.chat:
        LAST_BOT_MESSAGE_ID[message.chat.id] = sent.message_id
    return sent


async def send_text_by_chat(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    await cleanup_previous_message(bot, chat_id)
    sent = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
    LAST_BOT_MESSAGE_ID[chat_id] = sent.message_id


def text_preview(text: str, limit: int = 200) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def log_message_event(
    message: Message,
    event_type: str,
    subject: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    chat_id = message.chat.id if message.chat else None
    record_event(ANALYTICS_DB_PATH, event_type, message.from_user, chat_id, subject, data)


def log_callback_event(
    call: CallbackQuery,
    event_type: str,
    subject: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    chat_id = call.message.chat.id if call.message and call.message.chat else None
    record_event(ANALYTICS_DB_PATH, event_type, call.from_user, chat_id, subject, data)


def support_key(chat_id: int, user_id: int) -> tuple[int, int]:
    return (chat_id, user_id)


def cancel_support_reminder(key: tuple[int, int]) -> None:
    task = SUPPORT_REMINDER_TASKS.pop(key, None)
    if task:
        task.cancel()


def clear_support_pending(chat_id: int, user_id: int) -> None:
    key = support_key(chat_id, user_id)
    SUPPORT_PENDING.discard(key)
    SUPPORT_REMINDER_COUNTS.pop(key, None)
    SUPPORT_LANGS.pop(key, None)
    cancel_support_reminder(key)


async def schedule_support_reminder(
    bot: Bot, chat_id: int, user_id: int, lang: str
) -> None:
    key = support_key(chat_id, user_id)
    SUPPORT_PENDING.add(key)
    SUPPORT_LANGS[key] = lang
    cancel_support_reminder(key)
    if key not in SUPPORT_REMINDER_COUNTS:
        SUPPORT_REMINDER_COUNTS[key] = 0
    if SUPPORT_REMINDER_COUNTS[key] >= SUPPORT_REMINDER_MAX:
        return
    SUPPORT_REMINDER_TASKS[key] = asyncio.create_task(
        run_support_reminder(bot, chat_id, user_id)
    )


async def run_support_reminder(bot: Bot, chat_id: int, user_id: int) -> None:
    try:
        key = support_key(chat_id, user_id)
        while True:
            await asyncio.sleep(SUPPORT_REMINDER_SECONDS)
            if key not in SUPPORT_PENDING:
                return
            count = SUPPORT_REMINDER_COUNTS.get(key, 0)
            if count >= SUPPORT_REMINDER_MAX:
                return
            SUPPORT_REMINDER_COUNTS[key] = count + 1
            lang = SUPPORT_LANGS.get(key, DEFAULT_LANG)
            localized = get_localized_by_lang(lang)
            record_event(
                ANALYTICS_DB_PATH,
                EVENT_SUPPORT_REMINDER,
                None,
                chat_id,
                data={"user_id": user_id, "count": count + 1},
            )
            await send_text_by_chat(
                bot,
                chat_id,
                localized.messages["support_reminder"],
                reply_markup=localized.menus["support_reminder"],
            )
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Failed to send support reminder")
    finally:
        SUPPORT_REMINDER_TASKS.pop(support_key(chat_id, user_id), None)


async def send_answer(
    message: Message, localized: Localized, answer: Answer, subject: str
) -> None:
    if message.chat:
        await cleanup_previous_message(message.bot, message.chat.id)

    reply_markup = build_answer_menu(localized, subject)
    if answer.media_path and os.path.exists(answer.media_path):
        media = FSInputFile(answer.media_path)
        if answer.media_type == "photo":
            sent = await message.answer_photo(
                media,
                caption=answer.text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        elif answer.media_type == "video":
            sent = await message.answer_video(
                media,
                caption=answer.text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        else:
            logger.warning("Unknown media_type: %s", answer.media_type)
            sent = await message.answer(
                answer.text,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
    else:
        if answer.media_path:
            logger.warning("Media file not found: %s", answer.media_path)
        sent = await message.answer(
            answer.text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    if message.chat:
        LAST_BOT_MESSAGE_ID[message.chat.id] = sent.message_id


def build_support_payload(message: Message, localized: Localized) -> str:
    user = message.from_user
    username = (
        f"@{user.username}"
        if user and user.username
        else localized.messages["unknown_subject"]
    )
    text = message.text or localized.messages["support_payload_non_text_fallback"]
    template = localized.messages["support_payload_template"]
    user_id = user.id if user else localized.messages["unknown_subject"]

    return template.format(user_id=user_id, username=username, text=text)


async def main() -> None:
    bot_token = os.getenv("BOT_TOKEN")
    admin_chat_id_raw = os.getenv("ADMIN_CHAT_ID")

    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set")
    if not admin_chat_id_raw:
        raise RuntimeError("ADMIN_CHAT_ID is not set")
    try:
        admin_chat_id = int(admin_chat_id_raw)
    except ValueError as exc:
        raise RuntimeError("ADMIN_CHAT_ID must be an integer") from exc
    init_analytics_db(ANALYTICS_DB_PATH)

    bot = Bot(token=bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    router = Router()

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        log_message_event(message, EVENT_START)
        if message.from_user:
            USER_LANG.pop(message.from_user.id, None)
        await send_text(message, LANGUAGE_PROMPT, reply_markup=LANGUAGE_MENU)

    @router.callback_query(F.data.startswith(LANG_SELECT_PREFIX))
    async def language_select_callback(call: CallbackQuery, state: FSMContext) -> None:
        lang = call.data[len(LANG_SELECT_PREFIX) :]
        if lang not in TEXTS_BY_LANG:
            lang = DEFAULT_LANG
        if call.from_user:
            USER_LANG[call.from_user.id] = lang
        localized = get_localized_by_lang(lang)
        await state.clear()
        await send_text(
            call.message,
            localized.messages["welcome"],
            reply_markup=localized.menus["main"],
        )
        await call.answer()

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        localized = get_localized_for_user(message.from_user)
        stats_localized = get_localized_by_lang("ru")
        if message.chat.id != admin_chat_id and user_id != admin_chat_id:
            await send_text(
                message,
                stats_localized.messages["stats_unauthorized"],
                reply_markup=localized.menus["main"],
            )
            return

        stats = get_stats(ANALYTICS_DB_PATH, days=7)
        log_message_event(message, EVENT_STATS, data={"days": 7})

        lines = [
            stats_localized.messages["stats_header"],
            stats_localized.messages["stats_total"].format(total=stats["total"]),
            stats_localized.messages["stats_unique_users"].format(
                unique_users=stats["unique_users"]
            ),
        ]
        if stats["by_event"]:
            lines.append("")
            lines.append(stats_localized.messages["stats_by_event_title"])
            for event_type, count in stats["by_event"]:
                lines.append(f"- {event_type}: {count}")
        if stats["top_faq"]:
            lines.append("")
            lines.append(stats_localized.messages["stats_top_faq_title"])
            for subject, count in stats["top_faq"]:
                lines.append(f"- {subject_label(stats_localized, subject)}: {count}")
        if stats["top_install"]:
            lines.append("")
            lines.append(stats_localized.messages["stats_top_install_title"])
            for subject, count in stats["top_install"]:
                lines.append(f"- {subject_label(stats_localized, subject)}: {count}")
        lines.append("")
        lines.append(
            stats_localized.messages["stats_feedback"].format(
                helpful=stats["helpful"],
                unhelpful=stats["unhelpful"],
            )
        )

        await send_text(message, "\n".join(lines), reply_markup=localized.menus["main"])

    @router.message(Support.waiting_message, F.text.casefold().in_(CANCEL_TRIGGERS))
    async def support_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        log_message_event(message, EVENT_SUPPORT_CANCEL)
        if message.chat and message.from_user:
            clear_support_pending(message.chat.id, message.from_user.id)
        localized = get_localized_for_user(message.from_user)
        await send_text(
            message,
            localized.messages["support_cancel"],
            reply_markup=localized.menus["main"],
        )

    @router.message(Support.waiting_message)
    async def support_message(message: Message, state: FSMContext) -> None:
        localized = get_localized_for_user(message.from_user)
        lang = get_user_lang(message.from_user)
        if not message.text:
            log_message_event(
                message,
                EVENT_SUPPORT_NON_TEXT,
                data={"content_type": message.content_type},
            )
            await send_text(
                message,
                localized.messages["support_non_text"],
                reply_markup=localized.menus["support"],
            )
            if message.chat and message.from_user:
                await schedule_support_reminder(
                    message.bot, message.chat.id, message.from_user.id, lang
                )
            return

        log_message_event(
            message,
            EVENT_SUPPORT_SUBMIT,
            data={"text_len": len(message.text), "text_preview": text_preview(message.text)},
        )
        if message.chat and message.from_user:
            clear_support_pending(message.chat.id, message.from_user.id)
        await bot.send_message(admin_chat_id, build_support_payload(message, localized))
        await state.clear()
        await send_text(
            message,
            localized.messages["support_submit_thanks"],
            reply_markup=localized.menus["main"],
        )

    @router.callback_query(F.data == SUPPORT_START)
    async def support_start_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Support.waiting_message)
        log_callback_event(call, EVENT_SUPPORT_START)
        localized = get_localized_for_user(call.from_user)
        lang = get_user_lang(call.from_user)
        await send_text(
            call.message,
            localized.messages["support_start_prompt"],
            reply_markup=localized.menus["support"],
        )
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
            await schedule_support_reminder(
                call.bot, call.message.chat.id, call.from_user.id, lang
            )
        await call.answer()

    @router.callback_query(F.data == SUPPORT_RESOLVED)
    async def support_resolved_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_SUPPORT_RESOLVED)
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
        localized = get_localized_for_user(call.from_user)
        await send_text(
            call.message,
            localized.messages["support_resolved"],
            reply_markup=localized.menus["main"],
        )
        await call.answer()

    @router.callback_query(F.data == SUPPORT_CANCEL)
    async def support_cancel_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_SUPPORT_CANCEL)
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
        localized = get_localized_for_user(call.from_user)
        await send_text(
            call.message,
            localized.messages["support_cancel_callback"],
            reply_markup=localized.menus["main"],
        )
        await call.answer()

    @router.callback_query(F.data == MAIN_INSTALL)
    async def install_menu_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_INSTALL_MENU)
        localized = get_localized_for_user(call.from_user)
        await send_text(
            call.message,
            localized.messages["install_menu_prompt"],
            reply_markup=localized.menus["install"],
        )
        await call.answer()

    @router.callback_query(F.data.in_((MAIN_MENU_OPEN, INSTALL_BACK)))
    async def main_menu_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_MAIN_MENU_OPEN, data={"source": call.data})
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
        localized = get_localized_for_user(call.from_user)
        await send_text(
            call.message,
            localized.messages["main_menu"],
            reply_markup=localized.menus["main"],
        )
        await call.answer()

    @router.callback_query(F.data.in_(INSTALL_ANSWER_KEYS))
    async def install_answer_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_INSTALL_ANSWER, subject=call.data)
        localized = get_localized_for_user(call.from_user)
        answer = localized.install_answers[call.data]
        await send_answer(call.message, localized, answer, call.data)
        await call.answer()

    @router.callback_query(F.data.in_(ANSWER_KEYS))
    async def main_answer_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_FAQ_ANSWER, subject=call.data)
        localized = get_localized_for_user(call.from_user)
        answer = localized.answers[call.data]
        await send_answer(call.message, localized, answer, call.data)
        await call.answer()

    @router.callback_query(F.data.startswith(FEEDBACK_HELPFUL_PREFIX))
    async def feedback_helpful_callback(call: CallbackQuery) -> None:
        subject = call.data[len(FEEDBACK_HELPFUL_PREFIX) :]
        log_callback_event(call, EVENT_FEEDBACK_HELPFUL, subject=subject)
        localized = get_localized_for_user(call.from_user)
        if call.message:
            try:
                await call.message.edit_reply_markup(reply_markup=localized.menus["answer"])
            except Exception:
                logger.warning("Failed to update feedback menu")
        await call.answer(localized.messages["feedback_thanks"])

    @router.callback_query(F.data.startswith(FEEDBACK_UNHELPFUL_PREFIX))
    async def feedback_unhelpful_callback(call: CallbackQuery) -> None:
        subject = call.data[len(FEEDBACK_UNHELPFUL_PREFIX) :]
        log_callback_event(call, EVENT_FEEDBACK_UNHELPFUL, subject=subject)
        localized = get_localized_for_user(call.from_user)
        if call.message:
            try:
                await call.message.edit_reply_markup(reply_markup=localized.menus["answer"])
            except Exception:
                logger.warning("Failed to update feedback menu")
        await call.answer(localized.messages["feedback_thanks"])

    @router.callback_query()
    async def fallback_callback(call: CallbackQuery) -> None:
        log_callback_event(call, EVENT_FALLBACK_CALLBACK, data={"callback_data": call.data})
        localized = get_localized_for_user(call.from_user)
        await call.answer(localized.messages["fallback_callback"], show_alert=False)

    @router.message(StateFilter(None), F.text)
    async def fallback(message: Message) -> None:
        log_message_event(
            message,
            EVENT_FALLBACK_MESSAGE,
            data={"text_len": len(message.text), "text_preview": text_preview(message.text)},
        )
        localized = get_localized_for_user(message.from_user)
        await send_text(
            message,
            localized.messages["fallback_message"],
            reply_markup=localized.menus["main"],
        )

    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
