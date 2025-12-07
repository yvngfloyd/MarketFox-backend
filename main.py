import os
import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# -------------------------------------------------------------------
# ЛОГГЕР
# -------------------------------------------------------------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# -------------------------------------------------------------------
# КОНФИГ
# -------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

# публичный базовый URL твоего Railway-проекта
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://legalfox.up.railway.app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

# -------------------------------------------------------------------
# ШРИФТ ДЛЯ PDF (КИРИЛЛИЦА)
# -------------------------------------------------------------------
# если DejaVuSans.ttf лежит рядом с main.py:
FONT_PATH = os.path.join(BASE_DIR, "DejaVuSans.ttf")
# если переложишь в папку fonts/DejaVuSans.ttf — раскомментируй:
# FONT_PATH = os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf")

try:
    pdfmetrics.registerFont(TTFont("DejaVuSans", FONT_PATH))
    logger.info("Шрифт DejaVuSans зарегистрирован: %s", FONT_PATH)
except Exception as e:
    logger.exception("Не удалось зарегистрировать шрифт DejaVuSans: %s", e)

# -------------------------------------------------------------------
# ПРОМПТЫ
# -------------------------------------------------------------------

PROMPT_DRAFT_CONTRACT = """
Ты — ИИ-юрист LegalFox.

На основе вводных данных от пользователя подготовь черновик договора в текстовом виде.
Страна права по умолчанию — Россия, если явно не указано иное.

Формат ответа:
- аккуратный структурированный текст договора,
- без пояснений, без обращений к пользователю,
- без Markdown-разметки (**звёздочки** и т.п. не использовать),
- формулировки юридически аккуратные, но человеческим языком.

Если информации мало — сделай разумные нейтральные формулировки без конкретных цифр.
"""

PROMPT_CLAIM = """
Ты — ИИ-юрист LegalFox.

На основе вводных данных пользователя составь черновик претензии (досудебной).
Страна права по умолчанию — Россия, если явно не указано иное.

Формат:
1) "Шапка" претензии (кому, от кого, контакты) — одним блоком.
2) Описание ситуации и указание на нарушения (ссылки на нормы права, если это очевидно).
3) Чётко сформулированные требования.
4) Срок исполнения требований.
5) Указание на возможные последствия при неисполнении (суд, взыскание неустойки и т.п.).

Без пояснений, без Markdown-разметки, просто готовый текст претензии.
"""

PROMPT_CLAUSE = """
Ты — ИИ-юрист LegalFox.

Пользователь присылает пункт договора или кусок текста, который нужно:
- проанализировать на риски,
- предложить более безопасную/понятную формулировку.

Формат ответа:
1) Коротко: в чём суть риска/проблемы (если она есть).
2) Предложи 1–2 варианта формулировки, которые будут безопаснее/яснее.
3) Если всё ок и рисков нет — так и напиши.

Не используй Markdown-разметку, списки делай обычными строками с цифрами 1), 2), 3).
"""

PROMPT_CONTRACT_PDF = """
Ты — ИИ-юрист LegalFox.

На основе кратких вводных данных подготовь полный текст договора.
Страна права — Российская Федерация (ГК РФ), если явно не указано иное.

Требования к ответу:
- выведи ЧИСТЫЙ текст договора без пояснений и комментариев;
- не используй Markdown и спецразметку;
- структура договора: преамбула, предмет, права и обязанности сторон, порядок расчётов,
  ответственность, срок действия, порядок расторжения, прочие условия, реквизиты сторон;
- формулировки должны выглядеть как реальный договор, который можно распечатать и подписать.
"""

# -------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ GROQ
# -------------------------------------------------------------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будут срабатывать фолбэки")

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

# -------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# -------------------------------------------------------------------

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
        temperature=0.4,
        max_tokens=2000,
        top_p=1,
    )
    content = chat_completion.choices[0].message.content or ""
    return content.strip()


def create_contract_pdf(contract_text: str) -> str:
    """
    Генерирует PDF с текстом договора и сохраняет в папку files.
    Возвращает только имя файла (не полный путь).
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"contract_{ts}.pdf"
    filepath = os.path.join(FILES_DIR, filename)

    doc = SimpleDocTemplate(filepath, pagesize=A4,
                            leftMargin=40, rightMargin=40,
                            topMargin=40, bottomMargin=40)

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontName = "DejaVuSans"
    normal.fontSize = 11
    normal.leading = 14

    story = []

    # Разбиваем текст на блоки по пустой строке
    blocks = [b.strip() for b in contract_text.split("\n\n") if b.strip()]
    if not blocks:
        blocks = [contract_text.strip()]

    for block in blocks:
        # Переводим одиночные переводы строки в <br/>,
        # чтобы ReportLab корректно делал переносы.
        block_html = block.replace("\n", "<br/>")
        story.append(Paragraph(block_html, normal))
        story.append(Spacer(1, 8))

    doc.build(story)

    logger.info("PDF создан: %s", filepath)
    return filename


async def handle_draft_contract(payload: Dict[str, Any]) -> Dict[str, str]:
    t = payload.get("Тип договора") or payload.get("Тип_договора") or ""
    parties = payload.get("Стороны", "")
    subject = payload.get("Предмет", "")
    terms = payload.get("Сроки", "") or payload.get("Сроки и оплата", "")
    payment = payload.get("Оплата", "")
    special = payload.get("Особые условия") or payload.get("Особые_условия") or ""

    user_brief = (
        f"Тип договора: {t}\n"
        f"Стороны: {parties}\n"
        f"Предмет: {subject}\n"
        f"Сроки: {terms}\n"
        f"Оплата: {payment}\n"
        f"Особые условия: {special}"
    )

    try:
        text = await call_groq(PROMPT_DRAFT_CONTRACT, user_brief)
        return {"reply_text": text, "scenario": "draft_contract"}
    except Exception as e:
        logger.exception("Ошибка Groq в draft_contract: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "draft_contract"}


async def handle_claim(payload: Dict[str, Any]) -> Dict[str, str]:
    adresat = payload.get("Адресат", "")
    basis = payload.get("Основание", "")
    facts = payload.get("Нарушение и обстоятельства") or payload.get("Нарушение_и_обстоятельства", "")  # noqa: E501
    demands = payload.get("Требования", "")
    deadline = payload.get("Сроки исполнения") or payload.get("Срок_исполнения", "")
    contacts = payload.get("Контакты", "")

    user_brief = (
        f"Адресат: {adresat}\n"
        f"Основание: {basis}\n"
        f"Нарушение и обстоятельства: {facts}\n"
        f"Требования: {demands}\n"
        f"Срок исполнения: {deadline}\n"
        f"Контакты отправителя: {contacts}"
    )

    try:
        text = await call_groq(PROMPT_CLAIM, user_brief)
        return {"reply_text": text, "scenario": "claim"}
    except Exception as e:
        logger.exception("Ошибка Groq в claim: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "claim"}


async def handle_clause(payload: Dict[str, Any]) -> Dict[str, str]:
    clause_text = (
        payload.get("Текст") or
        payload.get("Пункт") or
        payload.get("Clause") or
        payload.get("Описание") or
        ""
    )

    if not clause_text.strip():
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "scenario": "clause",
        }

    try:
        text = await call_groq(PROMPT_CLAUSE, clause_text)
        return {"reply_text": text, "scenario": "clause"}
    except Exception as e:
        logger.exception("Ошибка Groq в clause: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "clause"}


async def handle_contract_pdf(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Сценарий: 'contract' — сделать PDF-черновик договора.
    """
    t = payload.get("Тип договора") or payload.get("Тип_договора") or ""
    parties = payload.get("Стороны", "")
    subject = payload.get("Предмет", "")
    terms = payload.get("Сроки и оплата") or payload.get("Сроки", "")
    payment = payload.get("Оплата", "")
    special = payload.get("Особые условия") or payload.get("Особые_условия") or ""

    user_brief = (
        f"Тип договора: {t}\n"
        f"Стороны: {parties}\n"
        f"Предмет: {subject}\n"
        f"Сроки и оплата: {terms}\n"
        f"Оплата: {payment}\n"
        f"Особые условия: {special}"
    )

    try:
        contract_text = await call_groq(PROMPT_CONTRACT_PDF, user_brief)
        filename = create_contract_pdf(contract_text)
        file_url = f"{PUBLIC_BASE_URL}/files/{filename}"
        return {
            "reply_text": "Готово! Я собрал черновик, лови файл:",
            "file_url": file_url,
            "scenario": "contract",
        }
    except Exception as e:
        logger.exception("Ошибка при генерации PDF договора: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "contract"}

# -------------------------------------------------------------------
# FASTAPI
# -------------------------------------------------------------------

app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.3.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or ""
    )

    if scenario == "draft_contract":
        result = await handle_draft_contract(payload)
    elif scenario == "claim":
        result = await handle_claim(payload)
    elif scenario == "clause":
        result = await handle_clause(payload)
    elif scenario == "contract":
        result = await handle_contract_pdf(payload)
    else:
        logger.info("Неизвестный сценарий: %s", scenario)
        result = {
            "reply_text": FALLBACK_TEXT,
            "scenario": scenario or "unknown",
        }

    logger.info("Scenario=%s ответ готов", result.get("scenario"))
    return result


@app.get("/files/{filename}")
async def download_file(filename: str):
    """
    Отдаём PDF по HTTP, чтобы BotHelp мог взять ссылку.
    """
    filepath = os.path.join(FILES_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        filepath,
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
