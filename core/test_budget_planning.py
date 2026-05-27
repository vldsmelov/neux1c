from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    AuditAction,
    AuditLog,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Currency,
    Department,
)
from core.services.budget_planning import (
    approve_budget_plan,
    approve_limit_adjustment,
    create_budget_plan,
    create_limit_adjustment,
    submit_budget_plan,
    submit_limit_adjustment,
)
from core.services.one_c_sync import sync_one_c_dataset



class BudgetPlanningTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")

    def test_create_budget_plan_builds_12_months(self):
        plan = self._create_plan()

        self.assertEqual(BudgetLimitMonth.objects.filter(plan=plan).count(), 12)
        self.assertEqual(plan.monthly_total, Decimal("1200000.00"))
        self.assertTrue(plan.is_monthly_total_valid)
        self.assertEqual(plan.status, BudgetPlanStatus.DRAFT)

    def test_invalid_monthly_total_is_rejected(self):
        with self.assertRaises(ValueError):
            self._create_plan(monthly_amounts={month: Decimal("1.00") for month in range(1, 13)})

    def test_submit_and_approve_plan(self):
        plan = self._create_plan()
        self.client.login(username="economist", password="demo12345")

        submit_response = self.client.post(reverse("planning_limits"), {"action": "submit", "plan_id": plan.id})
        plan.refresh_from_db()

        self.assertRedirects(submit_response, reverse("planning_limits"))
        self.assertEqual(plan.status, BudgetPlanStatus.PENDING_APPROVAL)

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        approve_response = self.client.post(reverse("planning_limits"), {"action": "approve", "plan_id": plan.id})
        plan.refresh_from_db()

        self.assertRedirects(approve_response, reverse("planning_limits"))
        self.assertEqual(plan.status, BudgetPlanStatus.APPROVED)
        self.assertIsNotNone(plan.approved_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.APPROVE, object_id=str(plan.id)).exists())

    def test_manager_cannot_create_plan(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.post(reverse("planning_limits"), self._create_payload())

        self.assertRedirects(response, reverse("planning_limits"))
        self.assertEqual(BudgetLimitPlan.objects.count(), 0)

    def test_economist_can_create_plan_from_ui(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(reverse("planning_limits"), self._create_payload())

        self.assertRedirects(response, reverse("planning_limits"))
        self.assertEqual(BudgetLimitPlan.objects.count(), 1)
        self.assertEqual(BudgetLimitMonth.objects.count(), 12)

    def _create_plan(self, monthly_amounts=None):

        return create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=CashFlowArticle.objects.first(),
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=Decimal("1200000.00"),
            approver=self.manager,
            monthly_amounts=monthly_amounts,
            comment="Тестовый лимит",
        )

    def _create_payload(self):

        return {
            "action": "create",
            "department_id": Department.objects.first().id,
            "article_id": CashFlowArticle.objects.first().id,
            "currency_id": Currency.objects.get(code="RUB").id,
            "planning_year": "2026",
            "planning_horizon": "1",
            "annual_amount": "1200000.00",
            "approver_id": self.manager.id,
            "comment": "Тестовый лимит",
            **{f"month_{month}": "100000.00" for month in range(1, 13)},
        }


class LimitAdjustmentTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.plan = self._approved_plan()

    def test_create_adjustment_requires_approved_plan(self):
        draft_plan = self._create_plan()

        with self.assertRaises(ValueError):
            create_limit_adjustment(
                author=self.economist,
                base_plan=draft_plan,
                new_annual_amount=Decimal("1320000.00"),
                reason="Уточнение",
                monthly_amounts={month: Decimal("110000.00") for month in range(1, 13)},
            )

    def test_create_adjustment_requires_reason(self):
        with self.assertRaises(ValueError):
            create_limit_adjustment(
                author=self.economist,
                base_plan=self.plan,
                new_annual_amount=Decimal("1320000.00"),
                reason="",
                monthly_amounts={month: Decimal("110000.00") for month in range(1, 13)},
            )

    def test_approve_adjustment_updates_base_plan_version_and_months(self):
        adjustment = self._create_adjustment()
        submit_limit_adjustment(adjustment, self.economist)
        approve_limit_adjustment(adjustment, self.manager)
        self.plan.refresh_from_db()
        adjustment.refresh_from_db()

        self.assertEqual(adjustment.status, BudgetPlanStatus.APPROVED)
        self.assertEqual(self.plan.annual_amount, Decimal("1320000.00"))
        self.assertEqual(self.plan.version, 2)
        self.assertEqual(self.plan.monthly_total, Decimal("1320000.00"))
        self.assertEqual(BudgetLimitAdjustmentMonth.objects.filter(adjustment=adjustment).count(), 12)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.APPROVE, object_type="BudgetLimitAdjustment").exists())

    def test_adjustment_ui_flow(self):
        self.client.login(username="economist", password="demo12345")

        create_response = self.client.post(reverse("planning_limits"), self._adjustment_payload())
        adjustment = BudgetLimitAdjustment.objects.get()
        submit_response = self.client.post(
            reverse("planning_limits"),
            {"action": "submit_adjustment", "adjustment_id": adjustment.id},
        )
        adjustment.refresh_from_db()

        self.assertRedirects(create_response, reverse("planning_limits"))
        self.assertRedirects(submit_response, reverse("planning_limits"))
        self.assertEqual(adjustment.status, BudgetPlanStatus.PENDING_APPROVAL)

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        approve_response = self.client.post(
            reverse("planning_limits"),
            {"action": "approve_adjustment", "adjustment_id": adjustment.id},
        )
        adjustment.refresh_from_db()
        self.plan.refresh_from_db()

        self.assertRedirects(approve_response, reverse("planning_limits"))
        self.assertEqual(adjustment.status, BudgetPlanStatus.APPROVED)
        self.assertEqual(self.plan.version, 2)

    def _approved_plan(self):
        plan = self._create_plan()
        submit_budget_plan(plan, self.economist)
        approve_budget_plan(plan, self.manager)
        return plan

    def _create_plan(self):

        return create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=CashFlowArticle.objects.first(),
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=Decimal("1200000.00"),
            approver=self.manager,
            monthly_amounts={month: Decimal("100000.00") for month in range(1, 13)},
            comment="Базовый лимит",
        )

    def _create_adjustment(self):
        return create_limit_adjustment(
            author=self.economist,
            base_plan=self.plan,
            new_annual_amount=Decimal("1320000.00"),
            reason="Уточнение бюджета",
            monthly_amounts={month: Decimal("110000.00") for month in range(1, 13)},
        )

    def _adjustment_payload(self):
        return {
            "action": "create_adjustment",
            "base_plan_id": self.plan.id,
            "new_annual_amount": "1320000.00",
            "reason": "Уточнение бюджета",
            **{f"adjustment_month_{month}": "110000.00" for month in range(1, 13)},
        }
