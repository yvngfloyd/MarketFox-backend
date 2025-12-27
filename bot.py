from aiogram import Bot, Dispatcher
from config import BOT_TOKEN
from handlers import router
import os

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)
