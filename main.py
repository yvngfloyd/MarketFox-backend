import os
import uuid
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body
from fastapi.responses import FileResponse
from groq import Groq
from docx import Document

# -------------------------------------------------
# Логгер
# -------------------------------------------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# -------------------------------------------------
# Конфиг
# -------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

# Адрес бэкенда (для ссылок на файлы)
BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app")

# Папка для временных файлов
FILES_DIR = Path("/tmp/files")
FILES_DIR.mkdir(parents=True, exist_ok=True)

# Fallback, если ИИ недоступен
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. "
    "Попробуй повторить запрос чуть позже или уточни вводные данные."
)

# -------------------------------------------------
# Промты
# -------------------------------------------------

PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-помощник юриста по российскому праву.

Твоя задача — на основе кратких вводных подготовить ЧЕРНОВИК договора.
Это именно черновик, который юрист потом будет проверять и дорабатывать.

Обязательные требования:
- Пиши только по-русски.
- Опираться на нормы российского права (ГК РФ и т.п.), но без длинных цитат статей.
- Структура договора: преамбула, предмет, права и обязанности сторон, порядок расчётов,
  ответственность, порядок разрешения споров, срок действия, заключительные положения.
- Используй нейтральный юридический стиль, без «воды» и оценки спора.
- Допускается 1–2 аккуратных переносов/пояснений, но без markdown-разметки (**жирного** и т.п.).

Формат:
1) Сначала краткая строка-описание: какой договор и между кем.
2) Далее — полноценный текст договора с нумерованными разделами и пунктами.
3) В конце добавь пометку: "Черновик подготовлен ИИ и требует проверки юристом."
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-помощник юриста по российскому праву.

Твоя задача — по введённым данным подготовить ЧЕРНОВИК ПРЕТЕНЗИИ (досудебной).
Это черновик, который юрист потом проверит и при необходимости изменит.

Обязательные требования:
- Пиши по-русски, официально-деловым языком.
- Обязательно укажи: кому адресовано, от кого, по какому договору/основанию,
  в чём нарушение, на что ссылается заявитель (кратко, без перегруза статьями),
  какие требования и в какой срок должны быть выполнены.
- Не придумывай конкретные реквизиты и суммы, если их нет во вводных.
- В конце добавь формулировку о том, что при неисполнении требований
  заявитель оставляет за собой право обратиться в суд.

Формат:
1) «Шапка» (адресат, данные заявителя) — в виде обычного текста.
2) Основная часть с описанием обстоятельств и нарушений.
3) Блок с требованиями.
4) Срок исполнения.
5) Завершение + пометка, что текст является черновиком и требует проверки юристом.
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ИИ-помощник юриста по анализу отдельных условий договоров (российское право).

К тебе присылают ОДИН или НЕСКОЛЬКО пунктов договора.
Твоя задача:
1) Понятно и коротко объяснить, что означает этот пункт для каждой стороны.
2) Указать потенциальные риски и односторонние условия.
3) При необходимости предложить 1–2 мягких варианта формулировки, которые более сбалансированы.

Формат ответа:
1) Краткое объяснение простым языком.
2) Блок «Риски для клиента:» — 2–5 тезисов.
3) Блок «Как можно улучшить формулировку:» — 1–3 вариантов, каждый с новой строки.

Не используй markdown (**жирный**, списки со звёздочками). 
Пиши обычным текстом с нумерацией 1), 2), 3) при необходимости.
Если текст совсем непонятный/рваный, сначала напиши, что он неполный, и попроси прислать целиком.
"""

# -------------------------------------------------
# Инициализация Groq
# -------------------------------------------------
client: Optional[Groq] = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будет использоваться только fallback")


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq. Если что-то идёт не так, наружу летит исключение.
    """
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=1400,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# -------------------------------------------------
# Генерация DOCX и раздача файлов
# -------------------------------------------------

def generate_docx(text: str, prefix: str) -> str:
    """
    Генерируем простой DOCX-черновик из текста и возвращаем публичную ссылку.
    """
    doc = Document()
    # Разобьём по абзацам, чтобы не было одной простыни
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        doc.add_paragraph(block)

    file_id = uuid.uuid4().hex
    filename = f"{prefix}_{file_id}.docx"
    filepath = FILES_DIR / filename
    doc.save(filepath)

    file_url = f"{BASE_URL}/files/{filename}"
    logger.info("Создан файл %s", file_url)
    return file_url


app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристу",
    version="0.2.0",
)


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}


@app.get("/files/{filename}")
async def get_file(filename: str):
    """
    Отдаём ранее сгенерированный DOCX-файл.
    """
    filepath = FILES_DIR / filename
    if not filepath.exists():
        # FastAPI сам вернёт 404, если FileResponse не сможет открыть файл
        logger.warning("Файл не найден: %s", filepath)
    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


# -------------------------------------------------
# Основная логика генерации ответа
# -------------------------------------------------

async def generate_reply(
    system_prompt: str,
    user_query: str,
    scenario: str,
    need_file: bool = False,
    file_prefix: str = "draft",
) -> Dict[str, str]:
    """
    Общий хелпер: получить ответ ИИ + при необходимости сгенерировать DOCX.
    """
    safe_scenario = scenario or "contract"

    if not user_query or not user_query.strip():
        logger.info("Пустые данные для сценария %s", safe_scenario)
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "scenario": safe_scenario,
        }

    try:
        answer = await call_groq(system_prompt, user_query)
        logger.info("Успешный ответ от Groq для сценария %s", safe_scenario)

        result: Dict[str, str] = {
            "reply_text": answer,
            "scenario": safe_scenario,
        }

        if need_file:
            try:
                file_url = generate_docx(answer, file_prefix)
                result["file_url"] = file_url
            except Exception:
                logger.exception("Не удалось сгенерировать DOCX для сценария %s", safe_scenario)

        return result

    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": safe_scenario,
        }


# -------------------------------------------------
# Endpoint для BotHelp
# -------------------------------------------------

@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Главная точка, куда шлёт запрос BotHelp.

    Ожидаем поле 'scenario' + набор полей в зависимости от ветки:
    1) 'contract' — черновик договора
    2) 'claim'    — черновик претензии
    3) 'clause'   — анализ пункта договора
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "contract"
    )

    # ---------------- CONTRACT ----------------
    if scenario == "contract":
        # Поля могут называться с пробелами в BotHelp — обрабатываем оба варианта
        contract_type = payload.get("Тип_договора") or payload.get("Тип договора") or ""
        parties = payload.get("Стороны") or ""
        subject = payload.get("Предмет") or ""
        terms = payload.get("Сроки_и_оплата") or payload.get("Сроки и оплата") or ""
        special = payload.get("Особые_условия") or payload.get("Особые условия") or ""

        parts = []
        if contract_type:
            parts.append(f"Тип договора: {contract_type}")
        if parties:
            parts.append(f"Стороны и реквизиты: {parties}")
        if subject:
            parts.append(f"Предмет договора: {subject}")
        if terms:
            parts.append(f"Сроки и порядок оплаты: {terms}")
        if special:
            parts.append(f"Особые условия и риски: {special}")

        user_query = "\n".join(parts)

        return await generate_reply(
            system_prompt=PROMPT_CONTRACT,
            user_query=user_query,
            scenario="contract",
            need_file=True,
            file_prefix="contract",
        )

    # ---------------- CLAIM ----------------
    if scenario == "claim":
        adresat = payload.get("Адресат") or ""
        basis = payload.get("Основание") or ""
        violation = (
            payload.get("Нарушение_и_обстоятельства")
            or payload.get("Нарушение и обстоятельства")
            or ""
        )
        demands = payload.get("Требования") or ""
        deadline = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
        contacts = payload.get("Контакты") or ""

        parts = []
        if adresat:
            parts.append(f"Адресат: {adresat}")
        if basis:
            parts.append(f"Основание (договор/ситуация): {basis}")
        if violation:
            parts.append(f"Суть нарушения и обстоятельства: {violation}")
        if demands:
            parts.append(f"Требования заявителя: {demands}")
        if deadline:
            parts.append(f"Желаемый срок исполнения: {deadline}")
        if contacts:
            parts.append(f"Контакты заявителя: {contacts}")

        user_query = "\n".join(parts)

        return await generate_reply(
            system_prompt=PROMPT_CLAIM,
            user_query=user_query,
            scenario="claim",
            need_file=True,
            file_prefix="claim",
        )

    # ---------------- CLAUSE ----------------
    # Анализ пункта/фрагмента договора
    fragment = (
        payload.get("Фрагмент")
        or payload.get("Текст")
        or payload.get("Текст/описание")
        or payload.get("Пункт")
        or ""
    )

    user_query = fragment
    return await generate_reply(
        system_prompt=PROMPT_CLAUSE,
        user_query=user_query,
        scenario="clause",
        need_file=False,
    )
