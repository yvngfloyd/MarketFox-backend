from fastapi import FastAPI, Request
from bot import bot, dp
from config import WEBHOOK_URL

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(WEBHOOK_URL)


@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    await dp.feed_raw_update(bot, data)
    return {"ok": True}
