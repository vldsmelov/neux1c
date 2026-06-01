"""Тесты кейсов финансирования."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    AccountingKind,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    FundingCase,
    FundingCaseStatus,
    Organization,
    PaymentDirection,
    PaymentFact,
)
from core.services.funding_cases import build_case_overview
from core.services.one_c_sync import sync_one_c_dataset


class FundingCaseTests(TestCase):
    def setUp(self):
        call_command("setup_access_roles", "--with-users", verbosity=0)
        sync_one_c_dataset(MockOneCProvider())
        User = get_user_model()
        self.economist = User.objects.get(username="economist")
        self.organization = Organization.objects.get(name='ООО "Гладиолус"')
        self.mother = Counterparty.objects.create(name='АО "Материнская"', source_system="manual")
        self.supplier = Counterparty.objects.create(name='ООО "Поставщик стройматериалов"', source_system="manual")
        self.lender = Counterparty.objects.create(name='ООО "Кредитор"', source_system="manual")
        self.rub = Currency.objects.get(code="RUB")
        self.article_in = CashFlowArticle.objects.create(code="DDS-IN-CASE", name="Поступление по ТЗ", direction="inflow")
        self.article_out = CashFlowArticle.objects.create(code="DDS-OUT-CASE", name="Закупка стройматериалов", direction="outflow")
        self.article_loan_in = CashFlowArticle.objects.create(code="DDS-LOAN-IN", name="Поступление займа", direction="inflow")

    def test_case_overview_handles_user_scenario(self):
        """Воспроизводим именно тот кейс что описал пользователь:
        - доходный договор от мамы на 2 000 000
        - расход поставщику 2 500 000
        - заём 500 000
        - возврат от мамы 500 000 покрывает заём
        """
        case = FundingCase.objects.create(
            code="FUND-001",
            name="Закупка стройматериалов по ТЗ материнской",
            organization=self.organization,
            owner=self.economist,
        )
        mother_contract = Contract.objects.create(
            number="CUST-001", date=date(2026, 1, 10),
            name="Доходный договор от мамы", kind=ContractKind.CUSTOMER,
            counterparty=self.mother, currency=self.rub,
            amount=Decimal("2000000.00"), funding_case=case,
        )
        supplier_contract = Contract.objects.create(
            number="SUP-001", date=date(2026, 1, 15),
            name="Поставщик стройматериалов", kind=ContractKind.SOLE_SUPPLIER,
            counterparty=self.supplier, currency=self.rub,
            amount=Decimal("2500000.00"), funding_case=case,
        )
        loan_contract = Contract.objects.create(
            number="LOAN-001", date=date(2026, 1, 20),
            name="Заём на покрытие дефицита", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("500000.00"), interest_rate=Decimal("12.000"),
            maturity_date=date(2026, 6, 30), funding_case=case,
        )
        # Сначала только доход 2M от мамы и заём 500k
        PaymentFact.objects.create(
            external_id="case-test-1",
            date=date(2026, 1, 12), organization=self.organization,
            article=self.article_in, counterparty=self.mother, contract=mother_contract,
            amount=Decimal("2000000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.INFLOW,
            account="51",
        )
        PaymentFact.objects.create(
            external_id="case-test-2",
            date=date(2026, 1, 21), organization=self.organization,
            article=self.article_loan_in, counterparty=self.lender, contract=loan_contract,
            amount=Decimal("500000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.INFLOW,
            account="51",
        )
        # Оплата поставщику
        PaymentFact.objects.create(
            external_id="case-test-3",
            date=date(2026, 1, 25), organization=self.organization,
            article=self.article_out, counterparty=self.supplier, contract=supplier_contract,
            amount=Decimal("2500000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.OUTFLOW,
            account="51",
        )

        overview = build_case_overview(case)
        # Пришло всего: 2M + 500k = 2.5M
        self.assertEqual(overview["total_inflow"], Decimal("2500000.00"))
        # Ушло: 2.5M
        self.assertEqual(overview["total_outflow"], Decimal("2500000.00"))
        # Доступно сейчас: 0
        self.assertEqual(overview["available_now"], Decimal("0.00"))
        # По доходному договору мама ничего больше не должна — весь приход уже был
        # Но! Нужно ещё 500k + проценты для возврата займа
        # expected_outflow_remaining = 0 (поставщику доплатили) + 500k + 60k проценты (12% от 500k) = 560k
        self.assertGreater(overview["expected_outflow_remaining"], Decimal("500000.00"))
        # Прогноз сальдо: 0 + 0 (по приходу) − 560k = −560k → дефицит
        self.assertLess(overview["projected_balance"], Decimal("0.00"))
        self.assertEqual(overview["status_class"], "danger")

    def test_economist_can_create_funding_case_via_ui(self):
        self.client.login(username="economist", password="demo12345")
        # Установим working organization в сессии чтобы view принял дефолт
        session = self.client.session
        session["working_organization_id"] = self.organization.id
        session.save()
        response = self.client.post(
            reverse("funding_cases_index"),
            {
                "action": "create",
                "code": "FUND-DEMO-1",
                "name": "Тестовый кейс",
                "organization_id": self.organization.id,
                "description": "Описание",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(FundingCase.objects.filter(code="FUND-DEMO-1").exists())

    def test_loan_repayment_fact_reduces_loan_balance(self):
        """Если факт-возврат привязан к договору займа, остаток к возврату по
        этому займу уменьшается на сумму погашенного тела."""
        case = FundingCase.objects.create(
            code="FUND-LOAN-1",
            name="Покрытие дефицита займом",
            organization=self.organization,
        )
        loan = Contract.objects.create(
            number="LOAN-T1", date=date(2026, 1, 1),
            name="Заём", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("500000.00"), interest_rate=Decimal("12.000"),
            maturity_date=date(2026, 6, 30), funding_case=case,
        )

        # Возврат тела займа 300k + проценты 30k
        article_repay = CashFlowArticle.objects.create(code="DDS-LOAN-OUT", name="Возврат займа", direction="outflow")
        PaymentFact.objects.create(
            external_id="loan-repay-1",
            date=date(2026, 5, 10), organization=self.organization,
            article=article_repay, counterparty=self.lender,
            loan_repayment_contract=loan,
            loan_interest_portion=Decimal("30000.00"),
            amount=Decimal("330000.00"), currency=self.rub,
            accounting_kind=AccountingKind.BU, direction=PaymentDirection.OUTFLOW,
            account="51",
        )

        overview = build_case_overview(case)
        loan_stat = overview["contracts_by_kind"][ContractKind.LOAN_RECEIVED][0]
        # Тело погашено на 300k → осталось 200k
        self.assertEqual(loan_stat.principal_repaid, Decimal("300000.00"))
        # Проценты заплатили 30k из расчётных 60k → осталось 30k
        self.assertEqual(loan_stat.interest_paid, Decimal("30000.00"))
        # expected_remaining = 200k тело + 30k проценты = 230k
        self.assertEqual(loan_stat.expected_remaining, Decimal("230000.00"))

    def test_counterparty_saldo_collects_per_partner_balance(self):
        case = FundingCase.objects.create(
            code="FUND-SALDO-1", name="Сальдо тест", organization=self.organization,
        )
        Contract.objects.create(
            number="MOM-001", date=date(2026, 1, 10), kind=ContractKind.CUSTOMER,
            name="От мамы", counterparty=self.mother, currency=self.rub,
            amount=Decimal("2000000.00"), funding_case=case,
        )
        Contract.objects.create(
            number="SUP-001", date=date(2026, 1, 15), kind=ContractKind.SOLE_SUPPLIER,
            name="Поставщику", counterparty=self.supplier, currency=self.rub,
            amount=Decimal("1500000.00"), funding_case=case,
        )
        overview = build_case_overview(case)
        # Должно быть два контрагента в сальдо
        buckets = {b["counterparty"].id: b for b in overview["counterparty_saldo"]}
        self.assertEqual(buckets[self.mother.id]["owed_to_us"], Decimal("2000000.00"))
        self.assertEqual(buckets[self.supplier.id]["we_owe"], Decimal("1500000.00"))
        self.assertGreater(buckets[self.mother.id]["net"], Decimal("0"))
        self.assertLess(buckets[self.supplier.id]["net"], Decimal("0"))

    def test_record_loan_repayment_quick_action_creates_fact(self):
        case = FundingCase.objects.create(
            code="FUND-QA-1", name="Quick action", organization=self.organization,
        )
        loan = Contract.objects.create(
            number="LOAN-QA", date=date(2026, 1, 1),
            name="Заём для QA", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("500000.00"), interest_rate=Decimal("12.000"),
            funding_case=case,
        )
        article = CashFlowArticle.objects.create(
            code="DDS-LOAN-OUT-QA", name="Возврат займа QA", direction="outflow",
        )

        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("funding_case_detail", args=[case.id]),
            {
                "action": "record_loan_repayment",
                "loan_contract_id": loan.id,
                "article_id": article.id,
                "date": "2026-04-10",
                "amount": "330000.00",
                "interest_portion": "30000.00",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("Записан возврат займа" in m for m in msgs), msgs)
        # Должен появиться PaymentFact с правильной привязкой
        fact = PaymentFact.objects.get(loan_repayment_contract=loan)
        self.assertEqual(fact.amount, Decimal("330000.00"))
        self.assertEqual(fact.loan_interest_portion, Decimal("30000.00"))
        self.assertEqual(fact.direction, PaymentDirection.OUTFLOW)
        self.assertEqual(fact.contract_id, loan.id)
        # И сальдо кейса должно обновиться: остаток = 500k - 300k тело + 60k - 30k проценты = 230k
        overview = build_case_overview(case)
        loan_stat = overview["contracts_by_kind"][ContractKind.LOAN_RECEIVED][0]
        self.assertEqual(loan_stat.expected_remaining, Decimal("230000.00"))

    def test_record_inflow_quick_action_for_customer_contract(self):
        case = FundingCase.objects.create(
            code="FUND-INF-1", name="Поступление от мамы", organization=self.organization,
        )
        customer = Contract.objects.create(
            number="CUST-INF", date=date(2026, 1, 10), kind=ContractKind.CUSTOMER,
            name="Доходный", counterparty=self.mother, currency=self.rub,
            amount=Decimal("500000.00"), funding_case=case,
        )
        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("funding_case_detail", args=[case.id]),
            {
                "action": "record_inflow",
                "contract_id": customer.id,
                "article_id": self.article_in.id,
                "date": "2026-04-15",
                "amount": "500000.00",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        fact = PaymentFact.objects.get(contract=customer, direction=PaymentDirection.INFLOW)
        self.assertEqual(fact.amount, Decimal("500000.00"))
        # Сальдо: пришло 500k, expected_remaining по доходному = 0
        overview = build_case_overview(case)
        cust_stat = overview["contracts_by_kind"][ContractKind.CUSTOMER][0]
        self.assertEqual(cust_stat.expected_remaining, Decimal("0.00"))
        # Кейс должен быть профицитным — рекомендация «закрыть»
        self.assertEqual(overview["suggested_status"], "closed")

    def test_record_inflow_rejects_supplier_contract(self):
        case = FundingCase.objects.create(
            code="FUND-INF-NO", name="Test reject", organization=self.organization,
        )
        sup = Contract.objects.create(
            number="SUP-NO", date=date(2026, 1, 10), kind=ContractKind.SOLE_SUPPLIER,
            name="Поставщик", counterparty=self.supplier, currency=self.rub,
            amount=Decimal("100000.00"), funding_case=case,
        )
        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("funding_case_detail", args=[case.id]),
            {
                "action": "record_inflow",
                "contract_id": sup.id,
                "article_id": self.article_in.id,
                "date": "2026-04-15",
                "amount": "100000.00",
            },
            follow=True,
        )
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("доходному" in m.lower() for m in msgs), msgs)

    def test_record_loan_repayment_rejects_invalid_interest(self):
        case = FundingCase.objects.create(
            code="FUND-QA-2", name="QA invalid", organization=self.organization,
        )
        loan = Contract.objects.create(
            number="LOAN-QA-2", date=date(2026, 1, 1),
            name="Заём", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("100000.00"), funding_case=case,
        )
        article = CashFlowArticle.objects.create(
            code="DDS-LOAN-OUT-QA2", name="Возврат QA2", direction="outflow",
        )
        self.client.login(username="economist", password="demo12345")
        response = self.client.post(
            reverse("funding_case_detail", args=[case.id]),
            {
                "action": "record_loan_repayment",
                "loan_contract_id": loan.id,
                "article_id": article.id,
                "date": "2026-04-10",
                "amount": "50000.00",
                "interest_portion": "60000.00",  # больше суммы — должно отклониться
            },
            follow=True,
        )
        msgs = [m.message for m in response.context["messages"]]
        self.assertTrue(any("процент" in m.lower() for m in msgs), msgs)
        # Факта быть не должно
        self.assertFalse(PaymentFact.objects.filter(loan_repayment_contract=loan).exists())

    def test_annuity_schedule_generates_correct_amortization(self):
        from datetime import date as date_cls
        from core.services.loan_schedule import generate_annuity_schedule

        case = FundingCase.objects.create(
            code="FUND-SCHED-1", name="Annuity test", organization=self.organization,
        )
        loan = Contract.objects.create(
            number="LOAN-SCHED", date=date_cls(2026, 1, 1),
            name="Заём аннуитет", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("100000.00"), interest_rate=Decimal("12.000"),
            maturity_date=date_cls(2026, 12, 31), funding_case=case,
        )
        rows = generate_annuity_schedule(loan, periods=12)
        self.assertEqual(len(rows), 12)
        # Сумма всех тел должна равняться principal (с точностью округления)
        total_principal = sum((r.principal_due for r in rows), Decimal("0"))
        self.assertEqual(total_principal, Decimal("100000.00"))
        # Остаток после последнего периода = 0
        self.assertEqual(rows[-1].balance_after, Decimal("0.00"))
        # На первом периоде проценты должны быть > чем на последнем
        self.assertGreater(rows[0].interest_due, rows[-1].interest_due)
        # Каждая строка не оплачена
        self.assertFalse(any(r.is_paid for r in rows))

    def test_repayment_marks_schedule_lines_paid_in_order(self):
        from datetime import date as date_cls
        from core.services.loan_schedule import apply_repayment_to_schedule, generate_annuity_schedule

        case = FundingCase.objects.create(
            code="FUND-PAY", name="Pay test", organization=self.organization,
        )
        loan = Contract.objects.create(
            number="LOAN-PAY", date=date_cls(2026, 1, 1),
            name="Заём", kind=ContractKind.LOAN_RECEIVED,
            counterparty=self.lender, currency=self.rub,
            amount=Decimal("100000.00"), interest_rate=Decimal("0.000"),
            maturity_date=date_cls(2026, 12, 31), funding_case=case,
        )
        generate_annuity_schedule(loan, periods=10)  # 10×10000 без процентов
        # Возврат 30k → должен закрыть 3 строки
        article = CashFlowArticle.objects.create(
            code="DDS-LOAN-OUT-PAY", name="Возврат займа PAY", direction="outflow",
        )
        fact = PaymentFact.objects.create(
            external_id="loan-sched-pay-1",
            date=date_cls(2026, 4, 1), organization=self.organization,
            article=article, counterparty=self.lender, contract=loan,
            loan_repayment_contract=loan, amount=Decimal("30000.00"),
            currency=self.rub, accounting_kind=AccountingKind.BU,
            direction=PaymentDirection.OUTFLOW, account="51",
        )
        touched = apply_repayment_to_schedule(fact)
        self.assertEqual(touched, 3)
        lines = list(loan.schedule_lines.order_by("period"))
        self.assertTrue(lines[0].is_paid)
        self.assertTrue(lines[1].is_paid)
        self.assertTrue(lines[2].is_paid)
        self.assertFalse(lines[3].is_paid)

    def test_case_detail_renders_with_attachable_contracts(self):
        case = FundingCase.objects.create(
            code="FUND-002", name="Пустой кейс",
            organization=self.organization, status=FundingCaseStatus.ACTIVE,
        )
        self.client.login(username="economist", password="demo12345")
        response = self.client.get(reverse("funding_case_detail", args=[case.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Пустой кейс")
        self.assertContains(response, "Привязать договор")
