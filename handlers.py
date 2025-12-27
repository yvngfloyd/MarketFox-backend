from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from keyboards import main_menu, materials_menu, back_to_menu
from calculations import (
    calc_concrete,
    calc_screed,
    calc_plaster,
    calc_tile
)

router = Router()
user_state = {}


@router.message(F.text == "/start")
async def start(msg: Message):
    user_state.pop(msg.from_user.id, None)
    await msg.answer(
        "🏗 LegalFox — строительный помощник\n\n"
        "Я помогу рассчитать материалы или прикинуть объём работ.",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "back_menu")
async def back(cb: CallbackQuery):
    user_state.pop(cb.from_user.id, None)
    await cb.message.answer("Главное меню:", reply_markup=main_menu())
    await cb.answer()


@router.callback_query(F.data == "materials")
async def materials(cb: CallbackQuery):
    await cb.message.answer("Выберите материал:", reply_markup=materials_menu())
    await cb.answer()


# ===== БЕТОН =====
@router.callback_query(F.data == "mat_concrete")
async def concrete(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "concrete", "step": "l"}
    await cb.message.answer("Введите длину (м):")
    await cb.answer()


# ===== СТЯЖКА =====
@router.callback_query(F.data == "mat_screed")
async def screed(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "screed", "step": "area"}
    await cb.message.answer("Введите площадь (м²):")
    await cb.answer()


# ===== ШТУКАТУРКА =====
@router.callback_query(F.data == "mat_plaster")
async def plaster(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "plaster", "step": "area"}
    await cb.message.answer("Введите площадь стен (м²):")
    await cb.answer()


# ===== ПЛИТКА =====
@router.callback_query(F.data == "mat_tile")
async def tile(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "tile", "step": "area"}
    await cb.message.answer("Введите площадь укладки (м²):")
    await cb.answer()


@router.message()
async def input_handler(msg: Message):
    uid = msg.from_user.id
    if uid not in user_state:
        return

    try:
        val = float(msg.text.replace(",", "."))
    except:
        await msg.answer("Введите число.")
        return

    st = user_state[uid]
    t = st["type"]

    # ===== БЕТОН =====
    if t == "concrete":
        if st["step"] == "l":
            st["l"] = val
            st["step"] = "w"
            await msg.answer("Введите ширину (м):")
        elif st["step"] == "w":
            st["w"] = val
            st["step"] = "h"
            await msg.answer("Введите высоту (м):")
        else:
            v, tot = calc_concrete(st["l"], st["w"], val)
            await msg.answer(
                f"🧱 Бетон:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {tot} м³\n\n"
                f"⚠️ Ориентировочный расчёт.",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    # ===== СТЯЖКА =====
    elif t == "screed":
        if st["step"] == "area":
            st["area"] = val
            st["step"] = "th"
            await msg.answer("Толщина стяжки (см):")
        else:
            v, tot = calc_screed(st["area"], val)
            await msg.answer(
                f"🧱 Стяжка пола:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {tot} м³",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    # ===== ШТУКАТУРКА =====
    elif t == "plaster":
        if st["step"] == "area":
            st["area"] = val
            st["step"] = "th"
            await msg.answer("Толщина слоя (мм):")
        else:
            v, tot = calc_plaster(st["area"], val)
            await msg.answer(
                f"🧱 Штукатурка:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {tot} м³",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    # ===== ПЛИТКА =====
    elif t == "tile":
        if st["step"] == "area":
            st["area"] = val
            st["step"] = "a"
            await msg.answer("Размер плитки A (см):")
        elif st["step"] == "a":
            st["a"] = val
            st["step"] = "b"
            await msg.answer("Размер плитки B (см):")
        else:
            cnt, tot = calc_tile(st["area"], st["a"], val)
            await msg.answer(
                f"🧱 Плитка:\n\n"
                f"Количество: {cnt} шт\n"
                f"С запасом: {tot} шт",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)
