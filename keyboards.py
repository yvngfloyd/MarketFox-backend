from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧱 Рассчитать материалы", callback_data="materials")],
        [InlineKeyboardButton(text="💰 Прикинуть стоимость работ", callback_data="price")]
    ])


def materials_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Бетон", callback_data="mat_concrete")],
        [InlineKeyboardButton(text="Стяжка пола", callback_data="mat_screed")],
        [InlineKeyboardButton(text="Штукатурка стен", callback_data="mat_plaster")],
        [InlineKeyboardButton(text="Плитка", callback_data="mat_tile")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_menu")]
    ])


def back_to_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_menu")]
    ])
