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

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from xml.sax.saxutils import escape


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
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

# Trial
def _safe_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

FREE_PDF_LIMIT = max(0, _safe_int("FREE_PDF_LIMIT", 1))

# Debug (по желанию)
DEBUG_ERRORS = (os.getenv("DEBUG_ERRORS", "0").strip() == "1")

# PDF layout
PDF_FONT_FAMILY = (os.getenv("PDF_FONT_FAMILY", "PTSerif") or "PTSerif").strip()
PDF_FONT_SIZE = _safe_int("PDF_FONT_SIZE", 14)
PDF_LINE_SPACING = float(os.getenv("PDF_LINE_SPACING", "1.5"))
PDF_FIRST_INDENT_MM = float(os.getenv("PDF_FIRST_INDENT_MM", "12.5"))
PDF_LEFT_MM = float(os.getenv("PDF_LEFT_MM", "30"))
PDF_RIGHT_MM = float(os.getenv("PDF_RIGHT_MM", "15"))
PDF_TOP_MM = float(os.getenv("PDF_TOP_MM", "20"))
PDF_BOTTOM_MM = float(os.getenv("PDF_BOTTOM_MM", "20"))

# ВАЖНО: чтобы PDF всегда доходил, кладём ссылку в текст (без дубля)
# (даже если BotHelp по какой-то причине не прикрепляет файл)
ALWAYS_INCLUDE_LINK_IN_TEXT = (os.getenv("ALWAYS_INCLUDE_LINK_IN_TEXT", "1").strip() == "1")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."
URL_ERROR_TEXT = "Техническая ошибка: не удалось сформировать публичную ссылку на PDF. Проверь PUBLIC_BASE_URL."
NO_UID_TEXT = "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id)."


# ----------------- Промты -----------------
PROMPT_CONTRACT = """
Ты — LegalFox, помощник по подготовке черновиков договоров по праву РФ.
Сгенерируй ЧЕРНОВИК ДОГОВОРА (официально-деловой стиль).

Жёсткие правила:
1) Без Markdown.
2) Не используй заглушки вида "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ". Если данных нет — ставь "___".
3) Ничего не выдумывай: реквизиты, суммы, сроки — только если пользователь дал явно.
4) Используй введённые поля максимально конкретно.
5) Разделы с нумерацией 1., 2., 3. Разделы отделяй пустой строкой.
6) Без маркеров •/—, только обычный текст и нумерация.
"""

PROMPT_CLAIM = """
Ты — LegalFox, помощник по подготовке претензий по праву РФ.
Сгенерируй ЧЕРНОВИК ПРЕТЕНЗИИ (официально-деловой стиль).

Правила:
1) Без Markdown.
2) Если данных нет — "___".
3) Ничего не выдумывай: даты, суммы, нормы закона — только если пользователь дал явно.
4) Блоки: Кому/От кого/ПРЕТЕНЗИЯ/Обстоятельства/Требования/Срок/Приложения/Дата/Подпись/Контакты.
5) Блоки отделяй пустой строкой.
6) Без маркеров •/—, только обычный текст.
"""

# Комментарии: без вопросов и без "?"
PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай краткий комментарий по кейсу договора (6–10 строк).
Строго:
- Без Markdown.
- Не задавай вопросы.
- Не используй символ '?'.
- Пиши утверждениями/инструкциями: "Проверь...", "Укажи...", "Добавь...", "Закрепи...".
"""

PROMPT_CLAIM_COMMENT = """
Ты — LegalFox. Дай краткий комментарий по кейсу претензии (6–12 строк).
Строго:
- Без Markdown.
- Не задавай вопросы.
- Не используй символ '?'.
- Пиши утверждениями/инструкциями.
- В конце отдельная строка: "Приложите копии: ...".
"""

PROMPT_CLAUSE = """
Ты — LegalFox. Ответь по-русски, по делу, без Markdown. Короткие абзацы.
"""


# ----------------- Утилиты -----------------
_PLACEHOLDER_RE = re.compile(r"^\s*\{\%.*\%\}\s*$")

def _is_bad_value(v: Any) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    if not s:
        return True
    low = s.lower()
    if low in ("none", "null", "undefined"):
        return True
    # BotHelp плейсхолдеры вида "{%bh_user_id%}"
    if "{%" in s and "%}" in s and _PLACEHOLDER_RE.match(s):
        return True
    return False

def _extract_digits(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    return re.sub(r"\D+", "", s)

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
    text = text.replace("`", "").replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()

def safe_filename(prefix: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:8]}.pdf"

def pick_uid(payload: Dict[str, Any]) -> Tuple[str, str]:
    """
    UID должен быть стабильным, иначе trial ломается.
    Берём числовой id (если есть) — это самый надёжный вариант.
    """
    priority = [
        "bh_user_id",
        "user_id",
        "tg_user_id",
        "telegram_user_id",
        "messenger_user_id",
        "bothelp_user_id",
        "cuid",
    ]

    for key in priority:
        if key not in payload:
            continue
        v = payload.get(key)
        if _is_bad_value(v):
            continue
        digits = _extract_digits(v)
        if digits:
            return digits, f"{key}:digits"
        s = str(v).strip()
        if s:
            return s, key

    return "", ""

def extract_extra(payload: Dict[str, Any]) -> str:
    extra = (
        payload.get("Доп данные")
        or payload.get("Доп_данные")
        or payload.get("Доп вопросы")
        or payload.get("Доп_вопросы")
        or payload.get("extra")
        or payload.get("Extra")
        or ""
    )
    return str(extra).strip()

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

def get_with_file_requested(payload: Dict[str, Any], scenario: str) -> bool:
    """
    Без изменений BotHelp:
    - если with_file пришёл — используем его
    - если with_file НЕ пришёл — для contract/claim считаем True
    """
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    if scenario in ("contract", "claim"):
        return True
    return get_premium_flag(payload)

def make_error_response(scenario: str, err: Optional[Exception] = None) -> Dict[str, str]:
    msg = FALLBACK_TEXT
    if DEBUG_ERRORS and err is not None:
        msg += f"\n\n[DEBUG] {type(err).__name__}: {str(err)[:180]}"
    # Критично: всегда пустой file_url, чтобы BotHelp не подхватывал старый файл
    return {"scenario": scenario, "reply_text": msg, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}


# ----------------- БД -----------------
def _db_connect():
    con = sqlite3.connect(DB_PATH, timeout=20)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    return con

def db_init():
    con = _db_connect()
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
    con = _db_connect()
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
    con = _db_connect()
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else 0

def consume_free(uid: str) -> bool:
    """
    Trial списываем только после успешного создания PDF и URL.
    """
    if not uid:
        return False
    ensure_user(uid)
    con = _db_connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE users SET free_pdf_left = free_pdf_left - 1 WHERE uid=? AND free_pdf_left > 0",
        (uid,),
    )
    ok = (cur.rowcount == 1)
    con.commit()
    con.close()
    logger.info("Trial consume uid=%s ok=%s", uid, ok)
    return ok


# ----------------- PDF Fonts -----------------
def register_fonts():
    font_map = [
        ("PTSerif", "fonts/PTSerif-Regular.ttf", "fonts/PTSerif-Bold.ttf"),
        ("PTSans", "fonts/PTSans-Regular.ttf", "fonts/PTSans-Bold.ttf"),
        ("LiberationSerif", "fonts/LiberationSerif-Regular.ttf", "fonts/LiberationSerif-Bold.ttf"),
        ("DejaVuSerif", "fonts/DejaVuSerif.ttf", "fonts/DejaVuSerif-Bold.ttf"),
    ]
    registered = set(pdfmetrics.getRegisteredFontNames())
    for family, regular_path, bold_path in font_map:
        try:
            if os.path.exists(regular_path) and family not in registered:
                pdfmetrics.registerFont(TTFont(family, regular_path))
                registered.add(family)
            bold_name = family + "-Bold"
            if os.path.exists(bold_path) and bold_name not in registered:
                pdfmetrics.registerFont(TTFont(bold_name, bold_path))
                registered.add(bold_name)
        except Exception:
            logger.exception("Font register failed: %s", family)

def pick_font_family() -> str:
    register_fonts()
    if PDF_FONT_FAMILY in pdfmetrics.getRegisteredFontNames():
        return PDF_FONT_FAMILY
    return "Helvetica"

def pick_bold_font(family: str) -> str:
    bold = family + "-Bold"
    return bold if bold in pdfmetrics.getRegisteredFontNames() else family

def render_pdf(text: str, out_path: str, title: str):
    family = pick_font_family()
    bold = pick_bold_font(family)
    leading = int(round(PDF_FONT_SIZE * PDF_LINE_SPACING))

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=PDF_LEFT_MM * mm,
        rightMargin=PDF_RIGHT_MM * mm,
        topMargin=PDF_TOP_MM * mm,
        bottomMargin=PDF_BOTTOM_MM * mm,
    )

    title_style = ParagraphStyle(
        "Title",
        fontName=bold,
        fontSize=PDF_FONT_SIZE,
        leading=leading,
        alignment=TA_CENTER,
        spaceAfter=10,
    )

    body_style = ParagraphStyle(
        "Body",
        fontName=family,
        fontSize=PDF_FONT_SIZE,
        leading=leading,
        alignment=TA_JUSTIFY,
        firstLineIndent=PDF_FIRST_INDENT_MM * mm,
        spaceAfter=6,
    )

    story: List[Any] = [Paragraph(escape(title), title_style), Spacer(1, 6)]
    raw = (text or "").strip()
    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    for p in paragraphs:
        p_html = escape(p).replace("\n", "<br/>")
        story.append(Paragraph(p_html, body_style))

    doc.build(story)


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
        _token_exp = _now() + 25 * 60
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
app = FastAPI(title="LegalFox API", version="1.8.0-trial-link-guarantee")

os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")
db_init()

if not PUBLIC_BASE_URL:
    logger.warning("PUBLIC_BASE_URL is empty. Рекомендуется задать PUBLIC_BASE_URL в Railway.")


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "LegalFox",
        "free_pdf_limit": FREE_PDF_LIMIT,
        "public_base_url": PUBLIC_BASE_URL or "",
        "always_include_link_in_text": bool(ALWAYS_INCLUDE_LINK_IN_TEXT),
        "model": GIGACHAT_MODEL,
        "verify_ssl": bool(GIGACHAT_VERIFY_SSL),
    }


def file_url_for(filename: str, request: Request) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/files/{filename}"

    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        if proto == "http":
            proto = "https"
        return f"{proto}://{host}/files/{filename}"

    base = str(request.base_url).rstrip("/")
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return f"{base}/files/{filename}" if base else ""


def build_pdf_reply(base_text: str, comment: str, url: str) -> str:
    txt = base_text
    if comment:
        txt += f"\n\nКомментарий по вашему кейсу:\n{comment}"
    if ALWAYS_INCLUDE_LINK_IN_TEXT and url:
        txt += f"\n\nСкачать PDF:\n{url}"
    return txt


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    uid, uid_src = pick_uid(payload)
    if not uid:
        return {"scenario": scenario, "reply_text": NO_UID_TEXT, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

    ensure_user(uid)

    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload, scenario)

    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s uid=%s(uid_src=%s) premium=%s trial_left=%s FREE_PDF_LIMIT=%s with_file_requested=%s with_file=%s",
        scenario, uid, uid_src, premium, trial_left, FREE_PDF_LIMIT, with_file_requested, with_file
    )

    try:
        # ----------------- CONTRACT -----------------
        if scenario == "contract":
            contract_type = payload.get("Тип договора") or payload.get("Тип_договора") or ""
            parties = payload.get("Стороны") or ""
            subject = payload.get("Предмет") or ""
            terms_pay = payload.get("Сроки и оплата") or payload.get("Сроки_и_оплата") or payload.get("Сроки") or ""
            special = payload.get("Особые условия") or payload.get("Особые_условия") or ""
            extra = extract_extra(payload)

            user_text = (
                f"Тип договора: {contract_type or '___'}\n"
                f"Стороны: {parties or '___'}\n"
                f"Предмет: {subject or '___'}\n"
                f"Сроки и оплата: {terms_pay or '___'}\n"
                f"Особые условия: {special or '___'}\n"
                f"Доп. данные: {extra or '___'}\n"
            ).strip()

            draft = await call_llm(PROMPT_CONTRACT, user_text, max_tokens=1400)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, user_text, max_tokens=420)
                comment = comment.replace("?", "")  # страховка
            except Exception as e:
                logger.warning("Comment generation failed (contract): %s", e)

            # Если надо отдавать PDF (premium или trial)
            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР (ЧЕРНОВИК)")

                if (not os.path.exists(out_path)) or (os.path.getsize(out_path) < 800):
                    return make_error_response("contract", RuntimeError("PDF not created"))

                url = file_url_for(fn, request)
                if not url:
                    return {"scenario": "contract", "reply_text": URL_ERROR_TEXT, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

                # Trial списываем строго после того, как URL уже сформирован
                if (not premium) and trial_left > 0:
                    consume_free(uid)

                reply_text = build_pdf_reply(
                    base_text="Готово. Я подготовил черновик договора и сформировал PDF-файл.",
                    comment=comment,
                    url=url,
                )

                # ВАЖНО: отдаём несколько полей-алиасов, чтобы BotHelp точно подхватил
                return {
                    "scenario": "contract",
                    "reply_text": reply_text,
                    "file_url": url,
                    "pdf_url": url,
                    "file": url,
                    "document_url": url,
                }

            # Если PDF не разрешён (trial закончился)
            txt = draft + (f"\n\nКомментарий:\n{comment}" if comment else "")
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += (
                    "\n\nPDF-документ доступен по подписке. "
                    "Пробный PDF уже использован — оформи подписку, чтобы получать PDF без ограничений."
                )
            return {"scenario": "contract", "reply_text": txt, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

        # ----------------- CLAIM -----------------
        if scenario == "claim":
            to_whom = payload.get("Адресат") or ""
            basis = payload.get("Основание") or ""
            viol = payload.get("Нарушение и обстоятельства") or payload.get("Нарушение_и_обстоятельства") or ""
            reqs = payload.get("Требования") or ""
            term = payload.get("Сроки исполнения") or payload.get("Срок_исполнения") or ""
            contacts = payload.get("Контакты") or ""
            extra = extract_extra(payload)

            user_text = (
                f"Адресат: {to_whom or '___'}\n"
                f"Основание: {basis or '___'}\n"
                f"Нарушение и обстоятельства: {viol or '___'}\n"
                f"Требования: {reqs or '___'}\n"
                f"Срок исполнения: {term or '___'}\n"
                f"Контакты: {contacts or '___'}\n"
                f"Доп. данные: {extra or '___'}\n"
            ).strip()

            draft = await call_llm(PROMPT_CLAIM, user_text, max_tokens=1400)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=420)
                comment = comment.replace("?", "")  # страховка
            except Exception as e:
                logger.warning("Comment generation failed (claim): %s", e)

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")

                if (not os.path.exists(out_path)) or (os.path.getsize(out_path) < 800):
                    return make_error_response("claim", RuntimeError("PDF not created"))

                url = file_url_for(fn, request)
                if not url:
                    return {"scenario": "claim", "reply_text": URL_ERROR_TEXT, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

                if (not premium) and trial_left > 0:
                    consume_free(uid)

                reply_text = build_pdf_reply(
                    base_text="Готово. Я подготовил черновик претензии и сформировал PDF-файл.",
                    comment=comment,
                    url=url,
                )

                return {
                    "scenario": "claim",
                    "reply_text": reply_text,
                    "file_url": url,
                    "pdf_url": url,
                    "file": url,
                    "document_url": url,
                }

            txt = draft + (f"\n\nКомментарий:\n{comment}" if comment else "")
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += (
                    "\n\nPDF-документ доступен по подписке. "
                    "Пробный PDF уже использован — оформи подписку, чтобы получать PDF без ограничений."
                )
            return {"scenario": "claim", "reply_text": txt, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

        # ----------------- CLAUSE -----------------
        q = payload.get("Запрос") or payload.get("query") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return {"scenario": "clause", "reply_text": "Напиши вопрос или вставь текст одним сообщением — помогу.", "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

        answer = await call_llm(PROMPT_CLAUSE, q, max_tokens=800)
        return {"scenario": "clause", "reply_text": answer, "file_url": "", "pdf_url": "", "file": "", "document_url": ""}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        return make_error_response(scenario, e)
