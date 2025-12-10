import os
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from textwrap import wrap

# --------------------------------------------------------------------
# Логгер
# --------------------------------------------------------------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# --------------------------------------------------------------------
# Конфиг
# --------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(BASE_DIR, "files")
FONT_PATH = os.path.join(BASE_DIR, "DejaVuSans.ttf")
FONT_NAME = "DejaVuSans"

os.makedirs(FILES_DIR, exist_ok=True)

# Регистрируем шрифт, чтобы PDF нормально отображал кириллицу
try:
    if os.path.exists(FONT_PATH):
        pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
        logger.info("Кириллический шрифт зарегистрирован: %s", FONT_PATH)
    else:
        logger.warning("Файл шрифта не найден: %s. Будет использован стандартный шрифт.", FONT_PATH)
        FONT_NAME = "Helvetica"
except Exception:
    logger.exception("Не удалось зарегистрировать шрифт, использую стандартный.")
    FONT_NAME = "Helvetica"

# --------------------------------------------------------------------
# Groq клиент
# --------------------------------------------------------------------
client: Optional[Groq] = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будет использоваться только fallback-текст.")


async def call_groq(system_prompt: str, user_prompt: str) -> str:
    """
    Вызов Groq. Если что-то идёт не так — бросаем исключение,
    чтобы сверху можно было отдать fallback.
    """
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=900,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# --------------------------------------------------------------------
# PDF утилиты
# --------------------------------------------------------------------
def _draw_paragraph(c: canvas.Canvas, text: str, x: int, y: int, max_width: int, leading: int) -> int:
    """
    Рисуем многострочный параграф с переносами.
    Возвращаем новую координату y (следующая строка сверху вниз).
    """
    lines = []
    for raw_line in text.split("\n"):
        raw_line = raw_line.rstrip()
        if not raw_line:
            lines.append("")  # пустая строка
            continue
        # примитивный wrap по ширине
        wrapped = wrap(raw_line, width=90)
        lines.extend(wrapped)

    for line in lines:
        if not line:
            y -= leading  # пустая строка
            continue
        c.drawString(x, y, line)
        y -= leading

    return y


def _new_pdf_filename(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.pdf"


def generate_contract_pdf(fields: Dict[str, str]) -> str:
    """
    Делает простой черновик договора в PDF и возвращает имя файла.
    """
    filename = _new_pdf_filename("contract")
    filepath = os.path.join(FILES_DIR, filename)

    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    c.setFont(FONT_NAME, 11)

    margin_x = 40
    y = height - 60
    leading = 14

    title = "ДОГОВОР"
    contract_type = fields.get("Тип договора") or fields.get("Тип_договора") or ""
    if contract_type:
        title += f" ({contract_type})"
    c.setFont(FONT_NAME, 14)
    c.drawString(margin_x, y, title)
    y -= 2 * leading
    c.setFont(FONT_NAME, 11)

    parties = fields.get("Стороны", "")
    subject = fields.get("Предмет", "")
    terms_payment = fields.get("Сроки и оплата") or fields.get("Сроки") or ""
    special = fields.get("Особые условия") or fields.get("Особые_условия") or ""

    body_parts = []

    if parties:
        body_parts.append(f"1. Стороны договора:\n{parties}")
    if subject:
        body_parts.append(f"2. Предмет договора:\n{subject}")
    if terms_payment:
        body_parts.append(f"3. Сроки и порядок оплаты:\n{terms_payment}")
    if special:
        body_parts.append(f"4. Особые условия и риски:\n{special}")
    body_parts.append(
        "Настоящий документ является черновиком. Перед подписанием стороны "
        "должны проверить текст и при необходимости доработать его."
    )

    full_text = "\n\n".join(body_parts)
    y = _draw_paragraph(c, full_text, margin_x, y, max_width=int(width - 2 * margin_x), leading=leading)

    c.showPage()
    c.save()
    logger.info("PDF договора создан: %s", filepath)
    return filename


def generate_claim_pdf(fields: Dict[str, str]) -> str:
    """
    Делает черновик претензии в PDF и возвращает имя файла.
    """
    filename = _new_pdf_filename("claim")
    filepath = os.path.join(FILES_DIR, filename)

    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4
    c.setFont(FONT_NAME, 11)

    margin_x = 40
    y = height - 60
    leading = 14

    c.setFont(FONT_NAME, 14)
    c.drawString(margin_x, y, "ПРЕТЕНЗИЯ")
    y -= 2 * leading
    c.setFont(FONT_NAME, 11)

    addressee = fields.get("Адресат", "")
    basis = fields.get("Основание", "")
    violation = (
        fields.get("Нарушение и обстоятельства")
        or fields.get("Нарушение_и_обстоятельства")
        or ""
    )
    demands = fields.get("Требования", "")
    deadline = fields.get("Сроки исполнения") or fields.get("Срок_исполнения") or ""
    contacts = fields.get("Контакты", "")

    body_parts = []

    if addressee:
        body_parts.append(f"Адресат: {addressee}")
    if basis:
        body_parts.append(f"На основании: {basis}")
    if violation:
        body_parts.append(f"Суть нарушения и обстоятельства:\n{violation}")
    if demands:
        body_parts.append(f"Требования заявителя:\n{demands}")
    if deadline:
        body_parts.append(f"Срок для добровольного исполнения требований: {deadline}")
    if contacts:
        body_parts.append(f"Контактные данные заявителя:\n{contacts}")

    body_parts.append(
        "Настоящая претензия составлена в упрощённом порядке с использованием ИИ. "
        "Перед направлением адресату рекомендуется проверить текст и при необходимости "
        "доработать его с юристом."
    )

    full_text = "\n\n".join(body_parts)
    y = _draw_paragraph(c, full_text, margin_x, y, max_width=int(width - 2 * margin_x), leading=leading)

    c.showPage()
    c.save()
    logger.info("PDF претензии создан: %s", filepath)
    return filename


# --------------------------------------------------------------------
# Промпты для Groq
# --------------------------------------------------------------------
PROMPT_CONTRACT = """
Ты — ИИ-помощник юриста. По краткому описанию сторон, предмета, сроков, оплаты и особых условий
составь аккуратный, лаконичный черновик договора на русском языке.

Важно:
- пиши нейтральным деловым стилем, без жаргона и эмоций;
- не указывай конкретные реквизиты, только структуру и формулировки;
- адаптируй формулировки под указанный тип договора;
- не добавляй Markdown-разметку, только обычный текст с абзацами.
"""

PROMPT_CLAIM = """
Ты — ИИ-помощник юриста. По описанию адресата, основания, обстоятельств нарушения и требований
составь аккуратный черновик претензии (досудебной претензии) на русском языке.

Важно:
- деловой, но понятный язык;
- без ссылок на конкретные статьи, если пользователь сам их не указал;
- в конце кратко сформулируй требования и срок исполнения, если он указан;
- не используй Markdown, только обычный текст.
"""

PROMPT_CLAUSE = """
Ты — ИИ-помощник юриста. Тебе присылают один или несколько пунктов договора.

Нужно:
- кратко объяснить, что по сути означает текст (простыми словами);
- указать возможные риски для отправителя текста;
- предложить 1–2 более безопасные альтернативные формулировки.

Пиши по-русски, без Markdown, структурируй ответ короткими абзацами.
"""


async def generate_ai_text_for_scenario(scenario: str, fields: Dict[str, str]) -> str:
    """
    Строим user_prompt из полей и вызываем Groq для нужного сценария.
    Если Groq недоступен или падает — выбрасываем исключение.
    """
    if scenario == "contract":
        user_prompt = (
            f"Тип договора: {fields.get('Тип договора') or fields.get('Тип_договора')}\n"
            f"Стороны: {fields.get('Стороны')}\n"
            f"Предмет: {fields.get('Предмет')}\n"
            f"Сроки и оплата: {fields.get('Сроки и оплата') or fields.get('Сроки')}\n"
            f"Особые условия: {fields.get('Особые условия') or fields.get('Особые_условия')}"
        )
        return await call_groq(PROMPT_CONTRACT, user_prompt)

    if scenario == "claim":
        user_prompt = (
            f"Адресат: {fields.get('Адресат')}\n"
            f"Основание: {fields.get('Основание')}\n"
            f"Нарушение и обстоятельства: "
            f"{fields.get('Нарушение и обстоятельства') or fields.get('Нарушение_и_обстоятельства')}\n"
            f"Требования: {fields.get('Требования')}\n"
            f"Сроки исполнения: {fields.get('Сроки исполнения') or fields.get('Срок_исполнения')}\n"
            f"Контакты: {fields.get('Контакты')}"
        )
        return await call_groq(PROMPT_CLAIM, user_prompt)

    if scenario == "clause":
        clause_text = fields.get("Текст_пункта") or fields.get("Текст пункта") or ""
        return await call_groq(PROMPT_CLAUSE, clause_text)

    raise ValueError(f"Неизвестный сценарий для Groq: {scenario}")


# --------------------------------------------------------------------
# FastAPI
# --------------------------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.9.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Главный эндпойнт для BotHelp.
    Ожидаем минимум поле 'scenario' и набор полей в зависимости от сценария.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (payload.get("scenario") or "").strip() or "contract"

    # Подготовим словарь полей так, как нам удобно
    fields: Dict[str, str] = {}

    if scenario in ("contract", "draft_contract"):
        # поддерживаем оба варианта имён
        fields["Тип договора"] = payload.get("Тип договора") or payload.get("Тип_договора") or ""
        fields["Стороны"] = payload.get("Стороны") or ""
        fields["Предмет"] = payload.get("Предмет") or ""
        fields["Сроки и оплата"] = payload.get("Сроки и оплата") or payload.get("Сроки") or ""
        fields["Особые условия"] = payload.get("Особые условия") or payload.get("Особые_условия") or ""
        scenario = "contract"

    elif scenario == "claim":
        fields["Адресат"] = payload.get("Адресат") or ""
        fields["Основание"] = payload.get("Основание") or ""
        fields["Нарушение и обстоятельства"] = (
            payload.get("Нарушение и обстоятельства") or payload.get("Нарушение_и_обстоятельства") or ""
        )
        fields["Требования"] = payload.get("Требования") or ""
        fields["Сроки исполнения"] = payload.get("Сроки исполнения") or payload.get("Срок_исполнения") or ""
        fields["Контакты"] = payload.get("Контакты") or ""

    elif scenario == "clause":
        # Для третьей кнопки — просто текст пункта
        fields["Текст_пункта"] = payload.get("Текст_пункта") or payload.get("Текст пункта") or ""
    else:
        logger.info("Неизвестный сценарий: %s", scenario)

    logger.info("Scenario=%s, fields=%s", scenario, {k: v for k, v in fields.items() if v})

    reply_text: str
    file_url: Optional[str] = None

    # --- сценарий: договор ----------------------------------------------------
    if scenario == "contract":
        try:
            ai_text = await generate_ai_text_for_scenario("contract", fields)
        except Exception as e:
            logger.exception("Ошибка Groq в сценарии contract: %s", e)
            ai_text = FALLBACK_TEXT

        pdf_name = generate_contract_pdf(fields)
        file_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', 'legalfox.up.railway.app')}/files/{pdf_name}"

        reply_text = (
            f"{ai_text}\n\n"
            f"Черновик договора в формате PDF можно скачать по ссылке:\n{file_url}\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
        )

    # --- сценарий: претензия --------------------------------------------------
    elif scenario == "claim":
        try:
            ai_text = await generate_ai_text_for_scenario("claim", fields)
        except Exception as e:
            logger.exception("Ошибка Groq в сценарии claim: %s", e)
            ai_text = FALLBACK_TEXT

        pdf_name = generate_claim_pdf(fields)
        file_url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', 'legalfox.up.railway.app')}/files/{pdf_name}"

        # ССЫЛКУ ДАЁМ ТОЛЬКО ОДИН РАЗ, чтобы не было дублей
        reply_text = (
            f"{ai_text}\n\n"
            f"Черновик претензии подготовлен. Файл можно скачать по ссылке ниже:\n{file_url}\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
        )

    # --- сценарий: работа с пунктом договора ---------------------------------
    elif scenario == "clause":
        try:
            ai_text = await generate_ai_text_for_scenario("clause", fields)
        except Exception as e:
            logger.exception("Ошибка Groq в сценарии clause: %s", e)
            ai_text = FALLBACK_TEXT

        reply_text = ai_text

    else:
        # Неизвестный сценарий — просто отправляем fallback
        reply_text = FALLBACK_TEXT

    result: Dict[str, Any] = {
        "reply_text": reply_text,
        "scenario": scenario,
    }
    if file_url:
        result["file_url"] = file_url

    return result


@app.get("/files/{filename}")
async def download_file(filename: str):
    """
    Отдаём PDF-файлы по HTTP, чтобы BotHelp/Telegram могли их скачать.
    """
    file_path = os.path.join(FILES_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(file_path, media_type="application/pdf", filename=filename)


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
