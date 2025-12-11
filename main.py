import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq
from fpdf import FPDF


# -------------------------------------------------
# Логирование
# -------------------------------------------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)


# -------------------------------------------------
# Конфиг
# -------------------------------------------------
BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)

FONTS_DIR = BASE_DIR  # шрифт лежит рядом с main.py
FONT_PATH = FONTS_DIR / "DejaVuSans.ttf"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app")

GROQ_MODEL = "llama-3.1-8b-instant"

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. "
    "Попробуй переформулировать запрос или повторить чуть позже."
)

# -------------------------------------------------
# Промпты
# -------------------------------------------------


PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-ассистент для подготовки черновиков гражданско-правовых договоров в РФ.

Тебе дают краткое описание:
- тип договора;
- стороны;
- предмет;
- сроки и порядок оплаты;
- особые условия и риски.

Твоя задача — на основе этих данных подготовить связный, аккуратный ТЕКСТ договора в деловом стиле.

Требования к ответу:
- Пиши по-русски, юридически нейтрально и понятно для обычного человека.
- Структурируй текст по пунктам и подпунктам, но без Markdown-разметки: НИКАКИХ **звёздочек**, #заголовков, списков с * и т.п.
- Не придумывай вымышленные реквизиты и суммы, если их нет во входных данных — используй формулировки вида «указывать реквизиты сторон», «сумма определяется соглашением сторон».
- НИЧЕГО не пиши про подписки, платный доступ, PDF и т.п.
- В конце текста можешь добавить короткий дисклеймер вроде «рекомендуется согласовать текст с юристом».

Верни только текст договора.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-ассистент по подготовке претензий (досудебных требований) в РФ.

Тебе дают:
- адресата;
- основание (договор, закон, ситуация);
- описание нарушения и обстоятельств;
- требования заявителя;
- срок добровольного исполнения;
- контактные данные заявителя.

Твоя задача — подготовить связный текст ПРЕТЕНЗИИ в официально-деловом стиле.

Требования к ответу:
- Пиши по-русски, юридически аккуратно, но понятно обычному человеку.
- Оформи как единый документ с логичным порядком: вводная часть, описание нарушения, требования, срок, заключительная часть.
- Не используй Markdown-разметку и спецсимволы: не нужно **жирного**, списков с *, заголовков с # и т.п.
- Не придумывай конкретные суммы и даты, если их нет во входных данных — используй формулировки вида «указать сумму», «указать дату».
- НИЧЕГО не пиши про подписки, платный доступ, PDF и т.п.
- В конце можешь добавить короткий дисклеймер о необходимости проверки юристом.

Верни только текст претензии.
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ассистент по анализу и доработке отдельных пунктов договора.

Пользователь присылает текст пункта или фрагмента договора. 
Твоя задача:
- кратко пояснить, что он означает простым языком;
- указать риски для стороны пользователя;
- предложить более безопасную альтернативную формулировку.

Требования:
- Пиши по-русски, без Markdown-разметки и спецсимволов (** и т.п.).
- Структурируй ответ в виде коротких абзацев: «Смысл пункта: …», «Риски: …», «Можно переписать так: …».
"""

# -------------------------------------------------
# Groq client
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


def clean_text(text: str) -> str:
    """Убираем простейший Markdown и лишние пробелы."""
    if not text:
        return ""
    cleaned = text.replace("**", "").replace("__", "")
    # уберём двойные пробелы и лишние пустые строки
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


async def call_groq(system_prompt: str, user_content: str) -> str:
    """Вызов Groq, бросает исключение при ошибке."""
    if client is None:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=1200,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return clean_text(content)


def parse_with_file_flag(payload: Dict[str, Any]) -> bool:
    """
    Интерпретируем флаг with_file.
    Любое НЕпустое значение, кроме '0' / 'false' / 'нет' — считаем истиной.
    Это делает интеграцию с BotHelp максимально терпимой к формату.
    """
    raw = str(
        payload.get("with_file")
        or payload.get("WithFile")
        or payload.get("premium")
        or payload.get("Premium")
        or ""
    ).strip().lower()

    if raw in ("", "0", "false", "нет", "no", "none", "off"):
        return False
    return True


# -------------------------------------------------
# Генерация PDF
# -------------------------------------------------


def create_pdf_from_text(title: str, body: str, prefix: str) -> str:
    """
    Создаёт простой PDF с заголовком и текстом.
    Возвращает имя файла (без BASE_URL).
    """
    if not FONT_PATH.exists():
        logger.error("Файл шрифта %s не найден", FONT_PATH)
        raise RuntimeError("Font file not found")

    pdf = FPDF(format="A4")
    pdf.add_page()

    # Подключаем TTF-шрифт с поддержкой кириллицы
    pdf.add_font("DejaVu", "", str(FONT_PATH), uni=True)
    pdf.set_auto_page_break(auto=True, margin=15)

    # Заголовок
    pdf.set_font("DejaVu", "", 16)
    pdf.multi_cell(0, 10, title, align="C")
    pdf.ln(5)

    # Основной текст
    pdf.set_font("DejaVu", "", 11)
    for paragraph in body.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            pdf.ln(3)
            continue
        pdf.multi_cell(0, 6, paragraph)
        pdf.ln(2)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts}.pdf"
    filepath = FILES_DIR / filename
    pdf.output(str(filepath))

    logger.info("PDF создан: %s", filepath)
    return filename


# -------------------------------------------------
# Обработчики сценариев
# -------------------------------------------------


async def handle_contract(payload: Dict[str, Any], with_file: bool) -> Tuple[str, Optional[str]]:
    t = payload.get("Тип_договора") or payload.get("Тип договора") or ""
    s = payload.get("Стороны", "")
    p = payload.get("Предмет", "")
    sr = payload.get("Сроки", "")
    op = payload.get("Оплата", "")
    spec = payload.get("Особые_условия") or payload.get("Особые условия") or ""

    user_desc = (
        f"Тип договора: {t}\n"
        f"Стороны: {s}\n"
        f"Предмет договора: {p}\n"
        f"Сроки: {sr}\n"
        f"Порядок оплаты: {op}\n"
        f"Особые условия и риски: {spec}"
    )

    try:
        text = await call_groq(PROMPT_CONTRACT, user_desc)
    except Exception as e:
        logger.exception("Groq error in contract: %s", e)
        return FALLBACK_TEXT, None

    if with_file:
        filename = create_pdf_from_text("ДОГОВОР (ЧЕРНОВИК)", text, prefix="contract")
        file_url = f"{BASE_URL}/files/{filename}"
        reply = (
            "Черновик договора подготовлен. Файл можно скачать по ссылке ниже.\n\n"
            f"{file_url}\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
        )
        return reply, file_url

    # Бесплатная версия — только текст + пич подписки, без ссылки
    reply = (
        f"{text}\n\n"
        "Важно: это примерный черновик, сформированный ИИ. "
        "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом.\n\n"
        "Сейчас я показал тебе черновик в виде текста. "
        "PDF-документ, оформленный и готовый к печати/отправке, доступен только по подписке."
    )
    return reply, None


async def handle_claim(payload: Dict[str, Any], with_file: bool) -> Tuple[str, Optional[str]]:
    addr = payload.get("Адресат", "")
    base = payload.get("Основание", "")
    viol = payload.get("Нарушение_и_обстоятельства") or payload.get("Нарушение и обстоятельства") or ""
    reqs = payload.get("Требования", "")
    term = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
    contacts = payload.get("Контакты", "")

    user_desc = (
        f"Адресат: {addr}\n"
        f"Основание: {base}\n"
        f"Нарушение и обстоятельства: {viol}\n"
        f"Требования заявителя: {reqs}\n"
        f"Срок добровольного исполнения: {term}\n"
        f"Контактные данные заявителя: {contacts}"
    )

    try:
        text = await call_groq(PROMPT_CLAIM, user_desc)
    except Exception as e:
        logger.exception("Groq error in claim: %s", e)
        return FALLBACK_TEXT, None

    if with_file:
        filename = create_pdf_from_text("ПРЕТЕНЗИЯ (ЧЕРНОВИК)", text, prefix="claim")
        file_url = f"{BASE_URL}/files/{filename}"
        reply = (
            "Черновик претензии подготовлен. Файл можно скачать по ссылке ниже.\n\n"
            f"{file_url}\n\n"
            "Важно: это примерный черновик претензии, сформированный ИИ. "
            "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
        )
        return reply, file_url

    reply = (
        f"{text}\n\n"
        "Важно: это примерный черновик претензии, сформированный ИИ. "
        "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом.\n\n"
        "Сейчас я показал тебе черновик в виде текста. "
        "PDF-версия, оформленная и готовая к печати/отправке, доступна только по подписке."
    )
    return reply, None


async def handle_clause(payload: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    clause_text = (
        payload.get("Текст") or payload.get("Пункт") or payload.get("Clause") or ""
    )
    if not clause_text.strip():
        return "Пока нет данных. Напиши текст/описание, с которым нужно помочь.", None

    try:
        answer = await call_groq(PROMPT_CLAUSE, clause_text)
    except Exception as e:
        logger.exception("Groq error in clause: %s", e)
        return FALLBACK_TEXT, None

    return answer, None


# -------------------------------------------------
# FastAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам и пользователям",
    version="0.9.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    scenario = str(payload.get("scenario", "contract")).strip().lower()
    with_file = parse_with_file_flag(payload)

    try:
        if scenario == "contract":
            reply_text, file_url = await handle_contract(payload, with_file)
            scenario_name = "contract"
        elif scenario == "claim":
            reply_text, file_url = await handle_claim(payload, with_file)
            scenario_name = "claim"
        elif scenario == "clause":
            reply_text, file_url = await handle_clause(payload)
            scenario_name = "clause"
        else:
            logger.info("Неизвестный сценарий: %s", scenario)
            reply_text = "Пока нет данных. Напиши текст или выбери нужный раздел в меню бота."
            file_url = None
            scenario_name = scenario or "unknown"

    except Exception as e:
        logger.exception("Unexpected error in /legalfox: %s", e)
        reply_text = FALLBACK_TEXT
        file_url = None
        scenario_name = scenario or "error"

    response: Dict[str, Any] = {
        "reply_text": reply_text,
        "scenario": scenario_name,
    }
    # file_url добавляем ТОЛЬКО когда реально есть файл
    if file_url:
        response["file_url"] = file_url

    return response


@app.get("/files/{filename}")
async def download_file(filename: str):
    filepath = FILES_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path=str(filepath),
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
