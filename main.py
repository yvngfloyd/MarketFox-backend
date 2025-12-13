import os
import re
import uuid
import time
import sqlite3
import logging
import asyncio
from typing import Any, Dict, Tuple, List, Optional

import httpx
from fastapi import FastAPI, Body, Request
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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
FILES_DIR = os.getenv("FILES_DIR", "files")
DB_PATH = os.getenv("DB_PATH", "legalfox.db")

LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "gigachat") or "gigachat").strip().lower()

# GigaChat
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")  # Basic <auth_key>
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

# 1 бесплатный PDF на пользователя
FREE_PDF_LIMIT = int(os.getenv("FREE_PDF_LIMIT", "1"))

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
   - Город: ___   Дата: ___
   - Разделы с нумерацией: 1. Предмет договора, 2. Права и обязанности, 3. Цена и порядок расчётов,
     4. Сроки, 5. Ответственность сторон, 6. Форс-мажор, 7. Порядок разрешения споров,
     8. Срок действия и расторжение, 9. Заключительные положения, 10. Реквизиты и подписи.
6) Суммы/сроки: если не указаны — "___ рублей", "___ календарных дней".
7) В конце: "Реквизиты и подписи сторон" с полями. Ничего не выдумывай — если нет данных, "___".
8) Аккуратная верстка: короткие абзацы, пустые строки между разделами.
"""

PROMPT_CLAIM = """
Ты — LegalFox, помощник по подготовке претензий по праву РФ (для обычных людей).
Сгенерируй ЧЕРНОВИК ПРЕТЕНЗИИ (досудебной) в официально-деловом стиле.

Жёсткие правила (обязательно):
1) Без Markdown.
2) Если данных нет — "___". Если есть — вставляй как есть.
3) Ничего не выдумывай: даты, суммы, реквизиты, нормы закона — только если пользователь дал явно.
4) Структура:
   - Кому / От кого
   - "ПРЕТЕНЗИЯ"
   - Обстоятельства
   - Требования
   - Срок исполнения: ___ дней
   - Приложения (если уместно)
   - Дата/подпись/контакты
5) Абзацы + пустые строки.
"""

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Пользователь ввёл данные для договора.
Сделай КОРОТКИЙ КОММЕНТАРИЙ на основе введённых данных.

Строго:
- Без Markdown.
- 6–10 коротких строк.
- Что проверить/уточнить/добавить, где риски.
- Если чего-то не хватает — перечисли конкретно.
- Не обещай результат.
"""

PROMPT_CLAIM_COMMENT = """
Ты — LegalFox. Пользователь ввёл данные для претензии.
Сделай КОРОТКИЙ КОММЕНТАРИЙ на основе введённых данных.

Строго:
- Без Markdown.
- 6–10 коротких строк.
- Какие данные/доказательства добавить, что приложить, что уточнить.
- Если чего-то не хватает — перечисли конкретно.
- Не обещай результат.
"""

PROMPT_CLAUSE = """
Ты — LegalFox. Пользователь прислал вопрос или кусок текста.
Ответь по-русски, по делу, без Markdown.
Если нужно — переформулируй юридически аккуратнее, сохранив смысл.
Короткие абзацы, пустые строки.
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
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = text.replace("`", "")
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def safe_filename(prefix: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}.pdf"


def pick_uid(payload: Dict[str, Any]) -> Tuple[str, str]:
    priority = [
        "bh_user_id",
        "user_id",
        "tg_user_id",
        "telegram_user_id",
        "messenger_user_id",
        "bothelp_user_id",
        "cuid",  # последним
    ]
    for key in priority:
        v = payload.get(key)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v, key
    return "", ""


def make_error_response(scenario: str) -> Dict[str, str]:
    # КРИТИЧНО: всегда возвращаем file_url="", чтобы BotHelp не “подхватил” старое значение
    return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}


# ----------------- БД -----------------
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
    cur.execute("INSERT OR IGNORE INTO users(uid, free_pdf_left) VALUES(?, ?)", (uid, FREE_PDF_LIMIT))
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
    return int(row[0]) if row else 0


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


# ----------------- PDF -----------------
def ensure_font_name() -> str:
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

    def wrap_line(line: str) -> List[str]:
        words = line.split()
        if not words:
            return [""]
        lines: List[str] = []
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


# ----------------- GigaChat Token Cache -----------------
_token_lock = asyncio.Lock()
_token_value: Optional[str] = None
_token_exp: float = 0.0


def _now() -> float:
    return time.time()


async def get_gigachat_access_token() -> str:
    global _token_value, _token_exp

    if not GIGACHAT_AUTH_KEY:
        raise RuntimeError("GIGACHAT_AUTH_KEY not set")

    if _token_value and _now() < (_token_exp - 60):
        return _token_value

    async with _token_lock:
        if _token_value and _now() < (_token_exp - 60):
            return _token_value

        rq_uid = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": rq_uid,
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        }
        data = {"scope": GIGACHAT_SCOPE}

        async with httpx.AsyncClient(timeout=60, verify=GIGACHAT_VERIFY_SSL) as client:
            r = await client.post(GIGACHAT_OAUTH_URL, headers=headers, data=data)
            r.raise_for_status()
            js = r.json()

        token = js.get("access_token")
        if not token:
            raise RuntimeError(f"OAuth ответ без access_token: {js}")

        _token_value = token
        _token_exp = _now() + 25 * 60  # заранее обновляем
        logger.info("GigaChat token refreshed rq_uid=%s", rq_uid)
        return _token_value


async def call_gigachat(system_prompt: str, user_content: str, max_tokens: int = 1400) -> str:
    token = await get_gigachat_access_token()
    url = f"{GIGACHAT_BASE_URL}/api/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "model": GIGACHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_content.strip()},
        ],
        "temperature": 0.25,
        "max_tokens": int(max_tokens),
        "top_p": 1,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=90, verify=GIGACHAT_VERIFY_SSL) as client:
        r = await client.post(url, headers=headers, json=payload)

        if r.status_code in (401, 403):
            logger.warning("GigaChat auth error %s, refreshing token and retrying once", r.status_code)
            global _token_value, _token_exp
            _token_value, _token_exp = None, 0.0
            token = await get_gigachat_access_token()
            headers["Authorization"] = f"Bearer {token}"
            r = await client.post(url, headers=headers, json=payload)

        r.raise_for_status()
        js = r.json()

    try:
        content = js["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"Unexpected GigaChat response: {js}")

    return strip_markdown_noise(content)


async def call_llm(system_prompt: str, user_input: str, max_tokens: int = 1400) -> str:
    if LLM_PROVIDER != "gigachat":
        raise RuntimeError("LLM_PROVIDER must be gigachat for this main.py")
    return await call_gigachat(system_prompt, user_input, max_tokens=max_tokens)


# ----------------- FastAPI -----------------
app = FastAPI(title="LegalFox API", version="1.5.2-gigachat")

os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

db_init()


@app.get("/")
async def root():
    return {"status": "ok", "service": "LegalFox", "llm": LLM_PROVIDER, "model": GIGACHAT_MODEL}


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("draft_contract", "contract", "договора", "договора_черновик"):
        return "contract"
    if s in ("draft_claim", "claim", "претензия", "претензии"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    return "contract"


def get_premium_flag(payload: Dict[str, Any]) -> bool:
    for key in ["Premium", "premium", "PREMIUM", "is_premium", "Подписка"]:
        if key in payload:
            return normalize_bool(payload.get(key))
    return False


def get_with_file_requested(payload: Dict[str, Any]) -> bool:
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return get_premium_flag(payload)


def file_url_for(filename: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    uid, uid_src = pick_uid(payload)
    if not uid:
        return {"scenario": scenario, "reply_text": "Техническая ошибка: не удалось определить пользователя.", "file_url": ""}

    ensure_user(uid)

    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload)

    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s uid=%s(uid_src=%s) premium=%s trial_left=%s with_file_requested=%s with_file=%s model=%s",
        scenario, uid, uid_src, premium, trial_left, with_file_requested, with_file, GIGACHAT_MODEL
    )

    try:
        if scenario == "contract":
            contract_type = payload.get("Тип договора") or payload.get("Тип_договора") or ""
            parties = payload.get("Стороны") or ""
            subject = payload.get("Предмет") or ""
            terms_pay = payload.get("Сроки и оплата") or payload.get("Сроки_и_оплата") or payload.get("Сроки") or ""
            special = payload.get("Особые условия") or payload.get("Особые_условия") or ""

            user_text = (
                f"Тип договора: {contract_type or '___'}\n"
                f"Стороны (как указано пользователем): {parties or '___'}\n"
                f"Предмет договора: {subject or '___'}\n"
                f"Сроки и оплата (как указано пользователем): {terms_pay or '___'}\n"
                f"Особые условия (как указано пользователем): {special or '___'}\n"
            ).strip()

            # Если LLM упал — НЕ даём файл
            draft = await call_llm(PROMPT_CONTRACT, user_text, max_tokens=1400)
            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, user_text, max_tokens=420)
            except Exception as e:
                logger.warning("Comment generation failed (contract): %s", e)
                comment = ""

            result: Dict[str, str] = {"scenario": "contract", "reply_text": "", "file_url": ""}

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    consume_free(uid)

                result["file_url"] = file_url_for(fn, request)
                result["reply_text"] = (
                    "Готово. Я подготовил черновик договора и приложил PDF-файл."
                    + (f"\n\nКомментарий по вашему кейсу:\n{comment}" if comment else "")
                )
            else:
                result["reply_text"] = draft + (f"\n\nКомментарий:\n{comment}" if comment else "")
                # важно: file_url уже "" (перезатираем BotHelp)
                if with_file_requested and (not premium) and trial_left <= 0:
                    result["reply_text"] += (
                        "\n\nPDF-документ доступен по подписке. "
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

            draft = await call_llm(PROMPT_CLAIM, user_text, max_tokens=1400)
            comment = ""
            try:
                comment = await call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=420)
            except Exception as e:
                logger.warning("Comment generation failed (claim): %s", e)
                comment = ""

            result: Dict[str, str] = {"scenario": "claim", "reply_text": "", "file_url": ""}

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    consume_free(uid)

                result["file_url"] = file_url_for(fn, request)
                result["reply_text"] = (
                    "Готово. Я подготовил черновик претензии и приложил PDF-файл."
                    + (f"\n\nКомментарий по вашему кейсу:\n{comment}" if comment else "")
                )
            else:
                result["reply_text"] = draft + (f"\n\nКомментарий:\n{comment}" if comment else "")
                if with_file_requested and (not premium) and trial_left <= 0:
                    result["reply_text"] += (
                        "\n\nPDF-документ доступен по подписке. "
                        "Пробный PDF уже использован — оформи подписку, чтобы получать PDF без ограничений."
                    )
            return result

        # clause
        q = payload.get("Запрос") or payload.get("query") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return {"scenario": "clause", "reply_text": "Напиши вопрос или вставь текст одним сообщением.", "file_url": ""}

        answer = await call_llm(PROMPT_CLAUSE, q, max_tokens=800)
        return {"scenario": "clause", "reply_text": answer, "file_url": ""}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        return make_error_response(scenario)
