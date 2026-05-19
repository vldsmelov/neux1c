from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import BudgetLimitPlan, BudgetPlan, BudgetPlanStatus, BudgetScope, CashFlowArticle, Currency, Department
from core.services.one_c_sync import sync_one_c_dataset


class PrimaryBudgetWizardTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.currency = Currency.objects.get(code="RUB")
        self.department = Department.objects.order_by("id").first()
        self.article = CashFlowArticle.objects.order_by("id").first()
        self.client.login(username="economist", password="demo12345")

    def test_wizard_page_renders(self):
        response = self.client.get(reverse("planning_wizard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "primary-budget-wizard")

    def test_wizard_creates_budget_and_limits_pending_approval(self):
        response = self.client.post(reverse("planning_wizard"), self._payload())

        self.assertRedirects(response, reverse("planning_budgets"))
        budget = BudgetPlan.objects.get()
        limit = BudgetLimitPlan.objects.get()
        self.assertEqual(budget.status, BudgetPlanStatus.PENDING_APPROVAL)
        self.assertEqual(limit.status, BudgetPlanStatus.PENDING_APPROVAL)
        self.assertEqual(limit.budget, budget)
        self.assertEqual(limit.monthly_total, Decimal("1000000.00"))

    def test_wizard_accepts_manual_monthly_limits(self):
        payload = self._payload(limit_amount="1200000.00", total_amount="1200000.00")
        for month in range(1, 13):
            payload[f"limit_1_month_{month}"] = "100000.00"

        response = self.client.post(reverse("planning_wizard"), payload)

        self.assertRedirects(response, reverse("planning_budgets"))
        limit = BudgetLimitPlan.objects.get()
        self.assertEqual(limit.months.count(), 12)
        self.assertEqual(limit.monthly_total, Decimal("1200000.00"))

    def test_wizard_rolls_back_package_when_limits_exceed_budget(self):
        response = self.client.post(
            reverse("planning_wizard"),
            self._payload(total_amount="500000.00", limit_amount="600000.00"),
        )

        self.assertRedirects(response, reverse("planning_wizard"))
        self.assertEqual(BudgetPlan.objects.count(), 0)
        self.assertEqual(BudgetLimitPlan.objects.count(), 0)

    def test_wizard_creates_department_budget_package(self):
        response = self.client.post(
            reverse("planning_wizard"),
            self._payload(
                scope=BudgetScope.BY_DEPARTMENT,
                total_amount="",
                department_amounts={self.department.id: "700000.00"},
                limit_amount="650000.00",
            ),
        )

        self.assertRedirects(response, reverse("planning_budgets"))
        budget = BudgetPlan.objects.get()
        limit = BudgetLimitPlan.objects.get()
        self.assertEqual(budget.scope, BudgetScope.BY_DEPARTMENT)
        self.assertEqual(budget.total_amount, Decimal("700000.00"))
        self.assertEqual(limit.budget, budget)

    def _payload(
        self,
        *,
        total_amount="1200000.00",
        limit_amount="1000000.00",
        scope=BudgetScope.OVERALL,
        department_amounts=None,
    ):
        payload = {
            "budget_year": "2026",
            "scope": scope,
            "currency_id": str(self.currency.id),
            "approver_id": str(self.manager.id),
            "total_amount": total_amount,
            "comment": "Первичный ввод",
            "limit_1_department_id": str(self.department.id),
            "limit_1_article_id": str(self.article.id),
            "limit_1_annual_amount": limit_amount,
            "limit_1_comment": "Первичный лимит",
        }
        for department_id, amount in (department_amounts or {}).items():
            payload[f"department_budget_{department_id}"] = amount
        return payload
