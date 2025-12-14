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
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")  # https://xxx.up.railway.app
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
# В Railway при CERTIFICATE_VERIFY_FAILED поставь 0
GIGACHAT_VERIFY_SSL = (os.getenv("GIGACHAT_VERIFY_SSL", "1").strip() != "0")

FALLBACK_TEXT = "Сейчас не могу обратиться к нейросети. Попробуй повторить чуть позже."

# Templates
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")
SERVICES_CONTRACT_TEMPLATE = os.getenv("SERVICES_CONTRACT_TEMPLATE", "contract_services_v1.txt")
COMMON_TAIL_TEMPLATE = os.getenv("COMMON_TAIL_TEMPLATE", "common_contract_tail_8_12.txt")
COMMON_TAIL_PLACEHOLDER = os.getenv("COMMON_TAIL_PLACEHOLDER", "{{COMMON_CONTRACT_TAIL}}")


# =========================
# ПРОМТЫ
# =========================
PROMPT_DRAFT_WITH_TEMPLATE = """
Ты — LegalFox. Ты готовишь черновик договора оказания услуг по праву РФ для самозанятых/фрилансеров/микробизнеса.

Тебе дан ШАБЛОН документа (строгая структура) и ДАННЫЕ пользователя.
Сгенерируй итоговый документ СТРОГО по шаблону, сохранив порядок разделов, заголовки и нумерацию.

Жёсткие правила:
1) Без Markdown: никаких #, **, ``` и т.п.
2) Ничего не выдумывай: паспорт, ИНН/ОГРН, адреса, суммы, даты, сроки — только если пользователь дал явно.
   Если данных нет — ставь "___".
3) Не используй заглушки вида "СТОРОНА_1", "АДРЕС_1", "ПРЕДМЕТ". Только реальные данные или "___".
4) Используй данные максимально конкретно и дословно, но аккуратно, без лишних кавычек.
5) В договоре оставляй пустые строки между разделами (чтобы выглядело как реальный документ).
6) Если пользователь дал "Доп данные" — встрои их в подходящие разделы (оплата/приёмка/ответственность/прочее), не создавая хаоса.

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
- Рекомендации только по делу: стороны/предмет/сроки/оплата/приёмка/ответственность/расторжение/уведомления.

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
    # В BotHelp ты шлёшь with_file="1" всегда — так и задумано.
    if "with_file" in payload:
        return normalize_bool(payload.get("with_file"))
    return False


def file_url_for(filename: str, request: Request) -> str:
    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    return f"{base}/files/{filename}"


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
    return t


def extract_extra(payload: Dict[str, Any]) -> str:
    # Ты используешь "Доп данные" — поддерживаем и вариации
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
    return "contract"


# =========================
# БД (TRIAL)
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
# PDF (кириллица)
# =========================
def ensure_font_name() -> str:
    # Положи любой из этих шрифтов в fonts/
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
    left, right, top, bottom = 85, 42, 57, 57  # ~30мм/15мм/20мм/20мм
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
    # Минимальная схема, которую ты сделал:
    # exec_type, exec_name, client_name, service_desc, deadline_value, price_value, acceptance_value, "Доп данные"
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


# =========================
# FASTAPI
# =========================
app = FastAPI(title="LegalFox API", version="3.0.0-minimal-fields")

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
        "templates_dir": TEMPLATES_DIR,
        "services_template": SERVICES_CONTRACT_TEMPLATE,
        "common_tail": COMMON_TAIL_TEMPLATE,
    }


@app.post("/legalfox")
async def legalfox(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, str]:
    scenario_raw = payload.get("scenario") or payload.get("Сценарий") or payload.get("сценарий") or "contract"
    scenario = scenario_alias(str(scenario_raw))

    # 3 функция: правки (может быть без uid)
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

    # Документы: нужен uid для trial
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
        # ============ CONTRACT (услуги, минимальные поля) ============
        if scenario == "contract":
            # 0) Собираем шаблон
            template_text = await build_services_contract_template()

            # 1) DATA
            data_text = build_services_data_min(payload)
            llm_user_msg = f"TEMPLATE:\n{template_text}\n\nDATA:\n{data_text}\n"

            # 2) Сначала текст договора (если LLM упал — PDF не делаем)
            draft = await call_llm(PROMPT_DRAFT_WITH_TEMPLATE, llm_user_msg, max_tokens=1900)

            # 3) Комментарий (не обязателен)
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

                # Важно: ссылку НЕ дублируем в тексте — BotHelp прикрепит файл по file_url
                result["file_url"] = file_url_for(fn, request)

                # Trial списываем только если реально выдали PDF
                if (not premium) and trial_left > 0:
                    ok = consume_free(uid)
                    logger.info("Trial consume uid=%s ok=%s", uid, ok)

                txt = "Готово. Я подготовил черновик договора и прикрепил PDF ниже."
                if comment:
                    txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
                result["reply_text"] = txt
                return result

            # без файла (либо trial исчерпан, либо with_file=0)
            txt = draft
            if comment:
                txt += f"\n\nКомментарий по твоему кейсу:\n{comment}"
            if with_file_requested and (not premium) and trial_left <= 0:
                txt += "\n\nПробный PDF уже использован. Подписка даёт неограниченные PDF и повторную сборку."
            return {"scenario": "contract", "reply_text": txt, "file_url": ""}

        # ============ CLAIM (оставляем совместимость) ============
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

        # неизвестно
        return {"scenario": scenario, "reply_text": "Неизвестный сценарий.", "file_url": ""}

    except Exception as e:
        logger.exception("legalfox error: %s", e)
        # КРИТИЧНО: file_url всегда пустой при ошибках (чтобы BotHelp не прикреплял старый файл)
        return {"scenario": scenario, "reply_text": FALLBACK_TEXT, "file_url": ""}
