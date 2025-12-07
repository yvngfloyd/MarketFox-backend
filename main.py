import os
import logging
from typing import Any, Dict

from fastapi import FastAPI, Body
from groq import Groq

# ================== ЛОГИ ==================

logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
if not logger.handlers:
    logger.addHandler(handler)

# ================== КОНФИГ ==================

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

FALLBACK_TEXT = (
    "Сейчас не получилось сформировать текст с помощью нейросети.\n\n"
    "Опиши ещё раз кратко суть договора (стороны, предмет, сроки, оплату), "
    "и юрист сможет сделать черновик вручную. "
    "Этот бот даёт только черновики и не заменяет полноценную юридическую экспертизу."
)

# ================== ПРОМПТЫ ==================

PROMPT_DRAFT_CONTRACT = """
Ты — LegalFox, ИИ-помощник для юристов в РФ.

Твоя задача — по краткому описанию клиента подготовить ЧЕРНОВИК гражданско-правового договора
(оказание услуг, поставка, аренда, подряд и т.п.). Черновик не должен считаться готовым документом.

Требования:

1) ПИШИ ТОЛЬКО ТЕКСТ ДОГОВОРА, без комментариев и без обращения к пользователю.
   Не добавляй вступления вроде "Вот черновик договора" и т.п.

2) Структура договора (примерный шаблон, можно адаптировать под ситуацию):
   - Преамбула: стороны, их статусы (Заказчик/Исполнитель, Арендодатель/Арендатор и т.п.), основание полномочий без конкретных реквизитов.
   - Предмет договора.
   - Права и обязанности сторон.
   - Порядок расчётов / цена и порядок оплаты.
   - Срок действия договора.
   - Ответственность сторон и неустойка (если уместно).
   - Порядок расторжения.
   - Конфиденциальность (если уместно).
   - Форс-мажор.
   - Порядок разрешения споров (ссылка на применимое право РФ, подсудность / арбитраж по умолчанию).
   - Заключительные положения.
   - Реквизиты и подписи сторон (можно оставить пустыми полями-заглушками).

3) Не указывай конкретные паспортные данные, ИНН, ОГРН и т.п. —
   вместо этого используй формулировки общего характера ("реквизиты Сторон указываются в конце договора").

4) Если клиент дал очень мало информации, делай разумные, но нейтральные допущения
   и используй формулировки "если иное не предусмотрено Сторонами" и подобные.

5) В самом конце отдельной строкой напиши:
   "ВНИМАНИЕ: данный текст является черновиком. Перед использованием юрист должен проверить и доработать документ."

6) Не используй Markdown, списки с * и заголовки с #, только обычный текст.
"""

PROMPT_CLAUSE_EXPLAIN = """
Ты — LegalFox, ИИ-помощник юриста в РФ.

Твоя задача — простым языком, но юридически аккуратно:
1) Пояснить смысл присланного пункта договора для клиента.
2) Указать, какие риски для каждой стороны скрываются в формулировках.
3) Дать 1–2 варианта более сбалансированной или безопасной формулировки.

Формат ответа:
1) Короткое пояснение "что вообще делает эта клаузула".
2) Перечень основных рисков (каждый риск с новой строки, без Markdown-маркеров).
3) 1–2 альтернативных варианта пункта.

Не давай общих шаблонных советов вне связи с конкретным текстом.
Не используй Markdown и сложные списки.
"""

PROMPT_CLAUSE_RISK = """
Ты — LegalFox, ИИ-помощник по оценке рисков в договорах.

На вход ты получаешь:
- Тип договора и краткий контекст.
- Несколько ключевых пунктов (сроки, ответственность, штрафы, односторонний отказ и т.п.).

Твоя задача:
1) Сфокусироваться только на потенциальных рисках для Клиента (того, от чьего имени задаётся вопрос).
2) Разделить риски на:
   - коммерческие (деньги, штрафы, убытки, невозможность сменить контрагента),
   - юридические (односторонний отказ, перекос ответственности, тяжело доказать нарушения, скрытая подсудность и т.п.),
   - операционные (сложно исполнить, зависимость от действий контрагента, неясные формулировки).

Формат ответа:
1) Краткий абзац, кто и в каком договоре несёт риски.
2) Список рисков по группам (коммерческие / юридические / операционные) — внутри группы риски выводи с новой строки, но без Markdown-маркеров.
3) В конце 2–3 практических шага, что можно попросить юриста/контрагента изменить,
   чтобы снизить риски (не переписывай весь договор, только намекни направления).

Не давай "это не юридическая консультация" — за это отвечает разработчик бота.
Не используй Markdown и сложные списки.
"""

# ================== ИНИЦИАЛИЗАЦИЯ GROQ ==================

client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — всегда будет использоваться fallback")


# ================== ХЕЛПЕРЫ ==================

def _safe_get(payload: Dict[str, Any], key: str) -> str:
    """Безопасно достаём строку из payload."""
    value = payload.get(key) or ""
    return str(value).strip()


def build_draft_contract_query(payload: Dict[str, Any]) -> str:
    """Собираем описание договора из полей BotHelp."""
    contract_type = _safe_get(payload, "Тип_договора")
    parties = _safe_get(payload, "Стороны")
    subject = _safe_get(payload, "Предмет")
    terms = _safe_get(payload, "Сроки")
    payment = _safe_get(payload, "Оплата")
    special = _safe_get(payload, "Особые_условия")

    parts = [
        f"Тип договора: {contract_type or 'не указан'}",
        f"Стороны: {parties or 'не указаны'}",
        f"Предмет: {subject or 'не указан'}",
        f"Сроки: {terms or 'не указаны'}",
        f"Оплата: {payment or 'не указана'}",
    ]
    if special:
        parts.append(f"Особые условия / риски: {special}")

    return "\n".join(parts)


async def call_groq(system_prompt: str, user_query: str) -> str:
    """Вызов Groq. Бросает исключение, если что-то идёт не так."""
    if client is None:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=1200,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


async def handle_scenario(scenario: str, payload: Dict[str, Any]) -> str:
    """
    Выбираем сценарий и формируем запрос к модели.
    scenario:
      - draft_contract
      - clause_explain
      - clause_risk
    """
    scenario = scenario or "draft_contract"

    if scenario == "clause_explain":
        clause_text = _safe_get(payload, "Текст_пункта") or _safe_get(payload, "clause_text")
        if not clause_text:
            return "Мне нужен текст пункта договора, чтобы я мог его пояснить."
        return await call_groq(PROMPT_CLAUSE_EXPLAIN, clause_text)

    if scenario == "clause_risk":
        context = _safe_get(payload, "Контекст") or _safe_get(payload, "context")
        clauses = _safe_get(payload, "Ключевые_пункты") or _safe_get(payload, "clauses")
        if not clauses and not context:
            return "Опиши хотя бы кратко тип договора и один-два ключевых пункта, которые вызывают вопросы."
        user_query = f"Контекст: {context or 'не указан'}\nКлючевые пункты:\n{clauses}"
        return await call_groq(PROMPT_CLAUSE_RISK, user_query)

    # по умолчанию — draft_contract
    contract_query = build_draft_contract_query(payload)
    return await call_groq(PROMPT_DRAFT_CONTRACT, contract_query)


# ================== FASTAPI ==================

app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам (черновики договоров и работа с пунктами)",
    version="0.1.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Главная точка для BotHelp.
    Ожидаем:
      - scenario: draft_contract / clause_explain / clause_risk
      - остальные поля — как мы их настроим в BotHelp.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "draft_contract"
    )

    logger.info("User scenario=%s", scenario)

    try:
        reply = await handle_scenario(scenario, payload)
        logger.info("Успешный ответ от Groq для сценария %s", scenario)
        return {
            "reply_text": reply,
            "scenario": scenario,
        }
    except Exception as e:
        logger.exception("LegalFox error: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": scenario,
        }


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
