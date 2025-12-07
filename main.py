import os
import logging
from typing import Any, Dict, List
from urllib.parse import quote_plus

from fastapi import FastAPI, Body
from groq import Groq
import httpx
import re

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

# ----------------- Промты -----------------

PROMPT_PRODUCT_PICK = """
Ты — MarketFox, дружелюбный и умный ассистент по выбору товаров на маркетплейсах (Wildberries, Ozon и т.п.).

Всегда отвечай по-русски и помогай человеку быстро понять:
- какой товар ему лучше подойдёт;
- на что смотреть при выборе;
- какие ошибки не допускать.

Тон:
- живой, дружелюбный, без канцелярщины;
- допускается 1–3 уместных эмодзи (например: 🙂 🔍 🎁 💡), не в каждое предложение;
- без сленга и пошлых шуток, но можно чуть по-дружески.

Формат ответа (строго придерживайся структуры):

1) Короткое обращение + одно предложение, что ты понял из запроса. Можно 1 эмодзи.

2) Блок "На что обратить внимание:" —
   сделай 3–5 пронумерованных пунктов (1), 2), 3) ...), каждый в одной строке;
   пиши про реальные, важные критерии именно для этого типа товара.

3) Блок "Что можно взять:" —
   предложи 2–4 вариантов решения в формате:
   "Вариант 1: такой-то тип/класс товара — коротко, зачем он подойдёт."
   Не придумывай конкретные артикулы и точные цены, говори диапазонами:
   "до 5000 ₽", "примерно 7–10 тысяч" и т.п.

4) В конце задай один короткий вопрос, чтобы продолжить диалог.

Запреты:
- не используй Markdown-разметку (никаких **звёздочек**, #заголовков и буллетов со звёздочками);
- не придумывай точные характеристики и цены, если их нет в запросе;
- не придумывай и не пиши никакие URL-ссылки на товары и магазины — ссылки добавит система после тебя.
"""

PROMPT_GIFT = """
Ты — MarketFox, ассистент по выбору подарков.

Твоя задача — по запросу пользователя предложить понятные, жизненные идеи подарков, а не абстрактную воду.

Тон:
- тёплый, дружелюбный, без сюсюканья;
- можно 1–3 эмодзи, уместно: 🎁 🙂 💡 ❤️;
- говори как старший друг, который шарит, но не давит.

Формат ответа:

1) Коротко переформулируй задачу.

2) Блок "Идеи подарков:" —
   3–7 вариантов, каждый с новой строки "1) …".

3) Блок "Как выбрать из этого:" —
   2–3 фразы, как отсечь лишнее и остановиться на одном варианте.

4) В конце один вопрос для продолжения.

Запреты:
- не используй Markdown, списки с * или •;
- не указывай ссылки и конкретные магазины;
- не предлагай слишком дорогие варианты, если бюджет явно низкий.
"""

PROMPT_COMPARE = """
Ты — MarketFox, ассистент по сравнению товаров.

Пользователь даёт два (иногда три) товара или модели и хочет понять, что ему лучше подойдёт.

Тон:
- уверенный, но не занудный;
- допускается 1–2 эмодзи максимум (⚖️ 💡 ✅);
- говори просто.

Формат ответа:

1) Вступление — переформулируешь, что сравниваешь.
2) "Основные отличия:" — 3–5 пунктов "1) …".
3) "Кому что подойдёт:" — пару фраз про каждый вариант.
4) Итог — краткая рекомендация.

Если пользователь прислал только один товар или непонятный текст — попроси явно два варианта.

Запреты:
- не используй Markdown и списки с *;
- не придумывай точные характеристики, если их нет в запросе;
- не пиши длинные полотна без пустых строк.
"""

# Модель Groq
GROQ_MODEL = "llama-3.1-8b-instant"

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


# ----------------- Вспомогательные функции -----------------

STOP_WORDS = [
    "подбери", "подобрать", "подбор",
    "найди", "найти",
    "выбери", "выбор",
    "интересные", "крутые", "лучшие", "хорошие",
    "для", "девушки", "парня", "мужчины", "женщины", "ребенка", "ребёнка",
    "до", "примерно", "где-то", "около",
    "рублей", "руб", "₽",
    "на", "из", "в",
]

STOP_RE = re.compile(r"\b(" + "|".join(STOP_WORDS) + r")\b", flags=re.IGNORECASE)


def simplify_query_for_search(query: str) -> str:
    """
    Очищаем пользовательский текст до вида, который проще для поиска:
    выкидываем служебные слова, оставляем главное.
    """
    text = query.lower()

    # убираем валюту и числа типа "до 3000", "5000р"
    text = re.sub(r"\d+\s*(₽|руб(лей)?|р)", " ", text)
    text = re.sub(r"\d+\s*", " ", text)

    # убираем стоп-слова
    text = STOP_RE.sub(" ", text)

    # сжимаем пробелы
    text = re.sub(r"\s+", " ", text).strip()

    # если вдруг всё вычистили — вернём исходник
    return text or query.strip()


def build_marketplace_links(query: str) -> str:
    """
    Строим безопасные поисковые ссылки на WB и Ozon по исходному запросу.
    """
    search_q = simplify_query_for_search(query)
    if not search_q:
        return ""

    encoded = quote_plus(search_q)

    wb_url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={encoded}"
    ozon_url = f"https://www.ozon.ru/search/?text={encoded}"

    links_block = (
        "\n\nЕсли хочешь сразу посмотреть варианты на маркетплейсах, вот удобные ссылки:\n"
        f"- Wildberries: {wb_url}\n"
        f"- Ozon: {ozon_url}"
    )
    return links_block


async def search_wildberries_simple(query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Очень простой поиск по Wildberries: забираем несколько популярных товаров
    по очищенному запросу и возвращаем name/price/url.
    """
    q = simplify_query_for_search(query)
    if not q:
        return []

    params = {
        "query": q,
        "resultset": "catalog",
        "page": 1,
        "sort": "popular",
        "appType": 1,
        "curr": "rub",
        "dest": "-1257786",
        "spp": 30,
        "lang": "ru",
    }
    url = "https://search.wb.ru/exactmatch/ru/common/v4/search"

    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            resp = await http_client.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "MarketFoxBot/1.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error("WB search failed (%s): %s", e.response.status_code, e)
        return []
    except Exception as e:
        logger.error("WB search error: %s", e)
        return []

    products = (data.get("data") or {}).get("products") or []
    products = products[:limit]

    results: List[Dict[str, Any]] = []
    for p in products:
        pid = p.get("id")
        name = (p.get("name") or "").strip()
        price_raw = p.get("salePriceU") or p.get("priceU")
        price = None
        if isinstance(price_raw, int):
            price = price_raw / 100  # цены в копейках

        if not pid or not name:
            continue

        item_url = f"https://www.wildberries.ru/catalog/{pid}/detail.aspx"
        results.append(
            {
                "name": name,
                "price": price,
                "url": item_url,
            }
        )

    return results


def format_wb_items_block(items: List[Dict[str, Any]]) -> str:
    """
    Красиво оформляем блок с товарами WB для текста ответа.
    """
    if not items:
        return (
            "\n\nСейчас не удалось подтянуть конкретные карточки с Wildberries, "
            "поэтому выше только общие ссылки на поиск."
        )

    lines = ["\n\nПара примеров товаров на Wildberries, чтобы было от чего оттолкнуться:"]
    for i, item in enumerate(items, start=1):
        name = item.get("name") or "Товар"
        price = item.get("price")
        url = item.get("url") or ""
        if price is not None:
            price_str = f"{int(price):,}".replace(",", " ")
            lines.append(f"{i}) {name} — примерно {price_str} ₽")
        else:
            lines.append(f"{i}) {name}")
        if url:
            lines.append(f"   {url}")

    return "\n".join(lines)


# ----------------- Вызов Groq -----------------
async def call_groq(system_prompt: str, user_query: str) -> str:
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


# ----------------- Генерация ответа -----------------
async def generate_reply(system_prompt: str, query: str, scenario: str) -> Dict[str, str]:
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

        if safe_scenario == "product_pick":
            # 1) поисковые ссылки
            answer += build_marketplace_links(query)

            # 2) попытка подтянуть несколько карточек WB
            wb_items = await search_wildberries_simple(query, limit=3)
            answer += format_wb_items_block(wb_items)

        return {
            "reply_text": answer,
            "scenario": safe_scenario,
        }

    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": safe_scenario,
        }


# ----------------- FastAPI -----------------
app = FastAPI(
    title="MarketFox API (Groq, Railway)",
    description="Backend для MarketFox маркетплейс-ассистента (Groq, Railway)",
    version="0.8.0",
)


@app.post("/marketfox")
async def marketfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    query = (
        payload.get("Запрос")
        or payload.get("query")
        or payload.get("Query")
        or ""
    )

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "product_pick"
    )

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
