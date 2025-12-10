import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import FileResponse
from groq import Groq

# ----------------- Логгер -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
logger.addHandler(handler)

# ----------------- Конфиг -----------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"

# домен для сборки ссылок на файлы
BASE_URL = os.getenv("BASE_URL", "https://legalfox.up.railway.app")

# папка для файлов PDF
FILES_DIR = Path("files")
FILES_DIR.mkdir(exist_ok=True)

# ----------------- Клиент Groq -----------------
client: Optional[Groq] = None
if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        logger.info("Groq client инициализирован")
    except Exception:
        logger.exception("Не удалось инициализировать Groq client")
else:
    logger.warning("GROQ_API_KEY не задан — Groq недоступен")


# ----------------- Вспомогательные функции -----------------
def parse_bool(value: Any, default: bool = False) -> bool:
    """
    Превращаем строку/число из BotHelp в bool.
    Все варианты вида '1', 'true', 'yes', 'да', 'paid' считаем True.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "y", "да", "paid", "подписка", "подписчик"}


async def call_groq(system_prompt: str, user_content: str) -> str:
    """
    Вызов Groq. Если не настроен — бросаем исключение, чтобы наверху
    можно было вернуть аккуратный fallback.
    """
    if client is None:
        raise RuntimeError("Groq client is not available")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content.strip()},
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


# ----------------- Промты -----------------
PROMPT_CONTRACT = """
Ты — ИИ-помощник по составлению гражданско-правовых договоров в РФ.

Твоя задача — на основе введённых пользователем данных подготовить ЧЕРНОВИК договора
в простой, но юридически аккуратной форме. Ты НЕ заменяешь юриста.

Требования к ответу:
- язык: русский;
- стиль: официально-деловой, но понятный обычному человеку;
- не указывай конкретные статьи закона, только если их явно не попросили;
- не придумывай условия, о которых пользователь не писал, кроме базовых
  (предмет, срок, порядок оплаты, ответственность, форс-мажор).

СТРУКТУРА ЧЕРНОВИКА:
1. «шапка» договора с указанием сторон и города;
2. раздел «Предмет договора»;
3. раздел «Права и обязанности сторон»;
4. раздел «Порядок расчётов»;
5. раздел «Срок действия договора»;
6. раздел «Ответственность сторон» (кратко и без излишней жёсткости);
7. раздел «Порядок разрешения споров»;
8. заключительная часть: реквизиты сторон и место для подписи.

Важно:
- делай аккуратные абзацы с пустыми строками между разделами;
- не ставь вымышленные реквизиты, оставляй вместо этого подчёркивания
  или фразы вроде «(указать реквизиты)»;
- в конце добавь короткое предупреждение, что это примерный черновик и
  перед подписанием его стоит проверить с юристом.
"""

PROMPT_CLAIM = """
Ты — ИИ-помощник, который готовит черновики ПРЕТЕНЗИЙ (досудебных требований) в РФ.

Твоя задача — на основе введённых пользователем данных составить аккуратный,
понятный обычному человеку текст претензии.

Требования:
- язык: русский;
- стиль: вежливый, официально-деловой, без угроз и агрессии;
- структура: шапка (адресат, заявитель), вводная часть, описание нарушения,
  требования, срок исполнения требований, завершающая часть (подпись/контакты).

Не указывай конкретные статьи закона, если пользователь сам их не привёл.
В конце одним предложением напомни, что это примерный черновик и перед отправкой
желательно согласовать текст с юристом.
"""

PROMPT_CLAUSE = """
Ты — ИИ-помощник юристу. Пользователь присылает один или несколько пунктов договора
или описывает ситуацию своими словами. Твоя задача:
- кратко оценить риски этих формулировок;
- предложить 1–2 более удачные формулировки;
- писать по-русски, без чрезмерной юридической тяжеловесности;
- если текста мало или он непонятен, сначала попроси уточнения.

Никогда не обещай, что формулировка «идеальна» или гарантирует результат.
"""


# ----------------- Генерация PDF -----------------
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def _make_pdf(filename: Path, title: str, body: str) -> None:
    """
    Простейший генератор PDF: один шрифт, базовая разметка, перенос по строкам.
    """
    c = canvas.Canvas(str(filename), pagesize=A4)
    width, height = A4

    # базовый шрифт с поддержкой кириллицы — DejaVuSans
    font_path = Path("fonts") / "DejaVuSans.ttf"
    if font_path.exists():
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        pdfmetrics.registerFont(TTFont("DejaVuSans", str(font_path)))
        font_name = "DejaVuSans"
    else:
        font_name = "Helvetica"

    c.setTitle(title)
    c.setFont(font_name, 12)

    left_margin = 40
    top_margin = height - 40
    line_height = 16

    y = top_margin
    for line in body.splitlines():
        if y < 60:  # новая страница
            c.showPage()
            c.setFont(font_name, 12)
            y = top_margin
        c.drawString(left_margin, y, line)
        y -= line_height

    c.showPage()
    c.save()


def create_contract_pdf(text: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"contract_{ts}.pdf"
    filepath = FILES_DIR / filename
    _make_pdf(filepath, "Договор", text)
    logger.info("PDF договора создан: %s", filepath)
    return filename


def create_claim_pdf(text: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"claim_{ts}.pdf"
    filepath = FILES_DIR / filename
    _make_pdf(filepath, "Претензия", text)
    logger.info("PDF претензии создан: %s", filepath)
    return filename


# ----------------- FastAPI -----------------
app = FastAPI(
    title="LegalFox API (Groq, Railway)",
    description="Backend для LegalFox — ИИ-помощника юристам/клиентам",
    version="0.9.0",
)


@app.post("/legalfox")
async def legalfox_endpoint(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """
    Главная точка, куда шлёт запрос BotHelp.
    Ключевые поля:
    - scenario: "contract" | "claim" | "clause"
    - with_file: true/false  (нужно ли делать PDF и отдавать file_url)

    Для contract/claim при with_file=false PDF НЕ создаём и file_url даём пустым.
    """
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario = (payload.get("scenario") or "").strip().lower() or "contract"
    with_file = parse_bool(payload.get("with_file"), default=False)

    # ----- Черновик договора -----
    if scenario == "contract":
        contract_type = payload.get("Тип_договора") or payload.get("Тип договора") or ""
        parties = payload.get("Стороны") or ""
        subject = payload.get("Предмет") or ""
        terms = payload.get("Сроки") or ""
        payment = payload.get("Оплата") or ""
        special = payload.get("Особые_условия") or payload.get("Особые условия") or ""

        user_content = (
            f"Тип договора: {contract_type}\n"
            f"Стороны: {parties}\n"
            f"Предмет: {subject}\n"
            f"Сроки: {terms}\n"
            f"Оплата: {payment}\n"
            f"Особые условия/риски: {special}"
        )

        try:
            draft_text = await call_groq(PROMPT_CONTRACT, user_content)
        except Exception as e:
            logger.exception("Groq API error (contract): %s", e)
            return {
                "reply_text": (
                    "Сейчас я не могу обратиться к нейросети. "
                    "Попробуй переформулировать запрос или повторить чуть позже."
                ),
                "file_url": "",
                "scenario": "contract",
            }

        # текст, который всегда показываем
        reply_text = (
            "Черновик договора подготовлен.\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед подписанием обязательно проверь текст и, по возможности, "
            "согласуй его с юристом."
        )

        # делаем PDF только если явно попросили
        if with_file:
            filename = create_contract_pdf(draft_text)
            file_url = f"{BASE_URL}/files/{filename}"
        else:
            file_url = ""

        return {
            "reply_text": reply_text,
            "draft_text": draft_text,  # можно вывести отдельным блоком, если нужно
            "file_url": file_url,
            "scenario": "contract",
        }

    # ----- Черновик претензии -----
    if scenario == "claim":
        addressee = payload.get("Адресат") or ""
        basis = payload.get("Основание") or ""
        facts = payload.get("Нарушение_и_обстоятельства") or payload.get("Нарушение и обстоятельства") or ""
        demands = payload.get("Требования") or ""
        deadline = payload.get("Срок_исполнения") or payload.get("Сроки исполнения") or ""
        contacts = payload.get("Контакты") or ""

        user_content = (
            f"Адресат: {addressee}\n"
            f"Основание претензии: {basis}\n"
            f"Суть нарушения и обстоятельства: {facts}\n"
            f"Требования заявителя: {demands}\n"
            f"Срок для добровольного исполнения требований: {deadline}\n"
            f"Контактные данные заявителя: {contacts}"
        )

        try:
            draft_text = await call_groq(PROMPT_CLAIM, user_content)
        except Exception as e:
            logger.exception("Groq API error (claim): %s", e)
            return {
                "reply_text": (
                    "Сейчас я не могу обратиться к нейросети. "
                    "Попробуй переформулировать запрос или повторить чуть позже."
                ),
                "file_url": "",
                "scenario": "claim",
            }

        reply_text = (
            "Черновик претензии подготовлен.\n\n"
            "Важно: это примерный черновик, сформированный ИИ. "
            "Перед отправкой обязательно проверь текст и, по возможности, "
            "согласуй его с юристом."
        )

        if with_file:
            filename = create_claim_pdf(draft_text)
            file_url = f"{BASE_URL}/files/{filename}"
        else:
            file_url = ""

        return {
            "reply_text": reply_text,
            "draft_text": draft_text,
            "file_url": file_url,
            "scenario": "claim",
        }

    # ----- Анализ пунктов договора / помощь с формулировками -----
    if scenario == "clause":
        clause_text = payload.get("Текст") or payload.get("text") or ""
        if not clause_text.strip():
            return {
                "reply_text": "Пока нет данных. Напиши текст/описание, с которым нужно помочь.",
                "file_url": "",
                "scenario": "clause",
            }

        try:
            answer = await call_groq(PROMPT_CLAUSE, clause_text)
        except Exception as e:
            logger.exception("Groq API error (clause): %s", e)
            return {
                "reply_text": (
                    "Сейчас я не могу обратиться к нейросети. "
                    "Попробуй переформулировать запрос или повторить чуть позже."
                ),
                "file_url": "",
                "scenario": "clause",
            }

        return {
            "reply_text": answer,
            "file_url": "",
            "scenario": "clause",
        }

    # ----- Неизвестный сценарий -----
    logger.info("Неизвестный сценарий: %s", scenario)
    return {
        "reply_text": "Я не понял, какой сценарий запустить. Попробуй ещё раз из меню.",
        "file_url": "",
        "scenario": scenario,
    }


@app.get("/files/{filename}")
async def download_file(filename: str):
    """
    Отдаём PDF-файлы по HTTP, чтобы BotHelp мог взять ссылку.
    """
    filepath = FILES_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(str(filepath), media_type="application/pdf", filename=filename)


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "LegalFox backend is running"}
