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
    ReportTemplate,
    ReportTemplateType,
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
            payment_purpose="Оплата поставки",
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
        rows = [item for item in response.context["rows"] if item["article"].code == "DDS-010"]
        self.assertGreaterEqual(len(rows), 1)

        self.assertEqual(sum(row["plan"] for row in rows), Decimal("1200000.00"))
        self.assertEqual(sum(row["adjustments"] for row in rows), Decimal("200000.00"))
        self.assertEqual(sum(row["reserved"] for row in rows), Decimal("3860000.00"))
        self.assertEqual(sum(row["requested"] for row in rows), Decimal("100000.00"))
        self.assertEqual(sum(row["fact_bu"] for row in rows), Decimal("800000.00"))
        self.assertEqual(sum(row["fact_nu"] for row in rows), Decimal("760000.00"))
        self.assertEqual(sum(row["balance_bu"] for row in rows), Decimal("-3360000.00"))
        self.assertTrue(any(row["has_limit_overrun"] for row in rows))
        self.assertTrue(any("Тестовая заявка" in row["comments"] for row in rows))
        self.assertTrue(any("Оплата поставки" in row["comments"] for row in rows))

    def test_plan_fact_report_detailed_filters(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(
            reverse("plan_fact_report"),
            {
                "year": 2026,
                "organization_id": self.organization.id,
                "counterparty_id": self.counterparty_supplier_1.id,
                "supplier_contract_id": self.contract_supplier_1.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            if row["organization"] is not None:
                self.assertEqual(row["organization"].id, self.organization.id)
            if row["counterparty"] is not None:
                self.assertEqual(row["counterparty"].id, self.counterparty_supplier_1.id)
            if row["supplier_contract"] is not None:
                self.assertEqual(row["supplier_contract"].id, self.contract_supplier_1.id)

    def test_plan_fact_report_export_csv(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("plan_fact_report"), {"year": 2026, "export": "excel"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("plan_fact_2026.csv", response["Content-Disposition"])
        payload = response.content.decode("utf-8-sig")
        self.assertIn("Организация", payload)
        self.assertIn("Контрагент", payload)
        self.assertIn("Комментарии", payload)
        self.assertIn("DDS-010", payload)
        self.assertIn("Тестовая заявка", payload)

    def test_economist_can_save_apply_and_delete_template(self):
        self.client.login(username="economist", password="demo12345")

        save_response = self.client.post(
            reverse("plan_fact_report"),
            {
                "action": "save_template",
                "template_name": "Операционные расходы",
                "is_default": "1",
                "year": "2026",
                "article_id": str(self.article_ops.id),
                "organization_id": str(self.organization.id),
                "counterparty_id": str(self.counterparty_supplier_1.id),
                "customer_contract_id": "",
                "supplier_contract_id": str(self.contract_supplier_1.id),
                "request_status": PaymentRequestStatus.APPROVED,
            },
        )
        self.assertEqual(save_response.status_code, 302)
        template = ReportTemplate.objects.get(
            owner=self.economist,
            report_type=ReportTemplateType.PLAN_FACT,
            name="Операционные расходы",
        )
        self.assertTrue(template.is_default)
        self.assertEqual(template.filters["year"], 2026)
        self.assertEqual(template.filters["article_id"], self.article_ops.id)

        apply_response = self.client.post(
            reverse("plan_fact_report"),
            {"action": "apply_template", "template_id": template.id},
        )
        self.assertEqual(apply_response.status_code, 302)
        self.assertIn("template_id=", apply_response["Location"])
        self.assertIn("article_id=", apply_response["Location"])

        delete_response = self.client.post(
            reverse("plan_fact_report"),
            {"action": "delete_template", "template_id": template.id},
        )
        self.assertRedirects(delete_response, reverse("plan_fact_report"))
        self.assertFalse(
            ReportTemplate.objects.filter(
                owner=self.economist,
                report_type=ReportTemplateType.PLAN_FACT,
                name="Операционные расходы",
            ).exists()
        )

    def test_plan_fact_filters_by_planning_scenario(self):
        from core.models import PlanningScenario, PlanningScenarioKind

        baseline = PlanningScenario.objects.create(
            name="Базовый",
            year=2026,
            organization=self.organization,
            kind=PlanningScenarioKind.BASE,
            is_baseline=True,
        )
        optimistic = PlanningScenario.objects.create(
            name="Оптимистичный",
            year=2026,
            organization=self.organization,
            kind=PlanningScenarioKind.OPTIMISTIC,
        )
        # Привяжем существующий план к базовому, добавим вторую план под оптимистичный
        self.plan.organization = self.organization
        self.plan.scenario = baseline
        self.plan.save(update_fields=["organization", "scenario"])
        BudgetLimitPlan.objects.create(
            number="PLN-2026-900002",
            document_date=date(2026, 1, 11),
            organization=self.organization,
            scenario=optimistic,
            planning_year=2026,
            planning_horizon=1,
            department=self.department,
            article=self.article_ops,
            currency=self.currency_rub,
            annual_amount=Decimal("5000000.00"),
            status=BudgetPlanStatus.APPROVED,
            approver=self.manager,
            approved_at=timezone.now(),
            author=self.economist,
        )

        self.client.login(username="economist", password="demo12345")

        baseline_resp = self.client.get(
            reverse("plan_fact_report"),
            {"year": 2026, "organization_id": self.organization.id, "scenario_id": baseline.id},
        )
        baseline_plan_total = sum(
            row["plan"] for row in baseline_resp.context["rows"] if row["article"].code == "DDS-010"
        )
        self.assertEqual(baseline_plan_total, Decimal("1200000.00"))

        optimistic_resp = self.client.get(
            reverse("plan_fact_report"),
            {"year": 2026, "organization_id": self.organization.id, "scenario_id": optimistic.id},
        )
        optimistic_plan_total = sum(
            row["plan"] for row in optimistic_resp.context["rows"] if row["article"].code == "DDS-010"
        )
        self.assertEqual(optimistic_plan_total, Decimal("5000000.00"))

    def test_economist_can_create_planning_scenario(self):
        from core.models import PlanningScenario

        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("planning_scenarios"),
            {
                "action": "create",
                "name": "Пессимистичный 2026",
                "year": 2026,
                "organization_id": self.organization.id,
                "kind": "pessimistic",
                "is_baseline": "1",
                "comment": "Снижение оборотов на 20%",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        scenario = PlanningScenario.objects.get(name="Пессимистичный 2026")
        self.assertTrue(scenario.is_baseline)
        self.assertEqual(scenario.kind, "pessimistic")
        self.assertEqual(scenario.organization_id, self.organization.id)

    def test_manager_cannot_save_template(self):
        self.client.login(username="manager", password="demo12345")

        response = self.client.post(
            reverse("plan_fact_report"),
            {
                "action": "save_template",
                "template_name": "Недоступный шаблон",
                "year": "2026",
            },
        )

        self.assertRedirects(response, reverse("plan_fact_report"))
        self.assertFalse(
            ReportTemplate.objects.filter(
                owner=self.manager,
                report_type=ReportTemplateType.PLAN_FACT,
                name="Недоступный шаблон",
            ).exists()
        )
