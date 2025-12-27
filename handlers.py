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

    material = state.get("type")

    # ===== СТЯЖКА =====
    if material == "screed":
        if state["step"] == "area":
            state["area"] = value
            state["step"] = "thickness"
            await msg.answer("Введите толщину стяжки (см):")

        elif state["step"] == "thickness":
            v, t = calc_screed(state["area"], value)
            await msg.answer(
                f"🧱 Стяжка пола:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {t} м³\n\n"
                f"⚠️ Расчёт ориентировочный.",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    # ===== ШТУКАТУРКА =====
    elif material == "plaster":
        if state["step"] == "area":
            state["area"] = value
            state["step"] = "thickness"
            await msg.answer("Введите среднюю толщину слоя (мм):")

        elif state["step"] == "thickness":
            v, t = calc_plaster(state["area"], value)
            await msg.answer(
                f"🧱 Штукатурка стен:\n\n"
                f"Объём: {v} м³\n"
                f"С запасом: {t} м³\n\n"
                f"⚠️ Расчёт ориентировочный.",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)

    # ===== ПЛИТКА =====
    elif material == "tile":
        if state["step"] == "area":
            state["area"] = value
            state["step"] = "a"
            await msg.answer("Введите сторону плитки A (см):")

        elif state["step"] == "a":
            state["a"] = value
            state["step"] = "b"
            await msg.answer("Введите сторону плитки B (см):")

        elif state["step"] == "b":
            count, total = calc_tile(state["area"], state["a"], value)
            await msg.answer(
                f"🧱 Плитка:\n\n"
                f"Необходимое количество: {count} шт\n"
                f"С запасом: {total} шт\n\n"
                f"⚠️ Рекомендуется брать с запасом.",
                reply_markup=back_to_menu()
            )
            user_state.pop(uid)
