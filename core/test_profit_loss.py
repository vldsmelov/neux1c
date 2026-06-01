"""Тесты БДР (P&L) отчёта."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from core.models import Contract, ContractKind, Counterparty, Currency, Organization
from core.services.profit_loss_report import ProfitLossFilters, build_profit_loss


class ProfitLossTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="ООО Тест P&L", source_system="manual")
        self.rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        self.cp_buyer = Counterparty.objects.create(name="Покупатель", source_system="manual")
        self.cp_supplier = Counterparty.objects.create(name="Поставщик", source_system="manual")

    def test_profit_calculated_from_signed_contracts(self):
        Contract.objects.create(
            number="CUST-1", date=date(2026, 1, 10), kind=ContractKind.CUSTOMER,
            name="Покупатель 1", counterparty=self.cp_buyer, currency=self.rub,
            amount=Decimal("2000000.00"),
        )
        Contract.objects.create(
            number="SUP-1", date=date(2026, 2, 5), kind=ContractKind.SOLE_SUPPLIER,
            name="Поставщик 1", counterparty=self.cp_supplier, currency=self.rub,
            amount=Decimal("1500000.00"),
        )
        payload = build_profit_loss(ProfitLossFilters(year=2026))
        self.assertEqual(payload["income_total"], Decimal("2000000.00"))
        self.assertEqual(payload["expense_total"], Decimal("1500000.00"))
        self.assertEqual(payload["profit_accrued"], Decimal("500000.00"))
        # Маржа 500k/2000k = 25%
        self.assertEqual(payload["margin_pct"], Decimal("25.0"))

    def test_loan_contracts_dont_count_as_revenue(self):
        lender = Counterparty.objects.create(name="Кредитор", source_system="manual")
        Contract.objects.create(
            number="LOAN-1", date=date(2026, 1, 10), kind=ContractKind.LOAN_RECEIVED,
            name="Заём", counterparty=lender, currency=self.rub,
            amount=Decimal("500000.00"),
        )
        payload = build_profit_loss(ProfitLossFilters(year=2026))
        self.assertEqual(payload["income_total"], Decimal("0.00"))
        self.assertEqual(payload["expense_total"], Decimal("0.00"))
        self.assertEqual(payload["profit_accrued"], Decimal("0.00"))

    def test_year_filter_excludes_other_years(self):
        Contract.objects.create(
            number="CUST-2025", date=date(2025, 6, 1), kind=ContractKind.CUSTOMER,
            name="Старый", counterparty=self.cp_buyer, currency=self.rub,
            amount=Decimal("9000000.00"),
        )
        Contract.objects.create(
            number="CUST-2026", date=date(2026, 6, 1), kind=ContractKind.CUSTOMER,
            name="Новый", counterparty=self.cp_buyer, currency=self.rub,
            amount=Decimal("1000000.00"),
        )
        payload = build_profit_loss(ProfitLossFilters(year=2026))
        self.assertEqual(payload["income_total"], Decimal("1000000.00"))
