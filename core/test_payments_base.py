"""Shared base for payment-request test suites.

Provides the common setUp (demo roles + Mock-1C data + an approved 2M limit
on DDS-010) and the _approve_limit_for_article helper. Feature-area suites
inherit from PaymentRequestTestBase so each file stays focused.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.integrations.one_c.mock import MockOneCProvider
from core.models import CashFlowArticle, Contract
from core.services.budget_planning import approve_budget_plan, create_budget_plan, submit_budget_plan
from core.services.one_c_sync import sync_one_c_dataset
from django.core.management import call_command


class PaymentRequestTestBase(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.contract = Contract.objects.filter(kind="sole_supplier").order_by("id").first()
        self.assertIsNotNone(self.contract)
        self.article = CashFlowArticle.objects.get(code="DDS-010")
        self._approve_limit_for_article(self.article, Decimal("2000000.00"))

    def _approve_limit_for_article(self, article, amount: Decimal):
        from core.models import Currency, Department

        monthly_amounts = {month: (amount / Decimal("12")).quantize(Decimal("0.01")) for month in range(1, 12)}
        monthly_amounts[12] = amount - sum(monthly_amounts.values())
        plan = create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=article,
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=amount,
            approver=self.manager,
            monthly_amounts=monthly_amounts,
            comment="Лимит для заявок",
        )
        submit_budget_plan(plan, self.economist)
        approve_budget_plan(plan, self.manager)
