import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
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

BASE_DIR = Path(__file__).resolve().parent
FILES_DIR = BASE_DIR / "files"
TEMPLATES_DIR = BASE_DIR / "templates"
FONT_PATH = BASE_DIR / "DejaVuSans.ttf"

FILES_DIR.mkdir(exist_ok=True)

# ----------------- Базовые сообщения -----------------
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

PROMPT_CONTRACT_SYSTEM = """
Ты — ИИ-помощник юриста. Твоя задача — на основе шаблона договора и исходных данных
сформировать аккуратный текст договора на русском языке.

Требования:
- используй структуру и стиль шаблона;
- добавь недостающие юридические формулировки при необходимости;
- не меняй суть вводных данных пользователя;
- верни только текст договора, без комментариев и пояснений, без markdown.
"""

PROMPT_CLAIM_SYSTEM = """
Ты — ИИ-помощник юриста. Твоя задача — на основе шаблона претензии и исходных данных
сформировать аккуратный текст претензии на русском языке.

Требования:
- используй структуру шаблона (заголовок, блоки по смыслу);
- пиши официально-деловым стилем;
- не добавляй от себя угроз, грубых формулировок и т.п.;
- верни только текст претензии, без комментариев и пояснений, без markdown.
"""

PROMPT_CLAUSE_SYSTEM = """
Ты — ИИ-помощник юриста. Пользователь присылает текст пункта договора или фрагмента.
Твоя задача: кратко объяснить, что он означает, и при необходимости предложить
более безопасную редакцию.

Формат:
1) сначала в 2–4 предложениях объясни смысл пункта простым языком;
2) затем предложи возможную улучшенную редакцию (если есть смысл);
3) не используй markdown, списки со звёздочками и пр.
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
    logger.warning("GROQ_API_KEY не задан — будет использоваться fallback-текст")

# ----------------- Вспомогательные функции -----------------


def load_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    if not path.exists():
        logger.error("Шаблон %s не найден по пути %s", name, path)
        return ""
    return path.read_text("utf-8")


def call_groq(system_prompt: str, user_prompt: str) -> str:
    if not client:
        raise RuntimeError("Groq client is not available")

    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
        max_tokens=1500,
        top_p=1,
    )
    content = resp.choices[0].message.content or ""
    return content.strip()


def register_font():
    try:
        if FONT_PATH.exists():
            pdfmetrics.registerFont(TTFont("DejaVuSans", str(FONT_PATH)))
            return "DejaVuSans"
    except Exception:
        logger.exception("Не удалось зарегистрировать DejaVuSans")

    # fallback
    return "Helvetica"


def create_pdf_from_text(text: str, filename: str) -> str:
    """Создаёт PDF с простым текстом, возвращает имя файла (только имя, не путь)."""
    FILES_DIR.mkdir(exist_ok=True)
    font_name = register_font()

    output_path = FILES_DIR / filename
    c = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4

    c.setFont(font_name, 11)
    left_margin = 40
    top_margin = height - 50
    line_height = 16

    y = top_margin

    # Простейший перенос строк
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # если строка пустая — просто пустая строка
        if not line:
            y -= line_height
            if y < 50:
                c.showPage()
                c.setFont(font_name, 11)
                y = top_margin
            continue

        # ручной перенос по ширине страницы
        max_width = width - 2 * left_margin
        words = line.split(" ")
        buf = ""
        for w in words:
            test = (buf + " " + w).strip()
            if c.stringWidth(test, font_name, 11) <= max_width:
                buf = test
            else:
                c.drawString(left_margin, y, buf)
                y -= line_height
                if y < 50:
                    c.showPage()
                    c.setFont(font_name, 11)
                    y = top_margin
                buf = w
        if buf:
            c.drawString(left_margin, y, buf)
            y -= line_height
            if y < 50:
                c.showPage()
                c.setFont(font_name, 11)
                y = top_margin

    c.save()
    logger.info("PDF создан: %s", output_path)
    return output_path.name


def generate_contract_text(data: Dict[str, str]) -> str:
    template = load_template("contract_base.txt")

    user_prompt = f"""
Исходные данные для договора:

Тип договора: {data.get("Тип_договора") or data.get("Тип договора") or "не указан"}
Стороны: {data.get("Стороны") or "не указаны"}
Предмет: {data.get("Предмет") or "не указан"}
Сроки и оплата: {data.get("Сроки")} | {data.get("Оплата")}
Особые условия: {data.get("Особые_условия") or data.get("Особые условия") or "нет специальных условий"}

Шаблон договора:

<<<Шаблон>>>
{template}
<<<Конец шаблона>>>

На основе шаблона и исходных данных сформируй полный текст договора.
Соблюдай структуру и стиль шаблона. Можно добавлять разумные юридические формулировки.
Верни только текст договора, без комментариев.
"""

    return call_groq(PROMPT_CONTRACT_SYSTEM, user_prompt)


def generate_claim_text(data: Dict[str, str]) -> str:
    template = load_template("claim_base.txt")

    user_prompt = f"""
Исходные данные для претензии:

Адресат: {data.get("Адресат") or "не указан"}
Основание (закон, договор и т.п.): {data.get("Основание") or "не указано"}
Нарушение и обстоятельства: {data.get("Нарушение_и_обстоятельства") or data.get("Нарушение и обстоятельства") or "не указано"}
Требования заявителя: {data.get("Требования") or "не указаны"}
Срок добровольного исполнения: {data.get("Срок_исполнения") or data.get("Сроки исполнения") or "не указан"}
Контакты заявителя: {data.get("Контакты") or "не указаны"}

Шаблон претензии:

<<<Шаблон>>>
{template}
<<<Конец шаблона>>>

На основе шаблона и исходных данных сформируй полный текст претензии.
Соблюдай структуру и стиль шаблона. Верни только текст претензии, без комментариев.
"""

    return call_groq(PROMPT_CLAIM_SYSTEM, user_prompt)


def generate_clause_answer(text: str) -> str:
    user_prompt = f"Текст пункта/фрагмента:\n\n{text}"
    return call_groq(PROMPT_CLAUSE_SYSTEM, user_prompt)


# ----------------- FastAPI -----------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="1.1.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    """
    Главный вебхук из BotHelp.
    Ожидаем поле 'scenario' + остальные поля зависят от ветки.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = payload.get("scenario") or payload.get("сценарий") or "contract"

    # ---------- CONTRACT ----------
    if scenario == "contract":
        try:
            text = generate_contract_text(payload)
            # имя файла
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"contract_{ts}.pdf"
            saved_name = create_pdf_from_text(text, filename)

            url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip() or 'legalfox.up.railway.app'}/files/{saved_name}"

            reply = (
                "Черновик договора подготовлен. Файл можно скачать по ссылке ниже.\n\n"
                f"{url}\n\n"
                "Важно: это примерный черновик, сформированный ИИ. "
                "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
            )

            return {
                "reply_text": reply,
                "file_url": url,
                "scenario": "contract",
            }
        except Exception as e:
            logger.exception("Ошибка при генерации договора: %s", e)
            return {
                "reply_text": FALLBACK_TEXT,
                "scenario": "contract",
            }

    # ---------- CLAIM ----------
    if scenario == "claim":
        try:
            text = generate_claim_text(payload)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"claim_{ts}.pdf"
            saved_name = create_pdf_from_text(text, filename)

            url = f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', '').strip() or 'legalfox.up.railway.app'}/files/{saved_name}"

            reply = (
                "Черновик претензии подготовлен. Файл можно скачать по ссылке ниже.\n\n"
                f"{url}\n\n"
                "Важно: это примерный черновик, сформированный ИИ. "
                "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
            )

            return {
                "reply_text": reply,
                "file_url": url,
                "scenario": "claim",
            }
        except Exception as e:
            logger.exception("Ошибка при генерации претензии: %s", e)
            return {
                "reply_text": FALLBACK_TEXT,
                "scenario": "claim",
            }

    # ---------- CLAUSE (анализ пункта) ----------
    if scenario == "clause":
        clause_text = (
            payload.get("Текст") or payload.get("text") or payload.get("Clause") or ""
        )
        if not clause_text.strip():
            return {
                "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
                "scenario": "clause",
            }

        try:
            answer = generate_clause_answer(clause_text)
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


@app.get("/files/{filename}")
async def download_file(filename: str):
    file_path = FILES_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path=str(file_path),
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
