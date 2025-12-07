import os
import logging
import re
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from groq import Groq

# -------------------------------------------------
#  ЛОГИ
# -------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("marketfox")

# -------------------------------------------------
#  НАСТРОЙКА ИИ (GROQ)
# -------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY is not set. API calls will fail.")
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# -------------------------------------------------
#  FASTAPI
# -------------------------------------------------
app = FastAPI(
    title="MarketFox API (Groq, Railway)",
    version="0.5.0",
    description="Backend для бота MarketFox (подбор товара, подарок, сравнение)",
)

# -------------------------------------------------
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -------------------------------------------------
def clean_text(text: str) -> str:
    """
    Чистим ответ от markdown/лишних символов:
    - убираем **жирный**
    - убираем буллеты *, -, • в начале строк
    - сжимаем лишние пустые строки
    """
    if not text:
        return text

    # убираем **жирный**
    text = text.replace("**", "")

    # убираем буллеты в начале строк
    text = re.sub(r"^[\-\*•]+\s*", "", text, flags=re.MULTILINE)

    # сжимаем >2 переносов подряд до 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def build_system_prompt(scenario: str) -> str:
    """
    Возвращаем системный промт под конкретный сценарий.
    Все промты:
    - только русский
    - без markdown
    - дружелюбный, но по делу
    """

    base = (
        "Ты MarketFox — персональный ассистент по выбору товаров "
        "на маркетплейсах (Wildberries, Ozon и похожие).\n"
        "Отвечай ТОЛЬКО на русском языке, коротко и по делу.\n"
        "Не используй Markdown-разметку: никаких звездочек, **жирного**, буллетов.\n"
        "Если нужно перечисление, пиши в формате '1) ... 2) ... 3) ...'.\n"
        "Избегай воды и общих фраз, говори как умный друг, который разбирается в товарах.\n"
        "Максимум 8–10 предложений."
    )

    if scenario == "gift":
        spec = (
            "\n\nСЦЕНАРИЙ: пользователю нужен ПОДАРОК.\n"
            "1) Кратко сформулируй, кому и по какому поводу подарок (если это понятно из запроса).\n"
            "2) Предложи 3–5 конкретных идей подарков, учитывая бюджет и интересы.\n"
            "3) Укажи плюсы каждой идеи и в каких случаях она особенно хорошо зайдет.\n"
            "4) Если информации мало (запрос очень общий), сначала задай 2–3 уточняющих вопроса, "
            "а уже потом предложи варианты."
        )
    elif scenario == "compare":
        spec = (
            "\n\nСЦЕНАРИЙ: сравнение ДВУХ ТОВАРОВ.\n"
            "Пользователь присылает 2 модели или 2 товара.\n"
            "1) Сначала кратко напомни, какие два товара сравниваем.\n"
            "2) Затем сравни по 3–5 ключевым аспектам (качество, удобство, надежность, цена и т.п.).\n"
            "3) В конце дай четкий вывод: какой товар лучше выбрать и почему.\n"
            "Если пользователь прислал что-то одно или непонятный текст — попроси корректно прислать "
            "2 названия или 2 ссылки."
        )
    else:
        # по умолчанию — подбор товара
        spec = (
            "\n\nСЦЕНАРИЙ: подбор ЛУЧШЕГО ТОВАРА под запрос пользователя.\n"
            "1) Понять, что именно человек хочет купить, под какой бюджет и зачем.\n"
            "2) Если запрос слишком короткий и непонятный (одно-два слова без контекста), "
            "задай 2–3 уточняющих вопроса вместо рекомендаций.\n"
            "3) Если информации достаточно, дай:\n"
            "   3–5 главных критериев выбора именно для этого товара;\n"
            "   3–5 рекомендаций по тому, на что смотреть при выборе (тип, характеристики, нюансы).\n"
            "4) Если уместно, можешь привести 2–3 примера подходящих решений (без детальной рекламы брендов)."
        )

    return base + spec


def extract_query(payload: Dict[str, Any]) -> Optional[str]:
    """
    Извлекаем текст запроса из полезной нагрузки.
    В BotHelp ты можешь называть поле как хочешь — здесь просто перечисляем варианты.
    """
    candidates = [
        "Запрос",
        "запрос",
        "query",
        "message",
        "text",
        "Продукт",
    ]

    for key in candidates:
        if key in payload and isinstance(payload[key], str):
            value = payload[key].strip()
            if value:
                return value

    return None


def extract_scenario(payload: Dict[str, Any]) -> str:
    """
    Сценарий нам приходит из BotHelp (product_pick / gift / compare).
    Если нет — по умолчанию считаем, что это подбор товара.
    """
    scenario = payload.get("scenario") or payload.get("Сценарий") or payload.get("scenario_name")
    if not isinstance(scenario, str):
        return "product_pick"

    scenario = scenario.strip().lower()
    if scenario in {"gift", "compare", "product_pick"}:
        return scenario
    return "product_pick"


def build_fallback_answer(scenario: str, query: Optional[str]) -> str:
    """
    Ответ, если с ИИ или API что-то пошло не так.
    """
    base = "Сейчас я не могу обратиться к нейросети, но вот как можно подойти к выбору:\n"

    if scenario == "gift":
        return (
            base
            + "1) Подумай, чем человек реально увлекается и что ему облегчит жизнь.\n"
              "2) Сузь бюджет и сразу отсей совсем бесполезные вещи.\n"
              "3) Выбери 3–5 идей (впечатления, гаджеты, аксессуары, хобби) и спроси себя, "
              "что ему будет приятно получать и использовать чаще всего."
        )
    if scenario == "compare":
        return (
            base
            + "1) Сравни товары по 3–4 ключевым параметрам: надежность, удобство, цена, отзывы.\n"
              "2) Важно смотреть не только на плюсы, но и на минусы в отзывах.\n"
              "3) Обычно лучше брать тот товар, у которого меньше критических минусов при схожей цене."
        )

    # product_pick
    return (
        base
        + "1) Определи бюджет и 1–2 главные характеристики товара.\n"
          "2) Отсей варианты без отзывов и с очень низким рейтингом.\n"
          "3) Сравни 3–5 адекватных моделей по ключевым параметрам.\n"
          "4) Посмотри негативные отзывы — они лучше всего показывают реальные минусы."
    )


def call_groq_sync(system_prompt: str, user_query: str) -> str:
    """
    Синхронный вызов Groq Chat Completions.
    FastAPI сам обернет в поток, тут нам не критична абсолютная асинхронность.
    """
    if not client:
        raise RuntimeError("GROQ_API_KEY is not set")

    response = client.chat.completions.create(
        model="llama-3.1-70b-versatile",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        max_tokens=500,
        temperature=0.6,
    )
    content = response.choices[0].message.content
    return content or ""


def generate_reply(scenario: str, query: Optional[str]) -> str:
    """
    Основная логика: строим промт, вызываем Groq, чистим ответ.
    """
    logger.info("Generate reply: scenario=%s, query=%s", scenario, query)

    if not query:
        # Совсем нет текста – объясняем, что нужен запрос
        if scenario == "gift":
            return "Пока не вижу запроса. Напиши, кому нужен подарок, по какому поводу и примерный бюджет."
        if scenario == "compare":
            return "Пока нет данных для сравнения. Пришли, пожалуйста, два товара: названия или ссылки."
        return "Пока не вижу запроса. Напиши, что ты хочешь купить и на какой бюджет."

    try:
        system_prompt = build_system_prompt(scenario)
        raw_answer = call_groq_sync(system_prompt, query)
        cleaned = clean_text(raw_answer)

        # на всякий случай ограничим длину
        if len(cleaned) > 2000:
            cleaned = cleaned[:2000].rsplit(" ", 1)[0] + "..."

        return cleaned or build_fallback_answer(scenario, query)

    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return build_fallback_answer(scenario, query)


# -------------------------------------------------
#  ЭНДПОИНТЫ
# -------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "message": "MarketFox backend is running"}


@app.post("/marketfox")
async def marketfox_endpoint(payload: Dict[str, Any]):
    """
    Главный вебхук для BotHelp.

    Ожидаем любой JSON, например:
    {
      "scenario": "product_pick",
      "Запрос": "беспроводные наушники до 3000"
    }

    Ответ:
    {
      "reply_text": "...текст для пользователя...",
      "scenario": "product_pick"
    }
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = extract_scenario(payload)
    query = extract_query(payload)
    reply_text = generate_reply(scenario, query)

    return JSONResponse(
        content={
            "reply_text": reply_text,
            "scenario": scenario,
        }
    )
