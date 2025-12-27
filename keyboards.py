from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧱 Рассчитать материалы", callback_data="materials")],
        [InlineKeyboardButton(text="💰 Прикинуть стоимость работ", callback_data="price")]
    ])


def materials_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Бетон", callback_data="mat_concrete")]
    ])


def price_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Стяжка пола", callback_data="price_stjazhka")]
    ])
