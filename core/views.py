from django.db import connection
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render

from .models import (
    AdditionalAgreement,
    CashFlowArticle,
    Contract,
    ContractKind,
    Counterparty,
    Currency,
    Department,
    Nomenclature,
    Organization,
    PaymentFact,
    SyncRun,
)


def workspace(request):
    modules = [
        {"name": "Планирование и контроль", "state": "каркас"},
        {"name": "НСИ и договоры", "state": "mock-данные"},
        {"name": "Mock-1C", "state": "адаптер готов"},
        {"name": "Отчетность", "state": "ожидает лимиты"},
    ]
    return render(request, "core/workspace.html", {"modules": modules})


def nsi_dashboard(request):
    article_stats = CashFlowArticle.objects.aggregate(
        total=Count("id"),
        missing_in_one_c=Count("id", filter=Q(exists_in_one_c=False)),
        internal_turnover=Count("id", filter=Q(is_internal_turnover=True)),
    )
    facts_total = PaymentFact.objects.aggregate(total=Sum("amount"))["total"] or 0
    supplier_contracts = Contract.objects.filter(kind=ContractKind.SOLE_SUPPLIER).prefetch_related(
        "additional_agreements",
        "counterparty",
        "currency",
    )

    context = {
        "latest_sync": SyncRun.objects.first(),
        "counts": {
            "organizations": Organization.objects.count(),
            "departments": Department.objects.count(),
            "currencies": Currency.objects.count(),
            "articles": article_stats["total"],
            "counterparties": Counterparty.objects.count(),
            "nomenclature": Nomenclature.objects.count(),
            "contracts": Contract.objects.count(),
            "agreements": AdditionalAgreement.objects.count(),
            "payment_facts": PaymentFact.objects.count(),
        },
        "article_stats": article_stats,
        "facts_total": facts_total,
        "articles": CashFlowArticle.objects.all()[:20],
        "supplier_contracts": supplier_contracts,
        "payment_facts": PaymentFact.objects.select_related("article", "counterparty", "currency").all()[:20],
    }
    return render(request, "core/nsi_dashboard.html", context)


def healthz(request):
    database = "ok"
    try:
        connection.ensure_connection()
    except Exception:
        database = "error"

    status = 200 if database == "ok" else 503
    return JsonResponse({"status": "ok" if status == 200 else "error", "database": database}, status=status)
