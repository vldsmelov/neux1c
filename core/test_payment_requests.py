from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    CashFlowArticle,
    Contract,
    PaymentLimitControlMode,
    PaymentRequest,
    PaymentRequestControlSettings,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.budget_planning import approve_budget_plan, create_budget_plan, submit_budget_plan
from core.services.one_c_sync import sync_one_c_dataset
from core.services.payment_requests import create_payment_request, submit_payment_request


class PaymentRequestWorkflowTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.contract = Contract.objects.get(number="ЕП-2026-014")
        self.article = CashFlowArticle.objects.get(code="DDS-010")
        self._approve_limit_for_article(self.article, Decimal("2000000.00"))

    def test_payment_request_ui_workflow(self):
        from core.models import Currency, Organization

        self.client.login(username="economist", password="demo12345")
        create_response = self.client.post(
            reverse("payment_requests"),
            {
                "action": "create",
                "request_kind": PaymentRequestKind.BY_CONTRACT,
                "organization_id": Organization.objects.first().id,
                "article_id": self.article.id,
                "counterparty_id": self.contract.counterparty_id,
                "contract_id": self.contract.id,
                "invoice_number": "",
                "currency_id": Currency.objects.get(code="RUB").id,
                "amount": "100000.00",
                "manual_exchange_rate": "1.0000",
                "approver_id": self.manager.id,
                "comment": "Пилотная заявка",
            },
        )
        payment_request = PaymentRequest.objects.get()
        submit_response = self.client.post(
            reverse("payment_requests"),
            {"action": "submit", "request_id": payment_request.id},
        )

        self.assertRedirects(create_response, reverse("payment_requests"))
        self.assertRedirects(submit_response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.PENDING_APPROVAL)

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        approve_response = self.client.post(
            reverse("payment_requests"),
            {"action": "approve", "request_id": payment_request.id},
        )
        self.assertRedirects(approve_response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.APPROVED)

        self.client.logout()
        self.client.login(username="economist", password="demo12345")
        transfer_response = self.client.post(
            reverse("payment_requests"),
            {"action": "transfer", "request_id": payment_request.id},
        )
        self.assertRedirects(transfer_response, reverse("payment_requests"))
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.TRANSFERRED)
        self.assertTrue(payment_request.do_external_id.startswith("DO-"))

    def test_block_mode_rejects_exceeded_request(self):
        from core.models import Currency, Organization

        PaymentRequestControlSettings.objects.create(
            name="Block",
            control_mode=PaymentLimitControlMode.BLOCK,
            is_active=True,
        )

        with self.assertRaises(ValueError):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_CONTRACT,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=self.contract,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("5000000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
            )

    def test_warning_mode_allows_exceeded_request(self):
        from core.models import Currency, Organization

        PaymentRequestControlSettings.objects.create(
            name="Warn",
            control_mode=PaymentLimitControlMode.WARNING,
            is_active=True,
        )

        payment_request = create_payment_request(
            author=self.economist,
            request_kind=PaymentRequestKind.BY_CONTRACT,
            organization=Organization.objects.first(),
            article=self.article,
            counterparty=self.contract.counterparty,
            contract=self.contract,
            currency=Currency.objects.get(code="RUB"),
            amount=Decimal("5000000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            approver=self.manager,
        )
        self.assertTrue(payment_request.limit_exceeded)
        self.assertLess(payment_request.limit_remaining_after_rub, 0)
        submit_payment_request(payment_request, self.economist)
        payment_request.refresh_from_db()
        self.assertEqual(payment_request.status, PaymentRequestStatus.PENDING_APPROVAL)

    def test_invoice_request_requires_invoice_number(self):
        from core.models import Currency, Organization

        with self.assertRaises(ValueError):
            create_payment_request(
                author=self.economist,
                request_kind=PaymentRequestKind.BY_INVOICE,
                organization=Organization.objects.first(),
                article=self.article,
                counterparty=self.contract.counterparty,
                contract=None,
                currency=Currency.objects.get(code="RUB"),
                amount=Decimal("150000.00"),
                manual_exchange_rate=Decimal("1.0000"),
                approver=self.manager,
                invoice_number="",
            )

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
