import os
import logging
import re
from typing import Literal, Tuple

import httpx
from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware


# -----------------------------
# Базовая настройка логов
# -----------------------------
logger = logging.getLogger("marketfox")
logging.basicConfig(level=logging.INFO)


# -----------------------------
# Настройки окружения
# -----------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY is not set. API вызовы Groq будут падать в фолбэк.")


# -----------------------------
# System-промпты для сценариев
# -----------------------------

SYSTEM_PRODUCT_PICK = """
Ты — MarketFox, эксперт по выбору товаров на маркетплейсах (Wildberries, Ozon и т.п.).

Твоя задача: по запросу пользователя помочь подобрать подходящий товар.

Формат ответа:
1. Короткое вступление (1 предложение).
2. 3–5 конкретных рекомендаций, но без нумерованных списков и без Markdown.
3. В конце сделай короткий вывод, на что обратить внимание при выборе.

Правила:
- Пиши только обычный текст, без символов *, •, #, без Markdown и таблиц.
- Не придумывай точные цены и артикулы, используй диапазоны и описания.
- Отвечай компактно: чтобы ответ помещался в один экран Telegram.
"""

SYSTEM_GIFT = """
Ты — MarketFox, эксперт по выбору подарков.

Твоя задача: по запросу пользователя предложить 3–5 идей подарков с короткими пояснениями,
почему это подойдёт.

Формат ответа:
1. Короткое вступление (1 предложение).
2. Затем 3–5 идей подарков, каждую описывай в одном предложении.
3. В конце предложи, при желании, уточнить детали (возраст, бюджет и т.п.).

Правила:
- Пиши только обычный текст, без символов *, •, #, без Markdown.
- Не используй эмодзи.
- Не указывай конкретные магазины и цены, только типы подарков и общее описание.
"""

SYSTEM_COMPARE = """
Ты — MarketFox, эксперт по аналитике и сравнению товаров.

Твоя задача: сравнить два товара по запросу пользователя и помочь понять, что лучше взять.

Формат ответа:
1. Короткое вступление: что именно ты сравниваешь.
2. Затем по очереди опиши:
   - преимущества первого товара;
   - недостатки первого товара;
   - преимущества второго товара;
   - недостатки второго товара.
3. В конце дай чёткую рекомендацию: какой вариант лучше и почему (1–2 предложения).

Правила:
- Не используй Markdown, не пиши списки с символами *, •, # и т.п.
- Пиши обычный текст с разбиением на абзацы (пустая строка между смысловыми блоками).
- Не придумывай точные характеристики, если их нет в запросе — используй общие формулировки.
- Отвечай компактно: чтобы ответ помещался в один экран Telegram.
"""


# -----------------------------
# Вспомогательные функции
# -----------------------------

def clean_text(text: str) -> str:
    """
    Убираем лишние символы (*, • и т.п.) и приводим текст в аккуратный вид.
    """
    if not text:
        return ""

    # Удаляем всякие маркеры Markdown/буллеты
    text = text.replace("*", "")
    text = text.replace("•", "")
    text = text.replace("#", "")

    # Сжимаем тройные и более переносы в двойные
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Убираем лишние пробелы по краям
    return text.strip()


def detect_scenario(query: str) -> Literal["product_pick", "gift", "compare"]:
    """
    Определяем, какой сценарий включать, по тексту запроса.
    Если у тебя в BotHelp есть отдельное поле со сценарием — сюда можно легко добавить.
    """
    q = (query or "").lower()

    # Похоже на сравнение
    compare_triggers = [
        " vs ",
        " против ",
        "сравни",
        "сравнить",
        "что лучше",
        "или ",
    ]
    if any(t in q for t in compare_triggers):
        return "compare"

    # Явно просит подарок
    if "подарок" in q or "подарком" in q or "подарить" in q:
        return "gift"

    # По умолчанию — подбор товара
    return "product_pick"


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq OpenAI-compatible API.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        "temperature": 0.6,
        "max_tokens": 700,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    content = data["choices"][0]["message"]["content"]
    return clean_text(content)


async def generate_reply(query: str) -> Tuple[str, str]:
    """
    Основная логика:
    - определяем сценарий,
    - выбираем правильный промпт,
    - вызываем Groq,
    - если что-то падает — отдаём аккуратный фолбэк.
    """
    query = (query or "").strip()

    if not query:
        # Нет текста запроса — сразу фолбэк
        return "Нет запроса. Напиши, что тебе нужно.", "product_pick"

    scenario = detect_scenario(query)
    logger.info("Detected scenario=%s for query=%r", scenario, query)

    if scenario == "compare":
        system_prompt = SYSTEM_COMPARE
    elif scenario == "gift":
        system_prompt = SYSTEM_GIFT
    else:
        system_prompt = SYSTEM_PRODUCT_PICK

    try:
        reply = await call_groq(system_prompt, query)
        return reply, scenario
    except Exception as e:
        logger.error("Groq API error: %s", e, exc_info=True)

        # Фолбэк — безопасный и простой текст
        if scenario == "compare":
            fallback = (
                "Сейчас я не могу полноценно сравнить эти товары, "
                "но общий алгоритм выбора такой:\n\n"
                "1) Определи бюджет и ключевые характеристики товара.\n"
                "2) Сравни модели по этим параметрам.\n"
                "3) Обрати внимание на отзывы и рейтинг.\n"
                "4) Выбери вариант, который лучше закрывает твои задачи."
            )
        elif scenario == "gift":
            fallback = (
                "Сейчас я не могу подсказать конкретный подарок, "
                "но попробуй отталкиваться от интересов человека, его возраста и бюджета. "
                "Практичные вещи, впечатления или что-то связанное с хобби обычно заходят лучше всего."
            )
        else:
            fallback = (
                "Сейчас я не могу обратиться к нейросети, "
                "но попробуй сузить запрос: уточни тип товара, примерный бюджет и важные характеристики."
            )

        return fallback, scenario


# -----------------------------
# FastAPI-приложение
# -----------------------------

app = FastAPI(title="MarketFox API (Groq, Railway)")

# CORS — на всякий случай, если BotHelp будет дёргать с браузера
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"status": "ok", "message": "MarketFox backend is running"}


@app.post("/marketfox")
async def marketfox_endpoint(payload: dict = Body(...)):
    """
    Основной webhook-эндпоинт для BotHelp.

    Ожидаем хотя бы поле "Запрос": "<текст от пользователя>".
    Остальные поля просто логируем и игнорируем.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    # Забираем текст запроса
    query = (
        payload.get("Запрос")
        or payload.get("query")
        or payload.get("text")
        or ""
    )

    reply_text, scenario = await generate_reply(query)

    response = {
        "reply_text": reply_text,
        "scenario": scenario,
    }

    logger.info("Response: %s", response)
    return response


# Для локального запуска (если нужно)
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
