from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    AccountingKind,
    AuditAction,
    AuditLog,
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    ContractKind,
    ExternalPaymentDocument,
    PaymentFact,
    UiThemeSettings,
    UserProfile,
    UserRole,
)
from core.services.budget_planning import create_budget_plan
from core.services.one_c_sync import sync_one_c_dataset


class MockOneCSyncTests(TestCase):
    def test_sync_is_idempotent(self):
        provider = MockOneCProvider()

        first_run = sync_one_c_dataset(provider)
        second_run = sync_one_c_dataset(provider)

        self.assertEqual(first_run.stats["created"]["cash_flow_articles"], 5)
        self.assertEqual(second_run.stats["updated"]["cash_flow_articles"], 5)
        self.assertEqual(CashFlowArticle.objects.count(), 5)
        self.assertEqual(Contract.objects.count(), 4)
        self.assertEqual(PaymentFact.objects.count(), 4)

    def test_article_flags_are_loaded(self):
        sync_one_c_dataset(MockOneCProvider())

        self.assertTrue(CashFlowArticle.objects.get(code="DDS-900").is_internal_turnover)
        self.assertFalse(CashFlowArticle.objects.get(code="DDS-MANUAL-001").exists_in_one_c)

    def test_supplier_contract_reserve_includes_additional_agreements(self):
        sync_one_c_dataset(MockOneCProvider())

        contract = Contract.objects.get(kind=ContractKind.SOLE_SUPPLIER, number="ЕП-2026-014")

        self.assertEqual(contract.reserved_amount, Decimal("3860000.00"))


class UiThemeSettingsTests(TestCase):
    def test_active_theme_exposes_css_variables(self):
        theme = UiThemeSettings.objects.create(
            name="Custom",
            is_active=True,
            primary_color="#006ecb",
        )

        variables = theme.as_css_variables()

        self.assertEqual(variables["--app-primary"], "#006ecb")
        self.assertIn("--app-bg", variables)

    def test_only_one_theme_stays_active(self):
        first = UiThemeSettings.objects.create(name="First", is_active=True)
        second = UiThemeSettings.objects.create(name="Second", is_active=True)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)


class AccessControlTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)

    def test_demo_users_and_groups_are_created(self):
        User = get_user_model()

        self.assertEqual(Group.objects.filter(name__in=["Администратор", "Экономист", "Руководитель", "Бухгалтер"]).count(), 4)
        self.assertEqual(User.objects.filter(username__in=["admin", "economist", "manager", "accountant"]).count(), 4)
        self.assertEqual(UserProfile.objects.get(user__username="accountant").role, UserRole.ACCOUNTANT)

    def test_workspace_requires_login(self):
        response = self.client.get(reverse("workspace"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_economist_can_open_nsi_and_action_is_logged(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("nsi_dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(user__username="economist", action=AuditAction.VIEW, path="/nsi/").exists())

    def test_manager_can_open_workspace_but_not_nsi(self):
        self.client.login(username="manager", password="demo12345")

        workspace_response = self.client.get(reverse("workspace"))
        nsi_response = self.client.get(reverse("nsi_dashboard"))

        self.assertEqual(workspace_response.status_code, 200)
        self.assertEqual(nsi_response.status_code, 403)
        self.assertTrue(AuditLog.objects.filter(user__username="manager", action=AuditAction.DENIED, path="/nsi/").exists())

    def test_accountant_is_blocked_from_app_pages(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("workspace"))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(UserProfile.objects.get(user__username="accountant").can_access_app)

    def test_accountant_login_redirects_to_external_accounting(self):
        response = self.client.post(
            reverse("login"),
            {"username": "accountant", "password": "demo12345"},
        )

        self.assertRedirects(response, reverse("external_accounting"), fetch_redirect_response=False)

    def test_accountant_can_pay_contract_in_external_system(self):
        sync_one_c_dataset(MockOneCProvider())
        contract = Contract.objects.get(kind=ContractKind.SOLE_SUPPLIER, number="ЕП-2026-014")
        self.client.login(username="accountant", password="demo12345")

        response = self.client.post(
            reverse("external_accounting"),
            {"contract_id": contract.id, "amount": "1000.00"},
        )

        self.assertRedirects(response, reverse("external_accounting"))
        payment = ExternalPaymentDocument.objects.get(contract=contract)
        self.assertEqual(payment.amount, Decimal("1000.00"))
        self.assertEqual(
            PaymentFact.objects.filter(contract=contract, external_id=f"external-payment-{payment.id}").count(),
            2,
        )
        self.assertTrue(
            PaymentFact.objects.filter(
                contract=contract,
                accounting_kind=AccountingKind.BU,
                amount=Decimal("1000.00"),
            ).exists()
        )
        self.assertTrue(AuditLog.objects.filter(user__username="accountant", action=AuditAction.PAY).exists())

    def test_economist_cannot_open_external_accounting(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("external_accounting"))

        self.assertEqual(response.status_code, 403)


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
        from core.models import Currency, Department

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
        from core.models import Currency, Department

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
