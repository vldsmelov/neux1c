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


@register.simple_tag
def post_value(source, *parts):
    """Resolve a dynamic POST key by concatenating parts and looking it up.

    Usage: ``{% post_value submitted "limit_" row.index "_annual_amount" %}``.
    Returns empty string for any missing source/key.
    """
    if not source:
        return ""
    key = "".join("" if p is None else str(p) for p in parts)
    try:
        return source.get(key, "")
    except AttributeError:
        return source[key] if isinstance(source, dict) and key in source else ""


@register.simple_tag
def post_equals(source, value, *parts):
    """Check whether the value at a dynamic POST key equals ``value``.

    Useful for ``selected``/``checked`` attributes on dynamically-named fields.
    Comparison is string-based to match request.POST semantics.
    """
    if not source:
        return False
    key = "".join("" if p is None else str(p) for p in parts)
    try:
        current = source.get(key, "")
    except AttributeError:
        current = source[key] if isinstance(source, dict) and key in source else ""
    return str(current) == str(value)


@register.filter
def dict_get(source, key):
    """Lookup an arbitrary key in a dict-like (e.g. request.POST / QueryDict / plain dict).

    Returns empty string when the source is falsy or the key is missing, so it is
    safe to compose with the ``default`` filter (e.g. ``{{ post|dict_get:name|default:'' }}``).
    """
    if not source:
        return ""
    if key is None:
        return ""
    try:
        getter = source.get
    except AttributeError:
        try:
            return source[key]
        except (KeyError, TypeError, IndexError):
            return ""
    value = getter(str(key), "")
    if value == "" and not isinstance(key, str):
        value = getter(key, "")
    return value

