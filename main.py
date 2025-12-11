import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ----------------- Логгер -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)


# ----------------- Конфиг -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

# базовый URL для формирования ссылок на файлы
BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://legalfox.up.railway.app")

# папка для PDF
FILES_DIR = Path(__file__).parent / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)

# путь к шрифту
FONT_PATH = Path(__file__).parent / "DejaVuSans.ttf"
FONT_NAME = "DejaVuSans"

if FONT_PATH.exists():
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
        logger.info("Шрифт %s зарегистрирован", FONT_NAME)
    except Exception:
        logger.exception("Не удалось зарегистрировать шрифт")
        FONT_NAME = "Helvetica"
else:
    logger.warning("Файл шрифта %s не найден, используем Helvetica", FONT_PATH)
    FONT_NAME = "Helvetica"


# ----------------- Фолбэк -----------------
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)


# ----------------- Промпты -----------------
PROMPT_CONTRACT = """
Ты — ИИ-помощник по составлению проектов гражданско-правовых договоров в РФ.

На основе описания сторон, предмета, сроков, оплаты и особых условий составь
черновик договора. Стиль — деловой, близкий к стандартным шаблонам, но без явного
копирования конкретных бланков. Используй нейтральную формулировку.

Обязательно:

1. Структура с разделами типа:
   1. Предмет договора
   2. Права и обязанности сторон
   3. Цена и порядок расчётов / Оплата
   4. Срок действия договора
   5. Ответственность сторон
   6. Порядок разрешения споров
   7. Заключительные положения

2. Пиши по-русски, вежливо и формально.
3. Не указывай реальные реквизиты, оставляй под них пустые поля/подсказки.

Текст должен быть полностью самодостаточным, чтобы его можно было распечатать
и доработать юристом.
"""

PROMPT_CLAIM = """
Ты — ИИ-помощник по составлению претензий (досудебных требований) в РФ.

Составь структурированную претензию на основании входных данных.
Структура текста (примерная):

1. Шапка: кому направляется претензия (Адресат).
2. Описание правового основания (например, Закон о защите прав потребителей,
   договор и т.п.).
3. Описание нарушения и фактических обстоятельств.
4. Конкретные требования заявителя.
5. Срок для добровольного исполнения требований.
6. Контактные данные заявителя.
7. Заключительный блок (предупреждение о возможном обращении в суд и т.п.).

Пиши по-русски, официально-деловым стилем. Оставляй место под ФИО, подпись,
дату при необходимости, но не подставляй реальные личные данные.
"""

PROMPT_CLAUSE = """
Ты — ИИ-помощник, который анализирует и переписывает отдельные пункты договора.

Пользователь присылает один или несколько пунктов договора. Твоя задача:

1. Кратко и понятным языком объяснить суть условий.
2. Показать потенциальные риски и односторонние формулировки.
3. При необходимости предложить более сбалансированную формулировку.

Структура ответа:

1) Краткое объяснение смысла пункта.
2) Возможные риски/на что обратить внимание.
3) Вариант переработанной формулировки (если уместно).

Не давай категорических юридических заключений и не изображай, что заменяешь
полноценную консультацию юриста.
"""


# ----------------- Groq клиент -----------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — будет всегда использоваться fallback")


async def call_groq(system_prompt: str, user_query: str) -> str:
    if not client:
        raise RuntimeError("GROQ_API_KEY is not set or Groq client init failed")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    try:
        chat_completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.4,
            max_tokens=1100,
            top_p=1,
        )
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        raise

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# ----------------- Вспомогательные функции -----------------
def is_premium(payload: Dict[str, Any]) -> bool:
    raw = (
        str(payload.get("subscription")
            or payload.get("Subscription")
            or payload.get("Подписка")
            or "")
        .strip()
        .lower()
    )
    return raw in {"1", "true", "yes", "да", "premium", "премиум"}


def create_pdf(filename: str, title: str, body: str) -> Path:
    """
    Делает простой, но аккуратный PDF с заголовком и многострочным текстом.
    """
    filepath = FILES_DIR / filename
    c = canvas.Canvas(str(filepath), pagesize=A4)

    width, height = A4
    left_margin = 40
    top_margin = height - 60
    line_height = 16

    c.setFont(FONT_NAME, 18)
    c.drawString(left_margin, top_margin, title)

    c.setFont(FONT_NAME, 12)
    y = top_margin - 2 * line_height

    for paragraph in body.split("\n"):
        # простейший перенос строк по ширине
        text = paragraph.strip()
        if not text:
            y -= line_height
            continue

        words = text.split()
        line = ""
        for w in words:
            test_line = (line + " " + w).strip()
            if pdfmetrics.stringWidth(test_line, FONT_NAME, 12) > (width - 2 * left_margin):
                c.drawString(left_margin, y, line)
                y -= line_height
                line = w
            else:
                line = test_line

        if line:
            c.drawString(left_margin, y, line)
            y -= line_height

        y -= 4  # небольшой отступ между абзацами

        if y < 80:
            c.showPage()
            c.setFont(FONT_NAME, 12)
            y = height - 60

    c.save()
    logger.info("PDF создан: %s", filepath)
    return filepath


def build_file_url(filename: str) -> str:
    return f"{BASE_URL}/files/{filename}"


# ----------------- FastAPI -----------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам и не только",
    version="0.9.0",
)


@app.get("/files/{filename}")
async def download_file(filename: str):
    filepath = FILES_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(str(filepath), media_type="application/pdf", filename=filename)


# ----------------- Основная логика -----------------
async def handle_contract(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Черновик договора: сценарий contract
    """
    is_prem = is_premium(payload)

    # собираем краткое описание из полей
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
        f"Особые условия: {special}\n"
    )

    try:
        draft_text = await call_groq(PROMPT_CONTRACT, user_query)
    except Exception:
        logger.exception("Ошибка Groq при генерации договора")
        return {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": "contract",
        }

    if is_prem:
        # премиум: делаем PDF
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"contract_{ts}.pdf"
        body_for_pdf = draft_text + (
            "\n\nВажно: это примерный черновик договора. Перед подписанием обязательно "
            "проверь текст и, по возможности, согласуй его с юристом."
        )
        create_pdf(filename, "ДОГОВОР (ЧЕРНОВИК)", body_for_pdf)

        reply = (
            "Черновик договора подготовлен. Файл можно скачать по ссылке ниже.\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед подписанием обязательно проверь текст и, по возможности, "
            "согласуй его с юристом."
        )
        return {
            "reply_text": reply,
            "file_url": build_file_url(filename),
            "scenario": "contract",
        }

    else:
        # бесплатный: только текст
        reply = (
            "Черновик договора (текст):\n\n"
            f"{draft_text}\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед подписанием обязательно проверь текст и, по возможности, "
            "согласуй его с юристом."
        )
        return {
            "reply_text": reply,
            "file_url": "",
            "scenario": "contract",
        }


async def handle_claim(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Черновик претензии: сценарий claim
    """
    is_prem = is_premium(payload)

    adresat = payload.get("Адресат", "")
    basis = payload.get("Основание", "")
    facts = payload.get("Нарушение_и_обстоятельства") or payload.get(
        "Нарушение и обстоятельства", ""
    )
    demands = payload.get("Требования", "")
    deadline = payload.get("Срок_исполнения") or payload.get("Сроки исполнения", "")
    contacts = payload.get("Контакты", "")

    user_query = (
        f"Адресат: {adresat}\n"
        f"Правовое основание: {basis}\n"
        f"Нарушение и обстоятельства: {facts}\n"
        f"Требования заявителя: {demands}\n"
        f"Срок добровольного исполнения требований: {deadline}\n"
        f"Контактные данные заявителя: {contacts}\n"
    )

    try:
        draft_text = await call_groq(PROMPT_CLAIM, user_query)
    except Exception:
        logger.exception("Ошибка Groq при генерации претензии")
        return {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": "claim",
        }

    if is_prem:
        # премиум: генерируем PDF
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"claim_{ts}.pdf"
        body_for_pdf = draft_text + (
            "\n\nВажно: это примерный черновик претензии. Перед направлением "
            "адресату обязательно проверь текст и, по возможности, согласуй его с юристом."
        )
        create_pdf(filename, "ПРЕТЕНЗИЯ (ЧЕРНОВИК)", body_for_pdf)

        reply = (
            "Черновик претензии подготовлен. Файл можно скачать по ссылке ниже.\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед отправкой обязательно проверь текст и, по возможности, "
            "согласуй его с юристом."
        )
        return {
            "reply_text": reply,
            "file_url": build_file_url(filename),
            "scenario": "claim",
        }

    else:
        # бесплатный: только текст претензии, без файла
        reply = (
            "Черновик претензии (текст):\n\n"
            f"{draft_text}\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед отправкой обязательно проверь текст и, по возможности, "
            "согласуй его с юристом."
        )
        return {
            "reply_text": reply,
            "file_url": "",
            "scenario": "claim",
        }


async def handle_clause(payload: Dict[str, Any]) -> Dict[str, str]:
    """
    Помощь с пунктами договора: сценарий clause
    """
    text = payload.get("Текст") or payload.get("text") or payload.get("Описание") or ""
    if not text.strip():
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "file_url": "",
            "scenario": "clause",
        }

    try:
        answer = await call_groq(PROMPT_CLAUSE, text)
    except Exception:
        logger.exception("Ошибка Groq при анализе пункта")
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


# ----------------- Endpoint -----------------
@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "contract"
    )
    scenario = str(scenario).strip().lower()

    if scenario == "contract":
        result = await handle_contract(payload)
    elif scenario == "claim":
        result = await handle_claim(payload)
    elif scenario == "clause":
        result = await handle_clause(payload)
    else:
        logger.info("Неизвестный сценарий: %s", scenario)
        result = {
            "reply_text": FALLBACK_TEXT,
            "file_url": "",
            "scenario": scenario,
        }

    logger.info("Scenario=%s ответ готов (file_url=%s)", result["scenario"], result.get("file_url"))
    return result


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
