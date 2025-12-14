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

# --- GigaChat ---
LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "gigachat") or "gigachat").strip().lower()
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")  # Basic base64(ClientID:ClientSecret)
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")

GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru")
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

# --- Trial ---
FREE_PDF_LIMIT = int(os.getenv("FREE_PDF_LIMIT", "1"))

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# --- Templates ---
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")


# ----------------- Промты -----------------
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик ДОГОВОРА ОКАЗАНИЯ УСЛУГ по праву РФ для фрилансера/самозанятого/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону, сохранив порядок разделов, заголовки и нумерацию.

Жёсткие правила:
1) Без Markdown: никаких #, **, ``` и т.п.
2) Обращение к пользователю в тексте договора не используй. Это официальный документ.
3) Не выдумывай реквизиты, даты, суммы, ИНН/ОГРН, адреса. Если нет — ставь "___".
4) Не используй заглушки вида "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ". Только реальные данные или "___".
5) Если пользователь дал данные — вставляй их максимально конкретно и дословно (без лишних кавычек).
6) В договоре оставляй пустые строки между разделами (чтобы выглядело как документ).
7) Если пользователь дал "доп. условия" — встрои их в подходящие разделы (оплата/приёмка/ответственность/прочее), не создавая хаоса.

Формат входа:
- TEMPLATE: текст шаблона
- DATA: данные пользователя

Выводи только финальный текст договора.
"""

PROMPT_CONTRACT_COMMENT = """
Ты — LegalFox. Дай короткий комментарий к договору оказания услуг (чек-лист).

Строго:
- Без Markdown.
- Обращайся к пользователю на "ты".
- 6–9 коротких строк.
- Каждая строка начинается с глагола: "Проверь", "Укажи", "Добавь", "Закрепи", "Пропиши", "Согласуй".
- Не задавай вопросов и не используй символ '?'.
- Не используй оценочные слова: "некорректно", "ошибки", "недействительно", "незаконно", "в надлежащий вид".
- Если данных мало — пиши нейтрально: "Чтобы документ был точнее, дополни: ...".
- Делай рекомендации по реальным полям: стороны/предмет/сроки/оплата/приёмка/ответственность/расторжение/коммуникации.

Выводи только комментарий.
"""

PROMPT_CLAIM = """
Ты — LegalFox. Подготовь ЧЕРНОВИК ПРЕТЕНЗИИ (досудебной) по праву РФ.

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
    # cuid — последним, чтобы uid не “скакал”
    priority = ["bh_user_id", "user_id", "tg_user_id", "telegram_user_id", "messenger_user_id", "bothelp_user_id", "cuid"]
    for key in priority:
        v = payload.get(key)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            # источники бывают "5", "5(uid_src...)" — чистим
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
    # BotHelp обычно шлёт with_file="1"
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    # если не пришло — по умолчанию файл НЕ просим
    return False


def file_url_for(filename: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


def sanitize_comment(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
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
    t = t.replace("?", "")
    if len(t) < 20:
        return "Чтобы документ был точнее, дополни недостающие данные и приложи подтверждающие документы."
    return t


# ----------------- Templates (cache) -----------------
_templates_cache: Dict[str, str] = {}
_templates_lock = asyncio.Lock()


async def load_text_file(path: str) -> str:
    # простой async wrapper
    loop = asyncio.get_event_loop()
    def _read():
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return await loop.run_in_executor(None, _read)


async def get_template(name: str) -> str:
    path = os.path.join(TEMPLATES_DIR, name)
    async with _templates_lock:
        if path in _templates_cache:
            return _templates_cache[path]
        if not os.path.exists(path):
            raise FileNotFoundError(f"Template not found: {path}")
        txt = await load_text_file(path)
        _templates_cache[path] = txt
        return txt


async def build_services_contract_template() -> str:
    base = await get_template(SERVICES_CONTRACT_TEMPLATE)
    tail = await get_template(COMMON_TAIL_TEMPLATE)
    if COMMON_TAIL_PLACEHOLDER not in base:
        raise RuntimeError(f"Placeholder {COMMON_TAIL_PLACEHOLDER} not found in contract template")
    return base.replace(COMMON_TAIL_PLACEHOLDER, tail)


def extract_extra(payload: Dict[str, Any]) -> str:
    extra = (
        payload.get("extra")
        or payload.get("Extra")
        or payload.get("Доп данные")
        or payload.get("Доп_данные")
        or payload.get("Доп вопросы")
        or payload.get("Доп_вопросы")
        or payload.get("extra_info")
        or ""
    )
    return str(extra).strip()


def build_services_data(payload: Dict[str, Any]) -> str:
    # Поддержка как новых полей (exec_*/client_*), так и старых ("Стороны", "Предмет"...)
    exec_type = payload.get("exec_type") or ""
    exec_name = payload.get("exec_name") or ""
    exec_inn = payload.get("exec_inn") or ""
    exec_address = payload.get("exec_address") or ""
    exec_contact = payload.get("exec_contact") or ""

    client_type = payload.get("client_type") or ""
    client_name = payload.get("client_name") or ""
    client_inn = payload.get("client_inn") or ""
    client_address = payload.get("client_address") or ""
    client_contact = payload.get("client_contact") or ""

    service_desc = payload.get("service_desc") or payload.get("Предмет") or ""
    deadline_mode = payload.get("deadline_mode") or ""
    deadline_value = payload.get("deadline_value") or payload.get("Сроки") or payload.get("Сроки и оплата") or ""
    price_mode = payload.get("price_mode") or ""
    price_value = payload.get("price_value") or payload.get("Сроки и оплата") or ""
    acceptance_mode = payload.get("acceptance_mode") or ""
    acceptance_value = payload.get("acceptance_value") or ""
    penalty_mode = payload.get("penalty_mode") or ""
    penalty_value = payload.get("penalty_value") or ""
    comms_channel = payload.get("comms_channel") or ""
    comms_value = payload.get("comms_value") or ""

    # Старое поле "Стороны" (если пользователь вводил руками) — подстрахуемся
    legacy_parties = payload.get("Стороны") or ""

    extra = extract_extra(payload)

    def v(x: Any) -> str:
        s = str(x).strip()
        return s if s else "___"

    # делаем понятный DATA блок для LLM
    data = (
        f"Исполнитель:\n"
        f"- статус: {v(exec_type)}\n"
        f"- имя/название: {v(exec_name)}\n"
        f"- ИНН: {v(exec_inn)}\n"
        f"- адрес: {v(exec_address)}\n"
        f"- контакт: {v(exec_contact)}\n\n"
        f"Заказчик:\n"
        f"- статус: {v(client_type)}\n"
        f"- имя/название: {v(client_name)}\n"
        f"- ИНН/ОГРН: {v(client_inn)}\n"
        f"- адрес: {v(client_address)}\n"
        f"- контакт: {v(client_contact)}\n\n"
        f"Услуга:\n"
        f"- описание (1 строка): {v(service_desc)}\n\n"
        f"Сроки:\n"
        f"- режим: {v(deadline_mode)}\n"
        f"- значение: {v(deadline_value)}\n\n"
        f"Оплата:\n"
        f"- режим: {v(price_mode)}\n"
        f"- сумма/порядок: {v(price_value)}\n\n"
        f"Приёмка:\n"
        f"- режим: {v(acceptance_mode)}\n"
        f"- срок/условия: {v(acceptance_value)}\n\n"
        f"Ответственность/неустойка:\n"
        f"- режим: {v(penalty_mode)}\n"
        f"- условия: {v(penalty_value)}\n\n"
        f"Коммуникации:\n"
        f"- канал: {v(comms_channel)}\n"
        f"- контакт/значение: {v(comms_value)}\n\n"
        f"Стороны (если пользователь вводил старым способом): {v(legacy_parties)}\n\n"
        f"Доп. условия:\n{v(extra)}\n"
    )
    return data.strip()


# ----------------- БД (trial) -----------------
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


def consume_free(uid: str) -> bool:
    if not uid:
        return False
    ensure_user(uid)
    con = sqlite3.connect(DB_PATH)
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


# ----------------- PDF (кириллица) -----------------
def ensure_font_name() -> str:
    # Положи fonts/DejaVuSans.ttf или fonts/PTSerif-Regular.ttf
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
            logger.exception("Не удалось зарегистрировать шрифт %s", font_path)
    return "Helvetica"


def render_pdf(text: str, out_path: str, title: str):
    font_name = ensure_font_name()
    c = canvas.Canvas(out_path, pagesize=A4)
    width, height = A4

    # Похоже на “документ”: поля ~ ГОСТ-лайк
    left, right, top, bottom = 85, 42, 57, 57  # ≈ 30мм / 15мм / 20мм / 20мм
    max_width = width - left - right

    c.setFont(font_name, 14)
    c.drawString(left, height - top, title)

    c.setFont(font_name, 12)
    y = height - top - 28
    line_height = 16  # около 1.3–1.4 интервала

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


# ----------------- GigaChat token cache -----------------
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
            # один мягкий retry
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


# ----------------- FastAPI -----------------
app = FastAPI(title="LegalFox API", version="2.0.0-services-specialized")

os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")
db_init()


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "LegalFox",
        "provider": LLM_PROVIDER,
        "model": GIGACHAT_MODEL,
        "verify_ssl": bool(GIGACHAT_VERIFY_SSL),
        "templates_dir": TEMPLATES_DIR,
        "services_template": SERVICES_CONTRACT_TEMPLATE,
        "common_tail": COMMON_TAIL_TEMPLATE,
    }


def scenario_alias(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("contract", "draft_contract", "договора", "договора_черновик"):
        return "contract"
    if s in ("claim", "draft_claim", "претензия", "претензии"):
        return "claim"
    if s in ("clause", "ask", "help", "пункты", "правки"):
        return "clause"
    return "contract"


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    # clause НЕ требует uid (это консультация/правка)
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

    # Для документов — нужен uid (trial/подписка)
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
        "Scenario=%s uid=%s(uid_src=%s) premium=%s trial_left=%s FREE_PDF_LIMIT=%s with_file_requested=%s with_file=%s",
        scenario, uid, uid_src, premium, trial_left, FREE_PDF_LIMIT, with_file_requested, with_file
    )

    try:
        # ---------- CONTRACT (услуги) ----------
        if scenario == "contract":
            # Если шаблон/плейсхолдер сломан — сразу техническая ошибка
            try:
                template_text = await build_services_contract_template()
            except Exception as e:
                logger.exception("Template error: %s", e)
                return {"scenario": "contract", "reply_text": "Техническая ошибка: не найден/сломан шаблон документа на сервере.", "file_url": ""}

            data_text = build_services_data(payload)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            # 1) Генерим договор
            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            # 2) Комментарий (мягкий)
            comment = ""
            try:
                comment = await call_llm(PROMPT_CONTRACT_COMMENT, data_text, max_tokens=420)
                comment = sanitize_comment(comment)
            except Exception as e:
                logger.warning("Comment generation failed (contract): %s", e)
                comment = ""

            result: Dict[str, str] = {"scenario": "contract", "reply_text": "", "file_url": ""}

            if with_file:
                fn = safe_filename("contract")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ДОГОВОР ОКАЗАНИЯ УСЛУГ (ЧЕРНОВИК)")

                # file_url возвращаем отдельно — BotHelp прикрепит файл; ссылку в тексте НЕ пишем (иначе будет дубль)
                result["file_url"] = file_url_for(fn, request)

                # Списываем trial только если реально выдали файл
                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                text = "Готово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    text += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                result["reply_text"] = text
            else:
                # PDF не доступен — показываем текст договора + комментарий + paywall, file_url пустой
                text = draft
                if comment:
                    text += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                if with_file_requested and (not premium) and trial_left <= 0:
                    text += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
                result["reply_text"] = text
                result["file_url"] = ""

            return result

        # ---------- CLAIM ----------
        if scenario == "claim":
            to_whom = payload.get("Адресат") or payload.get("to_whom") or ""
            basis = payload.get("Основание") or payload.get("basis") or ""
            viol = payload.get("Нарушение и обстоятельства") or payload.get("viol") or ""
            reqs = payload.get("Требования") or payload.get("reqs") or ""
            term = payload.get("Сроки исполнения") or payload.get("term") or ""
            contacts = payload.get("Контакты") or payload.get("contacts") or ""
            extra = extract_extra(payload)

            def v(x: Any) -> str:
                s = str(x).strip()
                return s if s else "___"

            user_text = (
                f"Адресат: {v(to_whom)}\n"
                f"Основание: {v(basis)}\n"
                f"Нарушение и обстоятельства: {v(viol)}\n"
                f"Требования: {v(reqs)}\n"
                f"Срок исполнения: {v(term)}\n"
                f"Контакты: {v(contacts)}\n"
                f"Доп. данные: {v(extra)}\n"
            ).strip()

            draft = await call_llm(PROMPT_CLAIM, user_text, max_tokens=1600)

            comment = ""
            try:
                comment = await call_llm(PROMPT_CLAIM_COMMENT, user_text, max_tokens=420)
                comment = sanitize_comment(comment)
            except Exception as e:
                logger.warning("Comment generation failed (claim): %s", e)
                comment = ""

            result: Dict[str, str] = {"scenario": "claim", "reply_text": "", "file_url": ""}

            if with_file:
                fn = safe_filename("claim")
                out_path = os.path.join(FILES_DIR, fn)
                render_pdf(draft, out_path, title="ПРЕТЕНЗИЯ (ЧЕРНОВИК)")

                result["file_url"] = file_url_for(fn, request)

                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                text = "Готово. Я подготовил черновик претензии и прикрепил PDF ниже."
                if comment:
                    text += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                result["reply_text"] = text
            else:
                text = draft
                if comment:
                    text += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                if with_file_requested and (not premium) and trial_left <= 0:
                    text += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
                result["reply_text"] = text
                result["file_url"] = ""

            return result

        # fallback
        return {"scenario": scenario, "reply_text": "Неизвестный сценарий.", "file_url": ""}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        # Важно: при ошибке file_url всегда пустой (чтобы BotHelp не подхватил прошлый файл)
        return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}
