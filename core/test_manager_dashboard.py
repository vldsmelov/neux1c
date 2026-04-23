from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    Counterparty,
    Currency,
    Department,
    Organization,
    PaymentRequest,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.manager_dashboard import build_manager_dashboard
from core.services.one_c_sync import sync_one_c_dataset


class ManagerDashboardTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")

        self.article_ops = CashFlowArticle.objects.get(code="DDS-010")
        self.department = Department.objects.get(code="CFO-001")
        self.currency_rub = Currency.objects.get(code="RUB")
        self.organization = Organization.objects.get(name='ООО "Гладиолус"')
        self.counterparty_supplier_1 = Counterparty.objects.get(inn="7702000002")
        self.contract_supplier_1 = Contract.objects.get(number="ЕП-2026-014")

        plan = BudgetLimitPlan.objects.create(
            number="PLN-2026-900002",
            document_date=date(2026, 1, 10),
            planning_year=2026,
            planning_horizon=1,
            department=self.department,
            article=self.article_ops,
            currency=self.currency_rub,
            annual_amount=Decimal("1200000.00"),
            status=BudgetPlanStatus.APPROVED,
            approver=self.manager,
            approved_at=timezone.now(),
            author=self.economist,
        )
        BudgetLimitAdjustment.objects.create(
            number="ADJ-2026-900002",
            document_date=date(2026, 2, 12),
            base_plan=plan,
            article=self.article_ops,
            new_annual_amount=Decimal("1400000.00"),
            reason="Перераспределение лимита",
            version=2,
            status=BudgetPlanStatus.APPROVED,
            approver=self.manager,
            approved_at=timezone.now(),
            author=self.economist,
        )

        PaymentRequest.objects.create(
            number="REQ-2026-910001",
            request_kind=PaymentRequestKind.BY_CONTRACT,
            request_date=date(2026, 4, 20),
            organization=self.organization,
            article=self.article_ops,
            counterparty=self.counterparty_supplier_1,
            contract=self.contract_supplier_1,
            invoice_number="",
            currency=self.currency_rub,
            amount=Decimal("50000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            amount_rub=Decimal("50000.00"),
            limit_remaining_before_rub=Decimal("0.00"),
            limit_remaining_after_rub=Decimal("-50000.00"),
            limit_exceeded=True,
            status=PaymentRequestStatus.DRAFT,
            approver=self.manager,
            author=self.economist,
            comment="Черновик",
        )
        PaymentRequest.objects.create(
            number="REQ-2026-910002",
            request_kind=PaymentRequestKind.BY_CONTRACT,
            request_date=date(2026, 4, 20),
            organization=self.organization,
            article=self.article_ops,
            counterparty=self.counterparty_supplier_1,
            contract=self.contract_supplier_1,
            invoice_number="",
            currency=self.currency_rub,
            amount=Decimal("60000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            amount_rub=Decimal("60000.00"),
            limit_remaining_before_rub=Decimal("0.00"),
            limit_remaining_after_rub=Decimal("-60000.00"),
            limit_exceeded=True,
            status=PaymentRequestStatus.PENDING_APPROVAL,
            approver=self.manager,
            author=self.economist,
            comment="На согласовании",
        )
        PaymentRequest.objects.create(
            number="REQ-2026-910003",
            request_kind=PaymentRequestKind.BY_CONTRACT,
            request_date=date(2026, 4, 20),
            organization=self.organization,
            article=self.article_ops,
            counterparty=self.counterparty_supplier_1,
            contract=self.contract_supplier_1,
            invoice_number="",
            currency=self.currency_rub,
            amount=Decimal("70000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            amount_rub=Decimal("70000.00"),
            limit_remaining_before_rub=Decimal("0.00"),
            limit_remaining_after_rub=Decimal("-70000.00"),
            limit_exceeded=True,
            status=PaymentRequestStatus.APPROVED,
            approver=self.manager,
            approved_at=timezone.now(),
            author=self.economist,
            comment="Согласована",
        )

    def test_dashboard_payload_contains_overruns_and_statuses(self):
        payload = build_manager_dashboard(year=2026)

        self.assertGreaterEqual(payload["summary"]["overrun_articles"], 1)
        self.assertGreaterEqual(len(payload["top_overruns"]), 1)
        self.assertEqual(payload["top_overruns"][0]["article"].code, "DDS-010")

        statuses = {item["status"]: item["total"] for item in payload["status_rows"]}
        self.assertEqual(statuses[PaymentRequestStatus.DRAFT], 1)
        self.assertEqual(statuses[PaymentRequestStatus.PENDING_APPROVAL], 1)
        self.assertEqual(statuses[PaymentRequestStatus.APPROVED], 1)

    def test_manager_dashboard_view(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.get(reverse("manager_dashboard"), {"year": 2026})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_year"], 2026)
        self.assertIn("top_overruns", response.context)
