import os
import re
import uuid
import time
import sqlite3
import logging
from typing import Any, Dict

import httpx
from fastapi import FastAPI, Body
from fastapi.staticfiles import StaticFiles

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# ----------------- Логгер -----------------
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
if not logger.handlers:
    logger.addHandler(handler)


# ----------------- Конфиг -----------------
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")  # например https://xxx.up.railway.app
FILES_DIR = os.getenv("FILES_DIR", "files")
DB_PATH = os.getenv("DB_PATH", "legalfox.db")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# 1 бесплатный PDF на пользователя
FREE_PDF_LIMIT = int(os.getenv("FREE_PDF_LIMIT", "1"))  # оставь 1

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."


# ----------------- Промты -----------------
PROMPT_CONTRACT = """
Ты — LegalFox, помощник по подготовке черновиков договоров по праву РФ.
Сгенерируй ЧЕРНОВИК ДОГОВОРА для печати (официально-деловой стиль).

Жёсткие правила (обязательно):
1) Без Markdown: никаких #, **, ``` и т.п.
2) Не используй заглушки вида "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ" и т.п.
   Вместо этого:
   - если данных нет — ставь "___"
   - если данные есть — вставляй их как есть, без кавычек.
3) Ничего не выдумывай: паспорт, ИНН, ОГРН, адреса, реквизиты, суммы, сроки — только если они есть во вводе.
   Если нет — "___".
4) Максимально конкретно используй входные данные: тип договора, стороны, предмет, сроки, оплата, особые условия.
5) Пиши как реальный документ РФ:
   - Название сверху: "ДОГОВОР <тип договора>" (если тип договора не указан — "ДОГОВОР")
   - Город: ___   Дата: ___  (если не указаны во вводе)
   - Разделы с нумерацией: 1. Предмет договора, 2. Права и обязанности, 3. Цена и порядок расчётов,
     4. Сроки, 5. Ответственность сторон, 6. Форс-мажор, 7. Порядок разрешения споров,
     8. Срок действия и расторжение, 9. Заключительные положения, 10. Реквизиты и подписи.
6) Форс-мажор: корректная формулировка ("чрезвычайные и непредотвратимые обстоятельства") без странных примеров,
   если пользователь сам их не указал.
7) Суммы/сроки:
   - если сумма/срок не указаны — оставь понятное место: "___ рублей", "___ календарных дней"
   - не пиши случайные числа.
8) Если "особые условия" содержат важные пункты (штрафы, порядок передачи, конфиденциальность, подсудность и т.д.) —
   встрои их в соответствующие разделы.
9) В конце:
   - "Реквизиты и подписи сторон" с аккуратными полями.
   - Для физлиц: ФИО, паспорт: ___, адрес: ___, телефон/email: ___
   - Для юрлиц/ИП: наименование, ИНН/ОГРН(ОГРНИП): ___, адрес, р/с, банк, БИК, к/с: ___
   Определи тип стороны по входным данным (если явно написано ООО/ИП — используй соответствующий блок).
   Если непонятно — сделай универсально: "ФИО/Наименование: ___".
10) Документ должен быть пригоден как черновик и выглядеть аккуратно: короткие абзацы, пустые строки между разделами.
"""

PROMPT_CLAIM = """
Ты — LegalFox, помощник по подготовке претензий по праву РФ (для обычных людей).
Сгенерируй ЧЕРНОВИК ПРЕТЕНЗИИ (досудебной) в официально-деловом стиле.

Жёсткие правила (обязательно):
1) Без Markdown.
2) Не используй заглушки вида "СТОРОНА_1", "АДРЕС_1", "СУММА_1" и т.п.
   Если данных нет — "___". Если есть — вставляй как есть, без кавычек.
3) Ничего не выдумывай: даты, суммы, реквизиты, нормы закона — только если пользователь дал их явно.
   Если не дал — оставь "___" или нейтральную формулировку без ссылок на конкретные статьи.
4) Структура:
   - Кому: ___
   - От кого: ___
   - Заголовок: "ПРЕТЕНЗИЯ"
   - Описание обстоятельств (кратко, по делу)
   - Требования (конкретно)
   - Срок исполнения требований: ___ дней (если не указан)
   - Приложения (если уместно)
   - Дата/подпись/контакты
5) Текст должен выглядеть как документ: абзацы, пустые строки между блоками.
"""

PROMPT_CLAUSE = """
Ты — LegalFox. Пользователь прислал вопрос или кусок текста.
Нужно:
- Ответить по-русски, по делу, без Markdown.
- Если нужно — переформулировать текст юридически аккуратнее, сохранив смысл.
- Делай структуру и отступы: короткие абзацы, пустые строки между блоками.
- Не выдавай себя за адвоката и не обещай гарантированный исход.
"""


# ----------------- Утилиты -----------------
def normalize_bool(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def strip_markdown_noise(text: str) -> str:
    if not text:
        return ""
    # удалить блоки ```...```
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    # убрать одиночные бэктики
    text = text.replace("`", "")
    # убрать жирность/подчёркивания
    text = text.replace("**", "").replace("__", "")
    # убрать маркдауны заголовков
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    # подчистить хвостовые пробелы
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def safe_filename(prefix: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}.pdf"


def pick_uid(payload: Dict[str, Any]) -> str:
    """
    Стабильный ID пользователя (лучше всего telegram user_id).
    + подстрахуем BotHelp макросами: bh_user_id / cuid
    """
    for key in [
        "user_id",
        "tg_user_id",
        "telegram_user_id",
        "messenger_user_id",
        "bh_user_id",
        "bothelp_user_id",
        "cuid",
    ]:
        v = payload.get(key)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v
    return ""


# ----------------- БД (trial 1 PDF) -----------------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            free_pdf_left INTEGER NOT NULL
        )
        """
    )
    con.commit()
    con.close()


def ensure_user(uid: str):
    if not uid:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users(uid, free_pdf_left) VALUES(?, ?)",
        (uid, FREE_PDF_LIMIT),
    )
    con.commit()
    con.close()


def free_left(uid: str) -> int:
    if not uid:
        return 0
    ensure_user(uid)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    con.close()
    if not row:
        return 0
    return int(row[0])


def consume_free(uid: str):
    if not uid:
        return
    ensure_user(uid)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "UPDATE users SET free_pdf_left = CASE WHEN free_pdf_left > 0 THEN free_pdf_left - 1 ELSE 0 END WHERE uid=?",
        (uid,),
    )
    con.commit()
    con.close()
    logger.info("Trial PDF списан uid=%s", uid)


# ----------------- PDF (кириллица) -----------------
def ensure_font_name() -> str:
    """
    Чтобы кириллица выглядела нормально — добавь файл:
      fonts/DejaVuSans.ttf
    """
    try:
        font_path = os.path.join("fonts", "DejaVuSans.ttf")
        if os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
            return "DejaVuSans"
    except Exception:
        logger.exception("Не удалось зарегистрировать TTF шрифт")
    return "Helvetica"


def render_pdf(text: str, out_path: str, title: str):
    font_name = ensure_font_name()
    c = canvas.Canvas(out_path, pagesize=A4)
    width, height = A4

    left, right, top, bottom = 40, 40, 60, 50
    max_width = width - left - right

    c.setFont(font_name, 14)
    c.drawString(left, height - top, title)

    c.setFont(font_name, 11)

    y = height - top - 30
    line_height = 14

    def wrap_line(line: str) -> list[str]:
        words = line.split()
        if not words:
            return [""]
        lines = []
        cur = words[0]
        for w in words[1:]:
            test = cur + " " + w
            if pdfmetrics.stringWidth(test, font_name, 11) <= max_width:
                cur = test
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    for raw in text.splitlines():
        if not raw.strip():
            y -= line_height
            if y < bottom:
                c.showPage()
                c.setFont(font_name, 11)
                y = height - top
            continue

        for ln in wrap_line(raw.rstrip()):
            c.drawString(left, y, ln)
            y -= line_height
            if y < bottom:
                c.showPage()
                c.setFont(font_name, 11)
                y = height - top

    c.save()


# ----------------- LLM (Groq через OpenAI-compatible endpoint) -----------------
async def call_groq(system_prompt: str, user_content: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not set")

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_content.strip()},
        ],
        "temperature": 0.25,   # меньше "креатива" — больше шаблонности и точности
        "max_tokens": 1400,
        "top_p": 1,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        return strip_markdown_noise(content)


# ----------------- FastAPI -----------------
app = FastAPI(title="LegalFox API", version="1.2.1")

os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

db_init()

if not PUBLIC_BASE_URL:
    logger.warning("PUBLIC_BASE_URL не задан — file_url может быть некорректным. Задай PUBLIC_BASE_URL в Railway.")


@app.get("/")
async def root():
    return {"status": "ok", "service": "LegalFox"}


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    # поддержка старых вариантов
    if s in ("draft_contract", "contract", "договора", "договора_черновик"):
        return "contract"
    if s in ("draft_claim", "claim", "претензия", "претензии"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    # дефолт
    return "contract"


def get_premium_flag(payload: Dict[str, Any]) -> bool:
    """
    Premium приходит из BotHelp как поле 1/0.
    Поддержим разные варианты ключа.
    """
    for key in ["Premium", "premium", "PREMIUM", "is_premium", "Подписка"]:
        if key in payload:
            return normalize_bool(payload.get(key))
    return False


def get_with_file_requested(payload: Dict[str, Any]) -> bool:
    """
    Ты обычно шлёшь with_file = 1.
    Поддержим:
      - with_file
      - Premium (как fallback)
    """
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return get_premium_flag(payload)


def file_url_for(filename: str) -> str:
    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/files/{filename}"


@app.post("/legalfox")
async def legalfox(payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    logger.info("Incoming payload keys: %s", list(payload.keys()))

    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    uid = pick_uid(payload)
    ensure_user(uid)

    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload)

    # 1 бесплатный PDF: разрешаем файл, если premium или есть trial
    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s uid=%s premium=%s trial_left=%s with_file=%s",
        scenario, uid, premium, trial_left, with_file
    )

    try:
        if scenario == "contract":
            contract_type = payload.get("Тип договора") or payload.get("Тип_договора") or ""
            parties = payload.get("Стороны") or ""
            subject = payload.get("Предмет") or ""
            terms_pay = payload.get("Сроки и оплата") or payload.get("Сроки_и_оплата") or payload.get("Сроки") or ""
            special = payload.get("Особые условия") or payload.get("Особые_условия") or ""

            # делаем ввод максимально "явным" для модели
            user_text = (
                f"Тип договора: {contract_type or '___'}\n"
                f"Стороны (как указано пользователем): {parties or '___'}\n"
                f"Предмет договора: {subject or '___'}\n"
                f"Сроки и оплата (как указано пользователем): {terms_pay or '___'}\n"
                f"Особые условия (как указано пользователем): {special or '___'}\n"
            ).strip()

            if not user_text.strip():
                return {"scenario": "contract", "reply_text": "Не вижу данных. Заполни поля и повтори."}

            draft = await call_groq(PROMPT_CONTRACT, user_text)

            result: Dict[str, str] = {"scenario": "contract", "reply_text": ""}

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР (ЧЕРНОВИК)")
                logger.info("PDF создан: %s", out_path)

                # если это был trial — списываем
                if (not premium) and trial_left > 0:
                    consume_free(uid)

                url = file_url_for(fn)
                if url:
                    result["file_url"] = url

                # короткий ответ вместо простыни
                result["reply_text"] = "Готово. Я подготовил черновик договора — просто скачай PDF по кнопке ниже."
            else:
                # если PDF не доступен — оставляем текст в чате
                result["reply_text"] = draft

                if with_file_requested and (not premium) and trial_left <= 0:
                    result["reply_text"] = (
                        draft
                        + "\n\nPDF-документ доступен по подписке. "
                          "Пробный PDF уже использован — оформи подписку, чтобы получать PDF без ограничений."
                    )

            return result

        if scenario == "claim":
            to_whom = payload.get("Адресат") or ""
            basis = payload.get("Основание") or ""
            viol = payload.get("Нарушение и обстоятельства") or payload.get("Нарушение_и_обстоятельства") or ""
            reqs = payload.get("Требования") or ""
            term = payload.get("Сроки исполнения") or payload.get("Срок_исполнения") or ""
            contacts = payload.get("Контакты") or ""

            user_text = (
                f"Адресат: {to_whom or '___'}\n"
                f"Основание: {basis or '___'}\n"
                f"Нарушение и обстоятельства: {viol or '___'}\n"
                f"Требования: {reqs or '___'}\n"
                f"Срок исполнения: {term or '___'}\n"
                f"Контакты: {contacts or '___'}\n"
            ).strip()

            if not user_text.strip():
                return {"scenario": "claim", "reply_text": "Не вижу данных. Заполни поля и повтори."}

            draft = await call_groq(PROMPT_CLAIM, user_text)

            result: Dict[str, str] = {"scenario": "claim", "reply_text": ""}

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")
                logger.info("PDF создан: %s", out_path)

                if (not premium) and trial_left > 0:
                    consume_free(uid)

                url = file_url_for(fn)
                if url:
                    result["file_url"] = url

                result["reply_text"] = "Готово. Я подготовил черновик претензии — просто скачай PDF по кнопке ниже."
            else:
                result["reply_text"] = draft
                if with_file_requested and (not premium) and trial_left <= 0:
                    result["reply_text"] = (
                        draft
                        + "\n\nPDF-документ доступен по подписке. "
                          "Пробный PDF уже использован — оформи подписку, чтобы получать PDF без ограничений."
                    )

            return result

        # scenario == "clause"
        q = payload.get("Запрос") or payload.get("query") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return {"scenario": "clause", "reply_text": "Напиши вопрос или вставь текст одним сообщением — помогу исправить/улучшить."}

        answer = await call_groq(PROMPT_CLAUSE, q)
        return {"scenario": "clause", "reply_text": answer}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        return {"scenario": scenario, "reply_text": FALLBACK_TEXT}
