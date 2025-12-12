import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

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
BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"
FILES_DIR.mkdir(exist_ok=True)

FONTS_DIR = BASE_DIR  # шрифт лежит рядом с main.py
FONT_PATH = FONTS_DIR / "DejaVuSans.ttf"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app")

GROQ_MODEL = "llama-3.1-8b-instant"

FALLBACK_TEXT = (
    "Сейчас я не могу обратиться к нейросети. "
    "Попробуй переформулировать запрос или повторить чуть позже."
)


# -------------------------------------------------
# Промпты
# -------------------------------------------------
PROMPT_CONTRACT = """
Ты — LegalFox, ИИ-ассистент для подготовки черновиков гражданско-правовых договоров в РФ.

Тебе дают ответы обычного человека на простой опросник:
- какой договор он хочет оформить (тип);
- между кем заключается договор (стороны);
- о чём договор (что именно делается/передаётся);
- на какой срок и с какими важными датами;
- как и сколько платят;
- есть ли какие-то особые условия.

Эти ответы могут быть написаны живым, разговорным языком, с примерами и без «юридических» формулировок.
Твоя задача — перевести это в аккуратный, связный ТЕКСТ договора в деловом стиле.

Требования к языку и оформлению:
- Пиши по-русски, юридически нейтрально и понятно для обычного человека.
- Переводи разговорные формулировки в официальный стиль, но без лишнего канцелярита.
- Структурируй текст по пунктам и подпунктам, но без Markdown-разметки: НИКАКИХ **звёздочек**, #заголовков, списков с * и т.п.
- Если пользователь пишет «без особых условий», не выдумывай лишние блоки.
- Не придумывай вымышленные реквизиты и суммы, если их нет во входных данных — используй формулировки вида
  «указать реквизиты сторон», «сумма определяется соглашением сторон».
- НИЧЕГО не пиши про подписки, платный доступ, PDF и т.п.

Формат ОТВЕТА (строго соблюдай):

1) Сначала дай КРАТКОЕ ОПИСАНИЕ документа и его ключевых условий (до 8–10 предложений).
   Это должна быть выжимка по сути: что за договор, между кем, какие основные условия, на что обратить внимание.

2) Затем напиши ОТДЕЛЬНОЙ строкой три дефиса:
   ---

3) После строки с тремя дефисами выведи ПОЛНЫЙ ТЕКСТ черновика договора в деловом стиле,
   так как он должен выглядеть в документе (один цельный текст, без пояснений про структуру ответа).

Не подписывай блоки словами «краткое описание» или «полный текст», не объясняй структуру —
просто выведи сначала краткий обзор, затем строку --- , затем полный текст договора.
"""


PROMPT_CLAIM = """
Ты — LegalFox, ИИ-ассистент по подготовке претензий (досудебных требований) в РФ.

Тебе дают ответы обычного человека на простой опросник:
- кому он хочет написать (магазин, сервис, арендодатель, конкретное лицо);
- что за ситуация/договор/покупка лежит в основе конфликта;
- что именно сделали не так (нарушение и обстоятельства);
- чего он хочет добиться (требования);
- какой срок он считает разумным для добровольного исполнения;
- на кого оформляем претензию и как с ним связаться.

Ответы могут быть свободными, местами эмоциональными и без юридического языка.
Твоя задача — на основе этих ответов подготовить связный текст ПРЕТЕНЗИИ в официально-деловом стиле.

Требования к языку и оформлению:
- Пиши по-русски, юридически аккуратно, но понятно обычному человеку.
- Преобразуй разговорный текст в официальный, убирая эмоции и жаргон.
- Оформи как единый документ в логичном порядке: вводная часть (к кому и от кого),
  описание ситуации и нарушения, требования, срок, заключительная часть.
- Не используй Markdown-разметку и спецсимволы: не нужно **жирного**, списков с *, заголовков с # и т.п.
- Не придумывай конкретные суммы и даты, если их нет во входных данных — используй формулировки вида
  «указать сумму», «указать дату».
- НИЧЕГО не пиши про подписки, платный доступ, PDF и т.п.

Формат ОТВЕТА (строго соблюдай):

1) Сначала дай КРАТКОЕ ОПИСАНИЕ сути претензии и ключевых требований (до 8–10 предложений).
   Опиши человеческим языком: к кому, по какому поводу, что не так и чего заявитель хочет.

2) Затем выведи ОТДЕЛЬНОЙ строкой три дефиса:
   ---

3) После строки с тремя дефисами выведи ПОЛНЫЙ ТЕКСТ черновика претензии как единого документа
   в официально-деловом стиле.

Не подписывай блоки, не объясняй структуру: сначала обзор, затем строка --- , затем полный текст.
"""


PROMPT_CLAUSE = """
Ты — LegalFox, универсальный ИИ-ассистент по юридическим вопросам в РФ.

Пользователь может:
- задать общий вопрос о своей ситуации;
- попросить подсказать, как примерно действовать;
- прислать текст пункта договора или претензии и попросить пояснить/исправить;
- попросить набросать черновик формулировки (письмо, пункт, заявление).

Твоя задача — дать понятное и аккуратное объяснение, но НЕ заменять полноценную юридическую консультацию.

Правила:
- Всегда отвечай по-русски.
- Ориентируйся на законодательство РФ в общем виде, без узкой специализации, если пользователь явно о ней не спрашивает.
- Объясняй простым языком, без лишнего жаргона и канцелярита.
- Не используй Markdown-разметку, не ставь **жирный**, списки с * и т.п.
- Если вопрос неполный или нечёткий — сначала коротко объясни, чего не хватает, и предложи 1–3 уточняющих вопроса.
- Если пользователь прислал именно текст пункта договора, сначала объясни его смысл, затем укажи риски и предложи вариант более удачной формулировки.
- Если просит «написать текст», отдавай именно ЧЕРНОВИК, не называй его окончательным документом.
- Всегда в конце кратко напоминай, что твой ответ — ориентир, а не официальная юридическая консультация.

Формат ответа:

1) Коротко переформулируй суть вопроса пользователя (1–3 предложения).
2) Основная часть:
   - если это общий вопрос/ситуация — дай структурированный разбор и возможные варианты действий, понятными абзацами;
   - если это пункт договора — последовательно выведи:
     «Смысл пункта: …»
     «Риски: …»
     «Можно переписать так: …».
3) В конце добавь 1–2 предложения-предупреждения о том, что законы меняются, многое зависит от документов и фактов,
   и при серьёзных последствиях лучше обратиться к юристу лично.

Не пиши ничего про подписки, PDF, оплату и сам бот.
"""


# -------------------------------------------------
# Groq client
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


def clean_text(text: str) -> str:
    """Убираем простейший Markdown и лишние пробелы."""
    if not text:
        return ""
    cleaned = text.replace("**", "").replace("__", "")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


async def call_groq(system_prompt: str, user_content: str) -> str:
    """Вызов Groq, бросает исключение при ошибке."""
    if client is None:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content.strip()},
    ]

    chat_completion = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.4,
        max_tokens=1500,
        top_p=1,
    )

    content = chat_completion.choices[0].message.content or ""
    return clean_text(content)


def parse_with_file_flag(payload: Dict[str, Any]) -> bool:
    """
    Интерпретируем флаг with_file.
    Любое НЕпустое значение, кроме '0' / 'false' / 'нет' — считаем истиной.
    """
    raw = str(
        payload.get("with_file")
        or payload.get("WithFile")
        or payload.get("premium")
        or payload.get("Premium")
        or ""
    ).strip().lower()

    if raw in ("", "0", "false", "нет", "no", "none", "off"):
        return False
    return True


# -------------------------------------------------
# Генерация PDF
# -------------------------------------------------
def create_pdf_from_text(title: str, body: str, prefix: str) -> str:
    """
    Создаёт простой PDF с заголовком и текстом.
    Возвращает имя файла (без BASE_URL).
    """
    if not FONT_PATH.exists():
        logger.error("Файл шрифта %s не найден", FONT_PATH)
        raise RuntimeError("Font file not found")

    pdf = FPDF(format="A4")
    pdf.add_page()

    pdf.add_font("DejaVu", "", str(FONT_PATH), uni=True)
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("DejaVu", "", 16)
    pdf.multi_cell(0, 10, title, align="C")
    pdf.ln(5)

    pdf.set_font("DejaVu", "", 11)
    for paragraph in body.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            pdf.ln(3)
            continue
        pdf.multi_cell(0, 6, paragraph)
        pdf.ln(2)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts}.pdf"
    filepath = FILES_DIR / filename
    pdf.output(str(filepath))

    logger.info("PDF создан: %s", filepath)
    return filename


# -------------------------------------------------
# Вспомогательная функция: разделение ответа на обзор и полный текст
# -------------------------------------------------
def split_summary_and_full(text: str) -> Tuple[str, str]:
    """
    Делим ответ модели на краткий обзор и полный текст по разделителю '---'.
    Если разделителя нет — считаем весь текст и обзором, и полным текстом.
    """
    parts = text.split("---", 1)
    summary = parts[0].strip()
    full = parts[1].strip() if len(parts) > 1 and parts[1].strip() else summary
    return summary, full


# -------------------------------------------------
# Обработчики сценариев
# -------------------------------------------------
async def handle_contract(payload: Dict[str, Any], with_file: bool) -> Tuple[str, Optional[str]]:
    t = payload.get("Тип_договора") or payload.get("Тип договора") or ""
    s = payload.get("Стороны", "")
    p = payload.get("Предмет", "")
    sr = payload.get("Сроки", "")
    op = payload.get("Оплата", "")
    spec = payload.get("Особые_условия") or payload.get("Особые условия") or ""

    user_desc = (
        f"Тип договора: {t}\n"
        f"Стороны: {s}\n"
        f"Предмет договора: {p}\n"
        f"Сроки: {sr}\n"
        f"Порядок оплаты: {op}\n"
        f"Особые условия и риски: {spec}"
    )

    try:
        raw = await call_groq(PROMPT_CONTRACT, user_desc)
        summary, full_text = split_summary_and_full(raw)
    except Exception as e:
        logger.exception("Groq error in contract: %s", e)
        return FALLBACK_TEXT, None

    if with_file:
        filename = create_pdf_from_text("ДОГОВОР (ЧЕРНОВИК)", full_text, prefix="contract")
        file_url = f"{BASE_URL}/files/{filename}"
        reply = (
            "Черновик договора подготовлен. Оформленный PDF-документ я прикрепил ниже.\n\n"
            "Кратко по сути:\n"
            f"{summary}\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед подписанием обязательно проверь текст и, по возможности, согласуй его с юристом."
        )
        return reply, file_url

    reply = (
        "Краткое описание твоего договора:\n"
        f"{summary}\n\n"
        "Полный текст договора и PDF-документ, оформленный и готовый к печати/отправке, "
        "доступны по подписке LegalFox."
    )
    return reply, None


async def handle_claim(payload: Dict[str, Any], with_file: bool) -> Tuple[str, Optional[str]]:
    addr = payload.get("Адресат", "")
    base = payload.get("Основание", "")
    viol = payload.get("Нарушение_и_обстоятельства") or payload.get("Нарушение и обстоятельства") or ""
    reqs = payload.get("Требования", "")
    term = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
    contacts = payload.get("Контакты", "")

    user_desc = (
        f"Адресат: {addr}\n"
        f"Основание: {base}\n"
        f"Нарушение и обстоятельства: {viol}\n"
        f"Требования заявителя: {reqs}\n"
        f"Срок добровольного исполнения: {term}\n"
        f"Контактные данные заявителя: {contacts}"
    )

    try:
        raw = await call_groq(PROMPT_CLAIM, user_desc)
        summary, full_text = split_summary_and_full(raw)
    except Exception as e:
        logger.exception("Groq error in claim: %s", e)
        return FALLBACK_TEXT, None

    if with_file:
        filename = create_pdf_from_text("ПРЕТЕНЗИЯ (ЧЕРНОВИК)", full_text, prefix="claim")
        file_url = f"{BASE_URL}/files/{filename}"
        reply = (
            "Черновик претензии подготовлен. Оформленный PDF-файл я прикрепил ниже.\n\n"
            "Кратко по сути:\n"
            f"{summary}\n\n"
            "Важно: это примерный черновик претензии, сформированный ИИ. "
            "Перед отправкой обязательно проверь текст и, по возможности, согласуй его с юристом."
        )
        return reply, file_url

    reply = (
        "Краткое описание твоей претензии:\n"
        f"{summary}\n\n"
        "Полный текст претензии и PDF-версия, оформленная и готовая к печати/отправке, "
        "доступны по подписке LegalFox."
    )
    return reply, None


async def handle_clause(payload: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Универсальный режим: любой юридический вопрос / пункт договора / просьба набросать текст.
    """
    clause_text = (
        payload.get("Текст")
        or payload.get("Пункт")
        or payload.get("Clause")
        or payload.get("Запрос")
        or payload.get("Вопрос")
        or ""
    )

    if not clause_text.strip():
        return (
            "Пока нет данных. Напиши, пожалуйста, свой вопрос или вставь текст пункта/договора, "
            "с которым нужно помочь.", 
            None
        )

    try:
        answer = await call_groq(PROMPT_CLAUSE, clause_text)
    except Exception as e:
        logger.exception("Groq error in clause/universal: %s", e)
        return FALLBACK_TEXT, None

    return answer, None


# -------------------------------------------------
# FastAPI
# -------------------------------------------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника по юридическим документам",
    version="1.2.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))
    scenario = str(payload.get("scenario", "contract")).strip().lower()
    with_file = parse_with_file_flag(payload)

    try:
        if scenario == "contract":
            reply_text, file_url = await handle_contract(payload, with_file)
            scenario_name = "contract"
        elif scenario == "claim":
            reply_text, file_url = await handle_claim(payload, with_file)
            scenario_name = "claim"
        elif scenario == "clause":
            reply_text, file_url = await handle_clause(payload)
            scenario_name = "clause"
        else:
            logger.info("Неизвестный сценарий: %s", scenario)
            reply_text = "Пока нет данных. Напиши текст или выбери нужный раздел в меню бота."
            file_url = None
            scenario_name = scenario or "unknown"

    except Exception as e:
        logger.exception("Unexpected error in /legalfox: %s", e)
        reply_text = FALLBACK_TEXT
        file_url = None
        scenario_name = scenario or "error"

    response: Dict[str, Any] = {
        "reply_text": reply_text,
        "scenario": scenario_name,
    }
    if file_url:
        response["file_url"] = file_url

    return response


@app.get("/files/{filename}")
async def download_file(filename: str):
    filepath = FILES_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        path=str(filepath),
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
