# main.py — LegalFox (trial 1 PDF + шаблоны 3 шт + “использовал шаблон <имя>”)
# Под BotHelp: отдаём reply_text + file_url (file_url НЕ дублируем в тексте)

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

# Trial: 1 PDF на пользователя (на ВСЕ документы: договор/претензия/шаблоны)
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

# Templates (файлы в repo/templates)
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")

# User templates (3 слота)
TEMPLATES_LIMIT = int(os.getenv("TEMPLATES_LIMIT", "3"))


# =========================
# ПРОМТЫ
# =========================
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик договора оказания услуг по праву РФ для самозанятых/фрилансеров/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону: сохраняй порядок разделов, заголовки и нумерацию.

Жёсткие правила:
1) Без Markdown: никаких #, **, ``` и т.п.
2) Ничего не выдумывай: паспорт, ИНН/ОГРН, адреса, реквизиты, суммы, даты, сроки — только если пользователь дал явно.
3) Если данных нет / они неполные / размытые / противоречивые / выглядят как “мусор” — НЕ вставляй их.
   Вместо этого оставляй ПУСТОЕ МЕСТО длинными подчёркиваниями.
   Используй такие заглушки:
   - короткие поля (ФИО, дата, сумма): "____________"
   - длинные поля (адрес, реквизиты, паспорт): "________________________________________"
   - суммы/даты: "________ руб.", "__.__.____"
4) Никогда не используй заглушки типа "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ", "ОПЛАТА" и т.п.
5) Запрещено выводить служебные строки и подсказки: "TEMPLATE:", "DATA:", "Пример:", "введите", "поле", "как указано пользователем" и т.п.
6) Если пользователь написал "нет" / "не знаю" / "пусто" / "—" / "-" / "0" / "n/a" — считай, что данных нет.
7) Жёсткая фильтрация мусора:
   - Если значение похоже на шаблонные слова/переменные/теги/emoji/набор символов — НЕ вставляй, оставь подчёркивания.
8) "Доп данные":
   - Если там есть конкретные условия — встрои 1–3 предложения в подходящий раздел.
   - Если там мусор/вода — игнорируй, не цитируй в договоре.
9) Верстка: короткие абзацы, пустые строки между разделами.

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
""".strip()

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Пользователь ввёл данные для договора оказания услуг.
Дай короткий комментарий (чек-лист), чтобы договор “работал”.

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 6–10 коротких строк.
- Не задавай вопросов и не используй '?'.
- Пиши нейтрально: "Укажи…", "Зафиксируй…", "Проверь…", "Добавь…".
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


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("contract", "draft_contract", "договора", "договора_черновик", "services_contract"):
        return "contract"
    if s in ("claim", "draft_claim", "претензия", "претензии", "claim_unpaid"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    if s in ("template_save", "templates_list", "template_use", "template_delete"):
        return s
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
    # В BotHelp ты обычно шлёшь with_file="1"
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return False


def file_url_for(filename: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


def sanitize_comment(text: str) -> str:
    if not text:
        return ""
    t = text.strip().replace("?", "")
    # убираем токсичные/пугающие формулировки, если модель вдруг выдала
    banned = [
        "некорректно",
        "ошибки",
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

    # Последний DATA (для “Сохранить шаблон”)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS last_contract (
            uid TEXT PRIMARY KEY,
            data_text TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # Сохранённые пользовательские шаблоны (до 3)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_templates (
            uid TEXT NOT NULL,
            slot INTEGER NOT NULL,
            name TEXT NOT NULL,
            data_text TEXT NOT NULL,
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


def save_last_contract(uid: str, data_text: str):
    ts = int(time.time())
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO last_contract(uid, data_text, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(uid) DO UPDATE SET data_text=excluded.data_text, updated_at=excluded.updated_at",
        (uid, data_text, ts),
    )
    con.commit()
    con.close()


def load_last_contract(uid: str) -> str:
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
    return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]


def load_user_template(uid: str, slot: int) -> Optional[Tuple[str, str, int]]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "SELECT name, data_text, updated_at FROM user_templates WHERE uid=? AND slot=?",
        (uid, slot),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return (str(row[0]), str(row[1]), int(row[2]))


def delete_user_template(uid: str, slot: int) -> Tuple[bool, str]:
    tpl = load_user_template(uid, slot)
    if not tpl:
        return False, f"В слоте {slot} нет шаблона. Удалять нечего."
    name, _, _ = tpl
    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute("DELETE FROM user_templates WHERE uid=? AND slot=?", (uid, slot))
    con.commit()
    con.close()
    return True, f"Удалил шаблон №{slot}: «{name}»."


def _clean_user_template_name(name: str) -> str:
    n = (name or "").strip()
    n = re.sub(r"\s+", " ", n)
    if not n:
        return ""
    if n.lower() in ("нет", "не знаю", "пусто", "—", "-", "0", "n/a"):
        return ""
    if len(n) > 40:
        n = n[:40].rstrip() + "…"
    return n


def _infer_template_name(data_text: str) -> str:
    if not data_text:
        return "Шаблон"
    client = ""
    service = ""
    m = re.search(r"Заказчик:\s*\n- как указать:\s*(.+)", data_text)
    if m:
        client = m.group(1).strip()
    m = re.search(r"Услуга:\s*\n- описание:\s*(.+)", data_text)
    if m:
        service = m.group(1).strip()

    def clean(s: str) -> str:
        s = re.sub(r"\s+", " ", s).strip()
        if s in ("___", "____________", "________________________________________"):
            return ""
        if len(s) > 32:
            s = s[:32].rstrip() + "…"
        return s

    client = clean(client)
    service = clean(service)
    if client and service:
        return f"{client} — {service}"
    return client or service or "Шаблон"


def save_user_template(uid: str, data_text: str, user_name: str = "") -> Tuple[bool, str]:
    items = list_user_templates(uid)
    if len(items) >= TEMPLATES_LIMIT:
        return False, f"Лимит шаблонов исчерпан ({TEMPLATES_LIMIT}/{TEMPLATES_LIMIT})."

    used = {slot for slot, _, _ in items}
    slot = 1
    while slot in used and slot <= TEMPLATES_LIMIT:
        slot += 1
    if slot > TEMPLATES_LIMIT:
        return False, f"Лимит шаблонов исчерпан ({TEMPLATES_LIMIT}/{TEMPLATES_LIMIT})."

    name = _clean_user_template_name(user_name) or _infer_template_name(data_text)
    ts = int(time.time())

    con = sqlite3.connect(DB_PATH, timeout=30)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO user_templates(uid, slot, name, data_text, updated_at) VALUES(?, ?, ?, ?, ?)",
        (uid, slot, name, data_text, ts),
    )
    con.commit()
    con.close()
    return True, f"Шаблон сохранён: №{slot} — {name}"


# =========================
# PDF (кириллица + поля “как документ”)
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

    # “ГОСТ-лайк” поля (примерно)
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
# TEMPLATES (cache)
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


async def call_gigachat(system_prompt: str, user_content: str, max_tokens: int = 1200) -> str:
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


async def call_llm(system_prompt: str, user_input: str, max_tokens: int = 1200) -> str:
    if LLM_PROVIDER != "gigachat":
        raise RuntimeError("LLM_PROVIDER must be gigachat")
    return await call_gigachat(system_prompt, user_input, max_tokens=max_tokens)


# =========================
# DATA BUILDER (минимальные поля для договора услуг)
# =========================
def build_services_data_min(payload: Dict[str, Any]) -> str:
    def v(x: Any) -> str:
        s = str(x).strip()
        return s if s else "___"

    exec_type = payload.get("exec_type") or ""
    exec_name = payload.get("exec_name") or ""
    client_name = payload.get("client_name") or ""
    service_desc = payload.get("service_desc") or payload.get("Предмет") or ""
    deadline_value = payload.get("deadline_value") or payload.get("Сроки") or payload.get("Сроки и оплата") or ""
    price_value = payload.get("price_value") or payload.get("Сроки и оплата") or ""
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


def format_templates_list(uid: str) -> str:
    items = list_user_templates(uid)
    if not items:
        return (
            "У тебя пока нет сохранённых шаблонов.\n\n"
            "Сначала собери договор → нажми «Сохранить шаблон»."
        )
    lines = [f"Твои шаблоны (макс. {TEMPLATES_LIMIT}):\n"]
    for slot, name, ts in items:
        dt = time.strftime("%d.%m %H:%M", time.localtime(ts))
        lines.append(f"{slot}) {name} ({dt})")
    lines.append("\nЧтобы использовать — нажми 1 / 2 / 3.\nЧтобы удалить — нажми «Удалить шаблон» и выбери слот.")
    return "\n".join(lines).strip()


# =========================
# FASTAPI
# =========================
app = FastAPI(title="LegalFox API", version="4.0.0-templates-3slots")

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
        "templates_limit": TEMPLATES_LIMIT,
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario = scenario_alias(payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract")

    # UID (нужен для trial и шаблонов)
    uid, uid_src = pick_uid(payload)

    # ---------------- CLAUSE (3-я функция, только по подписке) ----------------
    if scenario == "clause":
        premium = get_premium_flag(payload)
        if not premium:
            return {"scenario": "clause", "reply_text": "Эта функция доступна по подписке.", "file_url": ""}

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

    # Для всего остального uid обязателен
    if not uid:
        return {"scenario": scenario, "reply_text": "Техническая ошибка: не удалось определить пользователя (bh_user_id/user_id).", "file_url": ""}

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
        # -------------------- ШАБЛОНЫ: список --------------------
        if scenario == "templates_list":
            return {"scenario": "templates_list", "reply_text": format_templates_list(uid), "file_url": ""}

        # -------------------- ШАБЛОНЫ: сохранить --------------------
        if scenario == "template_save":
            last = load_last_contract(uid)
            if not last:
                return {
                    "scenario": "template_save",
                    "reply_text": "Пока нечего сохранять. Сначала собери договор и получи результат.",
                    "file_url": ""
                }
            user_name = payload.get("template_name") or payload.get("name") or ""
            ok, msg = save_user_template(uid, last, user_name=str(user_name))
            return {"scenario": "template_save", "reply_text": msg, "file_url": ""}

        # -------------------- ШАБЛОНЫ: удалить --------------------
        if scenario == "template_delete":
            slot_raw = str(payload.get("template_slot") or "").strip()
            if slot_raw not in ("1", "2", "3"):
                return {"scenario": "template_delete", "reply_text": "Нужен номер слота: 1, 2 или 3.", "file_url": ""}
            slot = int(slot_raw)
            ok, msg = delete_user_template(uid, slot)
            # можно сразу добавить обновлённый список
            if ok:
                msg = msg + "\n\n" + format_templates_list(uid)
            return {"scenario": "template_delete", "reply_text": msg, "file_url": ""}

        # -------------------- ШАБЛОНЫ: использовать (PDF+коммент) --------------------
        if scenario == "template_use":
            slot_raw = str(payload.get("template_slot") or "").strip()
            if slot_raw not in ("1", "2", "3"):
                return {"scenario": "template_use", "reply_text": "Нужен номер слота: 1, 2 или 3.", "file_url": ""}

            slot = int(slot_raw)
            tpl = load_user_template(uid, slot)
            if not tpl:
                return {
                    "scenario": "template_use",
                    "reply_text": f"В слоте {slot} нет сохранённого шаблона. Открой «Мои шаблоны» и выбери доступный.",
                    "file_url": ""
                }

            template_name, data_text, _ts = tpl

            # Генерация договора по сохранённым данным
            template_text = await build_services_contract_template()
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            # Параллельно: договор + комментарий (быстрее)
            draft_task = asyncio.create_task(call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1600))
            comment_task = asyncio.create_task(call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=360))

            draft = await draft_task
            comment = ""
            try:
                comment = sanitize_comment(await comment_task)
            except Exception:
                comment = ""

            result: Dict[str, str] = {"scenario": "template_use", "reply_text": "", "file_url": ""}

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")
                result["file_url"] = file_url_for(fn, request)

                # trial списываем только если реально выдали PDF
                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s (template_use)", uid, ok)

                txt = f"Готово. Ты использовал шаблон «{template_name}».\n\nЯ подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                result["reply_text"] = txt
                return result

            # без файла
            txt = f"Ты использовал шаблон «{template_name}».\n\n" + draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "template_use", "reply_text": txt, "file_url": ""}

        # -------------------- ДОГОВОР (основная функция 1) --------------------
        if scenario == "contract":
            template_text = await build_services_contract_template()
            data_text = build_services_data_min(payload)

            # сохраняем “последний договор” (DATA), чтобы кнопка “Сохранить шаблон” работала
            save_last_contract(uid, data_text)

            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            # Параллельно: договор + комментарий
            draft_task = asyncio.create_task(call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1700))
            comment_task = asyncio.create_task(call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=360))

            draft = await draft_task
            comment = ""
            try:
                comment = sanitize_comment(await comment_task)
            except Exception:
                comment = ""

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s (contract)", uid, ok)

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

        # -------------------- ПРЕТЕНЗИЯ (функция 2) --------------------
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

            draft_task = asyncio.create_task(call_llm(PROMPT_CLAIM, user_text, max_tokens=1400))
            comment_task = asyncio.create_task(call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=360))

            draft = await draft_task
            comment = ""
            try:
                comment = sanitize_comment(await comment_task)
            except Exception:
                comment = ""

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s (claim)", uid, ok)

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
        # важно: file_url всегда пустой при ошибках (чтобы BotHelp не прикреплял “старый” файл)
        return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}
