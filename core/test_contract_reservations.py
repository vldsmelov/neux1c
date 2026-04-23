from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import AccountingKind, ContractKind, PaymentFact
from core.services.budget_planning import approve_budget_plan, create_budget_plan, submit_budget_plan
from core.services.contract_reservations import build_contract_reservation_rows, build_contract_reservation_summary
from core.services.one_c_sync import sync_one_c_dataset


class ContractReservationTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self._approve_limit_for_supplier_article()

    def test_contract_reservation_row_calculates_available_limit(self):
        rows = build_contract_reservation_rows()
        summary = build_contract_reservation_summary(rows)

        row = next(item for item in rows if item["contract"].number == "ЕП-2026-014")
        self.assertEqual(row["reserved_amount"], Decimal("3860000.00"))
        self.assertEqual(row["remaining_amount"], Decimal("3860000.00"))
        self.assertIsNotNone(row["approved_limit"])
        self.assertIsNotNone(row["available_limit_after_reserve"])
        self.assertLessEqual(row["available_limit_after_reserve"], row["approved_limit"])
        self.assertEqual(summary["contracts"], len(rows))

    def test_contract_reservations_page_is_available_for_economist(self):
        self.client.login(username="economist", password="demo12345")

        response = self.client.get(reverse("contracts_reservations"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Договоры и резервирование")
        self.assertTrue(response.context["rows"])

    def _approve_limit_for_supplier_article(self):
        from core.models import Currency, Department

        supplier_fact = (
            PaymentFact.objects.filter(contract__kind=ContractKind.SOLE_SUPPLIER, accounting_kind=AccountingKind.BU)
            .select_related("article")
            .first()
        )
        monthly_amounts = {month: Decimal("416666.67") for month in range(1, 12)}
        monthly_amounts[12] = Decimal("416666.63")
        plan = create_budget_plan(
            author=self.economist,
            department=Department.objects.first(),
            article=supplier_fact.article,
            currency=Currency.objects.get(code="RUB"),
            planning_year=2026,
            planning_horizon=1,
            annual_amount=Decimal("5000000.00"),
            approver=self.manager,
            monthly_amounts=monthly_amounts,
            comment="Лимит для договоров",
        )
        submit_budget_plan(plan, self.economist)
        approve_budget_plan(plan, self.manager)
