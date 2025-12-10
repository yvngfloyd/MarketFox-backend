import os
import logging
from datetime import datetime
from textwrap import wrap
from typing import Any, Dict, Optional

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
# Стабильная модель Groq (заменяем старые, которые выпилили)
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

BASE_DIR = os.path.dirname(__file__)
FILES_DIR = os.path.join(BASE_DIR, "files")
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
os.makedirs(FILES_DIR, exist_ok=True)

# Шрифт для кириллицы
FONT_PATH = os.path.join(FONTS_DIR, "DejaVuSans.ttf")
PDF_FONT_NAME = "DejaVuSans"

try:
    if os.path.exists(FONT_PATH):
        pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, FONT_PATH))
        logger.info("Шрифт %s зарегистрирован (%s)", PDF_FONT_NAME, FONT_PATH)
    else:
        logger.warning("Файл шрифта не найден: %s", FONT_PATH)
except Exception:
    logger.exception("Не удалось зарегистрировать шрифт для PDF")

# ----------------- FALLBACK -----------------
FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. "
    "Попробуй повторить запрос чуть позже или переформулировать задачу."
)

# ----------------- ПРОМПТЫ -----------------

PROMPT_CONTRACT_UNIVERSAL = """
Ты — ИИ-юрист LegalFox.

Тебе передают краткое описание будущего договора по российскому праву в формате:
"Тип договора: ..."
"Стороны: ..."
"Предмет: ..."
"Сроки и оплата: ..."
"Оплата: ..."
"Особые условия: ..."

1. По полю "Тип договора" ОПРЕДЕЛИ, какой тип отношений:
   - если содержит "услуг" — используй структуру ДОГОВОРА ОКАЗАНИЯ УСЛУГ;
   - если содержит "постав" — структуру ДОГОВОРА ПОСТАВКИ;
   - если содержит "аренд" или "лизинг" — структуру ДОГОВОРА АРЕНДЫ;
   - если содержит "подряд" — структуру ДОГОВОРА ПОДРЯДА;
   - если содержит "конфиденц", "NDA", "неразглаш" — структуру СОГЛАШЕНИЯ О КОНФИДЕНЦИАЛЬНОСТИ;
   - если тип не очевиден — используй нейтральный шаблон "ДОГОВОР".

2. СФОРМИРУЙ текст договора в классическом виде по праву РФ.

Общие требования:
- язык: русский, деловой, без лишней воды;
- никаких пояснений, комментариев и обращений к пользователю;
- выведи ТОЛЬКО чистый текст договора, который можно распечатать и подписать;
- не используй Markdown и спецразметку (никаких *, #, списков с точками);
- разделы договора нумеруй: 1., 1.1., 2., 2.1. и т.п.;
- раздел "Реквизиты и подписи сторон" обязателен в конце.

Примеры структур (ориентируйся, но можешь адаптировать под тип договора):

Для договора оказания услуг:
1. ПРЕДМЕТ ДОГОВОРА
2. СРОКИ ОКАЗАНИЯ УСЛУГ
3. ПРАВА И ОБЯЗАННОСТИ СТОРОН
4. СТОИМОСТЬ УСЛУГ И ПОРЯДОК РАСЧЁТОВ
5. ОТВЕТСТВЕННОСТЬ СТОРОН
6. ФОРС-МАЖОР
7. СРОК ДЕЙСТВИЯ И ПОРЯДОК РАСТОРЖЕНИЯ
8. ПРОЧИЕ УСЛОВИЯ
9. РЕКВИЗИТЫ И ПОДПИСИ СТОРОН

Для договора поставки:
1. ПРЕДМЕТ ДОГОВОРА
2. ПОРЯДОК ПОСТАВКИ ТОВАРА
3. КАЧЕСТВО И КОМПЛЕКТНОСТЬ ТОВАРА
4. ЦЕНА ТОВАРА И ПОРЯДОК РАСЧЁТОВ
5. ПЕРЕХОД ПРАВА СОБСТВЕННОСТИ И РИСКА
6. ОТВЕТСТВЕННОСТЬ СТОРОН
7. ФОРС-МАЖОР
8. СРОК ДЕЙСТВИЯ И ПОРЯДОК РАСТОРЖЕНИЯ
9. ПРОЧИЕ УСЛОВИЯ
10. РЕКВИЗИТЫ И ПОДПИСИ СТОРОН

Для договора аренды:
1. ПРЕДМЕТ ДОГОВОРА
2. ПОРЯДОК ПЕРЕДАЧИ И ВОЗВРАТА ИМУЩЕСТВА
3. АРЕНДНАЯ ПЛАТА И ПОРЯДОК РАСЧЁТОВ
4. ПРАВА И ОБЯЗАННОСТИ СТОРОН
5. ОТВЕТСТВЕННОСТЬ СТОРОН
6. СОДЕРЖАНИЕ ИМУЩЕСТВА И РЕМОНТ
7. СРОК ДЕЙСТВИЯ ДОГОВОРА И ПОРЯДОК РАСТОРЖЕНИЯ
8. ПРОЧИЕ УСЛОВИЯ
9. РЕКВИЗИТЫ И ПОДПИСИ СТОРОН

Данные из описания используй так:
- "Предмет" — в разделе о предмете договора;
- "Сроки и оплата" / "Сроки" / "Оплата" — в разделах о сроках и расчётах;
- "Особые условия" — добавь там, где логично: ответственность, прочие условия, специальные оговорки.

Ещё раз: выведи ТОЛЬКО текст договора, без комментариев и пояснений.
"""

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

PROMPT_QA = """
Ты — LegalFox, ИИ-ассистент для юристов и предпринимателей в России.

Отвечай на вопросы пользователя:
- по российскому праву (общие разъяснения, не конкретная правовая позиция в суде),
- по структуре документов (какие разделы, как формулировать),
- по рабочим процессам юриста (как оптимизировать, что автоматизировать).

Требования:
- отвечай по-русски;
- не выдавай ответ как 100% юридическое заключение, это именно справка/ориентир;
- можешь рекомендовать обратиться к юристу для финальной проверки важных документов;
- не используй Markdown, списки со звёздочками и т.д., просто аккуратный текст с абзацами.
"""

# ----------------- ИНИЦИАЛИЗАЦИЯ GROQ -----------------
client: Optional[Groq] = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован, модель: %s", GROQ_MODEL)
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
        client = None
else:
    logger.warning("GROQ_API_KEY не задан — все сценарии будут уходить в fallback")

# ----------------- УТИЛИТЫ -----------------


def get_field(payload: Dict[str, Any], *keys: str, default: str = "") -> str:
    """
    Берём значение по одному из возможных ключей (с пробелами/подчёркиваниями).
    """
    for key in keys:
        if key in payload and payload[key]:
            return str(payload[key]).strip()
    return default


async def call_groq(system_prompt: str, user_query: str) -> str:
    """
    Вызов Groq. Если что-то идёт не так — кидаем исключение.
    """
    if not client:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt.strip()},
        {"role": "user", "content": user_query.strip()},
    ]
    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=2000,
        top_p=1,
    )
    content = chat_completion.choices[0].message.content or ""
    return content.strip()


def create_pdf_from_text(text: str) -> str:
    """
    Рендерим многостраничный PDF c кириллицей.
    Возвращаем имя файла.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"contract_{ts}.pdf"
    filepath = os.path.join(FILES_DIR, filename)

    c = canvas.Canvas(filepath, pagesize=A4)
    width, height = A4

    left_margin = 40
    top_margin = height - 40
    bottom_margin = 40
    line_height = 14

    font_name = PDF_FONT_NAME if PDF_FONT_NAME in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    c.setFont(font_name, 11)

    y = top_margin

    for paragraph in text.split("\n"):
        para = paragraph.rstrip()
        if not para:
            y -= line_height
            if y < bottom_margin:
                c.showPage()
                c.setFont(font_name, 11)
                y = top_margin
            continue

        # Простой перенос по длине строки
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


async def handle_contract(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """
    Сценарий: генерация договора + PDF.
    Работает ИЗ ОДНОЙ КНОПКИ:
    - если есть поля типа "Тип договора", "Стороны" и т.п. — используем их;
    - если только общее поле "Запрос" — тоже работаем (свободный ввод).
    """
    raw_type = get_field(payload, "Тип_договора", "Тип договора")
    parties = get_field(payload, "Стороны")
    subject = get_field(payload, "Предмет")
    joint_terms = get_field(payload, "Сроки и оплата")
    terms = get_field(payload, "Сроки")
    payment = get_field(payload, "Оплата")
    special = get_field(payload, "Особые_условия", "Особые условия")
    free_query = get_field(payload, "Запрос", "query", "Query")

    # Собираем бриф для промта
    if any([raw_type, parties, subject, joint_terms, terms, payment, special]):
        brief = (
            f"Тип договора: {raw_type or 'не указан'}\n"
            f"Стороны: {parties or 'не указаны'}\n"
            f"Предмет: {subject or 'не указан'}\n"
            f"Сроки и оплата: {joint_terms or terms or 'не указаны'}\n"
            f"Оплата: {payment or 'не указана отдельно'}\n"
            f"Особые условия: {special or 'нет или не указаны'}"
        )
    else:
        # если нет структурированных полей — работаем с общим запросом
        brief = free_query or "Пользователь не дал никакого описания, договор сформировать невозможно."

    try:
        contract_text = await call_groq(PROMPT_CONTRACT_UNIVERSAL, brief)
        filename = create_pdf_from_text(contract_text)

        base_url = str(request.base_url).rstrip("/")
        file_url = f"{base_url}/files/{filename}"

        return {
            "reply_text": "Готово! Я собрал черновик договора, лови файл:",
            "file_url": file_url,
            "scenario": "contract",
        }
    except Exception as e:
        logger.exception("Ошибка при генерации договора: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": "contract",
        }


async def handle_claim(payload: Dict[str, Any]) -> Dict[str, str]:
    adresat = get_field(payload, "Адресат")
    basis = get_field(payload, "Основание")
    facts = get_field(payload, "Нарушение_и_обстоятельства", "Нарушение и обстоятельства")
    demands = get_field(payload, "Требования")
    deadline = get_field(payload, "Срок_исполнения", "Сроки исполнения")
    contacts = get_field(payload, "Контакты")
    free_query = get_field(payload, "Запрос", "query", "Query")

    if any([adresat, basis, facts, demands, deadline, contacts]):
        query = (
            f"Адресат: {adresat}\n"
            f"Основание отношений/договора: {basis}\n"
            f"Нарушения и обстоятельства: {facts}\n"
            f"Требования заявителя: {demands}\n"
            f"Срок исполнения требований: {deadline}\n"
            f"Контакты для связи: {contacts}"
        )
    else:
        query = free_query

    if not query:
        return {
            "reply_text": "Пока нет данных для претензии. Опиши ситуацию и что именно хочешь потребовать.",
            "scenario": "claim",
        }

    try:
        text = await call_groq(PROMPT_CLAIM, query)
        return {
            "reply_text": text,
            "scenario": "claim",
        }
    except Exception as e:
        logger.exception("Ошибка Groq в claim: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": "claim",
        }


async def handle_clause(payload: Dict[str, Any]) -> Dict[str, str]:
    clause_text = get_field(payload, "Текст", "Фрагмент", "Clause", "Запрос", "query", "Query")

    if not clause_text:
        return {
            "reply_text": "Пришли, пожалуйста, пункт договора или фрагмент текста, который нужно разобрать.",
            "scenario": "clause",
        }

    try:
        text = await call_groq(PROMPT_CLAUSE, clause_text)
        return {
            "reply_text": text,
            "scenario": "clause",
        }
    except Exception as e:
        logger.exception("Ошибка Groq в clause: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": "clause",
        }


async def handle_qa(payload: Dict[str, Any]) -> Dict[str, str]:
    question = get_field(payload, "Запрос", "query", "Question", "Вопрос")

    if not question:
        return {
            "reply_text": "Задай вопрос: по договору, претензии, рискам или рабочим процессам юриста.",
            "scenario": "qa",
        }

    try:
        text = await call_groq(PROMPT_QA, question)
        return {
            "reply_text": text,
            "scenario": "qa",
        }
    except Exception as e:
        logger.exception("Ошибка Groq в qa: %s", e)
        return {
            "reply_text": FALLBACK_TEXT,
            "scenario": "qa",
        }

# ----------------- FASTAPI -----------------

app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам",
    version="1.0.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(
    payload: Dict[str, Any] = Body(...),
    request: Request = None,
) -> Dict[str, Any]:
    """
    ОДНА ТОЧКА ВХОДА ДЛЯ ВСЕХ СЦЕНАРИЕВ.

    В BotHelp ты можешь:
    - либо всегда слать scenario="contract"/"claim"/"clause"/"qa";
    - либо слать scenario="auto" или вообще не слать scenario — тогда мы сами решим по данным.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    scenario = (payload.get("scenario") or payload.get("Сценарий") or "").strip().lower()

    # Автоопределение сценария, если scenario пустой/auto
    if not scenario or scenario == "auto":
        # Если есть тип договора или поля по договору — считаем, что это contract
        if any(
            k in payload
            for k in ["Тип_договора", "Тип договора", "Стороны", "Предмет", "Сроки и оплата", "Сроки", "Оплата"]
        ):
            scenario = "contract"
        else:
            # иначе по ключевым словам в тексте
            text_all = (
                get_field(payload, "Запрос", "query", "Question", "Вопрос")
                + " "
                + get_field(payload, "Текст", "Фрагмент", "Clause")
            ).lower()
            if any(word in text_all for word in ["претензи", "досудебн", "претензия", "претензион"]):
                scenario = "claim"
            elif any(word in text_all for word in ["пункт", "клауза", "раздел", "договор", "формулировк"]):
                scenario = "clause"
            else:
                scenario = "qa"

    if scenario == "contract":
        return await handle_contract(payload, request)
    if scenario == "claim":
        return await handle_claim(payload)
    if scenario == "clause":
        return await handle_clause(payload)
    if scenario == "qa":
        return await handle_qa(payload)

    logger.info("Неизвестный сценарий: %s", scenario)
    return {
        "reply_text": FALLBACK_TEXT,
        "scenario": scenario,
    }


@app.get("/files/{filename}")
async def download_file(filename: str):
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
