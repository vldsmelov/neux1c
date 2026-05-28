"""Support helpers for the payments views: CSV export, bulk-action dispatch,
and POST → service argument builders.

Kept out of payments.py so that module reads as a list of request handlers.
All functions here are called only from core.views.payments.
"""

import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from ..models import (
    AdditionalAgreement,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    Organization,
    PaymentRequest,
    PaymentRequestStatus,
    UserRole,
)
from ..services.payment_requests import (
    approve_payment_request,
    cancel_payment_request,
    create_payment_request,
    reject_payment_request,
    submit_payment_request,
    transfer_payment_request_to_do,
    update_payment_request,
)
from ._permissions import (
    can_approve_payment_requests,
    can_manage_payment_requests,
)
from ._shared import parse_decimal, parse_optional_date, working_organization


User = get_user_model()


# --- CSV export -----------------------------------------------------------

def _export_payment_requests_csv(queryset) -> HttpResponse:
    """Render the filtered payment-request queryset as a single-file CSV.

    Columns mirror what an analyst typically needs in Excel for ad-hoc
    reporting and don't try to be a complete dump of the model — we keep
    it human-readable.
    """
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "Номер", "Дата заявки", "Тип", "Организация", "Контрагент",
        "Договор", "Доп.соглашение", "Счёт", "Дата счёта",
        "Сумма", "Валюта", "Сумма в руб.",
        "Статус", "Согласующий", "Автор",
        "Отправлена", "Согласована", "Передана в 1С:ДО",
        "Отклонена", "Причина отклонения",
        "Отменена", "Причина отмены",
        "Превышение лимита",
    ])
    for pr in queryset.iterator():
        writer.writerow([
            pr.number,
            pr.request_date.strftime("%Y-%m-%d") if pr.request_date else "",
            pr.get_request_kind_display(),
            pr.organization.name if pr.organization_id else "",
            pr.counterparty.name if pr.counterparty_id else "",
            pr.contract.number if pr.contract_id else "",
            pr.additional_agreement.number if pr.additional_agreement_id else "",
            pr.invoice_number,
            pr.invoice_date.strftime("%Y-%m-%d") if pr.invoice_date else "",
            f"{pr.amount}",
            pr.currency.code if pr.currency_id else "",
            f"{pr.amount_rub}",
            pr.get_status_display(),
            pr.approver.username if pr.approver_id else "",
            pr.author.username if pr.author_id else "",
            pr.submitted_at.strftime("%Y-%m-%d %H:%M") if pr.submitted_at else "",
            pr.approved_at.strftime("%Y-%m-%d %H:%M") if pr.approved_at else "",
            pr.transferred_at.strftime("%Y-%m-%d %H:%M") if pr.transferred_at else "",
            pr.rejected_at.strftime("%Y-%m-%d %H:%M") if pr.rejected_at else "",
            pr.approver_comment.replace("\n", " ") if pr.approver_comment else "",
            pr.cancelled_at.strftime("%Y-%m-%d %H:%M") if pr.cancelled_at else "",
            pr.cancellation_reason,
            "да" if pr.limit_exceeded else "нет",
        ])
    return _csv_response(buffer, "payment_requests")


def _export_payment_facts_csv(queryset) -> HttpResponse:
    """Render the filtered payment-fact queryset as CSV (same Excel-friendly
    conventions as the requests export: ';' separator + UTF-8 BOM)."""
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "Дата", "Учёт", "Счёт", "Статья", "Контрагент", "Договор",
        "Сумма", "Валюта", "Источник", "Версия корректировки",
    ])
    for fact in queryset.iterator():
        latest_adjustment = fact.adjustments.order_by("-version").first()
        writer.writerow([
            fact.date.strftime("%Y-%m-%d") if fact.date else "",
            fact.get_accounting_kind_display(),
            fact.account,
            f"{fact.article.code} · {fact.article.name}" if fact.article_id else "",
            fact.counterparty.name if fact.counterparty_id else "",
            fact.contract.number if fact.contract_id else "",
            f"{fact.amount}",
            fact.currency.code if fact.currency_id else "",
            fact.get_source_system_display(),
            f"v{latest_adjustment.version}" if latest_adjustment else "база",
        ])
    return _csv_response(buffer, "payment_facts")


def _csv_response(buffer: StringIO, basename: str) -> HttpResponse:
    """Wrap a CSV buffer in an attachment response with a UTF-8 BOM so Excel
    renders Cyrillic correctly, and a date-stamped filename."""
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().strftime("%Y%m%d")
    response["Content-Disposition"] = f'attachment; filename="{basename}_{stamp}.csv"'
    return response


# --- Bulk handlers --------------------------------------------------------

def _bulk_handle(request, action: str) -> None:
    """Apply `action` to every selected PaymentRequest, accumulating messages.

    Each item is handled independently in its own transaction (via the
    service helpers, which are @transaction.atomic). Per-item failures don't
    break the batch — they accumulate as messages.error so the user sees
    exactly which numbers were skipped and why.
    """
    raw_ids = [v for v in request.POST.getlist("request_id") if v]
    if not raw_ids:
        raise ValueError("Не выбрана ни одна заявка")
    # Limit to the working organization so we don't touch other companies.
    org = working_organization(request)
    queryset = PaymentRequest.objects.filter(id__in=raw_ids, organization=org).select_related(
        "counterparty", "currency", "approver", "author", "article", "organization",
    )

    if action == "bulk_approve":
        if not can_approve_payment_requests(request.user):
            raise ValueError("Согласовывать заявки может только назначенный руководитель")
        _bulk_apply(
            request,
            queryset.filter(status__in=[PaymentRequestStatus.PENDING_APPROVAL, PaymentRequestStatus.PENDING_FINAL_APPROVAL]),
            lambda pr: approve_payment_request(pr, request.user),
            verb_done="Согласовано",
            verb_skipped_reason="не в статусе ‘На согласовании’",
        )
        return

    if action == "bulk_reject":
        if not can_approve_payment_requests(request.user):
            raise ValueError("Отклонять заявки может только назначенный руководитель")
        comment = (request.POST.get("approver_comment") or "").strip()
        if not comment:
            raise ValueError("Для массового отклонения укажите комментарий")
        _bulk_apply(
            request,
            queryset.filter(status__in=[PaymentRequestStatus.PENDING_APPROVAL, PaymentRequestStatus.PENDING_FINAL_APPROVAL]),
            lambda pr: reject_payment_request(pr, request.user, comment),
            verb_done="Отклонено",
            verb_skipped_reason="не в статусе ‘На согласовании’",
        )
        return

    if action == "bulk_transfer":
        if not can_manage_payment_requests(request.user):
            raise ValueError("Передавать в 1С:ДО может только экономист или администратор")
        _bulk_apply(
            request,
            queryset.filter(status=PaymentRequestStatus.APPROVED),
            lambda pr: transfer_payment_request_to_do(pr, request.user),
            verb_done="Передано в 1С:ДО",
            verb_skipped_reason="не в статусе ‘Согласована’",
        )
        return

    if action == "bulk_submit":
        if not can_manage_payment_requests(request.user):
            raise ValueError("Отправлять заявки может только экономист или администратор")
        _bulk_apply(
            request,
            queryset.filter(status=PaymentRequestStatus.DRAFT),
            lambda pr: submit_payment_request(pr, request.user),
            verb_done="Отправлено на согласование",
            verb_skipped_reason="не в статусе ‘Черновик’",
        )
        return

    if action == "bulk_cancel":
        # Отменять можно только свои черновики/pending. Cервис проверит owner.
        only_mine = queryset.filter(
            author=request.user,
            status__in=[PaymentRequestStatus.DRAFT, PaymentRequestStatus.PENDING_APPROVAL, PaymentRequestStatus.PENDING_FINAL_APPROVAL],
        )
        reason = (request.POST.get("approver_comment") or "").strip()
        _bulk_apply(
            request,
            only_mine,
            lambda pr: cancel_payment_request(pr, request.user, reason),
            verb_done="Отменено",
            verb_skipped_reason="не ваша заявка либо уже не в статусе ‘Черновик’/’На согласовании’",
        )
        return


def _bulk_apply(request, eligible_queryset, action_callable, *, verb_done: str, verb_skipped_reason: str) -> None:
    """Run `action_callable(pr)` on every eligible row; collect successes and errors."""
    eligible_ids = set(eligible_queryset.values_list("id", flat=True))
    submitted_ids = {int(v) for v in request.POST.getlist("request_id") if v.isdigit()}
    skipped = submitted_ids - eligible_ids

    success_numbers = []
    error_lines = []
    for pr in eligible_queryset:
        try:
            action_callable(pr)
            success_numbers.append(pr.number)
        except ValueError as exc:
            error_lines.append(f"{pr.number}: {exc}")

    if success_numbers:
        messages.success(request, f"{verb_done}: {len(success_numbers)} ({', '.join(success_numbers[:5])}{'…' if len(success_numbers) > 5 else ''})")
    if skipped:
        messages.warning(request, f"Пропущено: {len(skipped)} (причина: {verb_skipped_reason})")
    for line in error_lines:
        messages.error(request, line)
    if not success_numbers and not skipped and not error_lines:
        messages.info(request, "Нет подходящих заявок для выбранного действия")


# --- POST extraction helpers ----------------------------------------------

def _payment_request_reference_context():
    contracts = list(
        Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).select_related("counterparty", "currency")
    )
    agreements = list(
        AdditionalAgreement.objects.select_related("contract", "currency", "contract__counterparty").order_by(
            "date", "number",
        )
    )
    return {
        "organizations": Organization.objects.all(),
        "articles": CashFlowArticle.objects.all(),
        "counterparties": Counterparty.objects.all(),
        "contracts": contracts,
        "agreements": agreements,
        "contracts_autofill": [
            {
                "id": contract.id,
                "counterparty_id": contract.counterparty_id,
                "currency_id": contract.currency_id,
                "amount": str(contract.amount),
                "manual_exchange_rate": str(contract.manual_exchange_rate),
            }
            for contract in contracts
        ],
        "agreements_autofill": [
            {"id": agreement.id, "contract_id": agreement.contract_id}
            for agreement in agreements
        ],
        "currencies": Currency.objects.all(),
        "approvers": User.objects.filter(profile__role=UserRole.MANAGER),
    }


def _create_payment_request_from_request(request):
    contract = None
    contract_id = request.POST.get("contract_id")
    if contract_id:
        contract = get_object_or_404(Contract, pk=contract_id)

    additional_agreement = None
    additional_agreement_id = request.POST.get("additional_agreement_id")
    if additional_agreement_id:
        additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)

    counterparty = contract.counterparty if contract else get_object_or_404(
        Counterparty, pk=request.POST.get("counterparty_id"),
    )
    currency = contract.currency if contract else get_object_or_404(
        Currency, pk=request.POST.get("currency_id"),
    )
    amount_raw = (request.POST.get("amount") or "").strip()
    amount = parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
    manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
    manual_exchange_rate = (
        parse_decimal(manual_exchange_rate_raw)
        if manual_exchange_rate_raw
        else (contract.manual_exchange_rate if contract else None)
    )
    return create_payment_request(
        author=request.user,
        request_kind=request.POST.get("request_kind"),
        organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
        article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
        counterparty=counterparty,
        contract=contract,
        additional_agreement=additional_agreement,
        currency=currency,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
        approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
        invoice_number=request.POST.get("invoice_number", ""),
        invoice_date=parse_optional_date(request.POST.get("invoice_date")),
        payment_purpose=request.POST.get("payment_purpose", ""),
        comment=request.POST.get("comment", ""),
        justification_text=request.POST.get("justification_text", ""),
        justification_file=request.FILES.get("justification_file"),
    )


def _update_payment_request_from_request(payment_request, request):
    contract = None
    contract_id = request.POST.get("contract_id")
    if contract_id:
        contract = get_object_or_404(Contract, pk=contract_id)

    additional_agreement = None
    additional_agreement_id = request.POST.get("additional_agreement_id")
    if additional_agreement_id:
        additional_agreement = get_object_or_404(AdditionalAgreement, pk=additional_agreement_id)

    counterparty = contract.counterparty if contract else get_object_or_404(
        Counterparty, pk=request.POST.get("counterparty_id"),
    )
    currency = contract.currency if contract else get_object_or_404(
        Currency, pk=request.POST.get("currency_id"),
    )
    amount_raw = (request.POST.get("amount") or "").strip()
    amount = parse_decimal(amount_raw) if amount_raw else (contract.amount if contract else None)
    manual_exchange_rate_raw = (request.POST.get("manual_exchange_rate") or "").strip()
    manual_exchange_rate = (
        parse_decimal(manual_exchange_rate_raw)
        if manual_exchange_rate_raw
        else (contract.manual_exchange_rate if contract else None)
    )
    return update_payment_request(
        request=payment_request,
        user=request.user,
        request_kind=request.POST.get("request_kind"),
        organization=get_object_or_404(Organization, pk=request.POST.get("organization_id")),
        article=get_object_or_404(CashFlowArticle, pk=request.POST.get("article_id")),
        counterparty=counterparty,
        contract=contract,
        additional_agreement=additional_agreement,
        currency=currency,
        amount=amount,
        manual_exchange_rate=manual_exchange_rate,
        approver=get_object_or_404(User, pk=request.POST.get("approver_id")),
        invoice_number=request.POST.get("invoice_number", ""),
        invoice_date=parse_optional_date(request.POST.get("invoice_date")),
        payment_purpose=request.POST.get("payment_purpose", ""),
        comment=request.POST.get("comment", ""),
        justification_text=request.POST.get("justification_text", ""),
        justification_file=request.FILES.get("justification_file"),
    )
