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
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")  # base64(ClientID:ClientSecret)
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# Templates (server-side doc templates)
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")

# User template slots
USER_TEMPLATE_SLOTS = 3


# =========================
# PROMPTS
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
   - короткие поля (ФИО, дата, сумма): "____________"
   - длинные поля (адрес, реквизиты, паспорт): "________________________________________"
   - суммы/даты: "________ руб.", "__.__.____"
4) Никогда не используй заглушки типа "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ", "ОПЛАТА" и т.п.
5) Запрещено переносить в итоговый договор служебные строки и подсказки:
   не выводи "TEMPLATE:", "DATA:", "Пример:", "введите", "поле", "как указано пользователем" и т.п.
6) Если пользователь написал "нет" / "не знаю" / "пусто" / "—" / "0" / "n/a" — считай, что данных нет.
7) Жёсткая фильтрация мусора:
   - переменные/теги вида {%...%}, {{...}}, <...>, набор букв без смысла — игнорируй и оставляй подчёркивания.
8) "Доп данные":
   - если есть конкретные условия — вставь 1–3 предложения в подходящий раздел.
   - если мусор/вода — игнорируй.
9) Верстка: короткие абзацы, пустые строки между разделами.
10) Обращайся нейтрально: без "вы", без "ты" внутри документа (документ официальный).

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
"""

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к договору (чек-лист), основываясь на данных пользователя.

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 6–10 коротких строк.
- Не задавай вопросов и не используй '?'.
- Не используй оценочные слова: "некорректно", "ошибка", "бесполезно", "не примут".
- Пиши нейтрально: "Добавь…", "Укажи…", "Зафиксируй…", "Проверь…".
- Если данных мало — перечисли, что именно стоит заполнить (срок, цена, приемка, реквизиты).

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
- Не используй оценочные слова: "некорректно", "ошибки", "в надлежащий вид", "не примут", "бесполезно".
- Пиши нейтрально: "Добавь…", "Укажи…", "Зафиксируй…", "Приложи…".
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
# UTILS
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


def v_str(x: Any, default: str = "___") -> str:
    s = str(x).strip() if x is not None else ""
    if not s:
        return default
    low = s.lower()
    if low in ("нет", "не знаю", "пусто", "-", "—", "0", "n/a", "na", "none"):
        return default
    return s


def extract_extra(payload: Dict[str, Any]) -> str:
    extra = (
        payload.get("Доп данные")
        or payload.get("Доп_данные")
        or payload.get("extra")
        or payload.get("Extra")
        or ""
    )
    return str(extra).strip()


def pick_uid(payload: Dict[str, Any]) -> Tuple[str, str]:
    priority = ["bh_user_id", "user_id", "tg_user_id", "telegram_user_id", "messenger_user_id", "bothelp_user_id", "cuid"]
    for key in priority:
        v = payload.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        digits = re.sub(r"\D+", "", s)
        if digits:
            return digits, f"{key}:digits"
        return s, key
    return "", ""


def get_premium_flag(payload: Dict[str, Any]) -> bool:
    for key in ["Premium", "premium", "PREMIUM", "is_premium", "Подписка"]:
        if key in payload:
            return normalize_bool(payload.get(key))
    return False


def get_with_file_requested(payload: Dict[str, Any]) -> bool:
    # BotHelp обычно шлёт with_file="1"
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return False


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("contract", "draft_contract", "договора", "договора_черновик", "services_contract"):
        return "contract"
    if s in ("claim", "draft_claim", "претензия", "претензии", "claim_unpaid"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    if s in ("template_save", "template_use", "template_list", "template_delete"):
        return s
    return "contract"


def get_template_slot(payload: Dict[str, Any]) -> Optional[int]:
    # принимает "template slot" (с пробелом), и варианты
    raw = (
        payload.get("template slot")
        or payload.get("template_slot")
        or payload.get("template-slot")
        or payload.get("slot")
        or ""
    )
    s = str(raw).strip()
    if not s:
        return None
    try:
        n = int(re.sub(r"\D+", "", s))
        if 1 <= n <= USER_TEMPLATE_SLOTS:
            return n
        return None
    except Exception:
        return None


def get_template_name(payload: Dict[str, Any]) -> str:
    # у тебя поле "template - name"
    raw = (
        payload.get("template - name")
        or payload.get("template_name")
        or payload.get("template-name")
        or payload.get("name")
        or ""
    )
    return str(raw).strip()


def sanitize_comment(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = t.replace("?", "")
    banned = [
        "составлена некорректно",
        "некорректно",
        "содержит ошибки",
        "ошибки",
        "привести её в надлежащий вид",
        "в надлежащий вид",
        "не примут",
        "бесполезно",
        "незаконно",
        "недействительно",
    ]
    for p in banned:
        t = re.sub(re.escape(p), "", t, flags=re.IGNORECASE).strip()
    # подчистим пустые строки
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def auto_template_name(payload: Dict[str, Any], slot: int) -> str:
    client = (payload.get("client_name") or payload.get("Заказчик") or "").strip()
    client = re.sub(r"\s+", " ", client)
    client = re.sub(r"[^\w\s\-А-Яа-яЁё]", "", client).strip()
    if client and len(client) >= 2:
        return f"Шаблон для {client[:32]}"
    return f"Шаблон {slot} ({time.strftime('%d.%m')})"


def make_response(scenario: str, text: str, file_url: str = "") -> Dict[str, str]:
    # Важно: отдаём и reply_text и AI (под твой BotHelp)
    return {"scenario": scenario, "reply_text": text, "AI": text, "file_url": file_url}


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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_templates (
            uid TEXT NOT NULL,
            slot INTEGER NOT NULL,
            name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (uid, slot)
        )
        """
    )

    con.commit()
    con.close()


def ensure_user(uid: str):
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users(uid, free_pdf_left) VALUES(?, ?)", (uid, FREE_PDF_LIMIT))
    con.commit()
    con.close()


def free_left(uid: str) -> int:
    ensure_user(uid)
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT free_pdf_left FROM users WHERE uid=?", (uid,))
    row = cur.fetchone()
    con.close()
    return int(row[0]) if row else 0


def consume_free(uid: str) -> bool:
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


def db_get_templates(uid: str) -> List[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT slot, name, payload_json FROM user_templates WHERE uid=? ORDER BY slot ASC", (uid,))
    rows = cur.fetchall()
    con.close()
    out = []
    for slot, name, payload_json in rows:
        out.append({"slot": int(slot), "name": name, "payload_json": payload_json})
    return out


def db_get_template(uid: str, slot: int) -> Optional[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT name, payload_json FROM user_templates WHERE uid=? AND slot=?", (uid, slot))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    name, payload_json = row
    return {"slot": slot, "name": name, "payload_json": payload_json}


def db_next_free_slot(uid: str) -> Optional[int]:
    used = {t["slot"] for t in db_get_templates(uid)}
    for s in range(1, USER_TEMPLATE_SLOTS + 1):
        if s not in used:
            return s
    return None


def db_save_template(uid: str, slot: int, name: str, payload_json: str) -> None:
    ts = int(time.time())
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO user_templates(uid, slot, name, payload_json, created_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(uid, slot) DO UPDATE SET
          name=excluded.name,
          payload_json=excluded.payload_json,
          updated_at=excluded.updated_at
        """,
        (uid, slot, name, payload_json, ts, ts),
    )
    con.commit()
    con.close()


def db_delete_template(uid: str, slot: int) -> bool:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("DELETE FROM user_templates WHERE uid=? AND slot=?", (uid, slot))
    n = cur.rowcount
    con.commit()
    con.close()
    return n > 0


# =========================
# PDF
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

    # поля ~ ГОСТ
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
# SERVER TEMPLATES (cache)
# =========================
_templates_cache: Dict[str, str] = {}
_templates_lock = asyncio.Lock()


async def _read_file(path: str) -> str:
    loop = asyncio.get_event_loop()

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

        # токен мог протухнуть
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


async def call_llm(system_prompt: str, user_input: str, max_tokens: int = 1400) -> str:
    if LLM_PROVIDER != "gigachat":
        raise RuntimeError("LLM_PROVIDER must be gigachat")
    return await call_gigachat(system_prompt, user_input, max_tokens=max_tokens)


# =========================
# DATA BUILDERS
# =========================
def build_services_data_min(payload: Dict[str, Any]) -> str:
    # твои поля:
    # exec_type, exec_name, client_name, service_desc, deadline_value, price_value, acceptance_value, "Доп данные"
    exec_type = v_str(payload.get("exec_type"))
    exec_name = v_str(payload.get("exec_name"))
    client_name = v_str(payload.get("client_name"))
    service_desc = v_str(payload.get("service_desc") or payload.get("Предмет"))
    deadline_value = v_str(payload.get("deadline_value") or payload.get("Сроки"))
    price_value = v_str(payload.get("price_value"))
    acceptance_value = v_str(payload.get("acceptance_value"))
    extra = v_str(extract_extra(payload))

    data = (
        f"Исполнитель:\n"
        f"- статус: {exec_type}\n"
        f"- как указать: {exec_name}\n\n"
        f"Заказчик:\n"
        f"- как указать: {client_name}\n\n"
        f"Услуга:\n"
        f"- описание: {service_desc}\n\n"
        f"Сроки:\n"
        f"- значение: {deadline_value}\n\n"
        f"Цена и оплата:\n"
        f"- значение: {price_value}\n\n"
        f"Приёмка:\n"
        f"- значение: {acceptance_value}\n\n"
        f"Доп данные:\n{extra}\n"
    )
    return data.strip()


def compact_payload_for_template(payload: Dict[str, Any]) -> Dict[str, Any]:
    # сохраняем только то, что нужно для повторной сборки
    return {
        "exec_type": str(payload.get("exec_type") or "").strip(),
        "exec_name": str(payload.get("exec_name") or "").strip(),
        "client_name": str(payload.get("client_name") or "").strip(),
        "service_desc": str(payload.get("service_desc") or "").strip(),
        "deadline_value": str(payload.get("deadline_value") or "").strip(),
        "price_value": str(payload.get("price_value") or "").strip(),
        "acceptance_value": str(payload.get("acceptance_value") or "").strip(),
        "Доп данные": str(payload.get("Доп данные") or payload.get("Доп_данные") or "").strip(),
    }


# =========================
# FASTAPI
# =========================
app = FastAPI(title="LegalFox API", version="4.0.0-user-templates-3-slots")

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
        "user_template_slots": USER_TEMPLATE_SLOTS,
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    # 3 функция (без uid можно, но лучше с uid для статистики)
    if scenario == "clause":
        q = payload.get("Запрос") or payload.get("query") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return make_response("clause", "Напиши вопрос или вставь текст одним сообщением — помогу.", "")
        try:
            answer = await call_llm(PROMPT_CLAUSE, q, max_tokens=900)
            return make_response("clause", answer, "")
        except Exception as e:
            logger.exception("legalfox error (clause): %s", e)
            return make_response("clause", FALLBACK_TEXT, "")

    # остальные сценарии: нужен uid
    uid, uid_src = pick_uid(payload)
    if not uid:
        return make_response(scenario, "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id).", "")

    ensure_user(uid)

    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload)

    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s uid=%s(uid_src=%s) premium=%s trial_left=%s FREE_PDF_LIMIT=%s with_file_requested=%s with_file=%s",
        scenario, uid, uid_src, premium, trial_left, FREE_PDF_LIMIT, with_file_requested, with_file
    )

    try:
        # =========================
        # TEMPLATE LIST
        # =========================
        if scenario == "template_list":
            items = db_get_templates(uid)
            if not items:
                txt = (
                    "Пока нет сохранённых шаблонов.\n\n"
                    "Сначала собери договор и получи результат, затем нажми «Сохранить шаблон»."
                )
                return make_response("template_list", txt, "")

            used_slots = {t["slot"] for t in items}
            free_slots = [str(s) for s in range(1, USER_TEMPLATE_SLOTS + 1) if s not in used_slots]

            lines = ["Твои шаблоны:"]
            for t in items:
                lines.append(f"{t['slot']}) {t['name']}")
            if free_slots:
                lines.append("")
                lines.append(f"Свободные слоты: {', '.join(free_slots)}")

            lines.append("")
            lines.append("Нажми 1–3, чтобы использовать шаблон. Для удаления — нажми «Удалить шаблон».")
            return make_response("template_list", "\n".join(lines), "")

        # =========================
        # TEMPLATE SAVE (autofill slot 1->2->3)
        # =========================
        if scenario == "template_save":
            # игнорируем просьбу "ИИ спроси имя" — мы не спрашиваем. Если поле пришло — можно использовать, но не обязательно.
            incoming_name = get_template_name(payload)
            incoming_name = re.sub(r"\s+", " ", incoming_name).strip()

            # слот из payload может быть пустой строкой — тогда автослот
            slot = get_template_slot(payload)
            if slot is None:
                slot = db_next_free_slot(uid)

            if slot is None:
                txt = (
                    "Лимит шаблонов достигнут: можно хранить максимум 3.\n"
                    "Удалите любой шаблон в «Мои шаблоны» и затем сохраните новый."
                )
                return make_response("template_save", txt, "")

            # имя: если пользователь прислал валидное — используем, иначе авто
            if incoming_name and incoming_name.lower() not in ("нет", "не знаю", "-", "—", "0", "n/a"):
                # чуть чистим
                name = re.sub(r"[^\w\s\-А-Яа-яЁё]", "", incoming_name).strip()
                name = name[:48] if name else auto_template_name(payload, slot)
            else:
                name = auto_template_name(payload, slot)

            tpl_payload = compact_payload_for_template(payload)
            payload_json = json.dumps(tpl_payload, ensure_ascii=False)

            db_save_template(uid, slot, name, payload_json)

            txt = (
                f"Сохранил шаблон «{name}» (слот {slot}).\n"
                "Открой «Мои шаблоны», чтобы использовать или удалить."
            )
            return make_response("template_save", txt, "")

        # =========================
        # TEMPLATE DELETE
        # =========================
        if scenario == "template_delete":
            slot = get_template_slot(payload)
            if slot is None:
                return make_response("template_delete", "Укажи номер слота (1–3), который нужно удалить.", "")

            ok = db_delete_template(uid, slot)
            if not ok:
                return make_response("template_delete", f"В слоте {slot} нет шаблона.", "")

            return make_response("template_delete", f"Удалил шаблон из слота {slot}.", "")

        # =========================
        # TEMPLATE USE (generate contract from saved data)
        # =========================
        if scenario == "template_use":
            slot = get_template_slot(payload)
            if slot is None:
                return make_response("template_use", "Выбери слот 1–3, чтобы использовать шаблон.", "")

            tpl = db_get_template(uid, slot)
            if not tpl:
                return make_response("template_use", f"В слоте {slot} нет шаблона. Открой «Мои шаблоны» и проверь список.", "")

            tpl_name = tpl["name"]
            try:
                tpl_payload = json.loads(tpl["payload_json"])
            except Exception:
                tpl_payload = {}

            # соберём как обычный contract payload
            merged = dict(payload)
            merged.update(tpl_payload)

            template_text = await build_services_contract_template()
            data_text = build_services_data_min(merged)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=360)
                comment = sanitize_comment(comment)
            except Exception as e:
                logger.warning("Comment generation failed (template_use): %s", e)
                comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")
                f_url = file_url_for(fn, request)

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = f"Готово. Использовал шаблон «{tpl_name}» и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                return make_response("template_use", txt, f_url)

            txt = f"Использовал шаблон «{tpl_name}».\n\n{draft}"
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return make_response("template_use", txt, "")

        # =========================
        # CONTRACT
        # =========================
        if scenario == "contract":
            template_text = await build_services_contract_template()
            data_text = build_services_data_min(payload)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=360)
                comment = sanitize_comment(comment)
            except Exception as e:
                logger.warning("Comment generation failed (contract): %s", e)
                comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")
                f_url = file_url_for(fn, request)

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = "Готово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                return make_response("contract", txt, f_url)

            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return make_response("contract", txt, "")

        # =========================
        # CLAIM
        # =========================
        if scenario == "claim":
            to_whom = payload.get("Адресат") or payload.get("to_whom") or ""
            from_whom = payload.get("От кого") or payload.get("from_whom") or ""
            circumstances = payload.get("Обстоятельства") or payload.get("viol") or payload.get("Нарушение и обстоятельства") or ""
            reqs = payload.get("Требования") or payload.get("reqs") or ""
            term = payload.get("Сроки исполнения") or payload.get("term") or ""
            contacts = payload.get("Контакты") or payload.get("contacts") or ""
            extra = extract_extra(payload)

            user_text = (
                f"Кому: {v_str(to_whom)}\n"
                f"От кого: {v_str(from_whom)}\n"
                f"Обстоятельства: {v_str(circumstances)}\n"
                f"Требования: {v_str(reqs)}\n"
                f"Срок исполнения: {v_str(term)}\n"
                f"Контакты: {v_str(contacts)}\n"
                f"Доп данные: {v_str(extra)}\n"
            ).strip()

            draft = await call_llm(PROMPT_CLAIM, user_text, max_tokens=1600)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=420)
                comment = sanitize_comment(comment)
            except Exception as e:
                logger.warning("Comment generation failed (claim): %s", e)
                comment = ""

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")
                f_url = file_url_for(fn, request)

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = "Готово. Я подготовил черновик претензии и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                return make_response("claim", txt, f_url)

            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return make_response("claim", txt, "")

        return make_response(scenario, "Неизвестный сценарий.", "")

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        # критично: не отдаём file_url на ошибках
        return make_response(scenario, FALLBACK_TEXT, "")
