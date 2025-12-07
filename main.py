import os
import logging
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Body

# ----------------- Логгер -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# ----------------- Конфиг -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"  # модель Groq для тестов

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. "
    "Сформируй черновик, опираясь на свой опыт, а этот текст используй как подсказку. "
    "Не забывай самостоятельно проверять формулировки и риски."
)

# ----------------- Промты -----------------

PROMPT_DRAFT_CONTRACT = """
Ты — AI-помощник практикающему юристу в России. Ты НЕ оказываешь юридические услуги клиентам,
а помогаешь юристу быстрее подготовить ЧЕРНОВИК договора.

Твоя задача:
- на основе входных данных составить структурированный черновик гражданско-правового договора;
- НЕ придумывать факты, которых нет в описании;
- если данных не хватает — ставить понятные заглушки (например, "___").

Обязательная структура договора:
1. Преамбула (стороны, далее — как именуются).
2. Предмет договора.
3. Права и обязанности сторон.
4. Цена и порядок расчётов (если применимо).
5. Порядок приёмки (если применимо).
6. Ответственность сторон и неустойка (если о ней упомянуто во входных данных).
7. Срок действия и порядок расторжения.
8. Прочие условия (конфиденциальность, форс-мажор и т.п., если уместно).
9. Реквизиты сторон (можно оставить как шаблон без конкретных реквизитов).

Требования к стилю:
- юридический деловой стиль РФ, но без излишней канцелярщины;
- не ссылайся на конкретные статьи законов, если юрист сам их не указал;
- не давай оценок "законно/незаконно", только формулируй текст;
- помни, что это именно ЧЕРНОВИК для доработки юристом.

В конце текста добавь короткое напоминание:
"Этот документ является черновиком. Перед использованием юрист должен его проверить и доработать."
"""

PROMPT_DRAFT_CLAIM = """
Ты — AI-помощник практикающему юристу в России. Ты НЕ оказываешь юридические услуги клиентам,
а помогаешь юристу быстро подготовить ЧЕРНОВИК претензии (досудебной претензии/требования).

Твоя задача:
- оформить входные данные в виде структурированной претензии;
- НЕ придумывать факты и суммы, которых нет во входных данных;
- если чего-то не хватает — оставлять разумные заглушки ("___").

Обязательная структура претензии:
1. "Шапка" — кому и от кого (можно кратко, без полных реквизитов).
2. Описание обстоятельств (краткая фабула: что произошло, когда, в чём нарушение).
3. Ссылка на документы и отношения (договор, чек, акт и т.п., только если юрист про них писал).
4. Правовая оценка — общими словами (без точного цитирования норм, если их не дали).
5. Требования (что именно просим: вернуть деньги, устранить недостатки, выполнить обязательство и т.п.).
6. Срок для исполнения требований (если указан, либо оставить "___" для заполнения).
7. Предупреждение о возможном обращении в суд или иные органы при невыполнении требований.

Стиль:
- формальный, вежливый, без эмоций;
- не используй категоричных формулировок типа "точно незаконно", "гарантированно взыщем" и т.д.;
- пиши так, чтобы юристу было удобно править текст.

В конце добавь фразу:
"Этот текст — черновик претензии. Юрист должен проверить и при необходимости скорректировать его перед направлением адресату."
"""

PROMPT_CLAUSE_REVIEW = """
Ты — AI-помощник практикающему юристу в России. Ты НЕ оказываешь юридические услуги клиентам,
а помогаешь юристу анализировать и переписывать отдельные пункты договоров.

Входные данные:
- текст пункта договора;
- за какую сторону выступает юрист (Например: Исполнитель, Заказчик, Арендодатель, Арендатор и т.п.);
- цель работы с пунктом (анализ рисков / усилить позицию моей стороны / смягчить формулировки).

Твоя задача:
1) Кратко разобрать пункт:
   - какие в нём потенциальные риски и односторонние моменты;
   - что может быть неясно или спорно.

2) В зависимости от цели:
   - "анализ рисков" — перечисли риски простыми фразами;
   - "усилить позицию моей стороны" — предложи 1–2 варианта переписанного пункта,
     которые более выгодны указанной стороне;
   - "смягчить формулировки" — предложи 1–2 варианта более мягкого и сбалансированного текста.

Правила:
- не давай категоричных выводов "точно недействительно", "гарантированно взыщется";
- не ссылайся на конкретные статьи законов, если юрист их сам не назвал;
- пиши компактно и структурированно, чтобы юристу было удобно сравнивать варианты.

В конце кратко напомни:
"Это варианты формулировок и список типовых рисков. Юрист должен самостоятельно оценить их применимость к конкретной ситуации."
"""

# ----------------- Вспомогательные функции -----------------


def build_query_for_draft_contract(payload: Dict[str, Any]) -> str:
    parties = payload.get("Стороны", "") or payload.get("parties", "")
    subject = payload.get("Предмет", "") or payload.get("subject", "")
    terms = payload.get("Сроки", "") or payload.get("terms", "")
    payment = payload.get("Оплата", "") or payload.get("payment", "")
    extras = payload.get("Особые_условия", "") or payload.get("extras", "")

    text = (
        "Подготовь черновик договора на основе следующих данных:\n\n"
        f"Тип договора: {payload.get('Тип_договора', 'общегражданский договор')}\n"
        f"Стороны: {parties}\n"
        f"Предмет: {subject}\n"
        f"Сроки: {terms}\n"
        f"Цена и порядок оплаты: {payment}\n"
        f"Особые условия и важные моменты: {extras}\n"
    )
    return text


def build_query_for_draft_claim(payload: Dict[str, Any]) -> str:
    to_whom = payload.get("Кому", "") or payload.get("to_whom", "")
    from_whom = payload.get("От_кого", "") or payload.get("from_whom", "")
    facts = payload.get("Суть_проблемы", "") or payload.get("facts", "")
    demands = payload.get("Требования", "") or payload.get("demands", "")
    deadline = payload.get("Срок_исполнения", "") or payload.get("deadline", "")
    legal_base = payload.get("Правовая_база", "") or payload.get("legal_base", "")

    text = (
        "Подготовь черновик досудебной претензии на основе следующих данных:\n\n"
        f"Кому адресована претензия: {to_whom}\n"
        f"От кого претензия: {from_whom}\n"
        f"Обстоятельства и суть проблемы: {facts}\n"
        f"Требования к адресату: {demands}\n"
        f"Желаемый срок для исполнения требований: {deadline}\n"
        f"Указанные юристом акты/правовая база (если есть): {legal_base}\n"
    )
    return text


def build_query_for_clause_review(payload: Dict[str, Any]) -> str:
    clause = payload.get("Пункт", "") or payload.get("clause", "")
    side = payload.get("Сторона", "") or payload.get("side", "")
    goal = payload.get("Цель", "") or payload.get("goal", "")

    text = (
        "Ниже текст пункта договора. Разбери его и при необходимости предложи альтернативные формулировки.\n\n"
        f"Пункт договора:\n{clause}\n\n"
        f"Юрист представляет сторону: {side}\n"
        f"Цель работы с пунктом: {goal}\n"
    )
    return text


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq через HTTP API. Бросает исключение, если что-то пошло не так.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query.strip()},
        ],
        "temperature": 0.4,
        "max_tokens": 1400,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=body,
        )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""
    return content.strip()


async def generate_reply(scenario: str, payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Выбор промта + сбор пользовательского запроса + вызов Groq.
    При ошибке возвращаем fallback-текст.
    """
    safe_scenario = scenario or "draft_contract"

    if safe_scenario == "draft_claim":
        system_prompt = PROMPT_DRAFT_CLAIM
        query = build_query_for_draft_claim(payload)
    elif safe_scenario == "clause_review":
        system_prompt = PROMPT_CLAUSE_REVIEW
        query = build_query_for_clause_review(payload)
    else:
        # по умолчанию — черновик договора
        system_prompt = PROMPT_DRAFT_CONTRACT
        safe_scenario = "draft_contract"
        query = build_query_for_draft_contract(payload)

    # Проверка, что есть хоть какой-то текст
    if not query.strip():
        return {
            "reply_text": (
                "Я не получил исходных данных. "
                "Ответь на вопросы бота о сторонах, предмете и обстоятельствах — "
                "и я подготовлю черновик."
            ),
            "scenario": safe_scenario,
        }

    try:
        answer = await call_groq(system_prompt, query)
        logger.info("Успешный ответ от Groq для сценария %s", safe_scenario)
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
    title="LegalFox API (Groq, Railway)",
    description="Backend для AI-помощника юристам (черновики и анализ пунктов договоров).",
    version="0.1.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Главная точка, куда BotHelp шлёт запрос.

    Ожидается, что в payload:
    - есть поле 'scenario' или 'Сценарий' со значениями:
      'draft_contract', 'draft_claim', 'clause_review';
    - остальные поля зависят от сценария (Стороны, Предмет, Пункт и т.п.).
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or "draft_contract"
    )

    logger.info("User scenario=%s", scenario)

    result = await generate_reply(scenario, payload)
    return result


@app.get("/")
async def root() -> Dict[str, str]:
    return {
        "status": "ok",
        "message": "LegalFox backend is running",
    }
