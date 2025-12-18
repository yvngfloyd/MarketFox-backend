import os
import re
import json
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

# Trial (1 PDF на пользователя на все документные сценарии)
FREE_PDF_LIMIT = int(os.getenv("FREE_PDF_LIMIT", "1"))

# LLM: GigaChat
LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "gigachat") or "gigachat").strip().lower()
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")  # base64(ClientID:ClientSecret)
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Pro")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# Templates (файлы в репо)
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")

# Шаблоны пользователя (лимит 3)
USER_TEMPLATES_LIMIT = int(os.getenv("USER_TEMPLATES_LIMIT", "3"))
TEMPLATE_SLOT_MIN = 1
TEMPLATE_SLOT_MAX = 3

# Комментарий (ускорение: можно выключить)
COMMENT_ENABLED = (os.getenv("COMMENT_ENABLED", "1").strip() != "0")


# =========================
# ПРОМТЫ
# =========================
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик договора оказания услуг по праву РФ для самозанятых/фрилансеров/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону: сохраняй порядок разделов, заголовки и нумерацию.

Ключевая цель: документ должен быть пригоден для печати и подписи, без мусора и без выдумок.

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
   Только реальные данные или подчёркивания.
5) Запрещено переносить в итоговый договор служебные строки и подсказки:
   не выводи "TEMPLATE:", "DATA:", "Пример:", "введите", "поле", "как указано пользователем" и т.п.
6) Если пользователь написал "нет" / "не знаю" / "пусто" / "—" / "0" / "n/a" — считай, что данных нет.
   В договоре оставляй подчёркивания.
7) Жёсткая фильтрация мусора (обязательно):
   - Если значение похоже на шаблонные слова, случайные маркеры, переменные, технические теги, emoji, набор букв/цифр без смысла —
     НЕ вставляй его. Оставь подчёркивания.
   - Если в значении встречаются слова: "СТОРОНА", "АДРЕС", "ПРЕДМЕТ", "ОПЛАТА", "ПАСПОРТ", "ИНН", "ОГРН" как заглушки,
     или оно выглядит как пример из инструкции — НЕ вставляй, оставь подчёркивания.
8) Используй введённые данные максимально конкретно, но только если они выглядят реальными.
9) "Доп данные":
   - Если там есть конкретные условия — вставь 1–3 предложения в подходящий раздел.
   - Если там мусор/вода — игнорируй и не цитируй это в договоре.
10) Верстка: короткие абзацы, пустые строки между разделами.

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
""".strip()

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к договору (чек-лист).

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 6–10 коротких строк.
- Не задавай вопросов и не используй '?'.
- Пиши нейтрально: "Проверь…", "Укажи…", "Зафиксируй…", "Пропиши…".
- Не обещай результат и не выдавай себя за адвоката.

Выводи только комментарий.
""".strip()

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
""".strip()

PROMPT_CLAIM_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к претензии (чек-лист).

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 7–11 коротких строк.
- Не задавай вопросов и не используй '?'.
- Пиши нейтрально: "Добавь…", "Укажи…", "Зафиксируй…", "Приложи…".
- Последняя строка ВСЕГДА: "Приложи копии: ...".

Выводи только комментарий.
""".strip()

PROMPT_CLAUSE = """
Ты — LegalFox. Пользователь прислал вопрос или кусок текста.
Ответь по-русски, по делу, без Markdown.
Обращайся на "ты".
Если нужно — переформулируй юридически аккуратнее, сохранив смысл.
Короткие абзацы, пустые строки.
""".strip()


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


def sanitize_comment(text: str) -> str:
    if not text:
        return ""
    t = text.strip().replace("?", "")
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


def pick_uid(payload: Dict[str, Any]) -> Tuple[str, str]:
    priority = ["bh_user_id", "user_id", "tg_user_id", "telegram_user_id", "messenger_user_id", "bothelp_user_id", "cuid"]
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


def get_with_file_requested(payload: Dict[str, Any]) -> bool:
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return False


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("contract", "draft_contract", "договора", "договора_черновик"):
        return "contract"
    if s in ("claim", "draft_claim", "претензия", "претензии", "claim_unpaid"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    if s in ("templates_list", "my_templates", "templates"):
        return "templates_list"
    if s in ("template_save", "save_template"):
        return "template_save"
    if s in ("template_use", "use_template"):
        return "template_use"
    if s in ("template_delete", "delete_template"):
        return "template_delete"
    # НОВОЕ: показать шаблон (без PDF/LLM)
    if s in ("template_show", "show_template", "template_preview", "preview_template"):
        return "template_show"
    return s


def get_template_slot(payload: Dict[str, Any]) -> Optional[int]:
    # ВАЖНО: "template slot" с пробелом
    raw = (
        payload.get("template slot")
        or payload.get("template_slot")
        or payload.get("templateSlot")
        or payload.get("slot")
        or payload.get("Slot")
        or None
    )
    if raw is None:
        return None
    s = str(raw).strip()
    s = re.sub(r"[^\d]", "", s)
    if not s:
        return None
    try:
        n = int(s)
    except Exception:
        return None
    if n < TEMPLATE_SLOT_MIN or n > TEMPLATE_SLOT_MAX:
        return None
    return n


def get_template_name(payload: Dict[str, Any]) -> str:
    raw = (
        payload.get("template_name")
        or payload.get("template name")
        or payload.get("Template name")
        or payload.get("Template_name")
        or payload.get("name")
        or payload.get("Name")
        or ""
    )
    return str(raw).strip()


# =========================
# БД
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
    cur.execute("INSERT OR IGNORE INTO users(uid, free_pdf_left) VALUES(?, ?)", (uid, FREE_PDF_LIMIT))
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
# TEMPLATES (файловые, кэш)
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
# USER TEMPLATES (3 slots)
# =========================
def db_list_user_templates(uid: str) -> List[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "SELECT slot, name, payload_json, created_at FROM user_templates WHERE uid=? ORDER BY slot ASC",
        (uid,),
    )
    rows = cur.fetchall()
    con.close()
    out: List[Dict[str, Any]] = []
    for slot, name, payload_json, created_at in rows:
        out.append({"slot": int(slot), "name": str(name), "payload_json": payload_json, "created_at": int(created_at)})
    return out


def db_get_user_template(uid: str, slot: int) -> Optional[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "SELECT slot, name, payload_json, created_at FROM user_templates WHERE uid=? AND slot=?",
        (uid, int(slot)),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    slot, name, payload_json, created_at = row
    return {"slot": int(slot), "name": str(name), "payload_json": payload_json, "created_at": int(created_at)}


def db_delete_user_template(uid: str, slot: int) -> bool:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("DELETE FROM user_templates WHERE uid=? AND slot=?", (uid, int(slot)))
    changed = cur.rowcount
    con.commit()
    con.close()
    return changed > 0


def db_find_next_free_slot(uid: str) -> Optional[int]:
    existing = {t["slot"] for t in db_list_user_templates(uid)}
    for s in range(TEMPLATE_SLOT_MIN, TEMPLATE_SLOT_MAX + 1):
        if s not in existing:
            return s
    return None


def db_save_user_template(uid: str, name: str, payload_dict: Dict[str, Any], slot: Optional[int] = None) -> Tuple[bool, Optional[int], str]:
    if not uid:
        return False, None, "uid empty"

    name = (name or "").strip()
    if not name:
        return False, None, "template name empty"

    if slot is None:
        slot = db_find_next_free_slot(uid)
        if slot is None:
            return False, None, "limit"
    else:
        if slot < TEMPLATE_SLOT_MIN or slot > TEMPLATE_SLOT_MAX:
            return False, None, "bad_slot"
        if db_get_user_template(uid, slot) is not None:
            return False, None, "slot_busy"

    payload_json = json.dumps(payload_dict, ensure_ascii=False)

    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO user_templates(uid, slot, name, payload_json, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (uid, int(slot), name, payload_json, int(time.time())),
    )
    con.commit()
    con.close()
    return True, slot, "ok"


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
# DATA BUILDER (минимальные поля)
# =========================
def build_services_data_min(payload: Dict[str, Any]) -> str:
    def v(x: Any) -> str:
        s = str(x).strip()
        return s if s else "___"

    exec_type = payload.get("exec_type") or ""
    exec_name = payload.get("exec_name") or ""
    client_name = payload.get("client_name") or ""
    service_desc = payload.get("service_desc") or payload.get("Предмет") or ""
    deadline_value = payload.get("deadline_value") or payload.get("Сроки") or ""
    price_value = payload.get("price_value") or payload.get("Цена") or payload.get("Сроки и оплата") or ""
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


def extract_contract_payload_for_template(payload: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "exec_type", "exec_name", "client_name", "service_desc",
        "deadline_value", "price_value", "acceptance_value",
        "Доп данные", "Доп_данные", "extra", "Extra",
        "Предмет", "Сроки", "Цена", "Сроки и оплата",
    ]
    out: Dict[str, Any] = {}
    for k in keys:
        if k in payload and payload.get(k) is not None:
            out[k] = payload.get(k)
    return out


# =========================
# FASTAPI
# =========================
app = FastAPI(title="LegalFox API", version="4.1.0-template-show")

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
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    uid, uid_src = pick_uid(payload)
    if not uid:
        return {"scenario": scenario, "reply_text": "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id).", "file_url": ""}

    ensure_user(uid)

    premium = get_premium_flag(payload)
    with_file_requested = get_with_file_requested(payload)

    # =========================
    # 3 функция (clause) — только по подписке
    # =========================
    if scenario == "clause":
        if not premium:
            return {
                "scenario": "clause",
                "reply_text": "Эта функция доступна по подписке. Она помогает править формулировки и давать рекомендации по тексту.",
                "file_url": ""
            }
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

    # =========================
    # ШАБЛОНЫ: список
    # =========================
    if scenario == "templates_list":
        try:
            items = db_list_user_templates(uid)
            if not items:
                return {
                    "scenario": "templates_list",
                    "reply_text": "У тебя пока нет сохранённых шаблонов.\nСгенерируй договор и нажми «Сохранить шаблон».",
                    "file_url": ""
                }

            lines = ["Твои шаблоны:"]
            used_slots = set()
            for it in items:
                used_slots.add(it["slot"])
                lines.append(f"{it['slot']}) {it['name']}")

            free_slots = [str(s) for s in range(1, 4) if s not in used_slots]
            if free_slots:
                lines.append("")
                lines.append("Свободные слоты: " + ", ".join(free_slots))

            lines.append("")
            lines.append("Напиши номер слота (1–3), чтобы посмотреть шаблон.")
            return {"scenario": "templates_list", "reply_text": "\n".join(lines), "file_url": ""}
        except Exception as e:
            logger.exception("templates_list error: %s", e)
            return {"scenario": "templates_list", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # =========================
    # НОВОЕ: показать шаблон (без PDF/LLM/Trial)
    # Это именно то, что ты хочешь в поле "ОТВЕТ ИИ"
    # =========================
    if scenario == "template_show":
        slot = get_template_slot(payload)
        if not slot:
            return {"scenario": "template_show", "reply_text": "Введи номер слота (1, 2 или 3).", "file_url": ""}

        try:
            tpl = db_get_user_template(uid, slot)
            if not tpl:
                return {"scenario": "template_show", "reply_text": f"В слоте {slot} нет шаблона. Открой «Мои шаблоны» и проверь список.", "file_url": ""}

            saved_payload = json.loads(tpl["payload_json"])
            data_text = build_services_data_min(saved_payload)

            txt = (
                f"Шаблон {slot}: «{tpl['name']}»\n\n"
                f"{data_text}\n\n"
                f"Если хочешь собрать PDF по этому шаблону — нажми кнопку «Шаблон {slot}» (или сделай вебхук template_use со slot={slot})."
            )
            return {"scenario": "template_show", "reply_text": txt, "file_url": ""}
        except Exception as e:
            logger.exception("template_show error: %s", e)
            return {"scenario": "template_show", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # =========================
    # ШАБЛОНЫ: сохранить
    # =========================
    if scenario == "template_save":
        try:
            template_name = get_template_name(payload)
            if not template_name:
                return {
                    "scenario": "template_save",
                    "reply_text": "Напиши название для шаблона (например: «Шаблон для Иванова»), и я сохраню его.",
                    "file_url": ""
                }

            tpl_payload = extract_contract_payload_for_template(payload)

            ok, used_slot, code = db_save_user_template(uid, template_name, tpl_payload, slot=None)

            if not ok and code == "limit":
                return {
                    "scenario": "template_save",
                    "reply_text": (
                        "Лимит шаблонов достигнут: у тебя уже сохранены 3 из 3.\n"
                        "Чтобы сохранить новый, удали один из текущих в разделе «Мои шаблоны» → «Удалить шаблон»."
                    ),
                    "file_url": ""
                }

            if not ok:
                return {"scenario": "template_save", "reply_text": "Не получилось сохранить шаблон. Попробуй ещё раз.", "file_url": ""}

            return {
                "scenario": "template_save",
                "reply_text": f"Сохранил. Шаблон «{template_name}» записан в слот {used_slot}.",
                "file_url": ""
            }

        except Exception as e:
            logger.exception("template_save error: %s", e)
            return {"scenario": "template_save", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # =========================
    # ШАБЛОНЫ: удалить
    # =========================
    if scenario == "template_delete":
        slot = get_template_slot(payload)
        if not slot:
            return {"scenario": "template_delete", "reply_text": "Укажи номер шаблона (1, 2 или 3), который нужно удалить.", "file_url": ""}
        try:
            ok = db_delete_user_template(uid, slot)
            if ok:
                return {"scenario": "template_delete", "reply_text": f"Удалил шаблон из слота {slot}.", "file_url": ""}
            return {"scenario": "template_delete", "reply_text": f"В слоте {slot} нет шаблона.", "file_url": ""}
        except Exception as e:
            logger.exception("template_delete error: %s", e)
            return {"scenario": "template_delete", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # =========================
    # ШАБЛОНЫ: использовать (генерация PDF как обычно)
    # =========================
    if scenario == "template_use":
        slot = get_template_slot(payload)
        if not slot:
            return {"scenario": "template_use", "reply_text": "Выбери слот шаблона (1, 2 или 3).", "file_url": ""}

        tpl = db_get_user_template(uid, slot)
        if not tpl:
            return {"scenario": "template_use", "reply_text": f"В слоте {slot} нет шаблона. Открой «Мои шаблоны» и проверь список.", "file_url": ""}

        trial_left = free_left(uid)
        can_file = premium or (trial_left > 0)
        with_file = True and can_file  # при использовании шаблона всегда пытаемся отдать PDF

        logger.info(
            "Scenario=template_use uid=%s(uid_src=%s) premium=%s trial_left=%s slot=%s with_file=%s",
            uid, uid_src, premium, trial_left, slot, with_file
        )

        try:
            template_text = await build_services_contract_template()

            saved_payload = json.loads(tpl["payload_json"])
            if extract_extra(payload).strip():
                saved_payload["Доп данные"] = extract_extra(payload)

            data_text = build_services_data_min(saved_payload)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            comment = ""
            if COMMENT_ENABLED:
                try:
                    comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=380)
                    comment = sanitize_comment(comment)
                except Exception as e:
                    logger.warning("Comment generation failed (template_use): %s", e)
                    comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = f"Готово. Ты использовал шаблон: «{tpl['name']}». Я прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"

                return {"scenario": "template_use", "reply_text": txt, "file_url": file_url_for(fn, request)}

            return {
                "scenario": "template_use",
                "reply_text": (
                    f"Шаблон «{tpl['name']}» выбран.\n"
                    "Пробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
                ),
                "file_url": ""
            }

        except Exception as e:
            logger.exception("template_use error: %s", e)
            return {"scenario": "template_use", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # =========================
    # ДОКУМЕНТЫ: общий trial
    # =========================
    trial_left = free_left(uid)
    can_file = premium or (trial_left > 0)
    with_file = with_file_requested and can_file

    logger.info(
        "Scenario=%s uid=%s(uid_src=%s) premium=%s trial_left=%s FREE_PDF_LIMIT=%s with_file_requested=%s with_file=%s",
        scenario, uid, uid_src, premium, trial_left, FREE_PDF_LIMIT, with_file_requested, with_file
    )

    try:
        if scenario == "contract":
            template_text = await build_services_contract_template()
            data_text = build_services_data_min(payload)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            comment = ""
            if COMMENT_ENABLED:
                try:
                    comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=380)
                    comment = sanitize_comment(comment)
                except Exception as e:
                    logger.warning("Comment generation failed (contract): %s", e)
                    comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = "Готово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"

                return {"scenario": "contract", "reply_text": txt, "file_url": file_url_for(fn, request)}

            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "contract", "reply_text": txt, "file_url": ""}

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

            draft = await call_llm(PROMPT_CLAIM, user_text, max_tokens=1600)

            comment = ""
            if COMMENT_ENABLED:
                try:
                    comment = await call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=380)
                    comment = sanitize_comment(comment)
                except Exception as e:
                    logger.warning("Comment generation failed (claim): %s", e)
                    comment = ""

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

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
        return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}
