"""Тесты Driver-based Planning."""

from decimal import Decimal

from django.test import TestCase

from core.models import Organization, PlanDriver
from core.services.driver_planning import compute_formula


class DriverPlanningTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="DP Test", source_system="manual")

    def _make_driver(self, name, monthly, kind=PlanDriver.KIND_VOLUME, code=""):
        return PlanDriver.objects.create(
            name=name,
            code=code,
            organization=self.org,
            year=2026,
            kind=kind,
            monthly_values=[str(v) for v in monthly],
        )

    def test_simple_multiplication_formula(self):
        # Volume = 100 шт. каждый месяц, Price = 1000 ₽/шт. каждый месяц
        volume = self._make_driver("Volume", [100] * 12)
        price = self._make_driver("Price", [1000] * 12, kind=PlanDriver.KIND_PRICE)

        result = compute_formula([
            {"driver_id": volume.id, "op": "multiply"},
            {"driver_id": price.id, "op": "multiply"},
        ])
        # Каждый месяц = 100 × 1000 = 100 000
        self.assertEqual(len(result.monthly_values), 12)
        for v in result.monthly_values:
            self.assertEqual(v, Decimal("100000.00"))
        # Σ = 1 200 000
        self.assertEqual(result.annual_total, Decimal("1200000.00"))

    def test_seasonality_changes_monthly(self):
        # Volume базовый 100, сезонность 0.5/1.0/2.0 разная по месяцам
        volume = self._make_driver("Volume", [100] * 12)
        seasonality = self._make_driver(
            "Seasonality",
            [Decimal("0.5"), Decimal("0.8"), Decimal("1.0"), Decimal("1.2"),
             Decimal("1.5"), Decimal("2.0"), Decimal("2.0"), Decimal("1.5"),
             Decimal("1.2"), Decimal("1.0"), Decimal("0.8"), Decimal("0.5")],
            kind=PlanDriver.KIND_MULTIPLIER,
        )
        result = compute_formula([
            {"driver_id": volume.id, "op": "multiply"},
            {"driver_id": seasonality.id, "op": "multiply"},
        ])
        # Январь: 100 × 0.5 = 50, Июнь: 100 × 2.0 = 200
        self.assertEqual(result.monthly_values[0], Decimal("50.00"))
        self.assertEqual(result.monthly_values[5], Decimal("200.00"))

    def test_add_operation(self):
        base = self._make_driver("Base", [100] * 12)
        bonus = self._make_driver("Bonus", [10] * 12)
        result = compute_formula([
            {"driver_id": base.id, "op": "multiply"},
            {"driver_id": bonus.id, "op": "add"},
        ])
        for v in result.monthly_values:
            self.assertEqual(v, Decimal("110.00"))

    def test_empty_formula_returns_zeros(self):
        result = compute_formula([])
        self.assertEqual(len(result.monthly_values), 12)
        self.assertTrue(all(v == Decimal("0") for v in result.monthly_values))
        self.assertEqual(result.annual_total, Decimal("0"))

    def test_driver_annual_total(self):
        d = self._make_driver("Test", [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
        self.assertEqual(d.annual_total(), Decimal("780"))
