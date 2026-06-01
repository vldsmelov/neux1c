"""Сервис работы с курсами валют. Используется для:
* перевода валютных сумм в RUB по курсу на дату документа;
* пересчёта остатков (FX revaluation) при изменении курса;
* отображения RUB-эквивалента валютных позиций в кейсах.
"""

from datetime import date as date_cls
from decimal import Decimal

from core.models import Currency, ExchangeRate


RUB_CODE = "RUB"


def get_rate_to_rub(currency: Currency | str, target_date: date_cls) -> Decimal | None:
    """Курс валюты к RUB на указанную дату.

    Логика:
    * RUB → 1 (trivial).
    * Иначе ищем последний курс с rate_date <= target_date.
    * Если нет ни одного курса — возвращаем None (вызывающий код
      должен сам решить fallback: использовать manual_exchange_rate
      контракта или 1).
    """
    code = currency.code if isinstance(currency, Currency) else currency
    if code == RUB_CODE:
        return Decimal("1.000000")

    qs = ExchangeRate.objects.filter(
        currency__code=code,
        rate_date__lte=target_date,
    ).order_by("-rate_date", "-id")
    rate = qs.first()
    return rate.rate_to_rub if rate else None


def convert_to_rub(amount: Decimal, currency: Currency | str, target_date: date_cls, fallback_rate: Decimal | None = None) -> Decimal:
    """Сумма × курс. Если курса нет — используем fallback_rate, иначе сумма как есть."""
    rate = get_rate_to_rub(currency, target_date)
    if rate is None:
        rate = fallback_rate or Decimal("1")
    return (Decimal(amount) * rate).quantize(Decimal("0.01"))


def fetch_cbr_rates(target_date: date_cls, *, http_fetcher=None) -> dict:
    """Загружает курсы валют ЦБ РФ на указанную дату.

    Использует публичный XML-фид cbr.ru/scripts/XML_daily.asp.
    Создаёт ExchangeRate записи с source=cbr для всех валют, которые
    есть в нашей таблице Currency (по коду ISO).

    Возвращает: {"created": N, "updated": M, "skipped": K, "errors": [...]}.

    http_fetcher — функция (url) → bytes, для тестов можно подменить.
    """
    import xml.etree.ElementTree as ET
    from urllib.request import urlopen

    url = f"https://www.cbr.ru/scripts/XML_daily.asp?date_req={target_date:%d/%m/%Y}"

    if http_fetcher is None:
        def http_fetcher(u):
            with urlopen(u, timeout=15) as resp:
                return resp.read()

    try:
        raw = http_fetcher(url)
    except Exception as exc:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": [f"HTTP fetch failed: {exc}"]}

    # XML ЦБ — в windows-1251, нужно декодировать
    try:
        text = raw.decode("windows-1251")
        root = ET.fromstring(text)
    except Exception as exc:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": [f"XML parse failed: {exc}"]}

    known_currencies = {c.code: c for c in Currency.objects.all()}
    created = updated = skipped = 0
    errors: list[str] = []

    for valute in root.findall("Valute"):
        code_el = valute.find("CharCode")
        nominal_el = valute.find("Nominal")
        value_el = valute.find("Value")
        if code_el is None or nominal_el is None or value_el is None:
            continue
        code = code_el.text.strip().upper()
        if code not in known_currencies:
            skipped += 1
            continue
        try:
            nominal = Decimal(nominal_el.text.replace(",", "."))
            value = Decimal(value_el.text.replace(",", "."))
            rate = (value / nominal).quantize(Decimal("0.000001"))
        except Exception as exc:
            errors.append(f"{code}: {exc}")
            continue

        _, was_created = ExchangeRate.objects.update_or_create(
            currency=known_currencies[code],
            rate_date=target_date,
            source=ExchangeRate.SOURCE_CBR,
            defaults={
                "rate_to_rub": rate,
                "comment": f"Автоматически загружено с cbr.ru на {target_date:%d.%m.%Y}",
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


def latest_rates_summary() -> list[dict]:
    """Сводка: последний курс для каждой валюты ≠ RUB. Для UI."""
    summary: list[dict] = []
    for cur in Currency.objects.exclude(code=RUB_CODE).order_by("code"):
        latest = (
            ExchangeRate.objects
            .filter(currency=cur)
            .order_by("-rate_date", "-id")
            .first()
        )
        summary.append({
            "currency": cur,
            "latest_rate": latest,
        })
    return summary
