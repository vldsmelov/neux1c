"""Тесты Multi-currency: курсы валют и пересчёт."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from core.models import Currency, ExchangeRate
from core.services.exchange_rates import (
    convert_to_rub,
    get_rate_to_rub,
    latest_rates_summary,
)


class ExchangeRatesTests(TestCase):
    def setUp(self):
        self.rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        self.usd = Currency.objects.create(code="USD", name="Доллар США", source_system="manual")
        self.eur = Currency.objects.create(code="EUR", name="Евро", source_system="manual")

    def test_rub_to_rub_is_one(self):
        self.assertEqual(get_rate_to_rub("RUB", date(2026, 5, 1)), Decimal("1.000000"))

    def test_get_rate_finds_latest_before_date(self):
        ExchangeRate.objects.create(
            currency=self.usd, rate_date=date(2026, 1, 1),
            rate_to_rub=Decimal("90.000000"), source="manual",
        )
        ExchangeRate.objects.create(
            currency=self.usd, rate_date=date(2026, 5, 1),
            rate_to_rub=Decimal("95.470000"), source="manual",
        )
        # На 1 марта — должен взять курс от 1 января (последний ≤ даты)
        self.assertEqual(get_rate_to_rub(self.usd, date(2026, 3, 15)), Decimal("90.000000"))
        # На 5 мая — курс от 1 мая
        self.assertEqual(get_rate_to_rub(self.usd, date(2026, 5, 5)), Decimal("95.470000"))
        # До первого курса — None
        self.assertIsNone(get_rate_to_rub(self.usd, date(2025, 12, 1)))

    def test_convert_to_rub_uses_rate(self):
        ExchangeRate.objects.create(
            currency=self.usd, rate_date=date(2026, 1, 1),
            rate_to_rub=Decimal("90.000000"), source="manual",
        )
        result = convert_to_rub(Decimal("100"), self.usd, date(2026, 3, 1))
        self.assertEqual(result, Decimal("9000.00"))

    def test_convert_to_rub_fallback_when_no_rate(self):
        result = convert_to_rub(Decimal("100"), self.eur, date(2026, 3, 1), fallback_rate=Decimal("100"))
        self.assertEqual(result, Decimal("10000.00"))

    def test_latest_rates_summary(self):
        ExchangeRate.objects.create(
            currency=self.usd, rate_date=date(2026, 5, 1),
            rate_to_rub=Decimal("95.47"), source="cbr",
        )
        summary = latest_rates_summary()
        codes = [s["currency"].code for s in summary]
        self.assertIn("USD", codes)
        self.assertIn("EUR", codes)
        usd_row = next(s for s in summary if s["currency"].code == "USD")
        self.assertEqual(usd_row["latest_rate"].rate_to_rub, Decimal("95.470000"))
        eur_row = next(s for s in summary if s["currency"].code == "EUR")
        self.assertIsNone(eur_row["latest_rate"])
