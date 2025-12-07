import os
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException

# =========================
# ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("marketfox")

# =========================
# Groq API
# =========================
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"  # мощная универсальная модель


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq Chat Completions API.
    Если ключ не задан или произошла ошибка — кидаем исключение, а выше дадим фоллбек.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

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
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
        try:
            data = resp.json()
        except Exception:
            logger.exception("Groq response is not JSON: %s", resp.text)
            resp.raise_for_status()
            raise

        if resp.status_code != 200:
            logger.error("Groq error %s: %s", resp.status_code, data)
            raise HTTPException(
                status_code=500,
                detail=f"Groq API error {resp.status_code}: {data}",
            )

        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.exception("Unexpected Groq response format: %s", data)
            raise HTTPException(
                status_code=500,
                detail="Groq API unexpected response format",
            )


# =========================
# УТИЛИТЫ
# =========================
def extract_query(data: Dict[str, Any]) -> Optional[str]:
    """
    Пытаемся достать текст запроса из разных возможных полей.
    Основной кейс: поле 'Запрос' (русское).
    """
    for key in ("Запрос", "query", "message", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def detect_scenario(text: str) -> str:
    """
    Грубая эвристика:
    - 'gift'      — запрос про подарок
    - 'compare'   — сравнение товаров
    - 'product_pick' — всё остальное (подбор товара)
    """
    lower = text.lower()

    gift_keywords = [
        "подарок", "подарить", "для девушки", "для парня",
        "для мамы", "для папы", "для жены", "для мужа",
        "на др", "на день рождения", "на новый год", "на нг",
    ]
    compare_keywords = ["сравни", "что лучше", "vs", "против", "сравнение"]

    if any(k in lower for k in gift_keywords):
        return "gift"
    if any(k in lower for k in compare_keywords):
        return "compare"

    return "product_pick"


async def generate_reply(query: str, scenario: str) -> str:
    """
    Генерируем ответ от MarketFox.
    Если Groq недоступен или нет ключа — отдаём дружелюбный фоллбек.
    """
    base_instructions = (
        "Ты — MarketFox, умный ассистент по выбору товаров на маркетплейсах "
        "(например, Wildberries и Ozon) для русскоязычных пользователей. "
        "Ты НЕ видишь реальные карточки товаров, поэтому действуешь как консультант:\n"
        "- помогаешь сузить запрос;\n"
        "- подсказываешь, какие характеристики важны;\n"
        "- предлагаешь, как сравнивать разные варианты;\n"
        "- предупреждаешь о типичных подводных камнях.\n\n"
        "Отвечай кратко, по делу, структурировано. Пиши на русском, используй списки, "
        "избегай длинных простыней текста."
    )

    if scenario == "gift":
        scenario_hint = (
            "Сценарий: выбор подарка. Основывайся на запросе пользователя. "
            "Предложи 3–7 идей подарков в разных ценовых диапазонах и стилях. "
            "Объясни, почему каждая идея может подойти. "
            "Если данных мало, мягко уточни, что ещё можно написать."
        )
    elif scenario == "compare":
        scenario_hint = (
            "Сценарий: сравнение двух или нескольких товаров. "
            "Помоги пользователю понять, по каким критериям сравнивать товары: "
            "качество, функционал, надежность, гарантия, отзывы, бренд, скрытые минусы. "
            "Дай структурированный чек-лист и подсказки, как принять решение."
        )
    else:
        scenario_hint = (
            "Сценарий: подбор одного товара под запрос. "
            "Помоги пользователю сузить выбор и понять, какой тип товара или "
            "какие характеристики ему подойдут. "
            "Предложи 3–7 идей, что искать (типы товаров, функции, особенности), "
            "но НЕ придумывай конкретные модели с фантазийными названиями."
        )

    system_prompt = base_instructions + "\n\n" + scenario_hint

    try:
        return await call_groq(system_prompt, query)
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return (
            "Сейчас я не могу обратиться к нейросети, но вот базовый алгоритм выбора:\n"
            "1) Сузь бюджет и убери откровенно дешёвые варианты без отзывов.\n"
            "2) Выбери 3–5 адекватных товаров и сравни их по 3–4 ключевым параметрам.\n"
            "3) Посмотри негативные отзывы — они покажут реальные проблемы.\n\n"
            "Попробуй немного позже ещё раз написать запрос — я постараюсь помочь подробнее 🦊."
        )


# =========================
# FASTAPI
# =========================
app = FastAPI(
    title="MarketFox API (Groq, Railway)",
    description="Backend for MarketFox marketplace assistant (Groq)",
    version="0.5.0",
)


@app.post("/marketfox")
async def marketfox_endpoint(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Эндпоинт, который будет вызывать BotHelp.
    Принимаем произвольный JSON (словари с любыми полями).
    """
    data: Dict[str, Any] = payload or {}
    logger.info("Incoming payload keys: %s", list(data.keys()))

    text_query = extract_query(data)
    user_id = str(data.get("user_id") or data.get("bothelp_user_id") or "")

    if not text_query:
        logger.warning("No query text found in payload from user_id=%s", user_id)
        return {
            "reply_text": (
                "Я не увидел в запросе текст. Напиши, пожалуйста, одним сообщением, "
                "что ты хочешь найти, для кого это и в каком бюджете."
            ),
            "scenario": "unknown",
        }

    scenario = detect_scenario(text_query)
    logger.info("User %s scenario=%s query=%s", user_id, scenario, text_query[:80])

    reply_text = await generate_reply(text_query, scenario)

    return {
        "reply_text": reply_text,
        "scenario": scenario,
    }
