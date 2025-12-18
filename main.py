import os
import re
import uuid
import time
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
# LOGGER
# =========================
logger = logging.getLogger("legalfox")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
if not logger.handlers:
    logger.addHandler(handler)


# =========================
# CONFIG
# =========================
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
FILES_DIR = os.getenv("FILES_DIR", "files")
DB_PATH = os.getenv("DB_PATH", "legalfox.db")

# Trial
FREE_PDF_LIMIT = int(os.getenv("FREE_PDF_LIMIT", "1"))

# LLM: GigaChat
LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "gigachat") or "gigachat").strip().lower()
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Pro")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
# если в Railway ловишь CERTIFICATE_VERIFY_FAILED — поставь env GIGACHAT_VERIFY_SSL=0
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# Templates on disk (document structure)
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")

# User saved templates
USER_TEMPLATES_LIMIT = int(os.getenv("USER_TEMPLATES_LIMIT", "3"))

# Speed knobs
CONTRACT_MAX_TOKENS = int(os.getenv("CONTRACT_MAX_TOKENS", "1600"))
COMMENT_MAX_TOKENS = int(os.getenv("COMMENT_MAX_TOKENS", "220"))
COMMENT_TIMEOUT_SEC = float(os.getenv("COMMENT_TIMEOUT_SEC", "10"))
LLM_TIMEOUT_SEC = float(os.getenv("LLM_TIMEOUT_SEC", "70"))
LLM_RETRY = int(os.getenv("LLM_RETRY", "1"))  # 1 retry on transient errors


# =========================
# PROMPTS
# =========================
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик договора оказания услуг по праву РФ для самозанятых/фрилансеров/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону: сохраняй порядок разделов, заголовки и нумерацию.

Жёсткие правила:
1) Без Markdown.
2) Обращайся к пользователю на "ты" только в комментариях, но НЕ в тексте договора (договор официально-деловой).
3) Ничего не выдумывай: паспорт, ИНН/ОГРН, адреса, реквизиты, суммы, даты, сроки — только если пользователь дал явно.
4) Если данных нет/они мусор — оставляй подчёркивания:
   - короткие поля: "____________"
   - длинные поля: "________________________________________"
   - суммы/даты: "________ руб.", "__.__.____"
5) Никогда не используй заглушки "СТОРОНА_1", "АДРЕС_1" и т.п.
6) Не выводи служебные строки: "TEMPLATE:", "DATA:" и т.п.
7) "нет/не знаю/пусто/—/0/n/a" = данных нет -> подчёркивания.
8) "Доп данные": вставляй только 1–3 конкретных предложения в подходящий раздел. Воду игнорируй.
9) Верстка: короткие абзацы, пустые строки между разделами.

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
"""

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к договору (чек-лист), чтобы он “работал”.

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 6–10 коротких строк.
- Не задавай вопросов и не используй '?'.
- Без токсичных оценок ("некорректно", "ошибки", "не примут").
- Пиши нейтрально: "Добавь…", "Укажи…", "Зафиксируй…", "Проверь…".
"""

PROMPT_CLAIM = """
Ты — LegalFox. Подготовь черновик претензии (досудебной) по праву РФ.

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
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
- Без оценок "некорректно/ошибки/не примут".
- Последняя строка всегда: "Приложи копии: ...".
"""

PROMPT_CLAUSE = """
Ты — LegalFox. Пользователь прислал вопрос или кусок текста.
Ответь по-русски, по делу, без Markdown.
Обращайся на "ты".
Если нужно — переформулируй юридически аккуратнее, сохранив смысл.
Короткие абзацы, пустые строки.
"""


# =========================
# UTIL
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


def pick_uid(payload: Dict[str, Any]) -> Tuple[str, str]:
    # максимально терпимо к BotHelp: берём любое поле, вычищаем цифры если есть
    priority = [
        "bh_user_id", "user_id", "tg_user_id", "telegram_user_id",
        "messenger_user_id", "bothelp_user_id", "cuid", "chat_id"
    ]
    for key in priority:
        v = payload.get(key)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
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


def get_with_file_requested(payload: Dict[str, Any], scenario: str) -> bool:
    # Главный фикс trial: если BotHelp не прислал with_file — по умолчанию считаем,
    # что для contract/claim файл НУЖЕН (и будет выдан при наличии trial/подписки).
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return scenario in ("contract", "claim", "template_use")


def file_url_for(filename: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


def sanitize_comment(text: str) -> str:
    if not text:
        return ""
    t = text.strip().replace("?", "")
    banned = [
        "составлена некорректно", "некорректно", "содержит ошибки", "ошибки",
        "в надлежащий вид", "не примут", "бесполезно", "незаконно", "недействительно",
    ]
    for p in banned:
        t = re.sub(re.escape(p), "", t, flags=re.IGNORECASE).strip()
    # убираем лишние пустые строки
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def extract_extra(payload: Dict[str, Any]) -> str:
    extra = (
        payload.get("Доп данные")
        or payload.get("Доп_данные")
        or payload.get("extra")
        or payload.get("Extra")
        or ""
    )
    return str(extra).strip()


def scenario_alias(raw: str) -> str:
    s = (raw or "").strip().lower()

    # стандартные сценарии
    if s in ("contract", "draft_contract", "договора", "договора_черновик"):
        return "contract"
    if s in ("claim", "draft_claim", "претензия", "претензии", "claim_unpaid"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"

    # мои шаблоны / шаблоны
    if s in ("my_templates", "templates", "мои шаблоны", "шаблоны"):
        return "templates_list"

    # выбор 1/2/3
    if s in ("1", "2", "3", "template_1", "template_2", "template_3"):
        return "template_use"

    # сохранение/удаление
    if s in ("save_template", "template_save", "сохранить шаблон"):
        return "template_save"
    if s in ("delete_template", "template_delete", "удалить шаблон", "delete"):
        return "template_delete"

    return "contract"


def extract_slot(payload: Dict[str, Any]) -> Optional[int]:
    # номер слота может прийти разными полями
    candidates = [
        payload.get("template_slot"),
        payload.get("slot"),
        payload.get("number"),
        payload.get("digit"),
        payload.get("idx"),
        payload.get("template"),
        payload.get("text"),
        payload.get("message"),
        payload.get("msg"),
    ]
    for c in candidates:
        if c is None:
            continue
        s = str(c)
        m = re.search(r"\b([1-3])\b", s)
        if m:
            return int(m.group(1))
    return None


def extract_template_name(payload: Dict[str, Any]) -> str:
    for key in ("template_name", "name", "Название шаблона", "Имя шаблона", "templateTitle"):
        if key in payload and str(payload.get(key)).strip():
            return str(payload.get(key)).strip()
    # иногда BotHelp сохраняет последний ввод в каком-то "answer"
    for key in ("answer", "user_answer", "text"):
        if key in payload and str(payload.get(key)).strip():
            return str(payload.get(key)).strip()
    return ""


# =========================
# DB
# =========================
def db_init():
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            free_pdf_left INTEGER NOT NULL
        )
        """
    )

    # храним "последний договор" как data_text (чтобы можно было сохранить шаблон даже если webhook пришёл без полей)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS last_contract (
            uid TEXT PRIMARY KEY,
            data_text TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # слоты шаблонов 1..3
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_templates (
            uid TEXT NOT NULL,
            slot INTEGER NOT NULL,
            name TEXT NOT NULL,
            data_text TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (uid, slot)
        )
        """
    )

    con.commit()
    con.close()


def ensure_user(uid: str):
    if not uid:
        return
    con = sqlite3.connect(DB_PATH, timeout=30)
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
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else 0


def consume_free(uid: str) -> bool:
    if not uid:
        return False
    ensure_user(uid)
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    left = int(row[0]) if row else 0
    if left <= 0:
        con.close()
        return False
    cur.execute("UPDATE users SET free_pdf_left = free_pdf_left - 1 WHERE uid=?", (uid,))
    con.commit()
    con.close()
    return True


def set_last_contract(uid: str, data_text: str):
    if not uid or not data_text:
        return
    ts = int(time.time())
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO last_contract(uid, data_text, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET data_text=excluded.data_text, updated_at=excluded.updated_at
        """,
        (uid, data_text, ts),
    )
    con.commit()
    con.close()


def get_last_contract(uid: str) -> str:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT data_text FROM last_contract WHERE uid=?", (uid,))
    row = cur.fetchone()
    con.close()
    return str(row[0]) if row else ""


def list_user_templates(uid: str) -> List[Tuple[int, str, int]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "SELECT slot, name, updated_at FROM user_templates WHERE uid=? ORDER BY slot ASC",
        (uid,),
    )
    rows = cur.fetchall()
    con.close()
    out = []
    for r in rows:
        out.append((int(r[0]), str(r[1]), int(r[2])))
    return out


def get_user_template(uid: str, slot: int) -> Optional[Tuple[str, str]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "SELECT name, data_text FROM user_templates WHERE uid=? AND slot=?",
        (uid, slot),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return str(row[0]), str(row[1])


def delete_user_template(uid: str, slot: int) -> bool:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("DELETE FROM user_templates WHERE uid=? AND slot=?", (uid, slot))
    n = cur.rowcount
    con.commit()
    con.close()
    return n > 0


def save_user_template(uid: str, name: str, data_text: str) -> Tuple[bool, str]:
    """
    сохраняем в первый свободный слот 1..3, если все заняты -> лимит
    """
    name = (name or "").strip()
    if not name:
        return False, "Не вижу название шаблона. Напиши коротко, например: «Шаблон для Иванова»."
    if len(name) > 50:
        name = name[:50].strip()

    existing = {slot for slot, _, _ in list_user_templates(uid)}
    if len(existing) >= USER_TEMPLATES_LIMIT:
        return False, "Лимит шаблонов исчерпан (можно сохранить только 3). Удали один из шаблонов и попробуй снова."

    slot = None
    for s in range(1, USER_TEMPLATES_LIMIT + 1):
        if s not in existing:
            slot = s
            break
    if slot is None:
        return False, "Лимит шаблонов исчерпан (можно сохранить только 3)."

    ts = int(time.time())
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO user_templates(uid, slot, name, data_text, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (uid, slot, name, data_text, ts, ts),
    )
    con.commit()
    con.close()
    return True, f"Шаблон сохранён: №{slot} — {name}"


# =========================
# PDF (Cyrillic)
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
            logger.exception("Font register failed: %s", font_path)
    return "Helvetica"


def render_pdf(text: str, out_path: str, title: str):
    font_name = ensure_font_name()
    c = canvas.Canvas(out_path, pagesize=A4)
    width, height = A4

    # margins (ГОСТ-like)
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
# Templates (disk cache)
# =========================
_templates_cache: Dict[str, str] = {}
_templates_lock = asyncio.Lock()

async def _read_file(path: str) -> str:
    loop = asyncio.get_event_loop()
    def _sync():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return await loop.run_in_executor(None, _sync)

async def get_template(name: str) -> str:
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
    base = await get_template(SERVICES_CONTRACT_TEMPLATE)
    tail = await get_template(COMMON_TAIL_TEMPLATE)
    if COMMON_TAIL_PLACEHOLDER not in base:
        raise RuntimeError(f"Placeholder {COMMON_TAIL_PLACEHOLDER} not found in {SERVICES_CONTRACT_TEMPLATE}")
    return base.replace(COMMON_TAIL_PLACEHOLDER, tail)


# =========================
# GigaChat token cache
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
            raise RuntimeError(f"OAuth without access_token: {js}")

        _token_value = token
        _token_exp = _now() + 25 * 60
        logger.info("GigaChat token refreshed rq_uid=%s", rq_uid)
        return _token_value


async def call_gigachat(system_prompt: str, user_content: str, max_tokens: int) -> str:
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
        "temperature": 0.2,
        "max_tokens": int(max_tokens),
        "top_p": 1,
        "stream": False,
    }

    last_err: Optional[Exception] = None
    for attempt in range(LLM_RETRY + 1):
        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SEC, verify=GIGACHAT_VERIFY_SSL) as client:
                r = await client.post(url, headers=headers, json=payload)

                if r.status_code in (401, 403):
                    global _token_value, _token_exp
                    _token_value, _token_exp = None, 0.0
                    token = await get_gigachat_access_token()
                    headers["Authorization"] = f"Bearer {token}"
                    r = await client.post(url, headers=headers, json=payload)

                r.raise_for_status()
                js = r.json()

            content = js["choices"][0]["message"]["content"]
            return strip_markdown_noise(content)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < LLM_RETRY:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            raise

    raise last_err or RuntimeError("LLM failed")


async def call_llm(system_prompt: str, user_input: str, max_tokens: int) -> str:
    if LLM_PROVIDER != "gigachat":
        raise RuntimeError("LLM_PROVIDER must be gigachat")
    return await call_gigachat(system_prompt, user_input, max_tokens=max_tokens)


# =========================
# Data builder (minimal fields)
# =========================
def build_services_data_min(payload: Dict[str, Any]) -> str:
    def v(x: Any) -> str:
        s = str(x).strip()
        if not s:
            return "___"
        if s.lower() in ("нет", "не знаю", "пусто", "-", "—", "0", "n/a"):
            return "___"
        return s

    exec_type = payload.get("exec_type") or ""
    exec_name = payload.get("exec_name") or ""
    client_name = payload.get("client_name") or ""
    service_desc = payload.get("service_desc") or payload.get("Предмет") or ""
    deadline_value = payload.get("deadline_value") or payload.get("Сроки") or ""
    price_value = payload.get("price_value") or payload.get("Цена") or ""
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
app = FastAPI(title="LegalFox API", version="4.0.0-templates")

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
        "user_templates_limit": USER_TEMPLATES_LIMIT,
        "templates_dir": TEMPLATES_DIR,
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    raw = str(payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "").strip()
    scenario = scenario_alias(raw)

    # clause может быть без uid
    if scenario == "clause":
        q = payload.get("Запрос") or payload.get("query") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return {"scenario": "clause", "reply_text": "Напиши вопрос или вставь текст одним сообщением — помогу.", "file_url": ""}
        try:
            answer = await call_llm(PROMPT_CLAUSE, q, max_tokens=900)
            return {"scenario": "clause", "reply_text": answer, "file_url": ""}
        except Exception as e:
            logger.exception("legalfox error (clause): %s", e)
            return {"scenario": "clause", "reply_text": FALLBACK_TEXT, "file_url": ""}

    uid, uid_src = pick_uid(payload)
    if not uid:
        # это причина, почему у тебя "не видит шаблоны" — webhook не присылает uid
        return {"scenario": scenario, "reply_text": "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id).", "file_url": ""}

    ensure_user(uid)

    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload, scenario if scenario not in ("templates_list", "template_save", "template_delete") else "contract")

    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s raw=%s uid=%s(uid_src=%s) premium=%s trial_left=%s with_file_requested=%s with_file=%s",
        scenario, raw, uid, uid_src, premium, trial_left, with_file_requested, with_file
    )

    try:
        # =========================
        # TEMPLATES LIST
        # =========================
        if scenario == "templates_list":
            items = list_user_templates(uid)
            lines = ["Вот твои шаблоны:"]
            # показываем 1..3 всегда
            by_slot = {s: n for s, n, _ in items}
            for s in range(1, USER_TEMPLATES_LIMIT + 1):
                name = by_slot.get(s)
                if name:
                    lines.append(f"{s}) {name}")
                else:
                    lines.append(f"{s}) (пусто)")
            lines.append("")
            lines.append("Выбери номер шаблона (1–3), чтобы использовать его.")
            lines.append("Если хочешь удалить — нажми «Удалить шаблон» и отправь номер 1–3.")
            return {"scenario": "templates_list", "reply_text": "\n".join(lines), "file_url": ""}

        # =========================
        # TEMPLATE SAVE
        # =========================
        if scenario == "template_save":
            # берём данные из payload (если они есть), иначе берём last_contract
            name = extract_template_name(payload)
            data_text = build_services_data_min(payload)
            # если BotHelp НЕ прислал поля договора, build_services_data_min будет почти пустой -> берём last_contract
            if data_text.count("___") > 10:
                fallback = get_last_contract(uid)
                if fallback:
                    data_text = fallback

            if not data_text or len(data_text) < 20:
                return {"scenario": "template_save", "reply_text": "Пока нечего сохранять. Сначала собери договор и получи результат.", "file_url": ""}

            ok, msg = save_user_template(uid, name=name, data_text=data_text)
            return {"scenario": "template_save", "reply_text": msg, "file_url": ""}

        # =========================
        # TEMPLATE DELETE
        # =========================
        if scenario == "template_delete":
            slot = extract_slot(payload)
            if slot is None:
                return {"scenario": "template_delete", "reply_text": "Напиши номер шаблона для удаления: 1, 2 или 3.", "file_url": ""}
            ok = delete_user_template(uid, slot)
            if ok:
                return {"scenario": "template_delete", "reply_text": f"Шаблон №{slot} удалён.", "file_url": ""}
            return {"scenario": "template_delete", "reply_text": f"Шаблон №{slot} и так пуст. Удалять нечего.", "file_url": ""}

        # =========================
        # TEMPLATE USE (1/2/3)
        # =========================
        if scenario == "template_use":
            # slot может быть задан через scenario "1" / "template_1", либо полем
            slot = extract_slot(payload)
            if slot is None:
                # пробуем вытащить из raw
                m = re.search(r"([1-3])", raw)
                slot = int(m.group(1)) if m else None
            if slot is None:
                return {"scenario": "template_use", "reply_text": "Выбери номер шаблона: 1, 2 или 3.", "file_url": ""}

            tpl = get_user_template(uid, slot)
            if not tpl:
                return {"scenario": "template_use", "reply_text": f"Шаблон №{slot} пуст. Сначала сохрани шаблон.", "file_url": ""}

            tpl_name, data_text = tpl

            # генерим договор по шаблону документа + данным из шаблона пользователя
            template_text = await build_services_contract_template()
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"
            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=CONTRACT_MAX_TOKENS)

            # быстрый комментарий, но без тормозов
            comment = ""
            try:
                comment = await asyncio.wait_for(
                    call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=COMMENT_MAX_TOKENS),
                    timeout=COMMENT_TIMEOUT_SEC,
                )
                comment = sanitize_comment(comment)
            except Exception:
                comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok_consume = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok_consume)

                txt = f"Готово. Ты использовал шаблон: {tpl_name}\nЯ подготовил договор и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"

                return {"scenario": "template_use", "reply_text": txt, "file_url": file_url_for(fn, request)}

            # без файла (нет premium и trial=0)
            txt = f"Ты использовал шаблон: {tpl_name}\n\n{draft}"
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "template_use", "reply_text": txt, "file_url": ""}

        # =========================
        # CONTRACT (generate from answers)
        # =========================
        if scenario == "contract":
            template_text = await build_services_contract_template()
            data_text = build_services_data_min(payload)

            # сохраняем last_contract ВСЕГДА (чтобы потом сохранить шаблон даже если webhook не прислал поля)
            set_last_contract(uid, data_text)

            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"
            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=CONTRACT_MAX_TOKENS)

            comment = ""
            try:
                comment = await asyncio.wait_for(
                    call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=COMMENT_MAX_TOKENS),
                    timeout=COMMENT_TIMEOUT_SEC,
                )
                comment = sanitize_comment(comment)
            except Exception:
                comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok_consume = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok_consume)

                txt = "Готово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                return {"scenario": "contract", "reply_text": txt, "file_url": file_url_for(fn, request)}

            # текст без файла
            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "contract", "reply_text": txt, "file_url": ""}

        # =========================
        # CLAIM
        # =========================
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

            draft = await call_llm(PROMPT_CLAIM, user_text, max_tokens=1400)

            comment = ""
            try:
                comment = await asyncio.wait_for(
                    call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=COMMENT_MAX_TOKENS),
                    timeout=COMMENT_TIMEOUT_SEC,
                )
                comment = sanitize_comment(comment)
            except Exception:
                comment = ""

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok_consume = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok_consume)

                txt = "Готово. Я подготовил черновик претензии и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                return {"scenario": "claim", "reply_text": txt, "file_url": file_url_for(fn, request)}

            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "claim", "reply_text": txt, "file_url": ""}

        return {"scenario": scenario, "reply_text": "Неизвестный сценарий.", "file_url": ""}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        # критично: при ошибке НИКОГДА не отдаём file_url (чтобы BotHelp не прикрепил старый файл)
        return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}
