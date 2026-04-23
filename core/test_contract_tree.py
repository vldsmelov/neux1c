from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import Counterparty, UserRole
from core.services.contract_tree import ContractTreeFilters, build_contract_tree
from core.services.one_c_sync import sync_one_c_dataset


class ContractTreeTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.manager = User.objects.get(username="manager")
        self.accountant = User.objects.get(username="accountant")

    def test_tree_contains_customer_supplier_and_agreements(self):
        rows, summary = build_contract_tree(ContractTreeFilters())

        self.assertTrue(any(row["row_type"] == "customer" for row in rows))
        self.assertTrue(any(row["row_type"] == "supplier" for row in rows))
        self.assertTrue(any(row["row_type"] == "agreement" for row in rows))
        supplier_row = next(row for row in rows if row["row_type"] == "supplier" and "ЕП-2026-014" in row["display_name"])
        self.assertEqual(supplier_row["reserved_native"], Decimal("3860000.00"))
        self.assertEqual(supplier_row["paid_native"], Decimal("800000.00"))
        self.assertEqual(supplier_row["remaining_native"], Decimal("3060000.00"))
        self.assertEqual(summary["suppliers"], 3)
        self.assertEqual(summary["agreements"], 3)

    def test_tree_filters_by_contract_number(self):
        rows, summary = build_contract_tree(ContractTreeFilters(contract_query="022"))

        supplier_rows = [row for row in rows if row["row_type"] == "supplier"]
        self.assertEqual(len(supplier_rows), 1)
        self.assertIn("ЕП-2026-022", supplier_rows[0]["display_name"])
        self.assertEqual(summary["suppliers"], 1)

    def test_tree_filters_by_counterparty(self):
        counterparty = Counterparty.objects.get(name='ООО "СофтПлатформа"')
        rows, summary = build_contract_tree(ContractTreeFilters(counterparty_id=counterparty.id))

        supplier_rows = [row for row in rows if row["row_type"] == "supplier"]
        self.assertEqual(len(supplier_rows), 1)
        self.assertIn("ЕП-2026-031", supplier_rows[0]["display_name"])
        self.assertEqual(summary["suppliers"], 1)

    def test_tree_page_access_and_permissions(self):
        self.client.login(username="economist", password="demo12345")
        economist_response = self.client.get(reverse("contracts_tree"))
        self.assertEqual(economist_response.status_code, 200)
        self.assertContains(economist_response, "Дерево договоров")

        self.client.logout()
        self.client.login(username="manager", password="demo12345")
        manager_response = self.client.get(reverse("contracts_tree"))
        self.assertEqual(manager_response.status_code, 200)

        self.client.logout()
        self.client.login(username="accountant", password="demo12345")
        accountant_response = self.client.get(reverse("contracts_tree"))
        self.assertEqual(self.accountant.profile.role, UserRole.ACCOUNTANT)
        self.assertEqual(accountant_response.status_code, 403)
