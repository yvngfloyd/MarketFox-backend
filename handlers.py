# handlers.py
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from keyboards import main_menu, back_to_menu
from calculations import (
    calc_concrete,
    calc_screed,
    calc_plaster,
    calc_tile,
    calc_price
)

router = Router()


@router.message(F.text == "/start")
async def start(msg: Message):
    await msg.answer(
        "🏗 LegalFox | Строительный помощник\n\n"
        "Рассчитаю материалы, прикину стоимость и подскажу как лучше сделать.",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery):
    await cb.message.answer("Главное меню:", reply_markup=main_menu())
    await cb.answer()


# ---------- МАТЕРИАЛЫ ----------

@router.callback_query(F.data == "materials")
async def materials(cb: CallbackQuery):
    await cb.message.answer(
        "Выбери, что нужно рассчитать:\n\n"
        "• Бетон: напиши `бетон 3`\n"
        "• Стяжка: `стяжка 40 5`\n"
        "• Штукатурка: `штукатурка 50 2`\n"
        "• Плитка: `плитка 20`",
        reply_markup=back_to_menu()
    )
    await cb.answer()


@router.message(F.text.lower().startswith("бетон"))
async def concrete(msg: Message):
    volume = float(msg.text.split()[1])
    result = calc_concrete(volume)
    await msg.answer(result["text"], reply_markup=back_to_menu())


@router.message(F.text.lower().startswith("стяжка"))
async def screed(msg: Message):
    _, area, thickness = msg.text.split()
    result = calc_screed(float(area), float(thickness))
    await msg.answer(result["text"], reply_markup=back_to_menu())


@router.message(F.text.lower().startswith("штукатурка"))
async def plaster(msg: Message):
    _, area, thickness = msg.text.split()
    result = calc_plaster(float(area), float(thickness))
    await msg.answer(result["text"], reply_markup=back_to_menu())


@router.message(F.text.lower().startswith("плитка"))
async def tile(msg: Message):
    _, area = msg.text.split()
    result = calc_tile(float(area))
    await msg.answer(result["text"], reply_markup=back_to_menu())


# ---------- СТОИМОСТЬ ----------

@router.callback_query(F.data == "price")
async def price(cb: CallbackQuery):
    await cb.message.answer(
        "💰 Рассчёт стоимости:\n\n"
        "Формат:\n"
        "`работа объём`\n\n"
        "Примеры:\n"
        "стяжка 40\n"
        "штукатурка 30\n"
        "плитка 25",
        reply_markup=back_to_menu()
    )
    await cb.answer()


@router.message(F.text.lower().split()[0].in_(["стяжка", "штукатурка", "плитка"]))
async def price_calc(msg: Message):
    work, volume = msg.text.split()
    result = calc_price(work, float(volume))
    await msg.answer(result["text"], reply_markup=back_to_menu())
