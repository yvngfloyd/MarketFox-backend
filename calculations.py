# ===== МАТЕРИАЛЫ =====

def calc_concrete(length, width, height, reserve=10):
    volume = length * width * height
    total = volume * (1 + reserve / 100)
    return round(volume, 2), round(total, 2)


def calc_screed(area, thickness_cm, reserve=10):
    volume = area * (thickness_cm / 100)
    total = volume * (1 + reserve / 100)
    return round(volume, 2), round(total, 2)


def calc_plaster(area, thickness_mm, reserve=10):
    volume = area * (thickness_mm / 1000)
    total = volume * (1 + reserve / 100)
    return round(volume, 2), round(total, 2)


def calc_tile(area, a_cm, b_cm, reserve=10):
    tile_area = (a_cm / 100) * (b_cm / 100)
    count = area / tile_area
    total = count * (1 + reserve / 100)
    return int(count), int(total)
