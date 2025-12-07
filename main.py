import os
import logging
from typing import Any, Dict

from fastapi import FastAPI, Body
from groq import Groq

# ----------------- Логгер -----------------
logger = logging.getLogger("marketfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# ----------------- Конфиг -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Базовый fallback-ответ, когда нейросеть недоступна
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети, но вот как можно подойти к выбору:\n\n"
    "1) Определи бюджет и 1–2 главные характеристики товара.\n"
    "2) Отсей варианты без отзывов и с очень низким рейтингом.\n"
    "3) Сравни 3–5 адекватных моделей по ключевым параметрам.\n"
    "4) Посмотри негативные отзывы — они лучше всего показывают реальные минусы.\n\n"
    "Попробуй немного уточнить запрос, и я постараюсь помочь более предметно 🐾"
)

# ВСТАВЬ сюда свои реальные промты
PROMPT_PRODUCT_PICK = (
    "Ты ассистент по подбору товаров на маркетплейсах. "
    "Коротко и по делу помогай выбрать ОДИН лучший товар под запрос пользователя. "
    "Структурируй ответ: сначала краткий вывод, потом 3–5 рекомендаций с объяснением, "
    "почему они подходят. Пиши по-русски, без эмодзи, без маркдауна, максимум конкретики."
)

PROMPT_GIFT = (
    "Ты эксперт по подбору подарков. Пользователь описывает, кому нужен подарок и бюджет. "
    "Дай 3–5 идей подарков, кратко объясни, почему это подойдёт, и предложи, что уточнить, "
    "если информации мало. Пиши по-русски, без лишней воды."
)

PROMPT_COMPARE = (
    "Ты помогаешь сравнивать 2–3 товара. Пользователь присылает названия или краткое описание. "
    "Сравни по ключевым параметрам и в конце скажи, что бы выбрал ты и почему. "
    "Пиши по-русски, без буллетов и без формального стиля."
)

# Модель Groq (можешь сменить на llama3-70b-8192, если хочешь мощнее)
GROQ_MODEL = "llama3-8b-8192"


# ----------------- Клиент Groq -----------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будет всегда использоваться fallback")


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq. Бросает исключение, если что-то пошло не так,
    чтобы наверху мы могли уйти в fallback.
    """
    if not client:
        raise RuntimeError("GROQ_API_KEY is not set or Groq client init failed")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=700,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


async def generate_reply(system_prompt: str, query: str, scenario: str) -> Dict[str, str]:
    """
    Общая логика генерации ответа: сначала проверяем запрос, потом пробуем Groq,
    при ошибке падаем в fallback.
    """
    safe_scenario = scenario or "product_pick"

    if not query or not query.strip():
        logger.info("Пустой запрос — отправляем подсказку пользователю")
        return {
            "reply_text": "Пока я не вижу запроса. Напиши, что именно хочешь найти, бюджет и важные характеристики.",
            "scenario": safe_scenario,
        }

    try:
        if client is None:
            raise RuntimeError("Groq client is not available")

        answer = await call_groq(system_prompt, query)
        logger.info("Успешный ответ от Groq для сценария %s", safe_scenario)
        return {
            "reply_text": answer,
            "scenario": safe_scenario,
        }

    except Exception as e:
        logger.exception("Groq API error: %s", e)
        # Возвращаем аккуратный fallback-ответ
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": safe_scenario,
        }


# ----------------- FastAPI -----------------
app = FastAPI(
    title="MarketFox API (Groq, Railway)",
    description="Backend для MarketFox маркетплейс-ассистента (Groq, Railway)",
    version="0.5.0",
)


@app.post("/marketfox")
async def marketfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Главная точка, куда шлёт запрос BotHelp.
    Ожидаем, что там есть поле 'Запрос' и (опционально) 'scenario' или 'Сценарий'.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    # Текст запроса от пользователя
    query = (
        payload.get("Запрос")
        or payload.get("query")
        or payload.get("Query")
        or ""
    )

    # Сценарий: 'product_pick', 'gift', 'compare'
    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "product_pick"
    )

    # Выбираем промт под сценарий
    if scenario == "gift":
        system_prompt = PROMPT_GIFT
    elif scenario == "compare":
        system_prompt = PROMPT_COMPARE
    else:
        system_prompt = PROMPT_PRODUCT_PICK
        scenario = "product_pick"

    logger.info("User scenario=%s query=%s", scenario, query)

    result = await generate_reply(system_prompt, query, scenario)
    return result


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "MarketFox backend is running"}
