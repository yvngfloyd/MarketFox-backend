import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = "llama-3.1-8b-instant"

# Базовый URL бекенда (нужно указать домен Railway)
BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app").rstrip("/")

# Папка для файлов
BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)

# Шрифт (поддержка кириллицы)
FONT_PATH = BASE_DIR / "fonts" / "DejaVuSans.ttf"
FONT_NAME = "DejaVuSans"

if FONT_PATH.exists():
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
        logger.info("Шрифт %s зарегистрирован", FONT_NAME)
    except Exception:
        logger.exception("Не удалось зарегистрировать шрифт, используем стандартный")
        FONT_NAME = "Helvetica"
else:
    logger.warning("Файл шрифта %s не найден, используем стандартный", FONT_PATH)
    FONT_NAME = "Helvetica"

# Текст по умолчанию при сбое ИИ
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

# -------------------------------------------------
# Клиент Groq
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


# -------------------------------------------------
# Вспомогательные функции
# -------------------------------------------------
def get_field(payload: Dict[str, Any], *keys: str, default: str = "") -> str:
    """
    Забирает значение по одному из возможных ключей (с пробелами/подчёркиваниями).
    """
    for k in keys:
        if k in payload and payload[k]:
            return str(payload[k])
    return default


async def call_groq(system_prompt: str, user_prompt: str) -> str:
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
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


def make_pdf_from_text(filename: str, title: str, body: str) -> Path:
    """
    Простейший генератор PDF: кладём текст построчно на лист A4.
    """
    pdf_path = FILES_DIR / filename

    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    width, height = A4

    c.setTitle(title)
    c.setFont(FONT_NAME, 12)

    # Отступы
    left_margin = 40
    top_margin = height - 50
    line_height = 16

    y = top_margin

    for line in body.splitlines():
        line = line.rstrip()
        if not line:
            y -= line_height
            continue

        if y < 50:  # новая страница
            c.showPage()
            c.setFont(FONT_NAME, 12)
            y = top_margin

        c.drawString(left_margin, y, line)
        y -= line_height

    c.showPage()
    c.save()

    logger.info("PDF создан: %s", pdf_path)
    return pdf_path


def build_file_url(filename: str) -> str:
    return f"{BASE_URL}/files/{filename}"


# -------------------------------------------------
# Промпты
# -------------------------------------------------
PROMPT_CONTRACT = """
Ты — ИИ-помощник по подготовке гражданско-правовых договоров в РФ.

На основе ввода пользователя составь понятный, структурированный черновик договора.
Учитывай ГК РФ, но не пытайся давать юридическую экспертизу — это именно черновик,
который человек затем доработает с юристом.

Требования к формату:
- пиши по-русски;
- оформляй текст в виде обычного документа без Markdown-разметки;
- используй нумерованные разделы: предмет договора, права и обязанности сторон,
  порядок расчетов, ответственность, порядок расторжения, прочие условия;
- в конце оставь место для реквизитов сторон и подписей.

Если исходных данных мало или они противоречивы — сделай максимально нейтральный
черновик и мягко укажи, какие моменты нужно уточнить с юристом.
"""

PROMPT_CLAIM = """
Ты — ИИ-помощник, который помогает составлять черновики претензий (досудебных требований)
в рамках российского права.

На основе ввода пользователя подготовь аккуратный текст претензии:
- шапка (адресат, данные заявителя можно описать в общем виде);
- краткое описание договора/ситуации;
- суть нарушения и обстоятельства;
- конкретные требования заявителя;
- срок для добровольного исполнения;
- предупреждение о возможном обращении в суд при неисполнении.

Требования к формату:
- русский язык;
- обычный текст без Markdown;
- стиль деловой, но понятный не юристу;
- в конце оставь место для подписи и даты.

Помни: это примерный черновик, а не юридическое заключение.
"""

PROMPT_CLAUSE = """
Ты — ИИ-помощник, который помогает людям разбираться в отдельных пунктах договоров.

Пользователь присылает один или несколько пунктов договора (или их фрагмент).
Твоя задача:
- простыми словами объяснить, что означает этот текст;
- указать, на что стоит обратить внимание (риски, односторонние права, штрафы и т.п.);
- при необходимости предложить 1–2 мягких варианта формулировок, как можно изменить пункт,
  чтобы он был более сбалансирован для обеих сторон.

Требования:
- пиши по-русски;
- без ссылок на конкретные статьи законов, если пользователь прямо не просит об этом;
- делай ответ структурированным, с абзацами, но без Markdown.
"""


# -------------------------------------------------
# FastAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам и клиентам",
    version="0.9.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Главная точка, куда шлёт запрос BotHelp.
    Ожидаем:
    - scenario: 'contract' | 'claim' | 'clause'
    - поля с ответами пользователя
    - Подписка / subscription: 'premium' (или другое значение)
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "contract"
    )

    subscription = payload.get("subscription") or payload.get("Подписка") or ""
    is_premium = str(subscription).lower() in {"premium", "премиум", "1", "true", "yes"}

    logger.info("Scenario=%s, is_premium=%s", scenario, is_premium)

    try:
        if scenario == "contract":
            return await handle_contract(payload, is_premium)
        elif scenario == "claim":
            return await handle_claim(payload, is_premium)
        elif scenario == "clause":
            return await handle_clause(payload)
        else:
            logger.info("Неизвестный сценарий: %s", scenario)
            return {
                "reply_text": FALLBACK_TEXT,
                "file_url": "",
                "scenario": scenario,
            }
    except Exception as e:
        logger.exception("Ошибка при обработке сценария %s: %s", scenario, e)
        return {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": scenario,
        }


# -------------------------------------------------
# Обработчики сценариев
# -------------------------------------------------
async def handle_contract(payload: Dict[str, Any], is_premium: bool) -> Dict[str, Any]:
    tipo = get_field(payload, "Тип_договора", "Тип договора")
    parties = get_field(payload, "Стороны")
    subject = get_field(payload, "Предмет")
    terms = get_field(payload, "Сроки", "Сроки и оплата")
    payment = get_field(payload, "Оплата")
    special = get_field(payload, "Особые_условия", "Особые условия")

    user_prompt = (
        f"Тип договора: {tipo}\n"
        f"Стороны: {parties}\n"
        f"Предмет договора: {subject}\n"
        f"Сроки: {terms}\n"
        f"Оплата: {payment}\n"
        f"Особые условия: {special}\n\n"
        "На основе этих данных подготовь черновик договора."
    )

    try:
        draft_text = await call_groq(PROMPT_CONTRACT, user_prompt)
    except Exception as e:
        logger.exception("Groq error (contract): %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": "contract",
        }

    file_url = ""
    if is_premium:
        # Создаём PDF только для платных
        filename = f"contract_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        make_pdf_from_text(filename, "Черновик договора", draft_text)
        file_url = build_file_url(filename)

    # Текстовое объяснение (одинаковое для всех)
    reply_text_base = (
        "Черновик договора подготовлен.\n\n"
        "Важно: это примерный черновик, сформированный ИИ. "
        "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом.\n"
    )

    if is_premium:
        reply_text_base += (
            "\nPDF-версию, оформленную и готовую к печати/отправке, я прикрепил отдельным файлом."
        )
    else:
        reply_text_base += (
            "\nСейчас я покажу черновик в виде текста. "
            "PDF-документ, оформленный и готовый к печати/отправке, доступен только по подписке."
        )

    # КЛЮЧЕВАЯ ЛОГИКА:
    # - если есть file_url (премиум) — в reply_text НЕ добавляем сам текст договора
    # - если файла нет (free) — приклеиваем текст черновика к ответу
    if file_url:
        final_reply = reply_text_base
    else:
        final_reply = f"{reply_text_base}\n\n{draft_text}"

    return {
        "reply_text": final_reply,
        "draft_text": draft_text,
        "file_url": file_url,
        "scenario": "contract",
    }


async def handle_claim(payload: Dict[str, Any], is_premium: bool) -> Dict[str, Any]:
    addressee = get_field(payload, "Адресат")
    basis = get_field(payload, "Основание")
    facts = get_field(payload, "Нарушение_и_обстоятельства", "Нарушение и обстоятельства")
    demands = get_field(payload, "Требования")
    deadline = get_field(payload, "Срок_исполнения", "Сроки исполнения")
    contacts = get_field(payload, "Контакты")

    user_prompt = (
        f"Адресат: {addressee}\n"
        f"Основание (договор/закон/ситуация): {basis}\n"
        f"Нарушение и обстоятельства: {facts}\n"
        f"Требования заявителя: {demands}\n"
        f"Срок для добровольного исполнения: {deadline}\n"
        f"Контактные данные заявителя: {contacts}\n\n"
        "На основе этих данных подготовь черновик претензии."
    )

    try:
        draft_text = await call_groq(PROMPT_CLAIM, user_prompt)
    except Exception as e:
        logger.exception("Groq error (claim): %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": "claim",
        }

    file_url = ""
    if is_premium:
        filename = f"claim_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        make_pdf_from_text(filename, "Черновик претензии", draft_text)
        file_url = build_file_url(filename)

    reply_text_base = (
        "Черновик претензии подготовлен.\n\n"
        "Важно: это примерный черновик, сформированный ИИ. Перед отправкой обязательно "
        "проверь текст и, по возможности, согласуй его с юристом."
    )

    if is_premium:
        reply_text_base += (
            "\n\nPDF-версия претензии, оформленная и готовая к печати/отправке, "
            "прикреплена отдельным файлом."
        )
    else:
        reply_text_base += (
            "\n\nСейчас я покажу черновик в виде текста. "
            "PDF-документ доступен только по подписке."
        )

    if file_url:
        final_reply = reply_text_base
    else:
        final_reply = f"{reply_text_base}\n\n{draft_text}"

    return {
        "reply_text": final_reply,
        "draft_text": draft_text,
        "file_url": file_url,
        "scenario": "claim",
    }


async def handle_clause(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = get_field(payload, "Текст_пункта", "Текст пункта", "Описание", default="").strip()
    if not text:
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "file_url": "",
            "scenario": "clause",
        }

    user_prompt = (
        "Вот фрагмент договора, который нужно объяснить простым языком и при необходимости "
        "предложить более безопасную формулировку:\n\n"
        f"{text}"
    )

    try:
        answer = await call_groq(PROMPT_CLAUSE, user_prompt)
    except Exception as e:
        logger.exception("Groq error (clause): %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": "clause",
        }

    return {
        "reply_text": answer,
        "file_url": "",
        "scenario": "clause",
    }


# -------------------------------------------------
# Эндпоинт для отдачи файлов
# -------------------------------------------------
@app.get("/files/{filename}")
async def download_file(filename: str):
    file_path = FILES_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        str(file_path),
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
