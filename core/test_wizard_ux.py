from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    BudgetLimitAdjustment,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    Currency,
    Department,
    IntegrationRequest,
    IntegrationRequestStatus,
    Organization,
    PaymentFact,
    PaymentFactAdjustment,
    PaymentRequest,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.budgets import create_budget
from core.services.budget_planning import approve_budget_plan, create_budget_plan, submit_budget_plan
from core.services.one_c_sync import sync_one_c_dataset
from core.services.payment_requests import create_payment_request, submit_payment_request


class WizardUxTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.admin = User.objects.get(username="admin")
        self.manager = User.objects.get(username="manager")
        self.accountant = User.objects.get(username="accountant")
        self.currency = Currency.objects.get(code="RUB")
        self.department = Department.objects.order_by("id").first()
        self.article = CashFlowArticle.objects.get(code="DDS-010")
        self.contract = Contract.objects.filter(kind="sole_supplier").order_by("id").first()
        self.organization = Organization.objects.order_by("id").first()
        self._approve_limit_for_article(self.article, Decimal("2000000.00"))

    def test_wizard_pages_render_for_economist(self):
        self.client.login(username="economist", password="demo12345")

        for route_name in [
            "payment_request_wizard",
            "payment_fact_adjustment_wizard",
            "integration_request_wizard",
            "limit_adjustment_wizard",
        ]:
            with self.subTest(route=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)

    def test_workspace_exposes_wizard_entry_points(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("workspace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("planning_wizard"))
        self.assertContains(response, reverse("payment_request_wizard"))
        self.assertContains(response, reverse("limit_adjustment_wizard"))
        self.assertContains(response, reverse("payment_fact_adjustment_wizard"))
        self.assertContains(response, "Мой рабочий день")

    def test_workspace_shows_manager_workday_approval_task(self):
        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=self.organization,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            additional_agreement=None,
            currency=self.currency,
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Оплата поставки",
            comment="pending",
        )
        submit_payment_request(payment_request, self.economist)
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("workspace"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Согласовать платеж")
        self.assertContains(
            response,
            reverse("document_action_wizard", args=["payment-request", payment_request.id, "approve"]),
        )

    def test_payment_request_wizard_creates_pending_request(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("payment_request_wizard"),
            {
                "request_kind": PaymentRequestKind.BY_CONTRACT,
                "organization_id": self.organization.id,
                "article_id": self.article.id,
                "counterparty_id": self.contract.counterparty_id,
                "contract_id": self.contract.id,
                "currency_id": self.currency.id,
                "amount": "100000.00",
                "manual_exchange_rate": "1.0000",
                "approver_id": self.manager.id,
                "payment_purpose": "Оплата поставки",
                "comment": "wizard",
            },
        )

        self.assertRedirects(response, reverse("payment_requests"))
        payment_request = PaymentRequest.objects.get()
        self.assertEqual(payment_request.status, PaymentRequestStatus.PENDING_APPROVAL)

    def test_payment_request_edit_wizard_updates_draft_request(self):
        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=self.organization,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            additional_agreement=None,
            currency=self.currency,
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Оплата поставки",
            comment="draft",
        )
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("payment_request_edit_wizard", args=[payment_request.id]))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("payment_request_edit_wizard", args=[payment_request.id]),
            {
                "request_kind": PaymentRequestKind.BY_CONTRACT,
                "organization_id": self.organization.id,
                "article_id": self.article.id,
                "counterparty_id": self.contract.counterparty_id,
                "contract_id": self.contract.id,
                "currency_id": self.currency.id,
                "amount": "125000.00",
                "manual_exchange_rate": "1.0000",
                "approver_id": self.manager.id,
                "payment_purpose": "Оплата поставки после уточнения",
                "comment": "edited by wizard",
            },
        )

        self.assertRedirects(response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.amount, Decimal("125000.00"))
        self.assertEqual(payment_request.payment_purpose, "Оплата поставки после уточнения")
        self.assertEqual(payment_request.status, PaymentRequestStatus.DRAFT)

    def test_payment_request_status_changes_go_through_action_wizard(self):
        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=self.organization,
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            additional_agreement=None,
            currency=self.currency,
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
            payment_purpose="Оплата поставки",
            comment="draft",
        )
        submit_url = reverse("document_action_wizard", args=["payment-request", payment_request.id, "submit"])
        approve_url = reverse("document_action_wizard", args=["payment-request", payment_request.id, "approve"])
        transfer_url = reverse("document_action_wizard", args=["payment-request", payment_request.id, "transfer"])

        self.client.login(username="economist", password="demo12345")
        response = self.client.get(reverse("payment_requests"))
        self.assertContains(response, submit_url)
        self.assertNotContains(response, 'name="action"')
        response = self.client.post(submit_url)
        self.assertRedirects(response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.PENDING_APPROVAL)

        self.client.login(username="manager", password="demo12345")
        response = self.client.get(approve_url)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(approve_url)
        self.assertRedirects(response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.APPROVED)

        self.client.login(username="economist", password="demo12345")
        response = self.client.post(transfer_url)
        self.assertRedirects(response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.TRANSFERRED)

    def test_planning_status_changes_go_through_action_wizard(self):
        budget = create_budget(
            author=self.economist,
            budget_year=2027,
            scope="overall",
            currency=self.currency,
            total_amount=Decimal("500000.00"),
            comment="draft budget",
        )
        plan = create_budget_plan(
            author=self.economist,
            budget=budget,
            department=self.department,
            article=CashFlowArticle.objects.get(code="DDS-020"),
            currency=self.currency,
            planning_year=2027,
            planning_horizon=1,
            annual_amount=Decimal("100000.00"),
            approver=self.manager,
            monthly_amounts=None,
            comment="draft limit",
        )
        budget_submit_url = reverse("document_action_wizard", args=["budget", budget.id, "submit"])
        limit_submit_url = reverse("document_action_wizard", args=["limit", plan.id, "submit"])

        self.client.login(username="economist", password="demo12345")
        response = self.client.get(reverse("planning_budgets"))
        self.assertContains(response, budget_submit_url)
        self.assertNotContains(response, 'name="action"')
        response = self.client.get(reverse("planning_limits"))
        self.assertContains(response, limit_submit_url)
        self.assertNotContains(response, 'name="action"')

        response = self.client.post(budget_submit_url)
        self.assertRedirects(response, reverse("planning_budgets"))
        budget.refresh_from_db()
        self.assertEqual(budget.status, BudgetPlanStatus.PENDING_APPROVAL)

        response = self.client.post(limit_submit_url)
        self.assertRedirects(response, reverse("planning_limits"))
        plan.refresh_from_db()
        self.assertEqual(plan.status, BudgetPlanStatus.PENDING_APPROVAL)

    def test_limit_adjustment_wizard_creates_pending_request(self):
        plan = self._approved_plan(Decimal("1200000.00"))
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("limit_adjustment_wizard"),
            {
                "base_plan_id": plan.id,
                "new_annual_amount": "1320000.00",
                "reason": "Уточнение бюджета",
            },
        )

        self.assertRedirects(response, reverse("planning_limits"))
        adjustment = BudgetLimitAdjustment.objects.get()
        self.assertEqual(adjustment.status, BudgetPlanStatus.PENDING_APPROVAL)
        self.assertEqual(adjustment.monthly_total, Decimal("1320000.00"))

    def test_payment_fact_adjustment_wizard_saves_version(self):
        fact = PaymentFact.objects.order_by("id").first()
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("payment_fact_adjustment_wizard"),
            {
                "fact_id": fact.id,
                "new_amount": str(fact.amount + Decimal("1.00")),
                "new_comment": "wizard correction",
                "reason": "Уточнение факта",
            },
        )

        self.assertRedirects(response, reverse("payment_facts"))
        self.assertEqual(PaymentFactAdjustment.objects.count(), 1)

    def test_integration_request_wizard_creates_request(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.post(
            reverse("integration_request_wizard"),
            {
                "integration_name": "Bank API",
                "target_system": "Bank",
                "description": "Автоматическая загрузка выписки",
            },
        )

        self.assertRedirects(response, reverse("integration_requests"))
        self.assertEqual(IntegrationRequest.objects.count(), 1)

    def test_integration_status_wizard_updates_status(self):
        self.client.login(username="economist", password="demo12345")
        self.client.post(
            reverse("integration_request_wizard"),
            {
                "integration_name": "Bank API",
                "target_system": "Bank",
                "description": "Автоматическая загрузка выписки",
            },
        )
        integration_request = IntegrationRequest.objects.get()
        self.client.login(username="admin", password="demo12345")

        response = self.client.get(reverse("integration_request_status_wizard", args=[integration_request.id]))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("integration_request_status_wizard", args=[integration_request.id]),
            {
                "status": IntegrationRequestStatus.IN_PROGRESS,
                "admin_comment": "Взято в работу",
            },
        )

        self.assertRedirects(response, reverse("integration_requests"))
        integration_request.refresh_from_db()
        self.assertEqual(integration_request.status, IntegrationRequestStatus.IN_PROGRESS)
        self.assertEqual(integration_request.admin_comment, "Взято в работу")
        self.assertEqual(integration_request.assigned_admin, self.admin)

    def test_external_payment_wizard_renders_for_accountant(self):
        self.client.login(username="accountant", password="demo12345")

        response = self.client.get(reverse("external_payment_wizard"))

        self.assertEqual(response.status_code, 200)

    def _approve_limit_for_article(self, article, amount):
        plan = create_budget_plan(
            author=self.economist,
            department=self.department,
            article=article,
            currency=self.currency,
            planning_year=2026,
            planning_horizon=1,
            annual_amount=amount,
            approver=self.manager,
            monthly_amounts=None,
            comment="limit",
        )
        submit_budget_plan(plan, self.economist)
        approve_budget_plan(plan, self.manager)
        return plan

    def _approved_plan(self, amount):
        return self._approve_limit_for_article(CashFlowArticle.objects.order_by("id").first(), amount)
