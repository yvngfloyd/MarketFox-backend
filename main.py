import os
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

# -------------------------------------------------
# ЛОГИРОВАНИЕ
# -------------------------------------------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# -------------------------------------------------
# КОНФИГ
# -------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

FILES_DIR = "files"
os.makedirs(FILES_DIR, exist_ok=True)

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

# -------------------------------------------------
# PROMPTS
# -------------------------------------------------
PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-помощник, который помогает составлять черновики гражданско-правовых договоров на русском языке.

Тебе передают:
- тип договора;
- стороны;
- предмет;
- сроки и оплату;
- особые условия (если есть).

Твоя задача:
1) Из этих данных составить аккуратный, читаемый ТЕКСТ договора (черновик), без оформления в виде таблиц.
2) Писать нейтральным юридическим стилем, но понятным обычному человеку.
3) Не добавлять вымышленных данных — используй только то, что передано пользователем, а недостающие вещи формулируй максимально общо.

Важно:
- НЕ используй Markdown, только обычный текст.
- Не вставляй никаких ссылок.
- Не пиши про PDF и файлы — этим занимается система отдельно.
- В конце текста добавь короткое примечание: 
  "Важно: это примерный черновик договора, сформированный ИИ. Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-помощник, который помогает составлять черновики претензий (досудебных требований) на русском языке.

Тебе передают:
- адресата (кому пишем претензию);
- основание (на какой норме/законе опираемся, если указано);
- суть нарушения и обстоятельства;
- требования заявителя;
- срок добровольного исполнения;
- контакты заявителя.

Твоя задача:
1) Составить аккуратный текст ПРЕТЕНЗИИ в официально-деловом стиле, но без лишней воды.
2) Структура: "Шапка", описание ситуации, ссылка на нормы (если переданы), требования, срок, финальная часть с подписью.
3) Не придумывай конкретные суммы, даты и законы, если их нет во входных данных.

Важно:
- НЕ используй Markdown, только обычный текст.
- Не вставляй никаких ссылок.
- Не пиши про PDF и файлы — этим занимается система отдельно.
- В конце добавь примечание: 
  "Настоящая претензия составлена в упрощённом порядке с использованием ИИ. Перед направлением адресату рекомендуется проверить текст и при необходимости доработать его совместно с юристом."
"""

PROMPT_POINT_HELPER = """
Ты — LegalFox, ИИ-помощник, который помогает разбирать и улучшать отдельные пункты договоров или фрагменты юридических документов.

Пользователь присылает:
- один или несколько пунктов договора;
- либо короткий фрагмент, который вызывает сомнения.

Твоя задача:
1) Пояснить простым языком, что означает этот пункт.
2) Указать возможные риски для стороны пользователя.
3) При необходимости предложить 1–2 формулировки, как можно смягчить риски или сделать условие более сбалансированным.

Важно:
- Отвечай по-русски.
- НЕ используй Markdown, пиши обычным текстом с абзацами.
- Не давай категоричных рекомендаций "ни в коем случае не подписывать" — формулируй аккуратно: "может повлечь такие риски", "имеет смысл обсудить изменение формулировки" и т.п.
"""

# -------------------------------------------------
# Grog client
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
        max_tokens=1200,
        top_p=1,
    )
    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# -------------------------------------------------
# PDF HELPERS
# -------------------------------------------------
def _draw_multiline_text(c: canvas.Canvas, text: str, x: float, y: float, max_width: float) -> float:
    """
    Простая разбивка текста по словам с переносом строк.
    Возвращает текущую координату Y после вывода текста.
    """
    from reportlab.pdfbase.pdfmetrics import stringWidth

    lines = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            lines.append("")
            continue

        words = paragraph.split()
        current = []
        current_width = 0.0

        for w in words:
            w_width = stringWidth((w + " "), "DejaVuSans", 11)
            if current and current_width + w_width > max_width:
                lines.append(" ".join(current))
                current = [w]
                current_width = w_width
            else:
                current.append(w)
                current_width += w_width
        if current:
            lines.append(" ".join(current))

    for line in lines:
        c.drawString(x, y, line)
        y -= 14  # шаг по строкам
    return y


def generate_contract_pdf(text: str, filename: str) -> str:
    filepath = os.path.join(FILES_DIR, filename)
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    left = 25 * mm
    right_margin = 25 * mm
    top = height - 25 * mm

    c.setFont("DejaVuSans", 14)
    c.drawString(left, top, "ДОГОВОР (черновик)")
    y = top - 20

    c.setFont("DejaVuSans", 11)
    y -= 10
    y = _draw_multiline_text(c, text, left, y, width - left - right_margin)

    c.showPage()
    c.save()
    return filepath


def generate_claim_pdf(text: str, filename: str) -> str:
    filepath = os.path.join(FILES_DIR, filename)
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    left = 25 * mm
    right_margin = 25 * mm
    top = height - 25 * mm

    c.setFont("DejaVuSans", 14)
    c.drawString(left, top, "ПРЕТЕНЗИЯ (черновик)")
    y = top - 20

    c.setFont("DejaVuSans", 11)
    y -= 10
    y = _draw_multiline_text(c, text, left, y, width - left - right_margin)

    c.showPage()
    c.save()
    return filepath


# -------------------------------------------------
# REPLY BUILDERS (важная правка: БЕЗ URL в тексте)
# -------------------------------------------------
async def generate_contract_reply(payload: Dict[str, Any]) -> Dict[str, str]:
    """Сценарий draft_contract / contract"""
    contract_type = payload.get("Тип_договора") or payload.get("Тип договора") or ""
    parties = payload.get("Стороны", "")
    subject = payload.get("Предмет", "")
    terms = payload.get("Сроки", "") or payload.get("Сроки и оплата", "")
    payment = payload.get("Оплата", "")
    special = payload.get("Особые_условия") or payload.get("Особые условия") or ""

    user_query = (
        f"Тип договора: {contract_type}\n"
        f"Стороны: {parties}\n"
        f"Предмет: {subject}\n"
        f"Сроки: {terms}\n"
        f"Оплата: {payment}\n"
        f"Особые условия: {special}"
    )

    try:
        main_text = await call_groq(PROMPT_CONTRACT, user_query)
    except Exception as e:
        logger.exception("Groq error in contract scenario: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "contract"}

    # Формируем PDF
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"contract_{ts}.pdf"
    filepath = generate_contract_pdf(main_text, filename)
    file_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()}/files/{filename}"

    # ВАЖНО: здесь БОЛЬШЕ НЕТ самой ссылки, только текст
    reply_text = (
        "Черновик договора подготовлен.\n\n"
        "Важно: это примерный черновик, сформированный ИИ. "
        "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
    )

    logger.info("Scenario=contract, pdf=%s", filepath)
    return {"reply_text": reply_text, "file_url": file_url, "scenario": "contract"}


async def generate_claim_reply(payload: Dict[str, Any]) -> Dict[str, str]:
    """Сценарий claim"""
    addressee = payload.get("Адресат", "")
    basis = payload.get("Основание", "")
    facts = payload.get("Нарушение_и_обстоятельства") or payload.get("Нарушение и обстоятельства") or ""
    demands = payload.get("Требования", "")
    deadline = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
    contacts = payload.get("Контакты", "")

    user_query = (
        f"Адресат: {addressee}\n"
        f"Основание: {basis}\n"
        f"Нарушение и обстоятельства: {facts}\n"
        f"Требования заявителя: {demands}\n"
        f"Срок для добровольного исполнения: {deadline}\n"
        f"Контакты заявителя: {contacts}"
    )

    try:
        main_text = await call_groq(PROMPT_CLAIM, user_query)
    except Exception as e:
        logger.exception("Groq error in claim scenario: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "claim"}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"claim_{ts}.pdf"
    filepath = generate_claim_pdf(main_text, filename)
    file_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip()}/files/{filename}"

    reply_text = (
        "Черновик претензии подготовлен.\n\n"
        "Важно: это примерный черновик, сформированный ИИ. "
        "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
    )

    logger.info("Scenario=claim, pdf=%s", filepath)
    return {"reply_text": reply_text, "file_url": file_url, "scenario": "claim"}


async def generate_point_helper_reply(payload: Dict[str, Any]) -> Dict[str, str]:
    """Сценарий point_helper — работа с отдельным пунктом/фрагментом"""
    text = (
        payload.get("Текст") or
        payload.get("Пункт") or
        payload.get("Описание") or
        ""
    ).strip()

    if not text:
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "scenario": "point_helper",
        }

    try:
        answer = await call_groq(PROMPT_POINT_HELPER, text)
        return {"reply_text": answer, "scenario": "point_helper"}
    except Exception as e:
        logger.exception("Groq error in point_helper scenario: %s", e)
        return {"reply_text": FALLBACK_TEXT, "scenario": "point_helper"}


# -------------------------------------------------
# FASTAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам и неюристам",
    version="1.0.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Главная точка, куда шлёт запрос BotHelp.

    Ожидается поле scenario:
    - "contract" или "draft_contract" — черновик договора + PDF
    - "claim" — черновик претензии + PDF
    - "point_helper" — помощь с отдельным пунктом (без PDF)
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    scenario = (payload.get("scenario") or payload.get("Сценарий") or "").strip() or "contract"

    if scenario in ("contract", "draft_contract"):
        result = await generate_contract_reply(payload)
    elif scenario == "claim":
        result = await generate_claim_reply(payload)
    elif scenario == "point_helper":
        result = await generate_point_helper_reply(payload)
    else:
        logger.info("Неизвестный сценарий: %s", scenario)
        result = {"reply_text": FALLBACK_TEXT, "scenario": scenario}

    logger.info("Scenario=%s ответ готов", result.get("scenario"))
    return result


@app.get("/files/{filename}")
async def download_file(filename: str):
    """
    Отдаём PDF-файл по HTTP, чтобы BotHelp мог взять ссылку.
    """
    filepath = os.path.join(FILES_DIR, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(filepath, media_type="application/pdf", filename=filename)


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
