from decimal import Decimal

from django.test import TestCase

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    CashFlowArticle,
    Contract,
    ContractKind,
    PaymentFact,
)
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


