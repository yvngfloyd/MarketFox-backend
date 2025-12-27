# keyboards.py
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import os

PAYMENT_URL = os.getenv("PAYMENT_URL")


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧱 Рассчитать материалы", callback_data="materials")],
            [InlineKeyboardButton(text="💰 Рассчитать стоимость работ", callback_data="price")],
            [
                InlineKeyboardButton(text="💳 Оформить подписку", url=PAYMENT_URL)
            ]
        ]
    )


def back_to_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬅️ Вернуться в меню", callback_data="menu"),
                InlineKeyboardButton(text="💳 Подписка", url=PAYMENT_URL)
            ]
        ]
    )
