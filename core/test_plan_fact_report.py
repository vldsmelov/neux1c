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
from core.services.one_c_sync import sync_one_c_dataset


class PlanFactReportTests(TestCase):
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

        self.plan = BudgetLimitPlan.objects.create(
            number="PLN-2026-900001",
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
            number="ADJ-2026-900001",
            document_date=date(2026, 2, 12),
            base_plan=self.plan,
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
            number="REQ-2026-900001",
            request_kind=PaymentRequestKind.BY_CONTRACT,
            request_date=date(2026, 4, 20),
            organization=self.organization,
            article=self.article_ops,
            counterparty=self.counterparty_supplier_1,
            contract=self.contract_supplier_1,
            invoice_number="",
            currency=self.currency_rub,
            amount=Decimal("100000.00"),
            manual_exchange_rate=Decimal("1.0000"),
            amount_rub=Decimal("100000.00"),
            limit_remaining_before_rub=Decimal("0.00"),
            limit_remaining_after_rub=Decimal("-100000.00"),
            limit_exceeded=True,
            status=PaymentRequestStatus.APPROVED,
            approver=self.manager,
            approved_at=timezone.now(),
            author=self.economist,
            comment="Тестовая заявка",
        )

    def test_plan_fact_report_row_metrics(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("plan_fact_report"), {"year": 2026})

        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.context["rows"] if item["article"].code == "DDS-010")
        self.assertEqual(row["plan"], Decimal("1200000.00"))
        self.assertEqual(row["adjustments"], Decimal("200000.00"))
        self.assertEqual(row["reserved"], Decimal("3860000.00"))
        self.assertEqual(row["requested"], Decimal("100000.00"))
        self.assertEqual(row["fact_bu"], Decimal("800000.00"))
        self.assertEqual(row["fact_nu"], Decimal("760000.00"))
        self.assertEqual(row["balance_bu"], Decimal("-3360000.00"))
        self.assertTrue(row["has_limit_overrun"])

    def test_plan_fact_report_export_csv(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("plan_fact_report"), {"year": 2026, "export": "excel"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("plan_fact_2026.csv", response["Content-Disposition"])
        payload = response.content.decode("utf-8-sig")
        self.assertIn("Статья ДДС", payload)
        self.assertIn("DDS-010", payload)
