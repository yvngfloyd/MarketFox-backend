import os
import logging
from typing import Any, Dict

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from textwrap import wrap
from uuid import uuid4

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
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

# Директория для файлов (PDF)
FILES_DIR = os.getenv("FILES_DIR", "files")
os.makedirs(FILES_DIR, exist_ok=True)

# Регистрируем шрифт для кириллицы
FONT_NAME = "DejaVuSans"
FONT_PATH = os.path.join("fonts", "DejaVuSans.ttf")
try:
    if os.path.exists(FONT_PATH):
        pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
    else:
        # если шрифт не нашли — используем стандартный (кириллица может отображаться криво)
        logger.warning("Файл шрифта %s не найден, кириллица в PDF может отображаться некорректно", FONT_PATH)
        FONT_NAME = "Helvetica"
except Exception:
    logger.exception("Не удалось зарегистрировать шрифт, используем Helvetica по умолчанию")
    FONT_NAME = "Helvetica"

FALLBACK_TEXT = (
    "Сейчас не могу обратиться к нейросети. "
    "Попробуй сформулировать задачу ещё раз попроще или позже 🦊"
)

# -------------------------------------------------
# Промпты
# -------------------------------------------------

PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-помощник для юристов. Твоя задача — на основе структурированной
информации от пользователя подготовить ЧЕРНОВИК гражданско-правового договора в РФ.

Формат:
1) Краткая вводная строка (1 предложение, что за договор).
2) Полный текст договора с нумерацией разделов и пунктов.
3) Текст должен быть пригоден для копирования в Word/Google Docs.
4) Не используй Markdown (**звёздочки**, #заголовки и т.п.).

Всегда соблюдай российское право (ГК РФ). Пиши нейтральным юридическим языком,
без шуток и воды.

Данные приходят в виде блоков:
- Тип договора;
- Стороны;
- Предмет;
- Сроки и оплата;
- Особые условия (штрафы, ответственность, конфиденциальность и т.п.).

Собери из этого один связный текст договора.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-помощник для юристов. Твоя задача — составить чёткий и структурированный
черновик ПРЕТЕНЗИИ/ДОСУДЕБНОЙ РАСПОРЯДИТЕЛЬНОЙ ПИСЬМА.

Формат:
1) "Шапка" (адресат, от кого, контакты без выдумывания ИНН и т.п.).
2) Описание основания и договора/отношений сторон.
3) Описание нарушения и обстоятельств (по сути факты).
4) Формулировка требований.
5) Срок исполнения и последствия при неисполнении.
6) Заключительная часть (подпись, дата — оставить пустыми строками).

Пиши сухим, деловым языком. Не придумывай номера статей, если их нет в данных,
но можешь ссылаться на общие нормы ГК РФ.
Не используй Markdown.
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ИИ-ассистент для юристов.

Задача: по присланному фрагменту договора (1–несколько пунктов) дать:
1) краткое человеческое пояснение, что это условие означает;
2) какие риски для клиента оно несёт;
3) при необходимости — предложить более безопасную формулировку.

Формат ответа:
1) "Краткий смысл:" — 1–3 предложения простым языком.
2) "Риски:" — 2–5 коротких пунктов через нумерацию 1), 2), 3) ...
3) "Можно поправить так:" — предложи один вариант переписанного пункта.
Не используй Markdown-разметку (*, #, и т.п.).
Пиши с учётом права РФ.
"""

# -------------------------------------------------
# Клиент Groq
# -------------------------------------------------

client: Groq | None = None

if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будет использоваться только fallback")


async def get_ai_text(system_prompt: str, user_query: str, scenario: str) -> str:
    """
    Вызов Groq, возвращаем только текст (или fallback).
    """
    if not user_query or not user_query.strip():
        return "Пока нет данных. Напиши текст/описание, с которым нужно помочь."

    try:
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
            max_tokens=1400,
            top_p=1,
        )

        content = chat_completion.choices[0].message.content or ""
        return content.strip()
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return FALLBACK_TEXT


# -------------------------------------------------
# Генерация PDF
# -------------------------------------------------

def create_contract_pdf(text: str) -> str:
    """
    Делает PDF с текстом договора и возвращает абсолютный путь к файлу.
    """
    # уникальное имя файла
    filename = f"contract_{uuid4().hex}.pdf"
    filepath = os.path.join(FILES_DIR, filename)

    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    # параметры текста
    left_margin = 40
    right_margin = 40
    top_margin = 40
    bottom_margin = 40
    line_height = 14

    max_width = width - left_margin - right_margin

    c.setFont(FONT_NAME, 11)

    # простое разбиение текста на строки с переносами
    y = height - top_margin
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            y -= line_height  # пустая строка
            continue

        # заворачиваем строку по символам (грубенько, но работает)
        for line in wrap(paragraph, 100):
            if y < bottom_margin:
                c.showPage()
                c.setFont(FONT_NAME, 11)
                y = height - top_margin
            c.drawString(left_margin, y, line)
            y -= line_height

    c.showPage()
    c.save()

    return filepath


def build_file_url(filename: str) -> str:
    """
    Собираем полный URL до файла для отдачи в бот.
    """
    if not BASE_URL:
        # если BASE_URL не задан — просто вернём относительный путь
        return f"/files/{filename}"
    return f"{BASE_URL}/files/{filename}"


# -------------------------------------------------
# FastAPI
# -------------------------------------------------

app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.3.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Главная точка, куда шлёт запрос BotHelp.
    Ожидаем поле 'scenario' и разные поля в зависимости от ветки.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or "contract"
    )

    # ---------------------------------------------
    # ВЕТКА 1. Черновик договора (contract)
    # ---------------------------------------------
    if scenario == "contract":
        contract_type = payload.get("Тип договора", "")
        parties = payload.get("Стороны", "")
        subject = payload.get("Предмет", "")
        terms = payload.get("Сроки и оплата", "")
        special = payload.get("Особые условия", "")

        user_text = (
            f"Тип договора: {contract_type}\n"
            f"Стороны: {parties}\n"
            f"Предмет: {subject}\n"
            f"Сроки и оплата: {terms}\n"
            f"Особые условия: {special}"
        )

        system_prompt = PROMPT_CONTRACT

        # Получаем текст договора от ИИ
        reply_text = await get_ai_text(system_prompt, user_text, scenario)

        # Генерируем PDF
        pdf_path = create_contract_pdf(reply_text)
        filename = os.path.basename(pdf_path)
        file_url = build_file_url(filename)

        return {
            "reply_text": reply_text,
            "file_url": file_url,
            "scenario": "contract",
        }

    # ---------------------------------------------
    # ВЕТКА 2. Претензия / досудебка (claim)
    # ---------------------------------------------
    elif scenario == "claim":
        adresat = payload.get("Адресат", "")
        basis = payload.get("Основание", "")
        facts = payload.get("Нарушение и обстоятельства", "")
        demands = payload.get("Требования", "")
        deadline = payload.get("Сроки исполнения", "")
        contacts = payload.get("Контакты", "")

        user_text = (
            f"Адресат: {adresat}\n"
            f"Основание: {basis}\n"
            f"Нарушение и обстоятельства: {facts}\n"
            f"Требования: {demands}\n"
            f"Срок исполнения: {deadline}\n"
            f"Контакты: {contacts}"
        )

        system_prompt = PROMPT_CLAIM
        reply_text = await get_ai_text(system_prompt, user_text, scenario)

        return {
            "reply_text": reply_text,
            "scenario": "claim",
        }

    # ---------------------------------------------
    # ВЕТКА 3. Проверка пункта договора (clause)
    # ---------------------------------------------
    elif scenario == "clause":
        clause_text = (
            payload.get("Текст", "")
            or payload.get("text", "")
            or ""
        )
        user_text = clause_text
        system_prompt = PROMPT_CLAUSE
        reply_text = await get_ai_text(system_prompt, user_text, scenario)

        return {
            "reply_text": reply_text,
            "scenario": "clause",
        }

    # ---------------------------------------------
    # Fallback / неизвестный сценарий
    # ---------------------------------------------
    else:
        user_text = payload.get("text", "") or ""
        reply_text = await get_ai_text(PROMPT_CONTRACT, user_text, "contract")
        return {
            "reply_text": reply_text,
            "scenario": "contract",
        }


@app.get("/files/{filename}")
async def download_file(filename: str):
    """
    Отдаём PDF-файлы по HTTP, чтобы BotHelp мог взять ссылку.
    """
    file_path = os.path.join(FILES_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, media_type="application/pdf", filename=filename)


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
