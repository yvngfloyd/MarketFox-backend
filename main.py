import os
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.staticfiles import StaticFiles

from groq import Groq

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


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

# База для формирования ссылок на файлы
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if not BASE_URL:
    # запасной вариант – домен Railway
    BASE_URL = "https://legalfox.up.railway.app"

# Каталог для PDF-файлов
BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)


# -------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ GROQ
# -------------------------------------------------
client: Optional[Groq] = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — нейросеть недоступна, будет fallback-текст")


# -------------------------------------------------
# PDF: ШРИФТЫ
# -------------------------------------------------
try:
    # ожидаем, что файл лежит в fonts/DejaVuSans.ttf
    pdfmetrics.registerFont(TTFont("DejaVuSans", str(BASE_DIR / "fonts" / "DejaVuSans.ttf")))
    DEFAULT_FONT = "DejaVuSans"
    logger.info("Шрифт DejaVuSans подключён")
except Exception:
    DEFAULT_FONT = "Helvetica"
    logger.warning("Не удалось подключить DejaVuSans, используется Helvetica")


# -------------------------------------------------
# УТИЛИТЫ
# -------------------------------------------------
def bool_from_payload(value: Any) -> bool:
    """Преобразуем поле Premium (1/0, true/false, да/нет) в bool."""
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "да", "y", "on", "+"}


def clean_markdown(text: str) -> str:
    """Убираем **жирный**, маркированные списки и лишний Markdown."""
    if not text:
        return ""
    # **bold**
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    # __bold__
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.DOTALL)
    # маркеры списков в начале строк "-", "*", "•"
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    return text.strip()


async def call_groq(system_prompt: str, user_prompt: str) -> str:
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    chat_completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.5,
        max_tokens=900,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


def create_pdf(filename: str, title: str, body_text: str) -> str:
    """Создаёт PDF и возвращает абсолютный URL для скачивания."""
    file_path = FILES_DIR / filename

    doc = SimpleDocTemplate(
        str(file_path),
        pagesize=A4,
        rightMargin=40,
        leftMargin=40,
        topMargin=60,
        bottomMargin=60,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "LegalTitle",
        parent=styles["Heading1"],
        fontName=DEFAULT_FONT,
        fontSize=16,
        leading=20,
        alignment=1,  # по центру
        spaceAfter=20,
    )
    normal_style = ParagraphStyle(
        "LegalNormal",
        parent=styles["Normal"],
        fontName=DEFAULT_FONT,
        fontSize=11,
        leading=14,
        spaceAfter=6,
    )

    story = []
    if title:
        story.append(Paragraph(title, title_style))
        story.append(Spacer(1, 12))

    for line in body_text.split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 8))
        else:
            story.append(Paragraph(line, normal_style))

    doc.build(story)

    return f"{BASE_URL}/files/{filename}"


# -------------------------------------------------
# PROMPTS
# -------------------------------------------------
CONTRACT_SYSTEM_PROMPT = """
Ты LegalFox — ИИ-помощник по подготовке гражданско-правовых договоров в РФ.

Твоя задача — на основе входных полей собрать ПРИМЕРНЫЙ текст договора.
Важно:

1) Пиши по-русски, деловым, но понятным языком.
2) НЕ используй Markdown, звёздочки, списки, разметку — только чистый текст.
3) Структурируй текст блоками с заголовками: 
   - ПРЕАМБУЛА
   - ПРЕДМЕТ ДОГОВОРА
   - ПРАВА И ОБЯЗАННОСТИ СТОРОН
   - СРОК ДЕЙСТВИЯ ДОГОВОРА
   - ПОРЯДОК РАСЧЁТОВ
   - ОТВЕТСТВЕННОСТЬ СТОРОН
   - ПРОЧИЕ УСЛОВИЯ
4) Учитывай, что это черновик: можешь добавлять нейтральные формулировки-заглушки, но не фантазируй конкретные цифры, если их не дали.
5) Не добавляй комментариев "я ИИ" и т.п.
"""

CLAIM_SYSTEM_PROMPT = """
Ты LegalFox — ИИ-помощник по подготовке претензий (досудебных требований) в РФ.

Составь ПРИМЕРНУЮ претензию с опорой на входные поля.

Требования к тексту:

1) Пиши по-русски, официально-деловым стилем, без Markdown и разметки.
2) Сохрани структуру:
   - Адресат
   - Вводная часть (на каком основании обращение)
   - Описание нарушения и обстоятельств
   - Требования заявителя
   - Срок для добровольного исполнения требований
   - Контактные данные заявителя
   - Заключительный блок (о возможном обращении в суд)
3) Не придумывай конкретные статьи и номера документов, если их нет во входных данных.
"""

CLAUSE_SYSTEM_PROMPT = """
Ты LegalFox — ИИ-помощник, который объясняет и улучшает отдельные пункты договоров.

Пользователь присылает текст пункта или фрагмента договора.
Твоя задача:

1) Кратко и понятным языком объяснить, о чём этот пункт.
2) Указать возможные риски для стороны пользователя.
3) Предложить улучшенную, более безопасную формулировку (если это уместно).

Формат ответа:
- сначала 2–4 предложения объяснения сути пункта;
- затем 2–5 предложений о рисках;
- затем вариант улучшенной формулировки.

НЕ используй Markdown, не применяй **звёздочки** и списки, только обычный текст с абзацами.
"""


# -------------------------------------------------
# FASTAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам и не только",
    version="0.3.0",
)

# раздача PDF
app.mount("/files", StaticFiles(directory=str(FILES_DIR)), name="files")


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Главный endpoint, на который смотрит BotHelp.
    Ожидаемые поля:
    - scenario: 'contract' | 'claim' | 'clause'
    - Premium: 1/0 (или true/false и т.п.)
    - поля с данными по договору/претензии/пункту.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (payload.get("scenario") or "").strip().lower() or "contract"
    is_premium = bool_from_payload(payload.get("Premium"))

    logger.info("Scenario=%s Premium=%s", scenario, is_premium)

    # ------------------------ Договор ------------------------
    if scenario == "contract":
        type_raw = payload.get("Тип_договора") or payload.get("Тип договора") or ""
        parties = payload.get("Стороны") or ""
        subject = payload.get("Предмет") or ""
        terms = payload.get("Сроки") or ""
        payment = payload.get("Оплата") or ""
        special = payload.get("Особые_условия") or payload.get("Особые условия") or ""

        user_prompt = (
            f"Тип договора: {type_raw}\n\n"
            f"Стороны: {parties}\n\n"
            f"Предмет договора: {subject}\n\n"
            f"Сроки: {terms}\n\n"
            f"Порядок оплаты: {payment}\n\n"
            f"Особые условия и риски: {special}"
        )

        try:
            text = await call_groq(CONTRACT_SYSTEM_PROMPT, user_prompt)
            text = clean_markdown(text)
        except Exception as e:
            logger.exception("Groq error (contract): %s", e)
            raise HTTPException(
                status_code=503,
                detail="Сейчас я не могу обратиться к нейросети. Попробуй повторить позже."
            )

        if is_premium:
            # генерируем PDF
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"contract_{ts}.pdf"
            file_url = create_pdf(filename, "ДОГОВОР", text)

            reply_text = (
                "Черновик договора подготовлен. Файл можно скачать по ссылке ниже.\n\n"
                "Важно: это примерный черновик, сформированный ИИ. "
                "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
            )

            return {
                "reply_text": reply_text,
                "file_url": file_url,
                "scenario": "contract",
            }
        else:
            reply_text = (
                text
                + "\n\nВажно: это примерный черновик, сформированный ИИ. "
                  "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом.\n\n"
                  "Сейчас я показал тебе черновик в виде текста. "
                  "PDF-документ, оформленный и готовый к печати/отправке, доступен по подписке."
            )
            return {
                "reply_text": reply_text,
                "scenario": "contract",
            }

    # ------------------------ Претензия ------------------------
    if scenario == "claim":
        adresat = payload.get("Адресат") or ""
        base = payload.get("Основание") or ""
        violation = payload.get("Нарушение_и_обстоятельства") or payload.get("Нарушение и обстоятельства") or ""
        demands = payload.get("Требования") or ""
        deadline = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
        contacts = payload.get("Контакты") or ""

        user_prompt = (
            f"Адресат: {adresat}\n\n"
            f"Основание обращения: {base}\n\n"
            f"Суть нарушения и обстоятельства: {violation}\n\n"
            f"Требования заявителя: {demands}\n\n"
            f"Срок добровольного исполнения требований: {deadline}\n\n"
            f"Контактные данные заявителя: {contacts}"
        )

        try:
            text = await call_groq(CLAIM_SYSTEM_PROMPT, user_prompt)
            text = clean_markdown(text)
        except Exception as e:
            logger.exception("Groq error (claim): %s", e)
            raise HTTPException(
                status_code=503,
                detail="Сейчас я не могу обратиться к нейросети. Попробуй повторить позже."
            )

        if is_premium:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"claim_{ts}.pdf"
            file_url = create_pdf(filename, "ПРЕТЕНЗИЯ", text)

            reply_text = (
                "Черновик претензии подготовлен. Файл можно скачать по ссылке ниже.\n\n"
                "Важно: это примерный черновик, сформированный ИИ. "
                "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
            )

            return {
                "reply_text": reply_text,
                "file_url": file_url,
                "scenario": "claim",
            }
        else:
            reply_text = (
                text
                + "\n\nВажно: это примерный черновик, сформированный ИИ. "
                  "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом.\n\n"
                  "Сейчас я показал тебе черновик в виде текста. "
                  "PDF-версия, оформленная и готовая к печати/отправке, доступна только по подписке."
            )
            return {
                "reply_text": reply_text,
                "scenario": "claim",
            }

    # ------------------------ Помощь с пунктами ------------------------
    if scenario == "clause":
        clause_text = (
            payload.get("Текст")
            or payload.get("Текст_пункта")
            or payload.get("Текст пункта")
            or ""
        )

        if not clause_text.strip():
            return {
                "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
                "scenario": "clause",
            }

        try:
            text = await call_groq(CLAUSE_SYSTEM_PROMPT, clause_text)
            text = clean_markdown(text)
        except Exception as e:
            logger.exception("Groq error (clause): %s", e)
            raise HTTPException(
                status_code=503,
                detail="Сейчас я не могу обратиться к нейросети. Попробуй повторить позже."
            )

        return {
            "reply_text": text,
            "scenario": "clause",
        }

    # ------------------------ Неизвестный сценарий ------------------------
    logger.info("Неизвестный сценарий: %s", scenario)
    return {
        "reply_text": "Пока я не понимаю, что именно нужно сделать. Попробуй выбрать одну из кнопок в меню бота.",
        "scenario": "unknown",
    }


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
