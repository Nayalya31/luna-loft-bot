import logging
import os
import re
from collections import deque
from io import BytesIO
from pathlib import Path

import telebot
from dotenv import load_dotenv
from telebot import types

from gigachat_client import GigaChatClient, GigaChatError
from proxyapi_client import ProxyAPIClient, ProxyAPIError

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "").strip()
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "").strip()
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
PROXY_API = (
    os.getenv("PROXY_API", "").strip()
    or os.getenv("PROXYAPI_API_KEY", "").strip()
)
PROXY_API_BASE_URL = (
    os.getenv("PROXY_API_BASE_URL", "").strip()
    or os.getenv("PROXYAPI_BASE_URL", "").strip()
    or "https://api.proxyapi.ru/openai/v1"
)
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-2")

SYSTEM_PROMPT = (
    "Ты — менеджер по продажам лофт-студии «Луна». "
    "Общайся с клиентами в Telegram: отвечай на вопросы, помогай выбрать услугу, "
    "рассказывай о студии и мягко подводи к бронированию.\n\n"
    "Студия «Луна» — стильное пространство для фото- и видеосъёмок, "
    "мероприятий, мастер-классов и частных встреч.\n\n"
    "Твои задачи:\n"
    "- узнать потребности клиента (формат, дата, количество людей, бюджет);\n"
    "- предложить подходящий формат аренды или съёмки;\n"
    "- ответить на вопросы о студии, залах, оборудовании и условиях;\n"
    "- помочь записаться или оставить заявку.\n\n"
    "Стиль общения: дружелюбный, профессиональный, без давления. "
    "Отвечай на русском языке, кратко и по делу. "
    "Не используй markdown-разметку (*, #). "
    "Если клиент просит нарисовать или сгенерировать изображение, "
    "подскажи команду /image и описание желаемой картинки. "
    "Если не хватает данных (цены, точное расписание), честно уточни у клиента "
    "контактные данные или предложи связаться с администратором."
)

TELEGRAM_MESSAGE_LIMIT = 4096
MAX_HISTORY_MESSAGES = 10
IMAGE_GENERATION_TRIGGERS = (
    "нарисуй",
    "сгенерируй изображение",
    "сгенерируй картинку",
    "сгенерируй фото",
    "создай изображение",
    "создай картинку",
    "сделай изображение",
    "сделай картинку",
)

chat_histories: dict[int, deque[dict[str, str]]] = {}


def get_history(chat_id: int) -> list[dict[str, str]]:
    history = chat_histories.get(chat_id)
    if not history:
        return []
    return list(history)


def add_to_history(chat_id: int, role: str, content: str) -> None:
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=MAX_HISTORY_MESSAGES)
    chat_histories[chat_id].append({"role": role, "content": content})


def clear_history(chat_id: int) -> None:
    chat_histories.pop(chat_id, None)


def clean_for_telegram(text: str) -> str:
    lines = [re.sub(r"^#+\s*", "", line) for line in text.splitlines()]
    cleaned = "\n".join(lines)
    return re.sub(r"\*+", "", cleaned)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts


def extract_image_prompt(text: str) -> str:
    cleaned = text.strip()
    parts = cleaned.split(maxsplit=1)
    if parts and (parts[0] == "/image" or parts[0].startswith("/image@")):
        return parts[1].strip() if len(parts) > 1 else ""

    lowered = cleaned.lower()
    for trigger in IMAGE_GENERATION_TRIGGERS:
        if lowered.startswith(trigger):
            prompt = cleaned[len(trigger) :].strip(" :,-—")
            return prompt

    return cleaned


def is_image_request(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False

    parts = cleaned.split(maxsplit=1)
    if parts[0] == "/image" or parts[0].startswith("/image@"):
        return True

    lowered = cleaned.lower()
    return any(lowered.startswith(trigger) for trigger in IMAGE_GENERATION_TRIGGERS)


def make_photo_buffer(image_bytes: bytes) -> BytesIO:
    photo = BytesIO(image_bytes)
    photo.name = "image.png"
    return photo


def create_bot() -> telebot.TeleBot:
    if not BOT_TOKEN:
        raise ValueError("Укажите токен бота в файле .env (переменная BOT_TOKEN)")
    if not GIGACHAT_AUTH_KEY and not (GIGACHAT_CLIENT_ID and GIGACHAT_CLIENT_SECRET):
        raise ValueError(
            "Укажите GIGACHAT_AUTH_KEY или пару "
            "GIGACHAT_CLIENT_ID + GIGACHAT_CLIENT_SECRET в .env"
        )

    bot = telebot.TeleBot(BOT_TOKEN)
    gigachat = GigaChatClient(
        auth_key=GIGACHAT_AUTH_KEY,
        client_id=GIGACHAT_CLIENT_ID,
        client_secret=GIGACHAT_CLIENT_SECRET,
        model=GIGACHAT_MODEL,
    )
    proxyapi = ProxyAPIClient(
        api_key=PROXY_API,
        base_url=PROXY_API_BASE_URL,
        model=IMAGE_MODEL,
    ) if PROXY_API else None

    def _get_command_args(message: types.Message, command: str) -> str:
        text = (message.text or "").strip()
        parts = text.split(maxsplit=1)
        if not parts:
            return ""
        command_part = parts[0]
        if command_part == f"/{command}" or command_part.startswith(f"/{command}@"):
            return parts[1].strip() if len(parts) > 1 else ""
        return ""

    def download_telegram_photo(message: types.Message) -> tuple[bytes, str]:
        photo = message.photo[-1]
        file_info = bot.get_file(photo.file_id)
        image_bytes = bot.download_file(file_info.file_path)
        extension = Path(file_info.file_path or "image.jpg").suffix or ".jpg"
        return image_bytes, f"image{extension}"

    def send_image_generation_status(message: types.Message) -> None:
        bot.send_chat_action(message.chat.id, "upload_photo")
        bot.send_message(
            message.chat.id,
            "Генерирую изображение. Это может занять до 2 минут, пожалуйста, подождите.",
            reply_to_message_id=message.message_id,
        )

    def generate_and_send_image(message: types.Message, prompt: str) -> None:
        if not proxyapi:
            bot.reply_to(message, "Генерация изображений не настроена. Добавьте PROXY_API в .env.")
            return

        if not prompt:
            bot.reply_to(
                message,
                "Укажите описание изображения.\n"
                "Пример: /image уютная лофт-студия с мягким светом\n"
                "Или: нарисуй уютную лофт-студию с мягким светом",
            )
            return

        send_image_generation_status(message)
        logger.info("Запрос генерации изображения от chat_id=%s", message.chat.id)

        try:
            image_bytes = proxyapi.generate_image(prompt)
        except ProxyAPIError as exc:
            logger.exception("ProxyAPI generation error")
            bot.reply_to(message, f"Не удалось сгенерировать изображение.\n\n{exc}")
            return

        bot.send_photo(
            message.chat.id,
            photo=make_photo_buffer(image_bytes),
            caption=f"Изображение по запросу: {prompt[:900]}",
            reply_to_message_id=message.message_id,
        )

    @bot.message_handler(commands=["start"])
    def start(message: types.Message) -> None:
        clear_history(message.chat.id)
        bot.reply_to(
            message,
            "Здравствуйте! Я менеджер лофт-студии «Луна».\n\n"
            "Помогу подобрать формат съёмки или аренды, ответить на вопросы "
            "и оформить заявку.\n"
            f"Помню последние {MAX_HISTORY_MESSAGES} сообщений нашего диалога.\n"
            "Команды: /start, /help, /clear, /image, /edit",
        )

    @bot.message_handler(commands=["help"])
    def help_command(message: types.Message) -> None:
        bot.reply_to(
            message,
            "Напишите, чем могу помочь: съёмка, аренда студии, мероприятие.\n"
            f"Контекст: последние {MAX_HISTORY_MESSAGES} сообщений.\n"
            "/image описание — сгенерировать изображение.\n"
            "Можно также написать: «нарисуй ...» или «сгенерируй изображение ...».\n"
            "/edit описание — ответьте этой командой на фото, чтобы изменить его.\n"
            "/clear — начать диалог заново.",
        )

    @bot.message_handler(commands=["image"])
    def generate_image(message: types.Message) -> None:
        generate_and_send_image(message, extract_image_prompt(message.text or ""))

    @bot.message_handler(commands=["edit"])
    def edit_image(message: types.Message) -> None:
        if not proxyapi:
            bot.reply_to(message, "Редактирование изображений не настроено. Добавьте PROXY_API в .env.")
            return

        prompt = _get_command_args(message, "edit")
        if not prompt:
            bot.reply_to(
                message,
                "Ответьте на фото и укажите, что изменить.\n"
                "Пример: /edit добавь неоновую вывеску «Луна» на стену",
            )
            return

        if not message.reply_to_message or not message.reply_to_message.photo:
            bot.reply_to(message, "Ответьте этой командой на фото, которое нужно изменить.")
            return

        bot.send_chat_action(message.chat.id, "upload_photo")
        bot.send_message(
            message.chat.id,
            "Редактирую изображение. Это может занять до 2 минут, пожалуйста, подождите.",
            reply_to_message_id=message.message_id,
        )

        try:
            image_bytes, filename = download_telegram_photo(message.reply_to_message)
            edited_bytes = proxyapi.edit_image(prompt, image_bytes, filename)
        except ProxyAPIError as exc:
            logger.exception("ProxyAPI edit error")
            bot.reply_to(message, f"Не удалось отредактировать изображение.\n\n{exc}")
            return

        bot.send_photo(
            message.chat.id,
            photo=make_photo_buffer(edited_bytes),
            caption=f"Редактирование: {prompt[:900]}",
            reply_to_message_id=message.message_id,
        )

    @bot.message_handler(commands=["clear"])
    def clear_command(message: types.Message) -> None:
        clear_history(message.chat.id)
        bot.reply_to(message, "История диалога очищена.")

    @bot.message_handler(content_types=["text"])
    def handle_question(message: types.Message) -> None:
        if message.text and message.text.startswith("/"):
            return

        if is_image_request(message.text or ""):
            generate_and_send_image(message, extract_image_prompt(message.text or ""))
            return

        bot.send_chat_action(message.chat.id, "typing")

        history = get_history(message.chat.id)

        try:
            answer = gigachat.ask(
                message.text,
                system_prompt=SYSTEM_PROMPT,
                history=history,
            )
        except GigaChatError as exc:
            logger.exception("GigaChat error")
            bot.reply_to(
                message,
                "Не удалось получить ответ от GigaChat.\n"
                "Проверьте ключ авторизации в .env "
                "(GIGACHAT_AUTH_KEY или CLIENT_ID + CLIENT_SECRET).\n\n"
                f"{exc}",
            )
            return

        add_to_history(message.chat.id, "user", message.text)
        add_to_history(message.chat.id, "assistant", answer)

        for part in split_message(clean_for_telegram(answer)):
            bot.reply_to(message, part)

    return bot


def main() -> None:
    bot = create_bot()
    logger.info(
        "Бот запущен (telebot + GigaChat: %s, ProxyAPI: %s)",
        GIGACHAT_MODEL,
        "да" if PROXY_API else "нет",
    )
    bot.infinity_polling()


if __name__ == "__main__":
    main()
