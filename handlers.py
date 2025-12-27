from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from keyboards import main_menu, materials_menu, price_menu
from calculations import calc_concrete, calc_price

router = Router()

user_state = {}


@router.message(F.text == "/start")
async def start(msg: Message):
    await msg.answer(
        "LegalFox | Строительный помощник\n\n"
        "Рассчитаю материалы или прикину стоимость работ.",
        reply_markup=main_menu()
    )


@router.callback_query(F.data == "materials")
async def materials(cb: CallbackQuery):
    await cb.message.answer("Выберите тип работ:", reply_markup=materials_menu())
    await cb.answer()


@router.callback_query(F.data == "mat_concrete")
async def concrete_start(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"step": "length"}
    await cb.message.answer("Введите длину (м):")
    await cb.answer()


@router.message()
async def input_handler(msg: Message):
    uid = msg.from_user.id
    if uid not in user_state:
        return

    state = user_state[uid]

    try:
        value = float(msg.text.replace(",", "."))
    except:
        await msg.answer("Введите число.")
        return

    if state["step"] == "length":
        state["length"] = value
        state["step"] = "width"
        await msg.answer("Введите ширину (м):")

    elif state["step"] == "width":
        state["width"] = value
        state["step"] = "height"
        await msg.answer("Введите высоту / толщину (м):")

    elif state["step"] == "height":
        volume, total = calc_concrete(
            state["length"], state["width"], value
        )

        await msg.answer(
            f"🧱 Расчёт бетона:\n\n"
            f"Объём: {volume} м³\n"
            f"С запасом (10%): {total} м³\n\n"
            f"Рекомендуется заказывать не меньше {round(total + 0.5)} м³.\n\n"
            f"⚠️ Расчёт ориентировочный."
        )

        user_state.pop(uid)


@router.callback_query(F.data == "price")
async def price(cb: CallbackQuery):
    user_state[cb.from_user.id] = {"step": "price_area"}
    await cb.message.answer("Введите площадь (м²):")
    await cb.answer()


@router.message()
async def price_handler(msg: Message):
    uid = msg.from_user.id
    if uid not in user_state:
        return

    state = user_state[uid]

    if state.get("step") == "price_area":
        try:
            area = float(msg.text.replace(",", "."))
        except:
            await msg.answer("Введите число.")
            return

        min_p, max_p = calc_price("stjazhka", area, "standard")

        await msg.answer(
            f"💰 Ориентировочная стоимость стяжки пола:\n\n"
            f"От {min_p:,} до {max_p:,} ₽\n\n"
            f"Цена зависит от толщины слоя и основания.\n"
            f"⚠️ Не является сметой."
        )

        user_state.pop(uid)
