import os
import logging
from datetime import datetime
from textwrap import wrap
from typing import Any, Dict

from fastapi import FastAPI, Body, HTTPException, Request
from fastapi.responses import FileResponse
from groq import Groq

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ----------------- ЛОГГЕР -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# ----------------- КОНФИГ -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

BASE_DIR = os.path.dirname(__file__)
FILES_DIR = os.path.join(BASE_DIR, "files")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
FONT_PATH = os.path.join(FONTS_DIR, "DejaVuSans.ttf")
PDF_FONT_NAME = "DejaVuSans"

os.makedirs(FILES_DIR, exist_ok=True)

# Регистрируем шрифт для кириллицы
try:
    if os.path.exists(FONT_PATH):
        pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, FONT_PATH))
        logger.info("Шрифт %s зарегистрирован", PDF_FONT_NAME)
    else:
        logger.warning("Шрифт %s не найден по пути %s", PDF_FONT_NAME, FONT_PATH)
except Exception:
    logger.exception("Не удалось зарегистрировать шрифт для PDF")

# ----------------- FALLBACK -----------------
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. Попробуй переформулировать запрос "
    "или повторить чуть позже."
)

# ----------------- ПРОМТЫ ДЛЯ GROQ -----------------

PROMPT_CLAIM = """
Ты — юридический ассистент по российскому праву.

Тебе передают черновые данные для претензии (досудебной).
Нужно на их основе СФОРМИРОВАТЬ ЧЕРНОВИК ТЕКСТА ПРЕТЕНЗИИ.

Требования к ответу:
- язык: русский, деловой, без лишней воды;
- ориентируйся на ГК РФ и общую деловую практику;
- НЕ придумывай конкретные статьи, если в описании их нет, но можешь аккуратно ссылаться на общие нормы (“в соответствии с действующим законодательством РФ”);
- текст должен выглядеть как готовая претензия, которую юрист может чуть допилить и отправить.

Структура:
1) “Шапка” (к кому, от кого — на основе входных данных).
2) Описание договора / основания отношений.
3) Описание нарушений и обстоятельств.
4) Требования заявителя.
5) Срок для добровольного исполнения.
6) Указание на возможные последствия при неисполнении (суд, взыскание неустойки и т.п.).
7) Место для подписи и контактных данных.

Пожалуйста, выдай ОДИН цельный текст претензии, без Markdown, без списков с * и #.
"""

PROMPT_CLAUSE = """
Ты — юридический ассистент по российскому праву.

Тебе присылают фрагмент договора (один пункт или несколько).
Нужно:
1) кратко объяснить простым языком, о чём этот пункт и к каким последствиям он ведёт;
2) указать, какие риски он может создавать для стороны, которая спрашивает;
3) при необходимости предложить более безопасную формулировку.

Требования:
- отвечай по-русски;
- не используй Markdown и сложное форматирование;
- будь максимально практичным и прикладным, избегай лишней теории.

Сначала сделай “Краткое объяснение: ...”, затем “Риски: ...”, затем “Как можно улучшить: ...”.
"""

# ----------------- КЛИЕНТ GROQ -----------------
client: Groq | None = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
        client = None
else:
    logger.warning("GROQ_API_KEY не задан — сценарии с нейросетью будут возвращать fallback")


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq. Если что-то идёт не так — бросаем исключение,
    выше это поймаем и уйдём в fallback.
    """
    if not client:
        raise RuntimeError("Groq client не инициализирован")

    messages = [
        {"role": "system", "content": system_prompt.strip()},
        {"role": "user", "content": user_query.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.5,
        max_tokens=800,
        top_p=1,
    )
    content = chat_completion.choices[0].message.content or ""
    return content.strip()


async def generate_reply(system_prompt: str, query: str, scenario: str) -> Dict[str, str]:
    safe_scenario = scenario or "generic"

    if not query or not query.strip():
        return {
            "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
            "scenario": safe_scenario,
        }

    try:
        if client is None:
            raise RuntimeError("Groq client is not available")

        answer = await call_groq(system_prompt, query)
        logger.info("Успешный ответ от Groq для сценария %s", safe_scenario)
        return {
            "reply_text": answer,
            "scenario": safe_scenario,
        }

    except Exception as e:
        logger.exception("Groq API error: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": safe_scenario,
        }

# ----------------- УТИЛЫ ДЛЯ КОНТРАКТА -----------------


def get_field(payload: Dict[str, Any], *keys: str, default: str = "") -> str:
    """
    Берём значение по одному из возможных ключей (с пробелами/подчёркиваниями).
    """
    for key in keys:
        if key in payload and payload[key]:
            return str(payload[key]).strip()
    return default


def build_contract_text_from_fields(payload: Dict[str, Any]) -> str:
    """
    Собираем ОФИЦИАЛЬНЫЙ шаблон договора из уже существующих полей BotHelp.
    Без вызова нейросети.
    """
    raw_type = get_field(payload, "Тип_договора", "Тип договора")
    parties = get_field(payload, "Стороны")
    subject = get_field(payload, "Предмет")
    joint_terms = get_field(payload, "Сроки и оплата")
    terms = get_field(payload, "Сроки")
    payment = get_field(payload, "Оплата")
    special = get_field(payload, "Особые_условия", "Особые условия")

    # Нормализация типа договора для заголовка
    t = (raw_type or "").lower()
    if "услуг" in t:
        title = "ДОГОВОР ОКАЗАНИЯ УСЛУГ"
    elif "постав" in t:
        title = "ДОГОВОР ПОСТАВКИ"
    elif "аренд" in t or "лизинг" in t:
        title = "ДОГОВОР АРЕНДЫ"
    else:
        title = "ДОГОВОР"

    # Если в БотХелпе только одно поле "Сроки и оплата" — используем его как общий блок
    if joint_terms and not (terms or payment):
        terms_text = joint_terms
        payment_text = "Порядок расчётов определяется условиями, указанными в разделе \"Сроки и оплата\" выше."
    else:
        terms_text = terms or joint_terms
        payment_text = payment or ""

    if not subject:
        subject = "Стороны определяют предмет договора дополнительно в приложении к договору."
    if not terms_text:
        terms_text = "Сроки исполнения обязательств Стороны согласуют дополнительно в письменной форме."
    if not payment_text:
        payment_text = "Порядок расчётов Стороны согласуют дополнительно в письменной форме."
    if not special:
        special = "Особых условий помимо изложенных в настоящем договоре Стороны не устанавливают."

    if not parties:
        parties_block = "Стороны договора."
    else:
        parties_block = parties

    text = f"""
{title}

г. ______________________                          «___» __________ 20__ г.

{parties_block},
именуемые в дальнейшем "Стороны",
заключили настоящий договор о нижеследующем.

1. ПРЕДМЕТ ДОГОВОРА
1.1. {subject}

2. СРОКИ И ПОРЯДОК ИСПОЛНЕНИЯ
2.1. {terms_text}

3. ЦЕНА И ПОРЯДОК РАСЧЁТОВ
3.1. {payment_text}

4. ОТВЕТСТВЕННОСТЬ СТОРОН
4.1. Стороны несут ответственность за неисполнение или ненадлежащее исполнение обязательств по настоящему договору в соответствии с действующим законодательством Российской Федерации.
4.2. {special}

5. ЗАКЛЮЧИТЕЛЬНЫЕ ПОЛОЖЕНИЯ
5.1. Настоящий договор вступает в силу с момента его подписания Сторонами.
5.2. Договор может быть изменён или расторгнут по соглашению Сторон, а также в иных случаях, предусмотренных действующим законодательством РФ.
5.3. Договор составлен в двух экземплярах, имеющих одинаковую юридическую силу, по одному для каждой из Сторон.

РЕКВИЗИТЫ И ПОДПИСИ СТОРОН:

{parties_block}

_____________________ /__________________/
_____________________ /__________________/
""".strip()

    return text


def create_pdf_from_text(text: str) -> str:
    """
    Рендерим многостраничный PDF с простым переносом строк.
    Возвращаем имя файла.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"contract_{timestamp}.pdf"
    filepath = os.path.join(FILES_DIR, filename)

    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    # Отступы
    left_margin = 40
    top_margin = height - 40
    bottom_margin = 40

    # Шрифт
    font_name = PDF_FONT_NAME if PDF_FONT_NAME in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    c.setFont(font_name, 11)

    y = top_margin
    line_height = 14

    for paragraph in text.split("\n"):
        para = paragraph.rstrip()
        if not para:
            y -= line_height  # пустая строка
            if y < bottom_margin:
                c.showPage()
                c.setFont(font_name, 11)
                y = top_margin
            continue

        # перенос по ширине страницы
        max_chars = 100
        for line in wrap(para, max_chars):
            if y < bottom_margin:
                c.showPage()
                c.setFont(font_name, 11)
                y = top_margin
            c.drawString(left_margin, y, line)
            y -= line_height

    c.save()
    logger.info("PDF создан: %s", filepath)
    return filename


async def handle_contract(payload: Dict[str, Any], request: Request) -> Dict[str, str]:
    """
    Обработка сценария 'contract':
    - собираем шаблонный текст договора;
    - делаем PDF;
    - возвращаем ссылку на файл.
    """
    contract_text = build_contract_text_from_fields(payload)
    filename = create_pdf_from_text(contract_text)

    base_url = str(request.base_url).rstrip("/")
    file_url = f"{base_url}/files/{filename}"

    logger.info("Scenario=contract, файл=%s", filename)
    return {
        "reply_text": "Готово! Я собрал черновик, лови файл:",
        "file_url": file_url,
        "scenario": "contract",
    }

# ----------------- FASTAPI -----------------

app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="0.3.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...), request: Request = None) -> Dict[str, Any]:
    """
    Главная точка, куда шлёт запрос BotHelp.
    Поле 'scenario' уже настроено в БотХелпе:
    - 'contract' — собрать черновик договора + PDF;
    - 'claim' — черновик претензии (текст);
    - 'clause' — разбор пункта договора (текст);
    - при других значениях вернётся fallback.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    scenario = (payload.get("scenario") or "").strip() or "contract"

    if scenario == "contract":
        # ТОЛЬКО шаблон + PDF, без нейросети
        return await handle_contract(payload, request)

    elif scenario == "claim":
        # Собираем описание для промта
        addr = get_field(payload, "Адресат")
        base = get_field(payload, "Основание")
        facts = get_field(payload, "Нарушение_и_обстоятельства", "Нарушение и обстоятельства")
        demands = get_field(payload, "Требования")
        deadline = get_field(payload, "Срок_исполнения", "Сроки исполнения")
        contacts = get_field(payload, "Контакты")

        query = f"""Адресат: {addr}
Основание отношений/договора: {base}
Нарушения и обстоятельства: {facts}
Требования заявителя: {demands}
Срок исполнения требований: {deadline}
Контакты для связи: {contacts}"""

        return await generate_reply(PROMPT_CLAIM, query, "claim")

    elif scenario == "clause":
        clause_text = get_field(payload, "Текст", "Фрагмент", default="")
        return await generate_reply(PROMPT_CLAUSE, clause_text, "clause")

    else:
        logger.info("Неизвестный сценарий: %s", scenario)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": scenario,
        }


@app.get("/files/{filename}")
async def download_file(filename: str):
    """
    Отдаём PDF-файл по HTTP, чтобы BotHelp мог взять ссылку.
    """
    file_path = os.path.join(FILES_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
