import os
import logging
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------------- ЛОГИ ---------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("marketfox")

# ---------------------- НАСТРОЙКИ ---------------------- #

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY is not set. Groq calls will fail.")

# --------- СИСТЕМНЫЕ ПРОМПТЫ ДЛЯ РАЗНЫХ СЦЕНАРИЕВ ---------- #

SYSTEM_PROMPT_PRODUCT_PICK = """
Ты MarketFox — персональный ассистент по подбору товаров на маркетплейсах.
Всегда отвечай по-русски. Пользователь описывает, что хочет купить.

Твоя задача:
1) Кратко понять запрос и сформулировать, что он ищет (1 предложение).
2) Дать до 5 ключевых критериев выбора именно для этой категории.
3) Предложить 3–5 примерных вариантов/типов товаров или моделей (бренды не обязательны).
4) Пиши обычным текстом: абзацы и списки через тире. Без Markdown, без нумерации "1)", без звёздочек и эмодзи.
5) Не выдумывай конкретные цены. Если упоминаешь бюджет — говори ориентировочно: "до 5 тысяч", "в районе 10–15 тысяч" и т.п.
"""

SYSTEM_PROMPT_GIFT = """
Ты MarketFox — ассистент по подбору подарков.
Всегда отвечай по-русски.

Пользователь описывает, кому нужен подарок и на какую сумму.
Твоя задача:
1) Кратко описать, кому и на какой повод подбираем подарок.
2) Предложить 3–7 конкретных идей категорий подарков (типы товаров, впечатления и т.п.).
3) Для каждой идеи коротко поясни, чем она может понравиться получателю.
4) Пиши обычным текстом, без Markdown и эмодзи.
"""

SYSTEM_PROMPT_COMPARE = """
Ты MarketFox — ассистент по сравнению товаров.

Пользователь присылает 2 товара (названия или модели) и хочет понять, что лучше взять.
Твоя задача:
1) Кратко пересказать, какие два варианта сравниваем.
2) Сравнить их по 3–6 ключевым параметрам (качество, автономность, удобство, цена и т.д.).
3) В конце дать чёткий вывод: что ты рекомендуешь и почему, с учётом типичного пользователя.
4) Пиши обычным текстом: блоками и списками через тире. Без Markdown, без "1)", без эмодзи.
"""

FALLBACK_TEXT = (
    "Сейчас не получается обратиться к нейросети. "
    "Попробуй повторить запрос чуть позже или переформулировать его."
)

NO_QUERY_TEXT = (
    "Нет запроса. Напиши, что именно тебе нужно: тип товара, примерный бюджет "
    "и важные характеристики."
)

# ---------------------- Pydantic-модели ---------------------- #


class MarketFoxResponse(BaseModel):
    reply_text: str
    scenario: str


# ---------------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---------------------- #


def extract_query(payload: Dict[str, Any]) -> str:
    """
    Забираем текст запроса из разных возможных полей BotHelp.
    Смотрим по очереди несколько ключей и берём первый непустой.
    """
    candidates: List[str] = []

    for key in ("query", "Запрос", "message", "text", "last_message"):
        value = payload.get(key)
        if isinstance(value, str):
            candidates.append(value.strip())

    for v in candidates:
        if v:
            return v

    return ""


def extract_scenario(payload: Dict[str, Any]) -> str:
    """
    Определяем сценарий, который пришёл из BotHelp.
    По умолчанию считаем, что это подбор товара.
    """
    scenario = str(payload.get("scenario") or "").strip().lower()

    if scenario in {"product_pick", "gift", "compare"}:
        return scenario

    # если что-то левое — сводим к product_pick
    return "product_pick"


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq как OpenAI-совместимого API.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    data = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        "temperature": 0.6,
        "max_tokens": 700,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GROQ_URL, headers=headers, json=data)
        # если ошибка — поднимем исключение, поймаем выше и отдадим фолбэк
        resp.raise_for_status()
        payload = resp.json()

    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected Groq response format: %s", e)
        raise RuntimeError("Bad Groq response")

    # немного подчистим лишние переводы строк
    text = str(content).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")

    return text


async def generate_reply(payload: Dict[str, Any]) -> MarketFoxResponse:
    """
    Основная логика: выбираем сценарий, вытаскиваем текст запроса,
    вызываем Groq и формируем ответ.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = extract_scenario(payload)
    query = extract_query(payload)

    logger.info("Scenario=%s, query=%r", scenario, query)

    if not query:
        # Пользователь нажал кнопку, но ещё ничего не написал
        return MarketFoxResponse(reply_text=NO_QUERY_TEXT, scenario=scenario)

    if scenario == "gift":
        system_prompt = SYSTEM_PROMPT_GIFT
    elif scenario == "compare":
        system_prompt = SYSTEM_PROMPT_COMPARE
    else:
        system_prompt = SYSTEM_PROMPT_PRODUCT_PICK

    try:
        answer = await call_groq(system_prompt, query)
        return MarketFoxResponse(reply_text=answer, scenario=scenario)
    except Exception as e:  # noqa: BLE001
        logger.exception("Groq API error: %s", e)
        return MarketFoxResponse(reply_text=FALLBACK_TEXT, scenario=scenario)


# ---------------------- FastAPI-приложение ---------------------- #

app = FastAPI(
    title="MarketFox API (Groq, Railway)",
    version="0.2.0",
    description="Backend для бота MarketFox (подбор товара / подарок / сравнение).",
)


@app.get("/")
async def root():
    return {"status": "ok", "message": "MarketFox backend is running"}


@app.post("/marketfox", response_model=MarketFoxResponse)
async def marketfox_endpoint(payload: Dict[str, Any]):
    """
    Основная точка входа для BotHelp.
    """
    return await generate_reply(payload)
