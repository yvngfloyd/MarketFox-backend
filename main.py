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

# ----------------- Промты -----------------

PROMPT_CONTRACT = """
Ты — LegalFox, ассистент юриста. Твоя задача — на основе входных данных составить
АККУРАТНЫЙ ЧЕРНОВИК гражданско-правового договора на русском языке.

Важно:
- Это ЧЕРНОВИК для юриста, не готовый к подписанию.
- Не придумывай факты, суммы, реквизиты — используй только то, что дал пользователь.
- Не давай правовых советов, как обходить закон или уходить от ответственности.
- Пиши нейтрально, без эмоций.

Формат результата:
1) Сначала коротко одной строкой: какой это договор (например: "Черновик договора поставки").
2) Далее полноценный текст договора с нумерацией разделов и пунктов (1., 1.1., 1.2. и т.п.).
3) Обязательно включи разделы:
   - Предмет договора
   - Права и обязанности сторон
   - Порядок расчётов / Оплата
   - Срок действия договора
   - Ответственность сторон
   - Порядок разрешения споров
   - Прочие условия
   - Реквизиты сторон (оставь поля для реквизитов, если их не дали).
4) Если каких-то данных нет — оставь формулировки общими, но не выдумывай детали.
5) Не используй Markdown, только обычный текст.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ассистент юриста. Твоя задача — на основе входных данных составить
ЧЕРНОВИК досудебной претензии / письма-требования на русском языке.

Важно:
- Это ЧЕРНОВИК для юриста, не готовый к отправке.
- Не придумывай суммы, даты и факты — используй только входные данные.
- Не давай инструкций, как нарушать закон или скрываться от ответственности.
- Пиши деловым, но понятным языком.

Формат результата:
1) Шапка: адресат, от кого (если указано), город, дата — город и дату можно оставить пустыми местами "___".
2) Заголовок: "Претензия" или "Претензионное письмо".
3) Описательная часть:
   - кратко изложи, по какому договору / основанию возникли отношения;
   - опиши, в чём состоит нарушение и какие последствия это вызвало.
4) Требования:
   - чётко и по пунктам перечисли требования (оплатить, передать товар, устранить недостатки и т.п.);
   - укажи срок исполнения требований, если он передан во входных данных.
5) Заключительная часть:
   - фраза о том, что в случае неисполнения требований в срок заявитель оставляет за собой право
     обратиться в суд и иные компетентные органы.
6) Подпись: место для ФИО и подписи заявителя.

Технические требования:
- Не используй Markdown, только обычный текст.
- Не придумывай нормы закона и номера статей, если их явно не дали.
- Структурируй текст с абзацами, чтобы было удобно читать.
"""

FALLBACK_TEXT = (
    "Сейчас не получается обратиться к нейросети, поэтому черновик не сформирован.\n"
    "Попробуй ещё раз чуть позже или собери текст вручную на основе своих заметок."
)

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


def call_groq(system_prompt: str, user_message: str) -> str:
    """
    Синхронный вызов Groq. Если что-то пойдёт не так — кидаем исключение.
    """
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=2000,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


def build_contract_message(payload: Dict[str, Any]) -> str:
    """
    Собираем один текст для LLM по договору.
    Ожидаем поля:
    - Тип_договора
    - Стороны
    - Предмет
    - Сроки
    - Оплата
    - Особые_условия
    """
    parts: list[str] = []

    contract_type = payload.get("Тип_договора", "")
    if contract_type:
        parts.append(f"Тип договора: {contract_type}")

    parties = payload.get("Стороны", "")
    if parties:
        parts.append(f"Стороны и описание: {parties}")

    subject = payload.get("Предмет", "")
    if subject:
        parts.append(f"Предмет договора: {subject}")

    terms = payload.get("Сроки", "")
    if terms:
        parts.append(f"Сроки: {terms}")

    payment = payload.get("Оплата", "")
    if payment:
        parts.append(f"Оплата: {payment}")

    special = payload.get("Особые_условия", "")
    if special:
        parts.append(f"Особые условия / риски: {special}")

    if not parts:
        parts.append("Данных почти нет. Нужен общий шаблон гражданско-правового договора.")

    return "\n".join(parts)


def build_claim_message(payload: Dict[str, Any]) -> str:
    """
    Собираем один текст для LLM по претензии.
    Ожидаем поля:
    - Адресат
    - Основание
    - Нарушение_и_обстоятельства
    - Требования
    - Срок_исполнения
    - Контакты
    """
    parts: list[str] = []

    addressee = payload.get("Адресат", "")
    if addressee:
        parts.append(f"Адресат претензии: {addressee}")

    base = payload.get("Основание", "")
    if base:
        parts.append(f"Основание отношений (договор / ситуация): {base}")

    violation = payload.get("Нарушение_и_обстоятельства", "")
    if violation:
        parts.append(f"Суть нарушения и обстоятельства: {violation}")

    demands = payload.get("Требования", "")
    if demands:
        parts.append(f"Требования заявителя: {demands}")

    deadline = payload.get("Срок_исполнения", "")
    if deadline:
        parts.append(f"Срок исполнения требований: {deadline}")

    contacts = payload.get("Контакты", "")
    if contacts:
        parts.append(f"Контакты заявителя: {contacts}")

    if not parts:
        parts.append("Данных почти нет. Нужен общий черновик досудебной претензии.")

    return "\n".join(parts)


async def generate_reply(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Общая логика генерации ответа:
    - выбираем сценарий;
    - собираем текст для модели;
    - вызываем Groq;
    - на ошибке уходим в fallback.
    """
    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "contract"
    )

    try:
        if scenario == "claim":
            system_prompt = PROMPT_CLAIM
            user_message = build_claim_message(payload)
        else:
            # по умолчанию — договор
            scenario = "contract"
            system_prompt = PROMPT_CONTRACT
            user_message = build_contract_message(payload)

        logger.info("Сценарий=%s, собираем черновик", scenario)

        answer = call_groq(system_prompt, user_message)
        logger.info("Успешный ответ от Groq для сценария %s", scenario)

        return {
            "reply_text": answer,
            "scenario": scenario,
        }

    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": scenario,
        }


# ----------------- FastAPI -----------------

app = FastAPI(
    title="LegalFox API",
    description="Backend для LegalFox — ИИ-помощника юриста (Groq, Railway)",
    version="0.2.0",
)


@app.post("/")
async def legalfox_entry(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    result = await generate_reply(payload)
    return result


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
