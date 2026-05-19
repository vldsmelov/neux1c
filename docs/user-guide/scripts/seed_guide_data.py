from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from core.integrations.one_c.mock import MockOneCProvider
from core.models import (
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    ContractKind,
    Currency,
    Department,
    IntegrationRequest,
    IntegrationRequestStatus,
    Organization,
    PaymentRequest,
    PaymentRequestKind,
    PaymentRequestStatus,
)
from core.services.one_c_sync import sync_one_c_dataset


def ensure_months(plan: BudgetLimitPlan, monthly_amount: Decimal) -> None:
    for month in range(1, 13):
        BudgetLimitMonth.objects.update_or_create(
            plan=plan,
            month=month,
            defaults={"amount": monthly_amount},
        )


def main():
    call_command("setup_access_roles", "--with-users", verbosity=0)
    sync_one_c_dataset(MockOneCProvider())

    User = get_user_model()
    economist = User.objects.get(username="economist")
    manager = User.objects.get(username="manager")
    admin = User.objects.get(username="admin")

    organization = Organization.objects.first()
    article = CashFlowArticle.objects.filter(exists_in_one_c=True).order_by("code").first()
    contract = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).order_by("date", "number").first()
    currency = Currency.objects.get(code="RUB")
    department = Department.objects.first()

    if not all([organization, article, contract, currency, department]):
        raise RuntimeError("Not enough reference data for guide seeding")

    draft_plan, _ = BudgetLimitPlan.objects.update_or_create(
        number="GUIDE-PLN-DRAFT",
        defaults={
            "document_date": timezone.localdate(),
            "planning_year": timezone.localdate().year,
            "planning_horizon": 1,
            "department": department,
            "article": article,
            "currency": currency,
            "annual_amount": Decimal("1200000.00"),
            "comment": "Guide draft plan",
            "status": BudgetPlanStatus.DRAFT,
            "approver": manager,
            "approved_at": None,
            "version": 1,
            "correction_reason": "",
            "author": economist,
        },
    )
    ensure_months(draft_plan, Decimal("100000.00"))

    pending_plan, _ = BudgetLimitPlan.objects.update_or_create(
        number="GUIDE-PLN-PENDING",
        defaults={
            "document_date": timezone.localdate(),
            "planning_year": timezone.localdate().year,
            "planning_horizon": 1,
            "department": department,
            "article": article,
            "currency": currency,
            "annual_amount": Decimal("1200000.00"),
            "comment": "Guide pending approval plan",
            "status": BudgetPlanStatus.PENDING_APPROVAL,
            "approver": manager,
            "approved_at": None,
            "version": 1,
            "correction_reason": "",
            "author": economist,
        },
    )
    ensure_months(pending_plan, Decimal("100000.00"))

    PaymentRequest.objects.update_or_create(
        number="GUIDE-REQ-DRAFT",
        defaults={
            "request_kind": PaymentRequestKind.BY_CONTRACT,
            "request_date": timezone.localdate(),
            "organization": organization,
            "article": article,
            "counterparty": contract.counterparty,
            "contract": contract,
            "invoice_number": "",
            "invoice_date": None,
            "payment_purpose": "Guide draft payment request",
            "currency": currency,
            "amount": Decimal("50000.00"),
            "manual_exchange_rate": Decimal("1.0000"),
            "amount_rub": Decimal("50000.00"),
            "limit_remaining_before_rub": Decimal("1000000.00"),
            "limit_remaining_after_rub": Decimal("950000.00"),
            "limit_exceeded": False,
            "status": PaymentRequestStatus.DRAFT,
            "approver": manager,
            "approver_comment": "",
            "rejected_at": None,
            "approved_at": None,
            "transferred_at": None,
            "do_external_id": "",
            "author": economist,
            "justification_text": "",
            "comment": "Guide draft",
        },
    )

    PaymentRequest.objects.update_or_create(
        number="GUIDE-REQ-PENDING",
        defaults={
            "request_kind": PaymentRequestKind.BY_CONTRACT,
            "request_date": timezone.localdate(),
            "organization": organization,
            "article": article,
            "counterparty": contract.counterparty,
            "contract": contract,
            "invoice_number": "",
            "invoice_date": None,
            "payment_purpose": "Guide pending payment request",
            "currency": currency,
            "amount": Decimal("75000.00"),
            "manual_exchange_rate": Decimal("1.0000"),
            "amount_rub": Decimal("75000.00"),
            "limit_remaining_before_rub": Decimal("900000.00"),
            "limit_remaining_after_rub": Decimal("825000.00"),
            "limit_exceeded": False,
            "status": PaymentRequestStatus.PENDING_APPROVAL,
            "approver": manager,
            "approver_comment": "",
            "rejected_at": None,
            "approved_at": None,
            "transferred_at": None,
            "do_external_id": "",
            "author": economist,
            "justification_text": "",
            "comment": "Guide pending",
        },
    )

    IntegrationRequest.objects.update_or_create(
        integration_name="Guide ERP integration",
        defaults={
            "target_system": "1C ERP",
            "description": "Guide scenario for integration request flow",
            "status": IntegrationRequestStatus.NEW,
            "requested_by": economist,
            "assigned_admin": admin,
            "admin_comment": "",
        },
    )

    print("Guide data seeded successfully.")


main()
