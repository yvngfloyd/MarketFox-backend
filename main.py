import os
import logging
from typing import Any, Dict

from fastapi import FastAPI, Body
from groq import Groq

# ----------------- Логгер -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# ----------------- Конфиг -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

FALLBACK_TEXT = (
    "Сейчас я временно не могу обратиться к нейросети.\n\n"
    "Но ты всё равно можешь набросать черновик сам:\n"
    "1) Чётко опиши стороны и предмет договора/спора.\n"
    "2) Зафиксируй ключевые сроки и порядок оплаты.\n"
    "3) Запиши отдельно все риски: штрафы, односторонний отказ, "
    "одностороннее изменение условий и т.п.\n\n"
    "Как только нейросеть снова будет доступна, я помогу превратить это в аккуратный текст."
)

# ----------------- ПРОМПТЫ -----------------
PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-помощник юриста по подготовке черновиков гражданско-правовых договоров в РФ.

На вход ты получаешь структурированную информацию:
- тип договора;
- стороны и краткое описание их ролей;
- предмет (что передаётся/выполняется);
- сроки и порядок оплаты;
- особые условия и риски.

Твоя задача — выдать ЧЕРНОВИК договора, который юрист потом доработает.
Пиши по российскому праву, нейтральным юридическим языком, без излишней воды.

Формат результата:
1) Краткое однострочное пояснение, какой договор ты приготовил.
2) Далее — сам текст договора с нумерованными разделами:
   1. Предмет договора
   2. Права и обязанности сторон
   3. Порядок расчётов
   4. Срок действия и порядок расторжения
   5. Ответственность сторон
   6. Форс-мажор
   7. Прочие условия
   8. Реквизиты и подписи сторон

Правила:
- используй обычный русский текст без Markdown-разметки (**звёздочки**, # и т.п. не нужны);
- не придумывай заведомо ложные реквизиты (ИНН, ОГРН и т.п.) — там достаточно заглушек типа «___»;
- если информации по какому-то разделу мало, формулируй его общими, но юридически аккуратными фразами;
- не давай никаких пояснений после текста договора.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-помощник по подготовке досудебных претензий (претензионный порядок) по праву РФ.

На вход ты получаешь:
- адресата (кому направляем претензию);
- основание (договор, ситуация, норма закона);
- суть нарушения и фактические обстоятельства;
- требования заявителя;
- желаемый срок исполнения требований;
- контакты заявителя.

Твоя задача — подготовить аккуратный ЧЕРНОВИК досудебной претензии.

Формат результата:
1) «шапка» с адресатом и данными заявителя (можно в текстовом виде, без табличной вёрстки);
2) вводная часть: ссылка на договор/основание и краткое описание отношений сторон;
3) описательная часть: по пунктам изложи нарушение и обстоятельства (когда, что произошло);
4) мотивировочная часть: сошлись на общие нормы ГК РФ и, при необходимости, ЗоЗПП (без избыточных цитат);
5) просительная часть: чёткий перечень требований с указанием срока исполнения;
6) фраза о том, что при неисполнении требований заявитель оставляет за собой право обратиться в суд;
7) дата и место для подписи.

Правила:
- пиши деловым, но человеческим языком;
- не используй Markdown и маркеры со звёздочками;
- не придумывай конкретный суд, сумму госпошлины и т.п., если их нет во входных данных.
"""

PROMPT_CHECKLIST = """
Ты — LegalFox, ИИ-ассистент, который помогает юристу быстро оценить текст договора или отдельного пункта
(по праву РФ) и выдать понятный разбор рисков.

На вход ты получаешь произвольный текст: это может быть пункт договора, блок условий или выдержка из договора.

Твоя задача — КРАТКО и структурированно ответить:

1) Что делает этот пункт / блок условий — 2–3 предложения простым языком.
2) Риски для клиента (считай, что пользователь — сторона, которая показывает тебе текст):
   - сделай 3–7 пронумерованных пунктов «1) ... 2) ...»;
   - выделяй именно практические риски: односторонние права контрагента, штрафы, неясные формулировки, 
     отсутствие важных оговорок.
3) Как можно улучшить формулировку:
   - предложи 1–3 вариантов переформулировки ключевых элементов пункта;
   - можно давать текст типа «Вместо: “…”, лучше: “…”.»
4) Какие уточняющие вопросы задать клиенту, чтобы оценить риск точнее
   (2–4 вопроса, каждый с новой строки).

Правила:
- не используй Markdown-разметку, только обычный текст с пронумерованными пунктами «1) 2) 3) …»;
- если текста слишком мало, сначала напиши, что информации недостаточно, и задай несколько уточняющих вопросов;
- не придумывай конкретные суммы, даты и реквизиты, если их нет в тексте.
"""

# ----------------- Клиент Groq -----------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — всегда будет использоваться fallback")


async def call_groq(system_prompt: str, user_query: str) -> str:
    if not client:
        raise RuntimeError("GROQ client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.6,
        max_tokens=1200,
        top_p=1,
    )
    content = chat_completion.choices[0].message.content or ""
    return content.strip()


async def generate_reply(system_prompt: str, query: str, scenario: str) -> Dict[str, str]:
    safe_scenario = scenario or "contract"

    if not query or not query.strip():
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "scenario": safe_scenario,
        }

    try:
        answer = await call_groq(system_prompt, query)
        logger.info("Успешный ответ от Groq для сценария %s", safe_scenario)
        return {"reply_text": answer, "scenario": safe_scenario}
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": safe_scenario}


# ----------------- FastAPI -----------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника для юристов",
    version="0.2.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or "contract"
    )

    # -------- Сценарий 1: договор --------
    if scenario == "contract":
        type_ = payload.get("Тип_договора") or payload.get("Тип договора") or ""
        parties = payload.get("Стороны") or ""
        subject = payload.get("Предмет") or ""
        terms = payload.get("Сроки") or ""
        payment = payload.get("Оплата") or ""
        special = payload.get("Особые_условия") or payload.get("Особые условия") or ""

        parts = []
        if type_:
            parts.append(f"Тип договора: {type_}")
        if parties:
            parts.append(f"Стороны и роли: {parties}")
        if subject:
            parts.append(f"Предмет: {subject}")
        if terms:
            parts.append(f"Сроки: {terms}")
        if payment:
            parts.append(f"Порядок оплаты: {payment}")
        if special:
            parts.append(f"Особые условия и риски: {special}")

        query = "\n".join(parts).strip()
        system_prompt = PROMPT_CONTRACT

    # -------- Сценарий 2: претензия --------
    elif scenario == "claim":
        adresat = payload.get("Адресат") or ""
        osn = payload.get("Основание") or ""
        narush = (
            payload.get("Нарушение_и_обстоятельства")
            or payload.get("Нарушение и обстоятельства")
            or ""
        )
        treb = payload.get("Требования") or ""
        srok = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
        contacts = payload.get("Контакты") or ""

        parts = []
        if adresat:
            parts.append(f"Адресат: {adresat}")
        if osn:
            parts.append(f"Основание: {osn}")
        if narush:
            parts.append(f"Нарушение и обстоятельства: {narush}")
        if treb:
            parts.append(f"Требования заявителя: {treb}")
        if srok:
            parts.append(f"Желаемый срок исполнения: {srok}")
        if contacts:
            parts.append(f"Контактные данные заявителя: {contacts}")

        query = "\n".join(parts).strip()
        system_prompt = PROMPT_CLAIM

    # -------- Сценарий 3: проверка/разбор пункта --------
    elif scenario == "checklist":
        text_for_check = (
            payload.get("Текст")
            or payload.get("Текст_для_проверки")
            or payload.get("Текст для проверки")
            or payload.get("Запрос")
            or ""
        )
        query = text_for_check.strip()
        system_prompt = PROMPT_CHECKLIST

    # fallback — считаем, что это общий запрос про договор
    else:
        raw_query = payload.get("Запрос") or ""
        query = raw_query.strip()
        system_prompt = PROMPT_CONTRACT
        scenario = "contract"

    logger.info("Detected scenario=%s", scenario)
    result = await generate_reply(system_prompt, query, scenario)
    return result


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
