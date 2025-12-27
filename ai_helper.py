from your_gigachat_module import get_gigachat_token, gigachat_generate

async def ai_recommendation(context: str) -> str:
    try:
        token = get_gigachat_token()
        prompt = f"Ты строительный эксперт. Дай советы:\n{context}"
        answer = gigachat_generate(token, prompt)
        return answer.strip()
    except Exception as e:
        return "Не удалось получить ответ ИИ."
