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
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Pro")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# Templates (файлы-шаблоны договора)
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")

# Лимит пользовательских шаблонов
MAX_USER_TEMPLATES = int(os.getenv("MAX_USER_TEMPLATES", "3"))  # слоты 1..3


# =========================
# ПРОМТЫ
# =========================
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик договора оказания услуг по праву РФ для самозанятых/фрилансеров/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону: сохраняй порядок разделов, заголовки и нумерацию.

Жёсткие правила:
1) Без Markdown.
2) Ничего не выдумывай: паспорт, ИНН/ОГРН, адреса, реквизиты, суммы, даты, сроки — только если пользователь дал явно и однозначно.
3) Если данных нет / они неполные / размытые / противоречивые / выглядят как “мусор” — НЕ вставляй их.
   Вместо этого оставляй ПУСТОЕ МЕСТО длинными подчёркиваниями.
   Используй такие заглушки:
   - короткие поля: "____________"
   - длинные поля: "________________________________________"
   - суммы/даты: "________ руб.", "__.__.____"
4) Никогда не используй "СТОРОНА_1", "АДРЕС_1" и т.п.
5) Не выводи служебные строки: "TEMPLATE:", "DATA:", "поле", "пример" и т.п.
6) Если пользователь написал "нет" / "не знаю" / "пусто" / "—" / "0" / "n/a" — считай, что данных нет → подчёркивания.
7) Доп. данные: если есть конкретика — вставь 1–3 предложения в подходящий раздел; мусор игнорируй.
8) Верстка: короткие абзацы, пустые строки между разделами.

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
"""

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай короткий комментарий (чек-лист) по договору на основе введённых данных.

Строго:
- Без Markdown.
- Обращайся на "ты".
- 6–10 коротких строк.
- Не задавай вопросов и не используй '?'.
- Пиши нейтрально: "Добавь…", "Укажи…", "Зафиксируй…", "Проверь…".
"""

PROMPT_CLAIM = """
Ты — LegalFox. Подготовь черновик претензии (досудебной) по праву РФ.

Строго:
- Без Markdown.
- Официальный стиль.
- Если данных нет — "___". Если есть — вставляй как есть.
- Ничего не выдумывай.
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
- Последняя строка ВСЕГДА: "Приложи копии: ...".
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


def sanitize_comment(text: str) -> str:
    if not text:
        return ""
    t = text.strip().replace("?", "")
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def parse_slot_any(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"(\d+)", s)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except Exception:
        return None
    if 1 <= n <= MAX_USER_TEMPLATES:
        return n
    return None


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
    # Шаблоны пользователя по слотам 1..3
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_templates (
            uid TEXT NOT NULL,
            slot INTEGER NOT NULL,
            name TEXT NOT NULL,
            data_json TEXT NOT NULL,
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


# --- Templates DB helpers ---
def get_templates_for_user(uid: str) -> Dict[int, Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT slot, name, data_json, created_at, updated_at FROM user_templates WHERE uid=? ORDER BY slot", (uid,))
    rows = cur.fetchall()
    con.close()
    out: Dict[int, Dict[str, Any]] = {}
    for slot, name, data_json, created_at, updated_at in rows:
        out[int(slot)] = {
            "slot": int(slot),
            "name": name,
            "data": json.loads(data_json),
            "created_at": int(created_at),
            "updated_at": int(updated_at),
        }
    return out


def pick_first_free_slot(uid: str) -> Optional[int]:
    existing = get_templates_for_user(uid)
    for s in range(1, MAX_USER_TEMPLATES + 1):
        if s not in existing:
            return s
    return None


def upsert_template(uid: str, slot: int, name: str, data: Dict[str, Any]) -> None:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO user_templates(uid, slot, name, data_json, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid, slot) DO UPDATE SET
            name=excluded.name,
            data_json=excluded.data_json,
            updated_at=excluded.updated_at
        """,
        (uid, slot, name.strip(), json.dumps(data, ensure_ascii=False), now, now),
    )
    con.commit()
    con.close()


def delete_template(uid: str, slot: int) -> bool:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("DELETE FROM user_templates WHERE uid=? AND slot=?", (uid, slot))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def templates_list_text(uid: str) -> str:
    t = get_templates_for_user(uid)
    lines = ["Вот твои шаблоны:"]
    for s in range(1, MAX_USER_TEMPLATES + 1):
        if s in t:
            lines.append(f"{s}) {t[s]['name']}")
        else:
            lines.append(f"{s}) — пусто")
    lines.append("")
    lines.append("Выбери номер шаблона, чтобы использовать его.")
    return "\n".join(lines).strip()


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
# Templates files cache
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
# DATA BUILDER
# =========================
def build_services_data_min(payload: Dict[str, Any]) -> Dict[str, Any]:
    def clean(x: Any) -> str:
        s = str(x).strip() if x is not None else ""
        return s

    return {
        "exec_type": clean(payload.get("exec_type")),
        "exec_name": clean(payload.get("exec_name")),
        "client_name": clean(payload.get("client_name")),
        "service_desc": clean(payload.get("service_desc") or payload.get("Предмет")),
        "deadline_value": clean(payload.get("deadline_value") or payload.get("Сроки")),
        "price_value": clean(payload.get("price_value")),
        "acceptance_value": clean(payload.get("acceptance_value")),
        "extra": clean(extract_extra(payload)),
    }


def data_dict_to_text(d: Dict[str, Any]) -> str:
    def v(x: Any) -> str:
        s = str(x).strip()
        return s if s else "___"

    return (
        f"Исполнитель:\n"
        f"- статус: {v(d.get('exec_type'))}\n"
        f"- как указать: {v(d.get('exec_name'))}\n\n"
        f"Заказчик:\n"
        f"- как указать: {v(d.get('client_name'))}\n\n"
        f"Услуга:\n"
        f"- описание: {v(d.get('service_desc'))}\n\n"
        f"Сроки:\n"
        f"- значение: {v(d.get('deadline_value'))}\n\n"
        f"Цена и оплата:\n"
        f"- значение: {v(d.get('price_value'))}\n\n"
        f"Приёмка:\n"
        f"- значение: {v(d.get('acceptance_value'))}\n\n"
        f"Доп данные:\n{v(d.get('extra'))}\n"
    ).strip()


# =========================
# FASTAPI
# =========================
app = FastAPI(title="LegalFox API", version="4.0.0-user-templates-3slots")

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
        "max_user_templates": MAX_USER_TEMPLATES,
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = (payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract")
    scenario = str(scenario_raw).strip().lower()

    # --------- UID ----------
    uid, uid_src = pick_uid(payload)
    if not uid and scenario != "clause":
        return {"scenario": scenario, "reply_text": "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id).", "file_url": ""}

    if uid:
        ensure_user(uid)

    # --------- CLAUSE ----------
    if scenario == "clause":
        q = payload.get("query") or payload.get("Запрос") or payload.get("Вопрос") or payload.get("Текст") or ""
        q = str(q).strip()
        if not q:
            return {"scenario": "clause", "reply_text": "Напиши вопрос или вставь текст одним сообщением — помогу.", "file_url": ""}
        try:
            answer = await call_llm(PROMPT_CLAUSE, q, max_tokens=900)
            return {"scenario": "clause", "reply_text": answer, "file_url": ""}
        except Exception as e:
            logger.exception("legalfox error (clause): %s", e)
            return {"scenario": "clause", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # --------- templates_list ----------
    if scenario == "templates_list":
        txt = templates_list_text(uid)
        return {"scenario": "templates_list", "reply_text": txt, "file_url": ""}

    # --------- template_save ----------
    if scenario == "template_save":
        name = (payload.get("template name") or payload.get("template_name") or "").strip()
        if not name:
            return {"scenario": "template_save", "reply_text": "Укажи имя шаблона (коротко), и я сохраню его.", "file_url": ""}

        # ВАЖНО: ключ с пробелом
        desired_slot = parse_slot_any(payload.get("template slot"))

        # данные сохраняем из текущих полей договора
        data = build_services_data_min(payload)

        existing = get_templates_for_user(uid)

        if desired_slot is not None:
            # если передали конкретный слот — сохраняем туда, но не перетираем “тихо”
            if desired_slot in existing:
                return {"scenario": "template_save", "reply_text": f"Слот {desired_slot} уже занят. Удали его или выбери другой слот.", "file_url": ""}
            slot = desired_slot
        else:
            slot = pick_first_free_slot(uid)
            if slot is None:
                return {"scenario": "template_save", "reply_text": "Лимит шаблонов исчерпан (3/3). Удали один шаблон, чтобы сохранить новый.", "file_url": ""}

        upsert_template(uid, slot, name, data)
        txt = f"Сохранил шаблон в слот {slot}: {name}\n\n" + templates_list_text(uid)
        return {"scenario": "template_save", "reply_text": txt, "file_url": ""}

    # --------- template_delete ----------
    if scenario == "template_delete":
        slot = parse_slot_any(payload.get("template slot"))
        if slot is None:
            return {"scenario": "template_delete", "reply_text": "Напиши номер слота (1, 2 или 3), который нужно удалить.", "file_url": ""}

        ok = delete_template(uid, slot)
        if not ok:
            return {"scenario": "template_delete", "reply_text": f"В слоте {slot} ничего нет.\n\n" + templates_list_text(uid), "file_url": ""}

        return {"scenario": "template_delete", "reply_text": f"Удалил шаблон из слота {slot}.\n\n" + templates_list_text(uid), "file_url": ""}

    # --------- template_use ----------
    if scenario == "template_use":
        slot = parse_slot_any(payload.get("template slot"))
        if slot is None:
            return {"scenario": "template_use", "reply_text": "Выбери номер шаблона: 1, 2 или 3.", "file_url": ""}

        t = get_templates_for_user(uid)
        if slot not in t:
            return {"scenario": "template_use", "reply_text": f"В слоте {slot} нет шаблона.\n\n" + templates_list_text(uid), "file_url": ""}

        tpl_name = t[slot]["name"]
        stored_data = t[slot]["data"]
        # Генерация как обычный contract (но data берём из шаблона)
        premium = get_premium_flag(payload)
        with_file_requested = get_with_file_requested(payload)

        trial_left = free_left(uid)
        can_file = premium or (trial_left > 0)
        with_file = with_file_requested and can_file

        logger.info(
            "Scenario=template_use uid=%s(uid_src=%s) slot=%s premium=%s trial_left=%s with_file_requested=%s with_file=%s",
            uid, uid_src, slot, premium, trial_left, with_file_requested, with_file
        )

        try:
            template_text = await build_services_contract_template()
            data_text = data_dict_to_text(stored_data)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=420)
                comment = sanitize_comment(comment)
            except Exception as e:
                logger.warning("Comment generation failed (template_use): %s", e)
                comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                # списываем trial только если реально отдаём PDF
                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = f"Ты использовал шаблон: {tpl_name}\n\nГотово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"

                return {"scenario": "template_use", "reply_text": txt, "file_url": file_url_for(fn, request)}

            # без файла
            txt = f"Ты использовал шаблон: {tpl_name}\n\n" + draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "template_use", "reply_text": txt, "file_url": ""}

        except Exception as e:
            logger.exception("legalfox error (template_use): %s", e)
            return {"scenario": "template_use", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # --------- CONTRACT (обычная генерация) ----------
    if scenario == "contract":
        premium = get_premium_flag(payload)
        with_file_requested = get_with_file_requested(payload)

        trial_left = free_left(uid)
        can_file = premium or (trial_left > 0)
        with_file = with_file_requested and can_file

        logger.info(
            "Scenario=contract uid=%s(uid_src=%s) premium=%s trial_left=%s with_file_requested=%s with_file=%s",
            uid, uid_src, premium, trial_left, with_file_requested, with_file
        )

        try:
            template_text = await build_services_contract_template()
            data_dict = build_services_data_min(payload)
            data_text = data_dict_to_text(data_dict)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=420)
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

        except Exception as e:
            logger.exception("legalfox error (contract): %s", e)
            return {"scenario": "contract", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # --------- CLAIM ----------
    if scenario == "claim":
        premium = get_premium_flag(payload)
        with_file_requested = get_with_file_requested(payload)

        trial_left = free_left(uid)
        can_file = premium or (trial_left > 0)
        with_file = with_file_requested and can_file

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

        try:
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

        except Exception as e:
            logger.exception("legalfox error (claim): %s", e)
            return {"scenario": "claim", "reply_text": FALLBACK_TEXT, "file_url": ""}

    # --------- default ----------
    return {"scenario": scenario, "reply_text": "Неизвестный сценарий.", "file_url": ""}
