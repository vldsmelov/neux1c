"""Тесты Intercompany Elimination & Consolidation."""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from core.models import (
    CashFlowArticle,
    Counterparty,
    Currency,
    Organization,
    PaymentDirection,
    PaymentFact,
)
from core.services.consolidation import build_consolidation_report


class ConsolidationTests(TestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name="Холд-А", source_system="manual", is_holding_member=True)
        self.org_b = Organization.objects.create(name="Холд-Б", source_system="manual", is_holding_member=True)
        self.org_ext = Organization.objects.create(name="Внешняя", source_system="manual", is_holding_member=False)
        self.rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        self.ext_article = CashFlowArticle.objects.create(
            code="DDS-EXT", name="Внешние оплаты", direction="outflow",
            is_internal_turnover=False, source_system="manual",
        )
        self.vgo_article = CashFlowArticle.objects.create(
            code="DDS-VGO", name="ВГО", direction="outflow",
            is_internal_turnover=True, source_system="manual",
        )
        self.cp = Counterparty.objects.create(name="Партнёр", source_system="manual")

    def _make_fact(self, eid, org, article, amount, direction=PaymentDirection.OUTFLOW):
        return PaymentFact.objects.create(
            external_id=eid, date=date(2026, 5, 15), organization=org,
            article=article, counterparty=self.cp, amount=Decimal(amount),
            currency=self.rub, accounting_kind="bu", direction=direction,
            account="51",
        )

    def test_eliminates_internal_turnover(self):
        # Холд-А → Холд-Б: ВГО 500k (для А — расход, для Б — приход)
        self._make_fact("c-1", self.org_a, self.vgo_article, "500000", PaymentDirection.OUTFLOW)
        self._make_fact("c-2", self.org_b, self.vgo_article, "500000", PaymentDirection.INFLOW)
        # Холд-А → внешнему: 1M расхода
        self._make_fact("c-3", self.org_a, self.ext_article, "1000000", PaymentDirection.OUTFLOW)
        # Холд-Б → внешнему: 800k поступления от внешних клиентов
        self._make_fact("c-4", self.org_b, self.ext_article, "800000", PaymentDirection.INFLOW)

        payload = build_consolidation_report(year=2026)

        # Холдинг = 2 компании
        self.assertEqual(payload["holding_size"], 2)
        # Brutto: in = 500k + 800k = 1.3M; out = 500k + 1M = 1.5M
        self.assertEqual(payload["total_gross_in"], Decimal("1300000.00"))
        self.assertEqual(payload["total_gross_out"], Decimal("1500000.00"))
        # ВГО устранено: in=500k, out=500k
        self.assertEqual(payload["total_eliminated_in"], Decimal("500000.00"))
        self.assertEqual(payload["total_eliminated_out"], Decimal("500000.00"))
        # Консолидированный: in=800k, out=1M, net=-200k
        self.assertEqual(payload["total_consolidated_in"], Decimal("800000.00"))
        self.assertEqual(payload["total_consolidated_out"], Decimal("1000000.00"))
        self.assertEqual(payload["total_consolidated_net"], Decimal("-200000.00"))
        # ВГО симметрия — баланс
        self.assertEqual(payload["elimination_imbalance"], Decimal("0.00"))

    def test_excludes_external_organizations(self):
        # Внешняя компания не в холдинге → не считается
        self._make_fact("e-1", self.org_ext, self.ext_article, "9999999.00", PaymentDirection.OUTFLOW)
        payload = build_consolidation_report(year=2026)
        self.assertEqual(payload["total_gross_out"], Decimal("0.00"))

    def test_vgo_imbalance_detected(self):
        # Только outflow ВГО без зеркального inflow → дисбаланс
        self._make_fact("imb-1", self.org_a, self.vgo_article, "300000", PaymentDirection.OUTFLOW)
        payload = build_consolidation_report(year=2026)
        # eliminated_in = 0, eliminated_out = 300k → imbalance = -300k
        self.assertEqual(payload["elimination_imbalance"], Decimal("-300000.00"))
