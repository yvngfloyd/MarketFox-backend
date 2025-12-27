from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from keyboards import (
    main_menu,
    materials_menu,
    price_menu,
    back_to_menu
)
from calculations import (
    calc_concrete,
    calc_screed,
    calc_plaster,
    calc_tile,
    calc_price
)
from ai_helper import ai_recommendation

router = Router()
user_state = {}


# ===== START =====
@router.message(F.text == "/start")
async def start(msg: Message):
    user_state.pop(msg.from_user.id, None)
    await msg.answer(
        "🏗 LegalFox — строительный помощник\n\n"
        "Я помогу рассчитать материалы или прикинуть стоимость работ.",
        reply_markup=main_menu()
    )


# ===== BACK TO MENU =====
@router.callback_query(F.data == "back_menu")
async def back_menu(cb: CallbackQuery):
    user_state.pop(cb.from_user.id, None)
    await cb.message.answer("Главное меню:", reply_markup=main_menu())
    await cb.answer()


# ===== MATERIALS =====
@router.callback_query(F.data == "materials")
async def materials(cb: CallbackQuery):
    await cb.message.answer("Выберите материал:", reply_markup=materials_menu())
    await cb.answer()


@router.callback_query(F.data == "mat_concrete")
async def mat_concrete(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "concrete", "step": "l"}
    await cb.message.answer("Введите длину (м):")
    await cb.answer()


@router.callback_query(F.data == "mat_screed")
async def mat_screed(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "screed", "step": "area"}
    await cb.message.answer("Введите площадь (м²):")
    await cb.answer()


@router.callback_query(F.data == "mat_plaster")
async def mat_plaster(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "plaster", "step": "area"}
    await cb.message.answer("Введите площадь стен (м²):")
    await cb.answer()


@router.callback_query(F.data == "mat_tile")
async def mat_tile(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"type": "tile", "step": "area"}
    await cb.message.answer("Введите площадь укладки (м²):")
    await cb.answer()


# ===== PRICE (2-я функция) =====
@router.callback_query(F.data == "price")
async def price(cb: CallbackQuery):
    await cb.message.answer("Выберите тип работ:", reply_markup=price_menu())
    await cb.answer()


@router.callback_query(F.data.startswith("price_"))
async def price_start(cb: CallbackQuery):
    work_type = cb.data.replace("price_", "")
    user_state[cb.from_user.id] = {"type": work_type}
    await cb.message.answer("Введите объём (м² или м³):")
    await cb.answer()


# ===== INPUT HANDLER (ЕДИНЫЙ) =====
@router.message()
async def input_handler(msg: Message):
    uid = msg.from_user.id
    if uid not in user_state:
        return

    try:
        value = float(msg.text.replace(",", "."))
    except:
        await msg.answer("Введите число.")
        return

    state = user_state[uid]
    t = state["type"]

    # ===== PRICE WITH AI =====
    if t in ["screed", "plaster", "tile", "concrete"] and "step" not in state:
        low, high = calc_price(t, value)

        context = f"""
Тип работ: {t}
Объём: {value}
Диапазон цены: от {low} до {high} ₽
"""

        # 🔥 ВОТ ЗДЕСЬ ВЫЗОВ GIGACHAT
        advice = await ai_recommendation(context)

        await msg.answer(
            f"💰 Ориентировочная стоимость:\n\n"
            f"От {low:,} до {high:,} ₽\n\n"
            f"🧠 Совет эксперта:\n{advice}\n\n"
            f"⚠️ Не является сметой.",
            reply_markup=back_to_menu()
        )

        user_state.pop(uid)
        return

    # ===== MATERIAL CALCULATIONS =====
    if t == "concrete":
        if state["step"] == "l":
            state["l"] = value
            state["step"] = "w"
            await msg.answer("Введите ширину (м):")
        elif state["step"] == "w":
            state["w"] = value
            state["step"] = "h"
            await msg.answer("Введите высоту (м):")
        else:
            v, tot = calc_concrete(state["l"], state["w"], value)
            await msg.answer(
                f"🧱 Бетон:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {tot} м³\n\n"
                f"⚠️ Расчёт ориентировочный.",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    elif t == "screed":
        if state["step"] == "area":
            state["area"] = value
            state["step"] = "th"
            await msg.answer("Толщина стяжки (см):")
        else:
            v, tot = calc_screed(state["area"], value)
            await msg.answer(
                f"🧱 Стяжка пола:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {tot} м³",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    elif t == "plaster":
        if state["step"] == "area":
            state["area"] = value
            state["step"] = "th"
            await msg.answer("Толщина слоя (мм):")
        else:
            v, tot = calc_plaster(state["area"], value)
            await msg.answer(
                f"🧱 Штукатурка:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {tot} м³",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    elif t == "tile":
        if state["step"] == "area":
            state["area"] = value
            state["step"] = "a"
            await msg.answer("Размер плитки A (см):")
        elif state["step"] == "a":
            state["a"] = value
            state["step"] = "b"
            await msg.answer("Размер плитки B (см):")
        else:
            cnt, tot = calc_tile(state["area"], state["a"], value)
            await msg.answer(
                f"🧱 Плитка:\n\n"
                f"Количество: {cnt} шт\n"
                f"С запасом: {tot} шт",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)
