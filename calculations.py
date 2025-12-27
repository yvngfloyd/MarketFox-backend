# calculations.py

# ---------- МАТЕРИАЛЫ ----------

def calc_concrete(volume_m3: float) -> dict:
    cement_kg = volume_m3 * 300
    sand_kg = volume_m3 * 800
    gravel_kg = volume_m3 * 1200

    return {
        "text": (
            f"🧱 Расчёт бетона на {volume_m3} м³:\n"
            f"• Цемент: ~{cement_kg:.0f} кг\n"
            f"• Песок: ~{sand_kg:.0f} кг\n"
            f"• Щебень: ~{gravel_kg:.0f} кг\n\n"
            f"Совет: добавляй 5–10% запаса."
        )
    }


def calc_screed(area_m2: float, thickness_cm: float) -> dict:
    volume = area_m2 * (thickness_cm / 100)
    cement_kg = volume * 350
    sand_kg = volume * 900

    return {
        "text": (
            f"🏗 Стяжка пола:\n"
            f"• Площадь: {area_m2} м²\n"
            f"• Толщина: {thickness_cm} см\n"
            f"• Объём: {volume:.2f} м³\n\n"
            f"Материалы:\n"
            f"• Цемент: ~{cement_kg:.0f} кг\n"
            f"• Песок: ~{sand_kg:.0f} кг"
        )
    }


def calc_plaster(area_m2: float, thickness_cm: float) -> dict:
    volume = area_m2 * (thickness_cm / 100)
    mix_kg = volume * 1400

    return {
        "text": (
            f"🧱 Штукатурка стен:\n"
            f"• Площадь: {area_m2} м²\n"
            f"• Толщина слоя: {thickness_cm} см\n\n"
            f"• Смесь: ~{mix_kg:.0f} кг"
        )
    }


def calc_tile(area_m2: float) -> dict:
    tile_with_reserve = area_m2 * 1.1

    return {
        "text": (
            f"🧩 Укладка плитки:\n"
            f"• Площадь: {area_m2} м²\n"
            f"• С запасом 10%: {tile_with_reserve:.1f} м²"
        )
    }


# ---------- СТОИМОСТЬ РАБОТ ----------

def calc_price(work_type: str, volume: float) -> dict:
    prices = {
        "screed": 800,     # ₽ за м²
        "plaster": 700,    # ₽ за м²
        "tile": 1200       # ₽ за м²
    }

    price_per_unit = prices.get(work_type)

    if not price_per_unit:
        return {
            "text": "❌ Неизвестный тип работ."
        }

    total = volume * price_per_unit

    return {
        "text": (
            f"💰 Приблизительная стоимость работ:\n"
            f"• Вид: {work_type}\n"
            f"• Объём: {volume}\n"
            f"• Цена за единицу: {price_per_unit} ₽\n\n"
            f"➡️ Итого: ~{total:,.0f} ₽"
        )
    }
