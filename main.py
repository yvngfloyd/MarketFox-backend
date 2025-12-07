import os
import logging
from typing import Any, Dict, List

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

# ----------------- Промты -----------------

PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-помощник юриста. Твоя задача — на основе краткого описания
сделать ЧЕРНОВИК гражданско-правового договора.

Важные правила:
- работаешь в праве РФ;
- это черновик, а не окончательный текст — не пытайся заменить юриста;
- используй нейтральный деловой стиль, без излишней воды;
- не выдумывай реквизиты и конкретные суммы, которых нет в описании;
- не используй Markdown, списки с * и заголовки с # — только обычный текст с абзацами.

Структура ответа:
1) Краткий заголовок: какой это договор (например, "ДОГОВОР ПОСТАВКИ").
2) Блок "Преамбула" — стороны договора (как в классических шаблонах).
3) Блок "Предмет договора" — что именно передаётся/оказывается.
4) Блок "Права и обязанности сторон" — 4–8 пунктов по существу.
5) Блок "Порядок расчётов" и "Срок действия договора".
6) Блок "Ответственность сторон" и "Прочие условия".
7) Завершение: "Реквизиты и подписи сторон" (без выдумывания реквизитов).

Если исходных данных мало или они противоречивы — добавь короткую ремарку
в начале: какие моменты юристу нужно будет обязательно проверить/уточнить.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-помощник юриста. Твоя задача — составить ЧЕРНОВИК
досудебной претензии на основе предоставленных данных.

Работаешь в праве РФ.

Правила:
- это черновик, не окончательный документ — пиши так, чтобы юрист мог быстро
  доработать текст;
- стиль деловой, но понятный, без чрезмерной канцелярщины;
- не выдумывай суммы, даты и реквизиты, которых нет в описании;
- не используй Markdown, только обычный текст с абзацами.

Структура ответа:
1) "Шапка" претензии — кому направляется, от кого (в общих чертах).
2) Блок "Обстоятельства" — кратко и по порядку описать ситуацию и нарушения.
3) Блок "Правовое обоснование" — со ссылками на общие нормы (ГК РФ и т.п.),
   но без глубоких научных комментариев.
4) Блок "Требования" — перечислить требования заявителя.
5) Блок "Срок исполнения требований" — указать срок из описания (или разумный).
6) Заключительная часть: предупреждение о возможном обращении в суд/контролирующие органы.
7) Место для подписи и контактов отправителя.

Если каких-то данных явно не хватает (суммы, даты, номера договора, реквизиты сторон),
в тексте поставь пустые места в кавычках или с пометкой "___".
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ассистент по работе с отдельными пунктами договора.

Тебе передают:
- исходный пункт договора;
- краткое описание договора и сторон;
- задачу: упростить, усилить, сбалансировать или адаптировать пункт.

Твоя задача:
1) Кратко пояснить (1–2 предложения), что делает исходный пункт и в чём его риск/слабость.
2) Предложить 1–2 варианта новой редакции пункта:
   - первый — более "за клиента";
   - второй — более сбалансированный.
3) Писать простым юридическим языком РФ без излишней воды.

Не используй Markdown, не пиши огромные полотна текста — работай блоками с абзацами.
"""

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети, но могу подсказать общие шаги:\n\n"
    "1) Чётко сформулируйте задачу (договор, претензия, редактирование пункта).\n"
    "2) Соберите факты: стороны, даты, суммы, ключевые условия.\n"
    "3) Составьте черновик и обязательно покажите его юристу перед использованием.\n\n"
    "Можете повторить запрос или уточнить детали — я попробую помочь более прицельно."
)

# ----------------- Groq клиент -----------------

client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будет использоваться fallback-текст")


async def call_groq(system_prompt: str, user_query: str) -> str:
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=900,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# ----------------- Вспомогательные сборщики -----------------


def build_contract_query(payload: Dict[str, Any]) -> str:
    """
    Собираем один текст из ответов первой ветки (договор).
    Поддерживаем и имена без пробелов, и имена с пробелами.
    """
    contract_type = payload.get("Тип_договора") or payload.get("Тип договора") or ""
    parties = payload.get("Стороны") or payload.get("Стороны договора") or ""
    subject = payload.get("Предмет") or payload.get("Предмет договора") or ""
    terms = payload.get("Сроки") or payload.get("Сроки и порядок") or ""
    payment = payload.get("Оплата") or payload.get("Условия оплаты") or ""
    special = payload.get("Особые_условия") or payload.get("Особые условия") or ""

    parts: List[str] = [
        f"Тип договора: {contract_type}",
        f"Стороны и краткое описание: {parties}",
        f"Предмет договора: {subject}",
        f"Сроки и порядок исполнения: {terms}",
        f"Условия оплаты: {payment}",
    ]
    if special:
        parts.append(f"Особые условия и риски: {special}")

    return "\n".join(parts)


def build_claim_query(payload: Dict[str, Any]) -> str:
    """
    Собираем один текст из ответов второй ветки (претензия).
    """
    recipient = payload.get("Адресат") or payload.get("Адресат претензии") or ""
    grounds = payload.get("Основание") or payload.get("Основание претензии") or ""
    facts = (
        payload.get("Нарушение_и_обстоятельства")
        or payload.get("Нарушение и обстоятельства")
        or ""
    )
    demands = payload.get("Требования") or payload.get("Требования претензии") or ""
    deadline = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
    contacts = payload.get("Контакты") or ""

    parts: List[str] = [
        f"Адресат (кому направляется претензия): {recipient}",
        f"Основание претензии (договор, ситуация): {grounds}",
        f"Нарушение и обстоятельства: {facts}",
        f"Требования заявителя: {demands}",
        f"Срок исполнения требований: {deadline}",
    ]
    if contacts:
        parts.append(f"Контакты заявителя: {contacts}")

    return "\n".join(parts)


def build_clause_query(payload: Dict[str, Any]) -> str:
    clause_text = payload.get("Пункт") or payload.get("Текст пункта") or ""
    context = payload.get("Контекст") or payload.get("Контекст договора") or ""
    task = payload.get("Задача") or payload.get("Задача_редакции") or ""

    parts: List[str] = [
        f"Исходный пункт договора: {clause_text}",
        f"Краткий контекст договора: {context}",
        f"Задача по редактированию пункта: {task}",
    ]
    return "\n".join(parts)


# ----------------- Общая генерация -----------------


async def generate_reply(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Определяем сценарий и генерируем текст.
    """
    # нормализуем scenario
    raw_scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or ""
    )
    raw_scenario = str(raw_scenario).strip().lower()

    if raw_scenario == "claim":
        scenario = "claim"
    elif raw_scenario == "clause":
        scenario = "clause"
    else:
        scenario = "contract"

    logger.info("Detected scenario=%s", scenario)

    # собираем user_query и выбираем системный промт
    if scenario == "claim":
        user_query = build_claim_query(payload)
        system_prompt = PROMPT_CLAIM
    elif scenario == "clause":
        user_query = build_clause_query(payload)
        system_prompt = PROMPT_CLAUSE
    else:
        user_query = build_contract_query(payload)
        system_prompt = PROMPT_CONTRACT
        scenario = "contract"

    logger.info("User query for scenario=%s: %s", scenario, user_query)

    if not user_query.strip():
        return {
            "reply_text": "Пока не вижу данных для черновика. Попробуй ответить на вопросы бота ещё раз.",
            "scenario": scenario,
        }

    try:
        if client is None:
            raise RuntimeError("Groq client is not available")
        answer = await call_groq(system_prompt, user_query)
        logger.info("Успешный ответ от Groq для сценария %s", scenario)
        return {"reply_text": answer, "scenario": scenario}
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": scenario}


# ----------------- FastAPI -----------------

app = FastAPI(
    title="LegalFox API",
    description="Backend для LegalFox — ИИ-помощника юриста",
    version="0.1.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    result = await generate_reply(payload)
    return result


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
