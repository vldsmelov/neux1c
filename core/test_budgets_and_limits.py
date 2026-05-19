from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import BudgetLimitPlan, BudgetPlan, BudgetScope, CashFlowArticle, Currency, Department
from core.services.budget_planning import create_budget_plan
from core.services.budgets import build_budget_rows, create_budget
from core.services.one_c_sync import sync_one_c_dataset


class BudgetAndLimitSplitTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.currency = Currency.objects.get(code="RUB")
        self.article = CashFlowArticle.objects.first()
        self.department = Department.objects.first()

    def test_budget_can_exceed_limits_and_reserve_is_reported(self):
        budget = create_budget(
            author=self.economist,
            budget_year=2026,
            scope=BudgetScope.OVERALL,
            currency=self.currency,
            total_amount=Decimal("1200000.00"),
        )
        limit = self._create_limit(budget=budget, amount=Decimal("1000000.00"))

        rows = build_budget_rows()

        self.assertEqual(limit.budget, budget)
        self.assertEqual(rows[0]["used_total"], Decimal("1000000.00"))
        self.assertEqual(rows[0]["reserve_total"], Decimal("200000.00"))

    def test_limit_cannot_exceed_overall_budget(self):
        budget = create_budget(
            author=self.economist,
            budget_year=2026,
            scope=BudgetScope.OVERALL,
            currency=self.currency,
            total_amount=Decimal("1200000.00"),
        )
        self._create_limit(budget=budget, amount=Decimal("1000000.00"))

        with self.assertRaisesMessage(ValueError, "Лимит превышает доступный бюджет"):
            self._create_limit(budget=budget, amount=Decimal("300000.00"))

    def test_department_budget_limits_are_checked_per_department(self):
        departments = list(Department.objects.order_by("id")[:2])
        budget = create_budget(
            author=self.economist,
            budget_year=2026,
            scope=BudgetScope.BY_DEPARTMENT,
            currency=self.currency,
            department_amounts={
                departments[0].id: Decimal("500000.00"),
                departments[1].id: Decimal("700000.00"),
            },
        )
        self._create_limit(budget=budget, department=departments[0], amount=Decimal("400000.00"))

        with self.assertRaisesMessage(ValueError, "Лимит превышает доступный бюджет"):
            self._create_limit(budget=budget, department=departments[0], amount=Decimal("200000.00"))

        second_department_limit = self._create_limit(
            budget=budget,
            department=departments[1],
            amount=Decimal("700000.00"),
        )
        self.assertEqual(second_department_limit.department, departments[1])

    def test_economist_can_create_overall_budget_from_ui(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("planning_budgets"),
            {
                "action": "create",
                "budget_year": "2026",
                "scope": BudgetScope.OVERALL,
                "currency_id": self.currency.id,
                "total_amount": "1200000.00",
                "comment": "Общий бюджет",
            },
        )

        self.assertRedirects(response, reverse("planning_budgets"))
        self.assertEqual(BudgetPlan.objects.count(), 1)
        self.assertEqual(BudgetPlan.objects.get().total_amount, Decimal("1200000.00"))

    def test_limit_ui_can_use_budget_without_monthly_split(self):
        budget = create_budget(
            author=self.economist,
            budget_year=2026,
            scope=BudgetScope.OVERALL,
            currency=self.currency,
            total_amount=Decimal("1200000.00"),
        )
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("planning_limits"),
            {
                "action": "create",
                "budget_id": budget.id,
                "department_id": self.department.id,
                "article_id": self.article.id,
                "currency_id": self.currency.id,
                "planning_year": "2026",
                "planning_horizon": "1",
                "annual_amount": "1200000.00",
                "approver_id": self.manager.id,
                "comment": "Лимит без ручной разбивки",
            },
        )

        self.assertRedirects(response, reverse("planning_limits"))
        limit = BudgetLimitPlan.objects.get()
        self.assertEqual(limit.budget, budget)
        self.assertEqual(limit.monthly_total, Decimal("1200000.00"))

    def _create_limit(self, *, budget, amount, department=None):
        return create_budget_plan(
            author=self.economist,
            budget=budget,
            department=department or self.department,
            article=self.article,
            currency=self.currency,
            planning_year=2026,
            planning_horizon=1,
            annual_amount=amount,
            approver=self.manager,
            monthly_amounts=None,
            comment="Лимит",
        )
