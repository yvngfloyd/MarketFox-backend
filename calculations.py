def calc_concrete(length, width, height, reserve=10):
    volume = length * width * height
    total = volume * (1 + reserve / 100)
    return round(volume, 2), round(total, 2)


PRICE_RANGES = {
    "stjazhka": (500, 900),
    "shtukaturka": (600, 1200),
    "plitka": (1200, 2500),
    "beton": (3500, 6000),
}

LEVEL_COEF = {
    "econom": 1.0,
    "standard": 1.2,
    "premium": 1.5,
}


def calc_price(work_type, amount, level):
    min_price, max_price = PRICE_RANGES[work_type]
    coef = LEVEL_COEF[level]

    total_min = amount * min_price * coef
    total_max = amount * max_price * coef

    return round(total_min), round(total_max)
