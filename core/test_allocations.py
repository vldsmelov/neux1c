"""Тесты Allocations Engine."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from core.models import (
    AllocationRule,
    CashFlowArticle,
    Counterparty,
    Currency,
    Department,
    Organization,
    PaymentDirection,
    PaymentFact,
)
from core.services.allocations import preview_allocation


class AllocationsTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        User = get_user_model()
        self.user = User.objects.get(username="economist")
        self.org = Organization.objects.create(name="AL Test", source_system="manual")
        self.rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        self.article = CashFlowArticle.objects.create(
            code="DDS-AL-1", name="Аренда офиса", direction="outflow", source_system="manual",
        )
        self.dept_a = Department.objects.create(code="CFO-A", name="Sales", source_system="manual")
        self.dept_b = Department.objects.create(code="CFO-B", name="Production", source_system="manual")
        self.dept_c = Department.objects.create(code="CFO-C", name="Admin", source_system="manual")
        self.cp = Counterparty.objects.create(name="Landlord", source_system="manual")
        # Факт: 1M за апрель
        PaymentFact.objects.create(
            external_id="al-1", date=date(2026, 4, 15), organization=self.org,
            article=self.article, counterparty=self.cp, amount=Decimal("1000000.00"),
            currency=self.rub, accounting_kind="bu", direction=PaymentDirection.OUTFLOW,
            account="51",
        )

    def test_percent_method_distributes_correctly(self):
        rule = AllocationRule.objects.create(
            name="Аренда: 40/40/20",
            organization=self.org,
            source_article=self.article,
            method=AllocationRule.METHOD_PERCENT,
            target_pct={
                str(self.dept_a.id): 40,
                str(self.dept_b.id): 40,
                str(self.dept_c.id): 20,
            },
        )
        preview = preview_allocation(rule, date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual(preview.fact_total, Decimal("1000000.00"))
        self.assertEqual(len(preview.slices), 3)
        amounts = {s.department.code: s.amount for s in preview.slices}
        self.assertEqual(amounts["CFO-A"], Decimal("400000.00"))
        self.assertEqual(amounts["CFO-B"], Decimal("400000.00"))
        self.assertEqual(amounts["CFO-C"], Decimal("200000.00"))
        # Сумма после округления = факту
        self.assertEqual(sum(amounts.values(), Decimal("0")), Decimal("1000000.00"))

    def test_equal_method_splits_evenly(self):
        rule = AllocationRule.objects.create(
            name="Аренда поровну",
            organization=self.org,
            source_article=self.article,
            method=AllocationRule.METHOD_EQUAL,
            target_pct={str(self.dept_a.id): 1, str(self.dept_b.id): 1, str(self.dept_c.id): 1},
        )
        preview = preview_allocation(rule, date(2026, 4, 1), date(2026, 4, 30))
        # 1M / 3 ≈ 333 333.33 — три равные доли, остаток фиксится на первом
        self.assertEqual(len(preview.slices), 3)
        total = sum((s.amount for s in preview.slices), Decimal("0"))
        self.assertEqual(total, Decimal("1000000.00"))

    def test_warns_when_pct_not_100(self):
        rule = AllocationRule.objects.create(
            name="Сломанные проценты",
            organization=self.org,
            source_article=self.article,
            method=AllocationRule.METHOD_PERCENT,
            target_pct={str(self.dept_a.id): 50, str(self.dept_b.id): 30},  # = 80
        )
        preview = preview_allocation(rule, date(2026, 4, 1), date(2026, 4, 30))
        self.assertTrue(any("100" in w for w in preview.warnings))
