import os
import json
import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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

BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app")
FILES_DIR = os.path.join(os.path.dirname(__file__), "files")
os.makedirs(FILES_DIR, exist_ok=True)

# Шрифт для PDF (DejaVuSans.ttf должен лежать в ./fonts)
FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_PATH = os.path.join(FONTS_DIR, "DejaVuSans.ttf")
if os.path.exists(FONT_PATH):
    pdfmetrics.registerFont(TTFont("DejaVuSans", FONT_PATH))
    PDF_FONT_NAME = "DejaVuSans"
    logger.info("Используется шрифт DejaVuSans.ttf")
else:
    PDF_FONT_NAME = "Helvetica"
    logger.warning("DejaVuSans.ttf не найден, используется стандартный Helvetica")

# -------------------------------------------------
# Промты
# -------------------------------------------------
PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-помощник юристам по российскому праву.

Задача: на основе краткого описания сделки подготовить ЧЕРНОВИК гражданско-правового договора.
Важно: это не окончательный вариант, а аккуратный черновик, который юрист потом доработает.

Требования к тексту:
- язык: русский, деловой, без разговорных выражений;
- структура: заголовок, преамбула, предмет, права и обязанности, ответственность, порядок разрешения споров, прочие условия, реквизиты сторон;
- никаких вымышленных реквизитов (ИНН, ОГРН, паспорт) — вместо них оставляй прочерки или заглушки вида «___»;
- не придумывай конкретные суммы, если их нет в описании — пиши «___ рублей»;
- учитывай, что договор будет проверять юрист, поэтому лучше чуть более формально, чем слишком разговорно.

В ответе НУЖЕН только чистый текст договора, без комментариев, пояснений и Markdown-разметки.
"""

PROMPT_CLAIM = """
Ты — LegalFox, ИИ-помощник юристам по российскому праву.

Задача: на основе входных данных подготовить ЧЕРНОВИК ПРЕТЕНЗИИ (досудебного требования).
Важно: это примерный вариант, который юрист потом доработает.

Требования к тексту:
- язык: русский, деловой;
- структура: «шапка» (кому, от кого), вводная часть (основание отношений), описание нарушений, требования, срок для добровольного исполнения, предупреждение о возможном обращении в суд, блок для подписи;
- не придумывай реквизиты (ИНН, ОГРН, паспорт) — ставь «___»;
- суммы указывай только если они есть во входных данных, иначе используй «___ рублей»;
- можно ссылаться на общие нормы ГК РФ, но без излишней «воды».

В ответе НУЖЕН только чистый текст претензии, без комментариев и Markdown.
"""

PROMPT_CLAUSE = """
Ты — LegalFox, ИИ-помощник юристам по российскому праву.

Пользователь присылает пункт договора или фрагмент текста. 
Твоя задача:
1) кратко объяснить, что означает этот пункт человеческим языком;
2) указать возможные риски для стороны пользователя;
3) предложить 1–2 варианта более безопасной или сбалансированной формулировки.

Формат ответа:
1) Краткое объяснение сути пункта.
2) Риски: 2–5 пунктов.
3) Возможные правки: 1–3 варианта альтернативной формулировки.

Не используй Markdown-разметку, просто аккуратные абзацы.
Если текста слишком мало или он непонятен, сначала попроси уточнить формулировку.
"""

# -------------------------------------------------
# Groq клиент
# -------------------------------------------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
        client = None
else:
    logger.warning("GROQ_API_KEY не задан — будем работать на шаблонах без ИИ")

# -------------------------------------------------
# Утилиты
# -------------------------------------------------
def create_pdf_from_text(text: str, filepath: str) -> None:
    """Простой генератор PDF построчно."""
    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    left_margin = 40
    top_margin = 40
    bottom_margin = 40
    line_height = 14

    y = height - top_margin
    c.setFont(PDF_FONT_NAME, 11)

    for paragraph in text.split("\n"):
        if paragraph.strip() == "":
            y -= line_height
            if y < bottom_margin:
                c.showPage()
                c.setFont(PDF_FONT_NAME, 11)
                y = height - top_margin
            continue

        # очень простой перенос по символам, чтобы не вылезало за край
        line = ""
        for ch in paragraph:
            test_line = line + ch
            if c.stringWidth(test_line, PDF_FONT_NAME, 11) > (width - 2 * left_margin):
                c.drawString(left_margin, y, line)
                y -= line_height
                if y < bottom_margin:
                    c.showPage()
                    c.setFont(PDF_FONT_NAME, 11)
                    y = height - top_margin
                line = ch
            else:
                line = test_line

        if line:
            c.drawString(left_margin, y, line)
            y -= line_height
            if y < bottom_margin:
                c.showPage()
                c.setFont(PDF_FONT_NAME, 11)
                y = height - top_margin

    c.showPage()
    c.save()
    logger.info("PDF создан: %s", filepath)


async def call_groq(system_prompt: str, user_content: str) -> str | None:
    """Вызываем Groq, в случае ошибки отдаём None."""
    if not client:
        return None

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.4,
            max_tokens=1800,
            top_p=1,
        )
        content = completion.choices[0].message.content or ""
        return content.strip()
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return None


def contract_template(data: Dict[str, str]) -> str:
    """Шаблон договора на случай, если ИИ недоступен."""
    return f"""ДОГОВОР {data.get('Тип_договора', '').upper() or '___'}

г. ____________________       «___» __________ 20___ г.

{data.get('Стороны') or '___'} заключили настоящий договор (далее — «Договор») о нижеследующем.

1. ПРЕДМЕТ ДОГОВОРА

1.1. По настоящему Договору {data.get('Стороны') or 'Стороны'} обязуются осуществить: 
{data.get('Предмет') or '___'}.

2. СРОКИ И ПОРЯДОК ИСПОЛНЕНИЯ

2.1. Срок исполнения обязательств: {data.get('Сроки') or '___'}.
2.2. Порядок оплаты: {data.get('Оплата') or '___'}.

3. ОТВЕТСТВЕННОСТЬ СТОРОН

3.1. За нарушение обязательств Стороны несут ответственность в соответствии с действующим законодательством РФ и условиями настоящего Договора.
{data.get('Особые_условия') or '3.2. Дополнительные условия ответственности Сторон могут быть согласованы отдельно.'}

4. РАЗРЕШЕНИЕ СПОРОВ

4.1. Споры и разногласия, возникающие из настоящего Договора или в связи с ним, Стороны стремятся урегулировать путём переговоров.
4.2. При недостижении соглашения спор подлежит рассмотрению в суде в порядке, установленном действующим законодательством РФ.

5. СРОК ДЕЙСТВИЯ ДОГОВОРА

5.1. Договор вступает в силу с момента его подписания Сторонами и действует до полного исполнения обязательств.

6. ПРОЧИЕ УСЛОВИЯ

6.1. Все изменения и дополнения к настоящему Договору действительны при условии их совершения в письменной форме и подписания Сторонами.
6.2. Во всём остальном, что не урегулировано настоящим Договором, Стороны руководствуются действующим законодательством РФ.

7. РЕКВИЗИТЫ И ПОДПИСИ СТОРОН

Сторона 1: __________________________
Сторона 2: __________________________

Подписи:

_________________/________________/
_________________/________________/
"""


def claim_template(data: Dict[str, str]) -> str:
    """Шаблон претензии на случай, если ИИ недоступен."""
    return f"""Кому: {data.get('Адресат') or '___'}
От кого: ______________________________

ПРЕТЕНЗИЯ

Основание отношений: {data.get('Основание') or '___'}.

НАРУШЕНИЕ ОБЯЗАТЕЛЬСТВ

{data.get('Нарушение и обстоятельства') or '___'}

ТРЕБОВАНИЯ

{data.get('Требования') or '___'}

СРОК ДЛЯ ДОБРОВОЛЬНОГО УДОВЛЕТВОРЕНИЯ ТРЕБОВАНИЙ

Прошу исполнить вышеуказанные требования в срок {data.get('Сроки исполнения') or '___'} с момента получения настоящей претензии.

В случае неисполнения требований в указанный срок оставляю за собой право обратиться в суд за защитой своих прав и законных интересов, а также взысканием убытков и неустойки.

Контактные данные для связи:
{data.get('Контакты') or '___'}

Подпись: ____________________   Дата: «___» __________ 20___ г.
"""


# -------------------------------------------------
# Обработчики сценариев
# -------------------------------------------------
async def handle_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = {
        "Тип_договора": payload.get("Тип_договора") or payload.get("Тип договора") or "",
        "Стороны": payload.get("Стороны") or "",
        "Предмет": payload.get("Предмет") or "",
        "Сроки": payload.get("Сроки") or payload.get("Сроки и оплата") or "",
        "Оплата": payload.get("Оплата") or "",
        "Особые_условия": payload.get("Особые_условия") or payload.get("Особые условия") or "",
    }

    payload_str = json.dumps(fields, ensure_ascii=False, indent=2)
    text = await call_groq(PROMPT_CONTRACT, payload_str)

    if not text or len(text) < 200:
        logger.info("Используем шаблонный текст договора (ИИ недоступен или дал пустой ответ)")
        text = contract_template(fields)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"contract_{timestamp}.pdf"
    filepath = os.path.join(FILES_DIR, filename)
    create_pdf_from_text(text, filepath)

    file_url = f"{BASE_URL}/files/{filename}"
    reply_text = (
        "Черновик договора подготовлен. Файл можно скачать по ссылке ниже.\n\n"
        f"{file_url}\n\n"
        "Важно: это примерный черновик, сформированный ИИ. "
        "Перед использованием обязательно проверь текст и, по возможности, согласуй его с юристом."
    )
    logger.info("Scenario=contract: pdf=%s", filename)
    return {"reply_text": reply_text, "file_url": file_url, "scenario": "contract"}


async def handle_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = {
        "Адресат": payload.get("Адресат") or "",
        "Основание": payload.get("Основание") or "",
        "Нарушение и обстоятельства": payload.get("Нарушение и обстоятельства") or "",
        "Требования": payload.get("Требования") or "",
        "Сроки исполнения": payload.get("Сроки исполнения") or "",
        "Контакты": payload.get("Контакты") or "",
    }

    payload_str = json.dumps(fields, ensure_ascii=False, indent=2)
    text = await call_groq(PROMPT_CLAIM, payload_str)

    if not text or len(text) < 150:
        logger.info("Используем шаблонный текст претензии (ИИ недоступен или дал пустой ответ)")
        text = claim_template(fields)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"claim_{timestamp}.pdf"
    filepath = os.path.join(FILES_DIR, filename)
    create_pdf_from_text(text, filepath)

    file_url = f"{BASE_URL}/files/{filename}"
    reply_text = (
        "Черновик претензии подготовлен. Файл можно скачать по ссылке ниже.\n\n"
        f"{file_url}\n\n"
        "Важно: это примерный черновик, сформированный ИИ. "
        "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
    )
    logger.info("Scenario=claim: pdf=%s", filename)
    return {"reply_text": reply_text, "file_url": file_url, "scenario": "claim"}


async def handle_clause(payload: Dict[str, Any]) -> Dict[str, Any]:
    clause_text = (
        payload.get("Текст") or
        payload.get("Пункт") or
        payload.get("Описание") or
        ""
    )

    if not clause_text.strip():
        return {
            "reply_text": "Пока нет текста пункта. Пришли формулировку, которую нужно разобрать.",
            "scenario": "clause",
        }

    answer = await call_groq(PROMPT_CLAUSE, clause_text)
    if not answer:
        answer = (
            "Сейчас я не могу обратиться к нейросети. "
            "Попробуй ещё раз чуть позже или сформулируй вопрос иначе."
        )

    logger.info("Scenario=clause: ответ сгенерирован")
    return {"reply_text": answer, "scenario": "clause"}


# -------------------------------------------------
# FastAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.9.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    scenario = payload.get("scenario") or payload.get("Сценарий") or "contract"

    if scenario == "contract":
        return await handle_contract(payload)
    elif scenario == "claim":
        return await handle_claim(payload)
    elif scenario == "clause":
        return await handle_clause(payload)
    else:
        logger.info("Неизвестный scenario=%s, используем contract по умолчанию", scenario)
        return await handle_contract(payload)


@app.get("/files/{filename}")
async def download_file(filename: str):
    filepath = os.path.join(FILES_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        filepath,
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
