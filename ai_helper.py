import time
import requests
import os

GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

_access_token = None
_token_expires_at = 0


def _get_access_token() -> str:
    global _access_token, _token_expires_at

    now = time.time()
    if _access_token and now < _token_expires_at:
        return _access_token

    if not GIGACHAT_AUTH_KEY:
        raise RuntimeError("GIGACHAT_AUTH_KEY not set")

    headers = {
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }

    data = {
        "scope": GIGACHAT_SCOPE
    }

    resp = requests.post(OAUTH_URL, headers=headers, data=data, timeout=10)
    resp.raise_for_status()

    payload = resp.json()
    _access_token = payload["access_token"]
    expires_in = payload.get("expires_in", 1800)

    # обновляем токен заранее
    _token_expires_at = now + expires_in - 60

    return _access_token


def gigachat_lite(prompt: str) -> str:
    token = _get_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    body = {
        "model": "GigaChat-Lite",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты опытный строитель и прораб. "
                    "Давай практичные, краткие советы без воды. "
                    "Пиши простым языком."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.4,
        "max_tokens": 250
    }

    resp = requests.post(CHAT_URL, headers=headers, json=body, timeout=15)
    resp.raise_for_status()

    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def ai_recommendation(context: str) -> str:
    try:
        prompt = f"""
Контекст:
{context}

Дай:
1) на что реально влияет цена
2) где чаще всего переплачивают
3) практический совет
"""
        return gigachat_lite(prompt)

    except Exception:
        return (
            "Совет: цена обычно зависит от состояния основания, "
            "толщины слоя и объёма работ. Часто переплачивают за лишние работы."
        )
