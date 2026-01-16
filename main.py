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


def build_answer_menu(subject: str) -> InlineKeyboardMarkup:
    return build_inline_keyboard(
        [
            [
                ("Помогло", f"{FEEDBACK_HELPFUL_PREFIX}{subject}"),
                ("Не помогло", f"{FEEDBACK_UNHELPFUL_PREFIX}{subject}"),
            ],
            [("Главное меню", MAIN_MENU_OPEN)],
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

MAIN_MENU_ROWS = [
    [("Не работает ни один из ключей", MAIN_KEYS)],
    [("Как установить приложение", MAIN_INSTALL)],
    [("Как продлить подписку", MAIN_RENEW)],
    [("Как пригласить человека", MAIN_INVITE)],
    [("Не нашли ответ на ваш вопрос", SUPPORT_START)],
]

INSTALL_MENU_ROWS = [
    [("iOS", "install:ios"), ("Android", "install:android")],
    [("Windows", "install:windows"), ("macOS", "install:macos")],
    [("Linux", "install:linux")],
    [("Назад", INSTALL_BACK)],
    [("Ответ мне не подходит", SUPPORT_START)],
]

SUPPORT_MENU_ROWS = [
    [("Отмена", SUPPORT_CANCEL)],
]

SUPPORT_REMINDER_MENU_ROWS = [
    [("Задача решена", SUPPORT_RESOLVED)],
    [("Отмена", SUPPORT_CANCEL)],
]

MAIN_MENU = build_inline_keyboard(MAIN_MENU_ROWS)
INSTALL_MENU = build_inline_keyboard(INSTALL_MENU_ROWS)
SUPPORT_MENU = build_inline_keyboard(SUPPORT_MENU_ROWS)
SUPPORT_REMINDER_MENU = build_inline_keyboard(SUPPORT_REMINDER_MENU_ROWS)
ANSWER_MENU = build_inline_keyboard([[("Главное меню", MAIN_MENU_OPEN)]])

ANSWERS = {
    MAIN_KEYS: Answer(
        text=(
            "1) Проверьте, что интернет работает без VPN.\n"
            "2) Если проблема остаётся, запросите новый ключ у поддержки @modern_1mctech"
        ),
        media_path="media/faq1.png",
        media_type="photo",
    ),
    MAIN_RENEW: Answer(
        text=(
            "Подписку можно продлить через менеджера или личный кабинет.\n"
            "Если у вас нет ссылки на оплату, напишите в поддержку — пришлём её."
        ),
        media_path="media/faq2.png",
        media_type="photo",
    ),
    MAIN_INVITE: Answer(
        text=(
            "Откройте бота @LockDown_VPN_Bbot → Главная → Пригласить друга.\n"
            "Скопируйте ссылку-приглашение и отправьте её человеку."
        ),
        media_path="media/faq3.png",
        media_type="photo",
    ),
}

INSTALL_ANSWERS = {
    "install:ios": Answer(
        text=(
            "1) Откройте App Store и установите приложение VPN.\n"
            "2) Запустите приложение и добавьте ключ из письма/чата.\n"
            "3) Включите VPN и подтвердите добавление конфигурации."
        ),
        media_path="media/install_ios.mp4",
        media_type="photo",
    ),
    "install:android": Answer(
        text=(
            "1) Установите приложение из Google Play.\n"
            "2) Импортируйте ключ и разрешите создание VPN.\n"
            "3) Включите VPN в приложении."
        ),
        media_path="media/install_android.mp4",
        media_type="photo",
    ),
    "install:windows": Answer(
        text=(
            "1) Установите приложение для Windows.\n"
            "2) Добавьте ключ через кнопку Import.\n"
            "3) Подключитесь и проверьте статус."
        ),
        media_path="media/install_windows.mp4",
        media_type="photo",
    ),
    "install:macos": Answer(
        text=(
            "1) Установите приложение для macOS.\n"
            "2) Импортируйте ключ и разрешите системное расширение, если нужно.\n"
            "3) Включите VPN и проверьте соединение."
        ),
        media_path="media/install_macos.mp4",
        media_type="photo",
    ),
    "install:linux": Answer(
        text=(
            "1) Установите клиент согласно вашей системе.\n"
            "2) Импортируйте ключ через CLI или GUI.\n"
            "3) Подключитесь и проверьте внешний IP."
        ),
        media_path="media/install_linux.mp4",
        media_type="photo",
    ),
}

SUBJECT_LABELS = {
    MAIN_KEYS: "Не работает ни один из ключей",
    MAIN_RENEW: "Как продлить подписку",
    MAIN_INVITE: "Как пригласить человека",
    "install:ios": "iOS",
    "install:android": "Android",
    "install:windows": "Windows",
    "install:macos": "macOS",
    "install:linux": "Linux",
}


def subject_label(subject: Optional[str]) -> str:
    if not subject:
        return "—"
    return SUBJECT_LABELS.get(subject, subject)


LAST_BOT_MESSAGE_ID: dict[int, int] = {}
SUPPORT_PENDING: set[tuple[int, int]] = set()
SUPPORT_REMINDER_TASKS: dict[tuple[int, int], asyncio.Task] = {}
SUPPORT_REMINDER_COUNTS: dict[tuple[int, int], int] = {}


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
    sent = await message.answer(text, reply_markup=reply_markup)
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
    sent = await bot.send_message(chat_id, text, reply_markup=reply_markup)
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
    cancel_support_reminder(key)


async def schedule_support_reminder(bot: Bot, chat_id: int, user_id: int) -> None:
    key = support_key(chat_id, user_id)
    SUPPORT_PENDING.add(key)
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
                "Если нужна помощь, опишите проблему одним сообщением — мы передадим её в поддержку.",
                reply_markup=SUPPORT_REMINDER_MENU,
            )
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Failed to send support reminder")
    finally:
        SUPPORT_REMINDER_TASKS.pop(support_key(chat_id, user_id), None)


async def send_answer(message: Message, answer: Answer, subject: str) -> None:
    if message.chat:
        await cleanup_previous_message(message.bot, message.chat.id)

    reply_markup = build_answer_menu(subject)
    if answer.media_path and os.path.exists(answer.media_path):
        media = FSInputFile(answer.media_path)
        if answer.media_type == "photo":
            sent = await message.answer_photo(media, caption=answer.text, reply_markup=reply_markup)
        elif answer.media_type == "video":
            sent = await message.answer_video(media, caption=answer.text, reply_markup=reply_markup)
        else:
            logger.warning("Unknown media_type: %s", answer.media_type)
            sent = await message.answer(answer.text, reply_markup=reply_markup)
    else:
        if answer.media_path:
            logger.warning("Media file not found: %s", answer.media_path)
        sent = await message.answer(answer.text, reply_markup=reply_markup)

    if message.chat:
        LAST_BOT_MESSAGE_ID[message.chat.id] = sent.message_id


def build_support_payload(message: Message) -> str:
    user = message.from_user
    username = f"@{user.username}" if user and user.username else "—"
    full_name = user.full_name if user else "—"
    text = message.text or "<не текстовое сообщение>"

    return (
        f"#SUPREQUEST #USER{user.id if user else '—'}\n"
        f"Имя: {username}\n"
        f"Текст: {text}"
    )


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
        await send_text(
            message,
            "Здраствуйте! Мы готовы ответить на любой ваш вопрос. Если вы его не нашли в данном боте, оставьте обращение или напишите @modern_1mctech",
            reply_markup=MAIN_MENU,
        )

    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if message.chat.id != admin_chat_id and user_id != admin_chat_id:
            await send_text(message, "Команда доступна только администратору.", reply_markup=MAIN_MENU)
            return

        stats = get_stats(ANALYTICS_DB_PATH, days=7)
        log_message_event(message, EVENT_STATS, data={"days": 7})

        lines = [
            "Статистика за 7 дней:",
            f"События: {stats['total']}",
            f"Уникальные пользователи: {stats['unique_users']}",
        ]
        if stats["by_event"]:
            lines.append("")
            lines.append("По событиям:")
            for event_type, count in stats["by_event"]:
                lines.append(f"- {event_type}: {count}")
        if stats["top_faq"]:
            lines.append("")
            lines.append("Топ FAQ:")
            for subject, count in stats["top_faq"]:
                lines.append(f"- {subject_label(subject)}: {count}")
        if stats["top_install"]:
            lines.append("")
            lines.append("Топ установки:")
            for subject, count in stats["top_install"]:
                lines.append(f"- {subject_label(subject)}: {count}")
        lines.append("")
        lines.append(f"Отзывы: помогло {stats['helpful']}, не помогло {stats['unhelpful']}")

        await send_text(message, "\n".join(lines), reply_markup=MAIN_MENU)

    @router.message(Support.waiting_message, F.text.casefold() == "отмена")
    async def support_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        log_message_event(message, EVENT_SUPPORT_CANCEL)
        if message.chat and message.from_user:
            clear_support_pending(message.chat.id, message.from_user.id)
        await send_text(message, "Запрос отменён.", reply_markup=MAIN_MENU)

    @router.message(Support.waiting_message)
    async def support_message(message: Message, state: FSMContext) -> None:
        if not message.text:
            log_message_event(
                message,
                EVENT_SUPPORT_NON_TEXT,
                data={"content_type": message.content_type},
            )
            await send_text(message, "Опишите проблему текстом.", reply_markup=SUPPORT_MENU)
            if message.chat and message.from_user:
                await schedule_support_reminder(message.bot, message.chat.id, message.from_user.id)
            return

        log_message_event(
            message,
            EVENT_SUPPORT_SUBMIT,
            data={"text_len": len(message.text), "text_preview": text_preview(message.text)},
        )
        if message.chat and message.from_user:
            clear_support_pending(message.chat.id, message.from_user.id)
        await bot.send_message(admin_chat_id, build_support_payload(message))
        await state.clear()
        await send_text(message, "Спасибо! Мы уже получили ваше обращение.", reply_markup=MAIN_MENU)

    @router.callback_query(F.data == SUPPORT_START)
    async def support_start_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(Support.waiting_message)
        log_callback_event(call, EVENT_SUPPORT_START)
        await send_text(
            call.message,
            "Опишите проблему одним сообщением — мы передадим её в поддержку.",
            reply_markup=SUPPORT_MENU,
        )
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
            await schedule_support_reminder(call.bot, call.message.chat.id, call.from_user.id)
        await call.answer()

    @router.callback_query(F.data == SUPPORT_RESOLVED)
    async def support_resolved_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_SUPPORT_RESOLVED)
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
        await send_text(
            call.message,
            "Отлично! Если появятся вопросы — напишите нам в любое время.",
            reply_markup=MAIN_MENU,
        )
        await call.answer()

    @router.callback_query(F.data == SUPPORT_CANCEL)
    async def support_cancel_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_SUPPORT_CANCEL)
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
        await send_text(call.message, "Запрос отменён.", reply_markup=MAIN_MENU)
        await call.answer()

    @router.callback_query(F.data == MAIN_INSTALL)
    async def install_menu_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_INSTALL_MENU)
        await send_text(call.message, "Выберите платформу:", reply_markup=INSTALL_MENU)
        await call.answer()

    @router.callback_query(F.data.in_((MAIN_MENU_OPEN, INSTALL_BACK)))
    async def main_menu_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_MAIN_MENU_OPEN, data={"source": call.data})
        if call.message.chat and call.from_user:
            clear_support_pending(call.message.chat.id, call.from_user.id)
        await send_text(call.message, "Здравствуйте! Мы готовы ответить на любой ваш вопрос. Если вы его не нашли в данном боте, оставьте обращение или напишите @modern_1mctech", reply_markup=MAIN_MENU)
        await call.answer()

    @router.callback_query(F.data.in_(INSTALL_ANSWERS.keys()))
    async def install_answer_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_INSTALL_ANSWER, subject=call.data)
        answer = INSTALL_ANSWERS[call.data]
        await send_answer(call.message, answer, call.data)
        await call.answer()

    @router.callback_query(F.data.in_(ANSWERS.keys()))
    async def main_answer_callback(call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        log_callback_event(call, EVENT_FAQ_ANSWER, subject=call.data)
        answer = ANSWERS[call.data]
        await send_answer(call.message, answer, call.data)
        await call.answer()

    @router.callback_query(F.data.startswith(FEEDBACK_HELPFUL_PREFIX))
    async def feedback_helpful_callback(call: CallbackQuery) -> None:
        subject = call.data[len(FEEDBACK_HELPFUL_PREFIX) :]
        log_callback_event(call, EVENT_FEEDBACK_HELPFUL, subject=subject)
        if call.message:
            try:
                await call.message.edit_reply_markup(reply_markup=ANSWER_MENU)
            except Exception:
                logger.warning("Failed to update feedback menu")
        await call.answer("Спасибо за отзыв!")

    @router.callback_query(F.data.startswith(FEEDBACK_UNHELPFUL_PREFIX))
    async def feedback_unhelpful_callback(call: CallbackQuery) -> None:
        subject = call.data[len(FEEDBACK_UNHELPFUL_PREFIX) :]
        log_callback_event(call, EVENT_FEEDBACK_UNHELPFUL, subject=subject)
        if call.message:
            try:
                await call.message.edit_reply_markup(reply_markup=ANSWER_MENU)
            except Exception:
                logger.warning("Failed to update feedback menu")
        await call.answer("Спасибо за отзыв!")

    @router.callback_query()
    async def fallback_callback(call: CallbackQuery) -> None:
        log_callback_event(call, EVENT_FALLBACK_CALLBACK, data={"callback_data": call.data})
        await call.answer("Неизвестная команда.", show_alert=False)

    @router.message(StateFilter(None), F.text)
    async def fallback(message: Message) -> None:
        log_message_event(
            message,
            EVENT_FALLBACK_MESSAGE,
            data={"text_len": len(message.text), "text_preview": text_preview(message.text)},
        )
        await send_text(message, "Пожалуйста, выберите пункт из меню.", reply_markup=MAIN_MENU)

    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
