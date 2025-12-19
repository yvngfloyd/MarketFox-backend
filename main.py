import os
import re
import uuid
import time
import json
import sqlite3
import logging
import asyncio
from typing import Any, Dict, Tuple, Optional, List

import httpx
from fastapi import FastAPI, Body, Request
from fastapi.staticfiles import StaticFiles

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# =========================
# ЛОГГЕР
# =========================
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
if not logger.handlers:
    logger.addHandler(handler)


# =========================
# КОНФИГ
# =========================
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
FILES_DIR = os.getenv("FILES_DIR", "files")
DB_PATH = os.getenv("DB_PATH", "legalfox.db")

# Trial
FREE_PDF_LIMIT = int(os.getenv("FREE_PDF_LIMIT", "1"))

# LLM: GigaChat
LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "gigachat") or "gigachat").strip().lower()
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")  # base64(ClientID:ClientSecret)
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-Lite")  # по умолчанию Lite
GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_CHAT_PATH = os.getenv("GIGACHAT_CHAT_PATH", "/api/v1/chat/completions")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# Templates (файлы-шаблоны договора)
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")

# Лимит сохранённых шаблонов
TEMPLATE_SLOTS_MAX = int(os.getenv("TEMPLATE_SLOTS_MAX", "3"))


# =========================
# ПРОМТЫ
# =========================
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик договора оказания услуг по праву РФ для самозанятых/фрилансеров/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону: сохраняй порядок разделов, заголовки и нумерацию.

Жёсткие правила:
1) Без Markdown: никаких #, **, ``` и т.п.
2) Ничего не выдумывай: паспорт, ИНН/ОГРН, адреса, реквизиты, суммы, даты, сроки — только если пользователь дал явно и однозначно.
3) Если данных нет / они неполные / размытые / противоречивые / выглядят как “мусор” — НЕ вставляй их.
   Вместо этого оставляй ПУСТОЕ МЕСТО длинными подчёркиваниями.
   Используй такие заглушки:
   - короткие поля: "____________"
   - длинные поля: "________________________________________"
   - суммы/даты: "________ руб.", "__.__.____"
4) Никогда не используй заглушки типа "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ", "ОПЛАТА" и т.п.
   Только реальные данные или подчёркивания.
5) Запрещено переносить в итоговый договор служебные строки и подсказки:
   не выводи "TEMPLATE:", "DATA:", "Пример:", "введите", "поле", "как указано пользователем" и т.п.
6) Если пользователь написал "нет" / "не знаю" / "пусто" / "—" / "0" / "n/a" — считай, что данных нет.
   В договоре оставляй подчёркивания.
7) Жёсткая фильтрация мусора:
   - любые переменные, технические теги, наборы букв, мусор — игнорируй, оставляй подчёркивания.
8) "Доп данные":
   - если там 1–3 конкретных условия — вставь в подходящий раздел.
   - если там вода/мусор — игнорируй.
9) Верстка: короткие абзацы, пустые строки между разделами.

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
"""

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к договору (чек-лист).

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 6–10 коротких строк.
- Не задавай вопросов и не используй '?'.
- Пиши нейтрально: "Добавь…", "Укажи…", "Зафиксируй…", "Проверь…".
- Не используй оценочные формулировки "некорректно/ошибки/не примут".

Выводи только комментарий.
"""

PROMPT_CLAIM = """
Ты — LegalFox. Подготовь черновик претензии (досудебной) по праву РФ.

Строго:
- Без Markdown.
- Официальный стиль.
- Если данных нет — "___". Если есть — вставляй как есть.
- Ничего не выдумывай: даты, суммы, реквизиты, нормы закона — только если пользователь дал явно.
- Структура:
  Кому: ___
  От кого: ___
  Заголовок: ПРЕТЕНЗИЯ
  Обстоятельства
  Требования
  Срок исполнения: ___ календарных дней
  Приложения
  Дата/подпись/контакты
- Пустые строки между блоками.
"""

PROMPT_CLAIM_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к претензии (чек-лист).

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 7–11 коротких строк.
- Не задавай вопросов и не используй '?'.
- Последняя строка ВСЕГДА: "Приложи копии: ...".

Выводи только комментарий.
"""

PROMPT_CLAUSE = """
Ты — LegalFox. Пользователь прислал вопрос или кусок текста.
Ответь по-русски, по делу, без Markdown.
Обращайся на "ты".
Если нужно — переформулируй юридически аккуратнее, сохранив смысл.
Короткие абзацы, пустые строки.
"""


# =========================
# УТИЛИТЫ
# =========================
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


def file_url_for(filename: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


def extract_extra(payload: Dict[str, Any]) -> str:
    extra = (
        payload.get("Доп данные")
        or payload.get("Доп_данные")
        or payload.get("extra")
        or payload.get("Extra")
        or ""
    )
    return str(extra).strip()


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("contract", "draft_contract", "договора", "договора_черновик"):
        return "contract"
    if s in ("claim", "draft_claim", "претензия", "претензии", "claim_unpaid"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    if s in ("template_save", "save_template"):
        return "template_save"
    if s in ("template_list", "templates", "my_templates", "templates_list"):
        return "template_list"
    if s in ("template_use", "use_template"):
        return "template_use"
    if s in ("template_delete", "delete_template"):
        return "template_delete"
    return "contract"


def pick_uid(payload: Dict[str, Any]) -> Tuple[str, str]:
    priority = ["bh_user_id", "user_id", "tg_user_id", "telegram_user_id", "messenger_user_id", "bothelp_user_id", "cuid"]
    for key in priority:
        v = payload.get(key)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        # оставляем digits-поведение как у тебя (uid=5 и т.п.)
        digits = re.sub(r"\D+", "", v)
        if digits:
            return digits, f"{key}:digits"
        return v, key
    return "", ""


def get_premium_flag(payload: Dict[str, Any]) -> bool:
    for key in ["Premium", "premium", "PREMIUM", "is_premium", "Подписка"]:
        if key in payload:
            return normalize_bool(payload.get(key))
    return False


def get_with_file_requested(payload: Dict[str, Any]) -> bool:
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return False


def get_template_slot(payload: Dict[str, Any]) -> Optional[int]:
    # поле именно "template slot" (с пробелом), но поддержим варианты
    for k in ["template slot", "template_slot", "slot", "templateSlot", "template-slot"]:
        if k in payload:
            v = str(payload.get(k) or "").strip()
            if not v:
                return None
            if v.isdigit():
                n = int(v)
                if 1 <= n <= TEMPLATE_SLOTS_MAX:
                    return n
    return None


def get_template_name(payload: Dict[str, Any]) -> str:
    # поддержка разных названий (ты сказал, что правильно template_name)
    for k in ["template_name", "template - name", "template - name ", "template-name", "template name", "templateName"]:
        if k in payload:
            v = str(payload.get(k) or "").strip()
            if v:
                return v
    return ""


def sanitize_template_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"\s+", " ", name)
    # убираем слишком длинные/мусорные имена
    name = name[:40].strip()
    if not name:
        return ""
    # режем опасные символы
    name = re.sub(r"[<>\"`{}[\]]+", "", name).strip()
    return name


def make_error_response(scenario: str) -> Dict[str, str]:
    # критично: file_url пустой, чтобы BotHelp не прикрепил старый файл
    return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}


# =========================
# БД
# =========================
def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def db_init():
    con = db_connect()
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            free_pdf_left INTEGER NOT NULL,
            last_contract_draft TEXT,
            last_contract_pdf TEXT,
            last_contract_payload TEXT,
            last_contract_ts INTEGER
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS templates (
            uid TEXT NOT NULL,
            slot INTEGER NOT NULL,
            template_name TEXT NOT NULL,
            draft_text TEXT,
            pdf_filename TEXT,
            payload TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(uid, slot)
        )
        """
    )

    con.commit()
    con.close()


def ensure_user(uid: str):
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users(uid, free_pdf_left) VALUES(?, ?)", (uid, FREE_PDF_LIMIT))
    con.commit()
    con.close()


def free_left(uid: str) -> int:
    ensure_user(uid)
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    con.close()
    return int(row["free_pdf_left"]) if row else 0


def consume_free(uid: str) -> bool:
    ensure_user(uid)
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    left = int(row["free_pdf_left"]) if row else 0
    if left <= 0:
        con.close()
        return False
    cur.execute("UPDATE users SET free_pdf_left = free_pdf_left - 1 WHERE uid=?", (uid,))
    con.commit()
    con.close()
    return True


def set_last_contract(uid: str, draft_text: str, pdf_filename: Optional[str], payload_obj: Dict[str, Any]):
    ensure_user(uid)
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        UPDATE users
        SET last_contract_draft=?,
            last_contract_pdf=?,
            last_contract_payload=?,
            last_contract_ts=?
        WHERE uid=?
        """,
        (
            draft_text,
            pdf_filename or "",
            json.dumps(payload_obj, ensure_ascii=False),
            int(time.time()),
            uid,
        ),
    )
    con.commit()
    con.close()


def get_last_contract(uid: str) -> Dict[str, Any]:
    ensure_user(uid)
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT last_contract_draft, last_contract_pdf, last_contract_payload, last_contract_ts FROM users WHERE uid=?",
        (uid,),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return {}
    return {
        "draft_text": row["last_contract_draft"] or "",
        "pdf_filename": row["last_contract_pdf"] or "",
        "payload": row["last_contract_payload"] or "",
        "ts": row["last_contract_ts"] or 0,
    }


def list_templates(uid: str) -> List[sqlite3.Row]:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT slot, template_name, pdf_filename, updated_at FROM templates WHERE uid=? ORDER BY slot ASC",
        (uid,),
    )
    rows = cur.fetchall()
    con.close()
    return rows or []


def get_template(uid: str, slot: int) -> Optional[sqlite3.Row]:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT uid, slot, template_name, draft_text, pdf_filename, payload FROM templates WHERE uid=? AND slot=?",
        (uid, slot),
    )
    row = cur.fetchone()
    con.close()
    return row


def find_next_free_slot(uid: str) -> Optional[int]:
    rows = list_templates(uid)
    used = {int(r["slot"]) for r in rows}
    for s in range(1, TEMPLATE_SLOTS_MAX + 1):
        if s not in used:
            return s
    return None


def upsert_template(uid: str, slot: int, template_name: str, draft_text: str, pdf_filename: str, payload_text: str):
    now = int(time.time())
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO templates(uid, slot, template_name, draft_text, pdf_filename, payload, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid, slot) DO UPDATE SET
            template_name=excluded.template_name,
            draft_text=excluded.draft_text,
            pdf_filename=excluded.pdf_filename,
            payload=excluded.payload,
            updated_at=excluded.updated_at
        """,
        (uid, slot, template_name, draft_text, pdf_filename, payload_text, now, now),
    )
    con.commit()
    con.close()


def delete_template(uid: str, slot: int) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM templates WHERE uid=? AND slot=?", (uid, slot))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


# =========================
# PDF (кириллица)
# =========================
def ensure_font_name() -> str:
    candidates = [
        ("PTSerif", os.path.join("fonts", "PTSerif-Regular.ttf")),
        ("DejaVuSans", os.path.join("fonts", "DejaVuSans.ttf")),
    ]
    for font_name, font_path in candidates:
        try:
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont(font_name, font_path))
                return font_name
        except Exception:
            logger.exception("Не удалось зарегистрировать шрифт: %s", font_path)
    return "Helvetica"


def render_pdf(text: str, out_path: str, title: str):
    font_name = ensure_font_name()
    c = canvas.Canvas(out_path, pagesize=A4)
    width, height = A4

    # Поля ~ ГОСТ-лайк
    left, right, top, bottom = 85, 42, 57, 57
    max_width = width - left - right

    c.setFont(font_name, 14)
    c.drawString(left, height - top, title)

    c.setFont(font_name, 12)
    y = height - top - 28
    line_height = 16

    def wrap_line(line: str) -> List[str]:
        words = line.split()
        if not words:
            return [""]
        lines: List[str] = []
        cur = words[0]
        for w in words[1:]:
            test = cur + " " + w
            if pdfmetrics.stringWidth(test, font_name, 12) <= max_width:
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
                c.setFont(font_name, 12)
                y = height - top
            continue

        for ln in wrap_line(raw.rstrip()):
            c.drawString(left, y, ln)
            y -= line_height
            if y < bottom:
                c.showPage()
                c.setFont(font_name, 12)
                y = height - top

    c.save()


# =========================
# TEMPLATES (файлы) cache
# =========================
_templates_cache: Dict[str, str] = {}
_templates_lock = asyncio.Lock()


async def _read_file(path: str) -> str:
    loop = asyncio.get_running_loop()

    def _sync():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    return await loop.run_in_executor(None, _sync)


async def get_template_file(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name)
    async with _templates_lock:
        if path in _templates_cache:
            return _templates_cache[path]
        if not os.path.exists(path):
            raise FileNotFoundError(f"Template not found: {path}")
        txt = await _read_file(path)
        _templates_cache[path] = txt
        return txt


async def build_services_contract_template() -> str:
    base = await get_template_file(SERVICES_CONTRACT_TEMPLATE)
    tail = await get_template_file(COMMON_TAIL_TEMPLATE)
    if COMMON_TAIL_PLACEHOLDER not in base:
        raise RuntimeError(f"Placeholder {COMMON_TAIL_PLACEHOLDER} not found in {SERVICES_CONTRACT_TEMPLATE}")
    return base.replace(COMMON_TAIL_PLACEHOLDER, tail)


# =========================
# GIGACHAT TOKEN CACHE
# =========================
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
    if LLM_PROVIDER != "gigachat":
        raise RuntimeError("LLM_PROVIDER must be gigachat")

    token = await get_gigachat_access_token()
    url = f"{GIGACHAT_BASE_URL.rstrip('/')}{GIGACHAT_CHAT_PATH}"

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
        # важное: для повторяемости
        "temperature": 0.0,
        "top_p": 1,
        "max_tokens": int(max_tokens),
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=120, verify=GIGACHAT_VERIFY_SSL) as client:
        r = await client.post(url, headers=headers, json=payload)

        # если токен протух — обновим один раз
        if r.status_code in (401, 403):
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


# =========================
# DATA BUILDER (минимальные поля)
# =========================
def build_services_data_min(payload: Dict[str, Any]) -> str:
    def v(x: Any) -> str:
        s = str(x).strip()
        if not s:
            return "___"
        low = s.lower()
        if low in ("нет", "не знаю", "пусто", "-", "—", "0", "n/a", "na"):
            return "___"
        return s

    exec_type = payload.get("exec_type") or ""
    exec_name = payload.get("exec_name") or ""
    client_name = payload.get("client_name") or ""
    service_desc = payload.get("service_desc") or payload.get("Предмет") or ""
    deadline_value = payload.get("deadline_value") or payload.get("Сроки") or ""
    price_value = payload.get("price_value") or ""
    acceptance_value = payload.get("acceptance_value") or ""
    extra = extract_extra(payload)

    data = (
        f"Исполнитель:\n"
        f"- статус: {v(exec_type)}\n"
        f"- как указать: {v(exec_name)}\n\n"
        f"Заказчик:\n"
        f"- как указать: {v(client_name)}\n\n"
        f"Услуга:\n"
        f"- описание: {v(service_desc)}\n\n"
        f"Сроки:\n"
        f"- значение: {v(deadline_value)}\n\n"
        f"Цена и оплата:\n"
        f"- значение: {v(price_value)}\n\n"
        f"Приёмка:\n"
        f"- значение: {v(acceptance_value)}\n\n"
        f"Доп данные:\n{v(extra)}\n"
    )
    return data.strip()


# =========================
# FASTAPI
# =========================
app = FastAPI(title="LegalFox API", version="4.2.0-templates-pdf-copy")

os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

db_init()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "LegalFox",
        "model": GIGACHAT_MODEL,
        "verify_ssl": bool(GIGACHAT_VERIFY_SSL),
        "free_pdf_limit": FREE_PDF_LIMIT,
        "template_slots_max": TEMPLATE_SLOTS_MAX,
        "templates_dir": TEMPLATES_DIR,
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    # ============ CLAUSE ============
    if scenario == "clause":
        q = payload.get("Запрос") or payload.get("query") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return {"scenario": "clause", "reply_text": "Напиши вопрос или вставь текст одним сообщением — помогу.", "file_url": ""}
        try:
            answer = await call_gigachat(PROMPT_CLAUSE, q, max_tokens=900)
            return {"scenario": "clause", "reply_text": answer, "file_url": ""}
        except Exception as e:
            logger.exception("legalfox error (clause): %s", e)
            return make_error_response("clause")

    # дальше нужен uid
    uid, uid_src = pick_uid(payload)
    if not uid:
        return {"scenario": scenario, "reply_text": "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id).", "file_url": ""}

    ensure_user(uid)
    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload)
    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s uid=%s(uid_src=%s) premium=%s trial_left=%s FREE_PDF_LIMIT=%s with_file_requested=%s with_file=%s model=%s",
        scenario, uid, uid_src, premium, trial_left, FREE_PDF_LIMIT, with_file_requested, with_file, GIGACHAT_MODEL
    )

    try:
        # ============ TEMPLATE LIST ============
        if scenario == "template_list":
            rows = list_templates(uid)
            if not rows:
                return {
                    "scenario": "template_list",
                    "reply_text": "Пока нет сохранённых шаблонов. Сначала собери договор и нажми «Сохранить шаблон».",
                    "file_url": ""
                }

            used = {int(r["slot"]) for r in rows}
            free_slots = [str(s) for s in range(1, TEMPLATE_SLOTS_MAX + 1) if s not in used]

            lines = ["Твои шаблоны:"]
            for r in rows:
                lines.append(f"{int(r['slot'])}) {r['template_name']}")
            if free_slots:
                lines.append("")
                lines.append(f"Свободные слоты: {', '.join(free_slots)}")
            lines.append("")
            lines.append("Нажми 1–3, чтобы использовать шаблон. Для удаления — кнопка «Удалить шаблон».")

            return {"scenario": "template_list", "reply_text": "\n".join(lines), "file_url": ""}

        # ============ TEMPLATE SAVE ============
        if scenario == "template_save":
            # имя (берём из payload)
            name = sanitize_template_name(get_template_name(payload))
            if not name:
                name = "Мой шаблон"

            # слот (если не задан — авто 1->2->3)
            slot = get_template_slot(payload)
            if slot is None:
                slot = find_next_free_slot(uid)
                if slot is None:
                    return {
                        "scenario": "template_save",
                        "reply_text": "Лимит шаблонов достигнут (3 из 3). Удали один шаблон в разделе «Мои шаблоны», чтобы сохранить новый.",
                        "file_url": ""
                    }

            # берём last_contract (это и есть “точная копия результата”)
            last = get_last_contract(uid)
            last_draft = (last.get("draft_text") or "").strip()
            last_pdf = (last.get("pdf_filename") or "").strip()
            last_payload = (last.get("payload") or "").strip()

            if not last_draft and not last_pdf:
                return {
                    "scenario": "template_save",
                    "reply_text": "Пока нечего сохранять. Сначала собери договор и получи результат, затем нажми «Сохранить шаблон».",
                    "file_url": ""
                }

            # Если PDF не сохранился (например был текст без файла) — сохраним хотя бы draft_text
            upsert_template(
                uid=uid,
                slot=int(slot),
                template_name=name,
                draft_text=last_draft,
                pdf_filename=last_pdf,
                payload_text=last_payload,
            )

            return {
                "scenario": "template_save",
                "reply_text": f"Сохранил шаблон «{name}» (слот {slot}). Открой «Мои шаблоны», чтобы использовать или удалить.",
                "file_url": ""
            }

        # ============ TEMPLATE DELETE ============
        if scenario == "template_delete":
            slot = get_template_slot(payload)
            if slot is None:
                return {"scenario": "template_delete", "reply_text": "Напиши номер слота (1–3), который нужно удалить.", "file_url": ""}

            ok = delete_template(uid, int(slot))
            if ok:
                return {"scenario": "template_delete", "reply_text": f"Удалил шаблон из слота {slot}.", "file_url": ""}
            return {"scenario": "template_delete", "reply_text": f"В слоте {slot} нет шаблона. Открой «Мои шаблоны» и проверь список.", "file_url": ""}

        # ============ TEMPLATE USE ============
        if scenario == "template_use":
            slot = get_template_slot(payload)
            if slot is None:
                return {"scenario": "template_use", "reply_text": "Напиши номер слота (1–3), чтобы использовать шаблон.", "file_url": ""}

            row = get_template(uid, int(slot))
            if not row:
                return {"scenario": "template_use", "reply_text": f"В слоте {slot} нет шаблона. Открой «Мои шаблоны» и проверь список.", "file_url": ""}

            tname = row["template_name"]
            pdf_fn = (row["pdf_filename"] or "").strip()
            draft_text = (row["draft_text"] or "").strip()
            payload_text = (row["payload"] or "").strip()

            # 1) Если PDF есть и файл существует — отдаём точную копию БЕЗ LLM
            if pdf_fn:
                pdf_path = os.path.join(FILES_DIR, pdf_fn)
                if os.path.exists(pdf_path):
                    file_url = file_url_for(pdf_fn, request)
                    txt = f"Ок. Использую шаблон «{tname}» (слот {slot}). Я прикрепил PDF ниже."
                    return {"scenario": "template_use", "reply_text": txt, "file_url": file_url}

            # 2) Если PDF нет/пропал, но есть текст — рендерим PDF из draft_text (тоже без LLM)
            if draft_text:
                fn = safe_filename("contract_saved")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft_text, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")
                file_url = file_url_for(fn, request)
                txt = f"Ок. Использую шаблон «{tname}» (слот {slot}). Я прикрепил PDF ниже."
                return {"scenario": "template_use", "reply_text": txt, "file_url": file_url}

            # 3) Последний шанс: перегенерация из payload (LLM)
            # (если нейросеть упадёт — вернём FALLBACK)
            if not payload_text:
                return {"scenario": "template_use", "reply_text": f"Шаблон «{tname}» повреждён: нет данных для восстановления. Удали и сохрани заново.", "file_url": ""}

            try:
                payload_obj = json.loads(payload_text)
            except Exception:
                payload_obj = {}

            # соберём LLM по шаблону из файлов
            template_text = await build_services_contract_template()
            data_text = build_services_data_min(payload_obj)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"
            draft = await call_gigachat(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            fn = safe_filename("contract_regen")
            out_path = os.path.join(FILES_DIR, fn)
            render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")
            file_url = file_url_for(fn, request)

            txt = f"Ок. Использую шаблон «{tname}» (слот {slot}). Я прикрепил PDF ниже."
            return {"scenario": "template_use", "reply_text": txt, "file_url": file_url}

        # ============ CONTRACT ============
        if scenario == "contract":
            # 0) строим текст шаблона из файлов
            template_text = await build_services_contract_template()

            # 1) строим DATA
            data_text = build_services_data_min(payload)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            # 2) генерим договор (если LLM упал — PDF не делаем)
            draft = await call_gigachat(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            # 3) коммент (не критично)
            comment = ""
            try:
                comment = await call_gigachat(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=420)
                comment = comment.replace("?", "").strip()
            except Exception as e:
                logger.warning("Comment generation failed (contract): %s", e)
                comment = ""

            # 4) если PDF разрешён — делаем файл
            result: Dict[str, str] = {"scenario": "contract", "reply_text": "", "file_url": ""}

            pdf_filename = ""
            if with_file:
                pdf_filename = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, pdf_filename)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")
                result["file_url"] = file_url_for(pdf_filename, request)

                # trial списываем только если реально выдали PDF
                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = "Готово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                result["reply_text"] = txt
            else:
                # без файла: текст + комментарий + paywall если запросили файл, но trial=0
                txt = draft
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                if with_file_requested and (not premium) and trial_left <= 0:
                    txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
                result["reply_text"] = txt
                result["file_url"] = ""

            # 5) сохраняем last_contract (важно для template_save)
            # сохраняем draft всегда; pdf_filename только если действительно создали
            try:
                set_last_contract(
                    uid=uid,
                    draft_text=draft,
                    pdf_filename=pdf_filename if with_file else "",
                    payload_obj=payload,
                )
            except Exception as e:
                logger.warning("Failed to set last_contract: %s", e)

            return result

        # ============ CLAIM ============
        if scenario == "claim":
            def v(x: Any) -> str:
                s = str(x).strip()
                return s if s else "___"

            to_whom = payload.get("Адресат") or payload.get("to_whom") or ""
            from_whom = payload.get("От кого") or payload.get("from_whom") or ""
            circumstances = payload.get("Обстоятельства") or payload.get("viol") or payload.get("Нарушение и обстоятельства") or ""
            reqs = payload.get("Требования") or payload.get("reqs") or ""
            term = payload.get("Сроки исполнения") or payload.get("term") or ""
            contacts = payload.get("Контакты") or payload.get("contacts") or ""
            extra = extract_extra(payload)

            user_text = (
                f"Кому: {v(to_whom)}\n"
                f"От кого: {v(from_whom)}\n"
                f"Обстоятельства: {v(circumstances)}\n"
                f"Требования: {v(reqs)}\n"
                f"Срок исполнения: {v(term)}\n"
                f"Контакты: {v(contacts)}\n"
                f"Доп данные: {v(extra)}\n"
            ).strip()

            draft = await call_gigachat(PROMPT_CLAIM, user_text, max_tokens=1600)

            comment = ""
            try:
                comment = await call_gigachat(PROMPT_CLAIM_COMMENT, user_text, max_tokens=420)
                comment = comment.replace("?", "").strip()
            except Exception as e:
                logger.warning("Comment generation failed (claim): %s", e)
                comment = ""

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")
                file_url = file_url_for(fn, request)

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = "Готово. Я подготовил черновик претензии и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"

                return {"scenario": "claim", "reply_text": txt, "file_url": file_url}

            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "claim", "reply_text": txt, "file_url": ""}

        return {"scenario": scenario, "reply_text": "Неизвестный сценарий.", "file_url": ""}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        return make_error_response(scenario)
