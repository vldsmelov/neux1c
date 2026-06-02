"""Тесты N-stage approval chains."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import ApprovalChain, Organization
from core.services.approval_chains import (
    find_applicable_chain,
    first_stage,
    next_stage_after,
    parse_stages,
)


class ApprovalChainsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.org = Organization.objects.create(name="Chain Org", source_system="manual")
        self.manager = User.objects.create_user(username="manager_ac", password="x")
        self.cfo = User.objects.create_user(username="cfo_ac", password="x")
        self.ceo = User.objects.create_user(username="ceo_ac", password="x")

    def _make_chain(self, name, min_amt, max_amt, usernames, **kw):
        stages = [
            {"order": i + 1, "approver_username": u, "comment": ""}
            for i, u in enumerate(usernames)
        ]
        return ApprovalChain.objects.create(
            name=name,
            organization=self.org,
            applies_to_kind=ApprovalChain.APPLIES_PAYMENT_REQUEST,
            min_amount_rub=Decimal(min_amt),
            max_amount_rub=Decimal(max_amt) if max_amt is not None else None,
            stages=stages,
            **kw,
        )

    def test_find_applicable_chain_by_amount(self):
        # Маленькие платежи (0-100k): только manager
        self._make_chain("Малые", 0, 100000, ["manager_ac"])
        # Средние (100k-1M): manager → cfo
        mid_chain = self._make_chain("Средние", 100000, 1000000, ["manager_ac", "cfo_ac"])
        # Большие (1M+): manager → cfo → ceo
        big_chain = self._make_chain("Большие", 1000000, None, ["manager_ac", "cfo_ac", "ceo_ac"])

        # 50k → малая
        small = find_applicable_chain(
            organization=self.org,
            applies_to_kind=ApprovalChain.APPLIES_PAYMENT_REQUEST,
            amount_rub=Decimal("50000"),
        )
        self.assertEqual(small.name, "Малые")

        # 500k → средняя
        mid = find_applicable_chain(
            organization=self.org,
            applies_to_kind=ApprovalChain.APPLIES_PAYMENT_REQUEST,
            amount_rub=Decimal("500000"),
        )
        self.assertEqual(mid, mid_chain)

        # 5M → большая
        big = find_applicable_chain(
            organization=self.org,
            applies_to_kind=ApprovalChain.APPLIES_PAYMENT_REQUEST,
            amount_rub=Decimal("5000000"),
        )
        self.assertEqual(big, big_chain)

    def test_find_returns_none_when_no_chain(self):
        result = find_applicable_chain(
            organization=self.org,
            applies_to_kind=ApprovalChain.APPLIES_PAYMENT_REQUEST,
            amount_rub=Decimal("100"),
        )
        self.assertIsNone(result)

    def test_inactive_chain_not_returned(self):
        self._make_chain("Inactive", 0, None, ["manager_ac"], is_active=False)
        result = find_applicable_chain(
            organization=self.org,
            applies_to_kind=ApprovalChain.APPLIES_PAYMENT_REQUEST,
            amount_rub=Decimal("100"),
        )
        self.assertIsNone(result)

    def test_parse_stages_resolves_users(self):
        chain = self._make_chain("Test", 0, None, ["manager_ac", "cfo_ac", "ceo_ac"])
        stages = parse_stages(chain)
        self.assertEqual(len(stages), 3)
        self.assertEqual(stages[0].approver, self.manager)
        self.assertEqual(stages[1].approver, self.cfo)
        self.assertEqual(stages[2].approver, self.ceo)

    def test_next_stage_after_advances(self):
        chain = self._make_chain("Test", 0, None, ["manager_ac", "cfo_ac", "ceo_ac"])
        first = first_stage(chain)
        self.assertEqual(first.order, 1)
        self.assertEqual(first.approver, self.manager)

        second = next_stage_after(chain, current_order=1)
        self.assertEqual(second.order, 2)
        self.assertEqual(second.approver, self.cfo)

        third = next_stage_after(chain, current_order=2)
        self.assertEqual(third.order, 3)
        self.assertEqual(third.approver, self.ceo)

        # После последнего — None
        none_stage = next_stage_after(chain, current_order=3)
        self.assertIsNone(none_stage)
