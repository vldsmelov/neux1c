from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template


register = template.Library()


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@register.filter
def ru_number(value, digits=2):
    number = _to_decimal(value)
    if number is None:
        return value if value not in (None, "") else "0,00"

    try:
        precision = int(digits)
    except (TypeError, ValueError):
        precision = 2
    precision = max(0, precision)

    quant = Decimal("1").scaleb(-precision)
    rounded = number.quantize(quant, rounding=ROUND_HALF_UP)
    sign = "-" if rounded < 0 else ""
    rounded = abs(rounded)

    fixed = f"{rounded:.{precision}f}"
    if "." in fixed:
        whole, frac = fixed.split(".", 1)
    else:
        whole, frac = fixed, ""

    grouped = f"{int(whole):,}".replace(",", " ")
    if precision == 0:
        return f"{sign}{grouped}"
    return f"{sign}{grouped},{frac}"

