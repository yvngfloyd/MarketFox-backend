# main.py
from fastapi import FastAPI
from bot import bot, dp
import os
from aiogram.types import Update

app = FastAPI()


@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(os.getenv("WEBHOOK_URL"))


@app.post("/webhook")
async def webhook(update: dict):
    await dp.feed_update(bot, Update(**update))
    return {"ok": True}
