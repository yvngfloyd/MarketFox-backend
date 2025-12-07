import os
import uuid
import logging
from typing import Any, Dict

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq
from fpdf import FPDF

# ----------------- Логгер -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# ----------------- Конфиг -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Домен твоего бэкенда (нужен для формирования file_url в ответе)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://legalfox.up.railway.app")

GROQ_MODEL = "llama-3.1-8b-instant"

# ----------------- Промпты -----------------
PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-ассистент для юристов. Твоя задача — собирать аккуратные,
юридически выверенные ЧЕРНОВИКИ договоров на русском языке.

Требования:
- Пиши структурированный текст с заголовками и пунктами.
- Не используй Markdown-разметку (никаких **звёздочек**, #заголовков).
- Это черновик, который юрист потом будет дорабатывать, не «юридическая консультация».
- Следи за логикой: стороны, предмет, права и обязанности, цена/порядок расчетов,
  ответственность, порядок расторжения, прочие условия.

Формат:
- Начни с названия договора и шапки (город, дата, стороны).
- Далее пункты договора по классической структуре.
- В конце можно оставить блок "Реквизиты и подписи сторон".
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-ассистент для юристов. Нужно подготовить черновик ПРЕТЕНЗИИ
(претензионного письма) на русском языке.

Требования:
- Пиши деловым, но простым языком.
- Не используй Markdown-разметку.
- Структура: шапка (кому, от кого), вводная (договор/основание),
  описание нарушения и обстоятельств, ссылки на нормы права (общо, без номера статей, если уверенности нет),
  требования заявителя, срок для исполнения, предупреждение о дальнейших действиях,
  заключительная часть и реквизиты.
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ИИ-ассистент, который помогает юристу анализировать
отдельные условия договора.

На вход получаешь текст пункта или фрагмента договора.
Твоя задача:
- Кратко пересказать, что именно закреплено в этом пункте (1–3 предложения).
- Отметить возможные риски или односторонние формулировки.
- Предложить 1–2 варианта, как можно переписать пункт более безопасно/сбалансированно.

Не используй Markdown и сложные списки — дели ответ на короткие абзацы.
"""


# ----------------- Groq client -----------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — ИИ работать не будет")


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
        max_tokens=1500,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# ----------------- PDF генерация -----------------
def create_pdf_from_text(text: str, prefix: str = "document") -> str:
    """
    Создаём PDF с использованием шрифта DejaVuSans.ttf (должен лежать в корне проекта).
    Возвращаем ИМЯ файла (без пути).
    """
    os.makedirs("files", exist_ok=True)

    filename = f"{prefix}_{uuid.uuid4().hex}.pdf"
    filepath = os.path.join("files", filename)

    pdf = FPDF()
    pdf.add_page()

    # Юникод-шрифт (ты уже загрузил DejaVuSans.ttf в репозиторий)
    pdf.add_font("DejaVu", "", "DejaVuSans.ttf", uni=True)
    pdf.set_font("DejaVu", size=11)

    # Простая разбивка по строкам
    for line in text.split("\n"):
        line = line.replace("\r", "")
        if not line.strip():
            pdf.ln(4)
            continue
        pdf.multi_cell(0, 6, line)

    pdf.output(filepath)
    logger.info("PDF создан: %s", filepath)
    return filename


# ----------------- Логика по сценариям -----------------
async def handle_contract(payload: Dict[str, Any]) -> Dict[str, str]:
    # Поля из BotHelp (с пробелами)
    type_ = payload.get("Тип договора", "").strip()
    parties = payload.get("Стороны", "").strip()
    subject = payload.get("Предмет", "").strip()
    terms_payment = payload.get("Сроки и оплата", "").strip() or payload.get(
        "Сроки", ""
    ).strip()
    special = payload.get("Особые условия", "").strip()

    if not any([type_, parties, subject]):
        return {
            "reply_text": "Пока мало данных. Напиши хотя бы тип договора, стороны и предмет.",
            "scenario": "contract",
        }

    user_summary = (
        f"Тип договора: {type_}\n"
        f"Стороны: {parties}\n"
        f"Предмет: {subject}\n"
        f"Сроки и оплата: {terms_payment or 'не указано'}\n"
        f"Особые условия: {special or 'не указано'}\n\n"
        "Собери, пожалуйста, аккуратный черновик договора."
    )

    try:
        draft_text = await call_groq(PROMPT_CONTRACT, user_summary)
    except Exception as e:
        logger.exception("Groq error in contract scenario: %s", e)
        return {
            "reply_text": "Не удалось обратиться к ИИ. Попробуй позже или измени данные договора.",
            "scenario": "contract",
        }

    # Генерируем PDF
    filename = create_pdf_from_text(draft_text, prefix="contract")
    file_url = f"{PUBLIC_BASE_URL.rstrip('/')}/files/{filename}"

    reply_text = f"Готово! Я собрал черновик, лови 📄\n{file_url}"

    return {
        "reply_text": reply_text,
        "file_url": file_url,
        "scenario": "contract",
    }


async def handle_claim(payload: Dict[str, Any]) -> Dict[str, str]:
    addressee = payload.get("Адресат", "").strip()
    basis = payload.get("Основание", "").strip()
    facts = payload.get("Нарушение и обстоятельства", "").strip() or payload.get(
        "Нарушение_и_обстоятельства", ""
    ).strip()
    demands = payload.get("Требования", "").strip()
    deadline = payload.get("Сроки исполнения", "").strip() or payload.get(
        "Срок_исполнения", ""
    ).strip()
    contacts = payload.get("Контакты", "").strip()

    if not facts and not demands:
        return {
            "reply_text": "Пока нет данных для претензии. Напиши, в чём нарушение и чего ты требуешь.",
            "scenario": "claim",
        }

    user_summary = (
        f"Адресат: {addressee or 'не указан'}\n"
        f"Основание: {basis or 'не указано'}\n"
        f"Нарушение и обстоятельства: {facts}\n"
        f"Требования: {demands or 'не указаны'}\n"
        f"Срок исполнения требований: {deadline or 'не указан'}\n"
        f"Контакты заявителя: {contacts or 'не указаны'}\n\n"
        "Собери, пожалуйста, черновик претензионного письма."
    )

    try:
        text = await call_groq(PROMPT_CLAIM, user_summary)
    except Exception as e:
        logger.exception("Groq error in claim scenario: %s", e)
        return {
            "reply_text": "Не удалось обратиться к ИИ. Попробуй позже или измени данные.",
            "scenario": "claim",
        }

    return {
        "reply_text": text,
        "scenario": "claim",
    }


async def handle_clause(payload: Dict[str, Any]) -> Dict[str, str]:
    clause_text = (
        payload.get("Текст условия", "")
        or payload.get("Фрагмент", "")
        or payload.get("Текст", "")
    ).strip()

    if not clause_text:
        return {
            "reply_text": "Пока нет текста условия. Пришли пункт договора, который нужно разобрать.",
            "scenario": "clause",
        }

    try:
        text = await call_groq(PROMPT_CLAUSE, clause_text)
    except Exception as e:
        logger.exception("Groq error in clause scenario: %s", e)
        return {
            "reply_text": "Не удалось обратиться к ИИ. Попробуй позже.",
            "scenario": "clause",
        }

    return {
        "reply_text": text,
        "scenario": "clause",
    }


# ----------------- FastAPI -----------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.4.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (
        payload.get("scenario")
        or payload.get("Сценарий")
        or payload.get("сценарий")
        or "contract"
    )

    logger.info("User scenario=%s", scenario)

    if scenario == "contract":
        return await handle_contract(payload)
    elif scenario == "claim":
        return await handle_claim(payload)
    elif scenario == "clause":
        return await handle_clause(payload)

    # неизвестный сценарий
    return {
        "reply_text": "Пока не понимаю, что за сценарий. Попробуй заново из меню.",
        "scenario": scenario,
    }


@app.get("/files/{filename}")
async def download_file(filename: str):
    filepath = os.path.join("files", filename)
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
