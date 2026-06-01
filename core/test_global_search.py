"""Тесты универсального поиска."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.models import (
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    FundingCase,
    Organization,
)
from core.services.global_search import global_search


class GlobalSearchTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        User = get_user_model()
        self.user = User.objects.get(username="economist")
        self.org = Organization.objects.create(name="Тест Орг", source_system="manual")
        self.rub = Currency.objects.create(code="RUB", name="Рубль", source_system="manual")
        self.cp = Counterparty.objects.create(name="Уникальный Контрагент", inn="7777000077", source_system="manual")
        self.contract = Contract.objects.create(
            number="UNIQUE-2026-001", date=date(2026, 1, 1), kind=ContractKind.CUSTOMER,
            name="Уникальный договор", counterparty=self.cp, currency=self.rub, amount=Decimal("1000.00"),
        )
        self.case = FundingCase.objects.create(
            code="FUND-SEARCH-1", name="Уникальный кейс", organization=self.org,
        )

    def test_search_finds_contract_by_number(self):
        result = global_search("UNIQUE-2026")
        self.assertFalse(result["empty"])
        self.assertEqual(len(result["contracts"]), 1)
        self.assertEqual(result["contracts"][0].number, "UNIQUE-2026-001")

    def test_search_finds_counterparty_by_inn(self):
        result = global_search("7777000077")
        self.assertEqual(len(result["counterparties"]), 1)
        self.assertEqual(result["counterparties"][0].inn, "7777000077")

    def test_search_finds_funding_case_by_code(self):
        result = global_search("FUND-SEARCH")
        self.assertEqual(len(result["funding_cases"]), 1)
        self.assertEqual(result["funding_cases"][0].code, "FUND-SEARCH-1")

    def test_search_too_short_returns_empty(self):
        self.assertTrue(global_search("a")["empty"])
        self.assertTrue(global_search("")["empty"])

    def test_search_view_renders(self):
        self.client.login(username="economist", password="demo12345")
        response = self.client.get(reverse("global_search"), {"q": "Уникальный"})
        self.assertEqual(response.status_code, 200)
        # Шаблон рендерит номер договора, код кейса, имя контрагента
        self.assertContains(response, "UNIQUE-2026-001")
        self.assertContains(response, "FUND-SEARCH-1")
        self.assertContains(response, "Уникальный Контрагент")
