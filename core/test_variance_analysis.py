"""Тесты Variance Analysis."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.models import (
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Counterparty,
    Currency,
    Department,
    Organization,
    PaymentDirection,
    PaymentFact,
)
from core.services.variance_analysis import build_variance_analysis


class VarianceAnalysisTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        User = get_user_model()
        self.user = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.org = Organization.objects.create(name="VA Test", source_system="manual")
        self.rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        self.dept = Department.objects.create(code="CFO-VA", name="Test CFO", source_system="manual")
        self.article = CashFlowArticle.objects.create(
            code="DDS-VA-1", name="Test article", direction="outflow", source_system="manual",
        )
        self.cp1 = Counterparty.objects.create(name="Big spender", source_system="manual")
        self.cp2 = Counterparty.objects.create(name="Small spender", source_system="manual")

    def _make_plan(self, amount):
        return BudgetLimitPlan.objects.create(
            number=f"VA-PL-{amount}",
            document_date=date(2026, 1, 1),
            organization=self.org,
            planning_year=2026,
            planning_horizon=1,
            department=self.dept,
            article=self.article,
            currency=self.rub,
            annual_amount=Decimal(amount),
            status=BudgetPlanStatus.APPROVED,
            approver=self.manager,
            author=self.user,
        )

    def _make_fact(self, eid, amount, cp):
        return PaymentFact.objects.create(
            external_id=eid,
            date=date(2026, 5, 1),
            organization=self.org,
            article=self.article,
            counterparty=cp,
            amount=Decimal(amount),
            currency=self.rub,
            accounting_kind="bu",
            direction=PaymentDirection.OUTFLOW,
            account="51",
        )

    def test_overrun_detected_with_drivers(self):
        self._make_plan("1000000.00")
        # Факт = 1.5M (перерасход 50%)
        self._make_fact("va-1", "1000000.00", self.cp1)
        self._make_fact("va-2", "500000.00", self.cp2)
        payload = build_variance_analysis(year=2026)
        self.assertEqual(len(payload["variances"]), 1)
        v = payload["variances"][0]
        self.assertEqual(v.plan_amount, Decimal("1000000.00"))
        self.assertEqual(v.fact_amount, Decimal("1500000.00"))
        self.assertEqual(v.variance, Decimal("500000.00"))
        self.assertEqual(v.variance_pct, Decimal("50.0"))
        self.assertTrue(v.is_overrun)
        self.assertEqual(v.severity, "critical")  # 50%
        # Топ-драйвер — Big spender (66.7% факта)
        self.assertEqual(v.top_drivers[0].counterparty_name, "Big spender")
        self.assertEqual(v.top_drivers[0].fact_amount, Decimal("1000000.00"))

    def test_savings_detected(self):
        self._make_plan("1000000.00")
        self._make_fact("va-s1", "300000.00", self.cp1)
        payload = build_variance_analysis(year=2026)
        v = payload["variances"][0]
        self.assertFalse(v.is_overrun)
        self.assertEqual(v.variance, Decimal("-700000.00"))
        self.assertGreater(payload["total_savings"], Decimal("-1000000"))

    def test_threshold_filters_minor_variances(self):
        self._make_plan("1000000.00")
        self._make_fact("va-mini", "1050000.00", self.cp1)  # +5%
        payload = build_variance_analysis(year=2026, threshold_pct=Decimal("10"))
        self.assertEqual(len(payload["variances"]), 0)

    def test_no_plan_but_fact_treated_as_overspend(self):
        # План не задавали, но есть факт → 100% перерасход
        self._make_fact("va-x", "50000.00", self.cp1)
        payload = build_variance_analysis(year=2026)
        self.assertEqual(len(payload["variances"]), 1)
        v = payload["variances"][0]
        self.assertEqual(v.plan_amount, Decimal("0"))
        self.assertEqual(v.fact_amount, Decimal("50000.00"))
        self.assertTrue(v.is_overrun)
