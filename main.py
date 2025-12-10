import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app")

FILES_DIR = Path(__file__).parent / "files"
FILES_DIR.mkdir(exist_ok=True)

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

# -------------------------------------------------
# Промпты
# -------------------------------------------------
PROMPT_CONTRACT = """
Ты — LegalFox, ассистент-юрист. Твоя задача — составлять понятные проектные тексты
гражданско-правовых договоров по российскому праву.

Всегда:
- учитывай, что это именно ЧЕРНОВИК, а не финальная редакция;
- пиши по-русски, без жаргона и воды;
- соблюдай базовую структуру договора: преамбула, предмет, права и обязанности,
  порядок расчётов, ответственность, порядок расторжения, прочие условия, реквизиты.

Важно:
- не указывай конкретные статьи законов (это сделает юрист при доработке);
- не придумывай заведомо незаконные условия;
- делай аккуратные абзацы и подзаголовки, чтобы текст удобно читался.

В конце добавь короткое предупреждение: что это примерный черновик и перед подписанием
его нужно проверить юристом.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ассистент-юрист. Твоя задача — составлять проектные тексты
претензий (досудебных требований) по российскому праву.

Всегда:
- пиши по-русски, деловым, но понятным языком;
- соблюдай структуру претензии: шапка (адресат, отправитель), вводная часть
  (какой договор/основание), описание нарушения, требования, срок исполнения,
  предупреждение о возможном обращении в суд, подпись и реквизиты отправителя.

Важно:
- не гарантируй исход спора и не давай юридических гарантий;
- в конце добавь предупреждение, что текст — примерный черновик и перед направлением
желательно согласовать его с юристом.
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ассистент-юрист. Пользователь присылает пункт или фрагмент договора.
Твоя задача —:
- коротко объяснить человеческим языком, что он означает;
- указать потенциальные риски для стороны пользователя;
- при необходимости предложить 1–2 варианта более безопасной или сбалансированной формулировки.

Всегда:
- пиши по-русски, без излишней юридической казуистики;
- не притворяйся адвокатом в конкретном деле, а давай общую оценку формулировки;
- в конце напомни, что для конкретной ситуации нужен юрист, знакомый с документами.
"""

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
    logger.warning("GROQ_API_KEY не задан — всегда будет использоваться fallback")


async def call_groq(system_prompt: str, user_query: str) -> str:
    if not client:
        raise RuntimeError("GROQ_API_KEY is not set or Groq client init failed")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        temperature=0.4,
        max_tokens=1200,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return content.strip()


# -------------------------------------------------
# Генерация PDF
# -------------------------------------------------
def create_pdf_from_text(text: str, scenario: str) -> str:
    """
    Один генератор PDF для договоров и претензий.
    scenario: "contract" или "claim"
    """
    prefix = "contract" if scenario == "contract" else "claim"
    filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    file_path = FILES_DIR / filename

    pdf = FPDF(format="A4")
    pdf.add_page()

    # Шрифт с поддержкой кириллицы
    font_path = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"
    if font_path.exists():
        pdf.add_font("DejaVu", "", str(font_path), uni=True)
        pdf.set_font("DejaVu", "", 11)
    else:
        pdf.set_font("Arial", size=11)

    pdf.set_auto_page_break(auto=True, margin=15)

    for line in text.split("\n"):
        pdf.multi_cell(0, 7, line)
    pdf.output(str(file_path), "F")

    logger.info("PDF создан: %s", file_path)
    return filename


# -------------------------------------------------
# FastAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.4.0",
)


@app.get("/files/{filename}")
async def download_file(filename: str):
    file_path = FILES_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(str(file_path), media_type="application/pdf", filename=filename)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (payload.get("scenario") or "").strip() or "contract"
    scenario = scenario.lower()

    # ---------- Сценарий: черновик договора ----------
    if scenario == "contract":
        contract_type = payload.get("Тип договора") or payload.get("Тип_договора") or ""
        parties = payload.get("Стороны", "")
        subject = payload.get("Предмет", "")
        terms = payload.get("Сроки и оплата") or payload.get("Сроки") or ""
        special = payload.get("Особые условия") or payload.get("Особые_условия") or ""

        user_query = (
            "Нужно подготовить черновик гражданско-правового договора.\n\n"
            f"Тип договора: {contract_type}\n"
            f"Стороны: {parties}\n"
            f"Предмет: {subject}\n"
            f"Сроки и порядок оплаты: {terms}\n"
            f"Особые условия и риски: {special}"
        )

        try:
            answer = await call_groq(PROMPT_CONTRACT, user_query)
            filename = create_pdf_from_text(answer, scenario="contract")
            file_url = f"{BASE_URL}/files/{filename}"

            reply = (
                "Черновик договора подготовлен. Файл можно скачать по ссылке ниже.\n\n"
                "Важно: это примерный черновик, сформированный ИИ. Перед подписанием "
                "обязательно проверь текст и, по возможности, согласуй его с юристом.\n\n"
                f"{file_url}"
            )

            return {
                "reply_text": reply,
                "file_url": file_url,
                "scenario": "contract",
            }
        except Exception as e:
            logger.exception("Ошибка при генерации договора: %s", e)
            return {
                "reply_text": FALLBACK_TEXT,
                "scenario": "contract",
            }

    # ---------- Сценарий: черновик претензии ----------
    if scenario == "claim":
        addressee = payload.get("Адресат", "")
        base = payload.get("Основание", "")
        facts = payload.get("Нарушение и обстоятельства") or payload.get(
            "Нарушение_и_обстоятельства"
        ) or ""
        demands = payload.get("Требования", "")
        deadline = payload.get("Сроки исполнения") or payload.get("Срок_исполнения") or ""
        contacts = payload.get("Контакты", "")

        user_query = (
            "Нужно подготовить черновик досудебной претензии.\n\n"
            f"Адресат: {addressee}\n"
            f"Основание (договор, ситуация): {base}\n"
            f"Нарушение и обстоятельства: {facts}\n"
            f"Требования: {demands}\n"
            f"Срок исполнения: {deadline}\n"
            f"Контакты отправителя: {contacts}"
        )

        try:
            answer = await call_groq(PROMPT_CLAIM, user_query)
            filename = create_pdf_from_text(answer, scenario="claim")
            file_url = f"{BASE_URL}/files/{filename}"

            reply = (
                "Черновик претензии подготовлен. Файл можно скачать по ссылке ниже.\n\n"
                "Важно: это примерный черновик, сформированный ИИ. Перед отправкой "
                "обязательно проверь текст и, по возможности, согласуй его с юристом.\n\n"
                f"{file_url}"
            )

            return {
                "reply_text": reply,
                "file_url": file_url,
                "scenario": "claim",
            }
        except Exception as e:
            logger.exception("Ошибка при генерации претензии: %s", e)
            return {
                "reply_text": FALLBACK_TEXT,
                "scenario": "claim",
            }

    # ---------- Сценарий: помощь с пунктами договора ----------
    if scenario == "clause":
        clause_text = (
            payload.get("Текст пункта")
            or payload.get("Текст_пункта")
            or payload.get("Текст")
            or ""
        ).strip()

        if not clause_text:
            return {
                "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
                "scenario": "clause",
            }

        try:
            answer = await call_groq(PROMPT_CLAUSE, clause_text)
            return {
                "reply_text": answer,
                "scenario": "clause",
            }
        except Exception as e:
            logger.exception("Ошибка при анализе пункта: %s", e)
            return {
                "reply_text": FALLBACK_TEXT,
                "scenario": "clause",
            }

    # ---------- Неизвестный сценарий ----------
    logger.info("Неизвестный сценарий: %s", scenario)
    return {
        "reply_text": FALLBACK_TEXT,
        "scenario": scenario,
    }


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
