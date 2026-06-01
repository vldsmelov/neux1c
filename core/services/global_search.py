"""Универсальный поиск по всем основным сущностям холдинга.

Не использует Postgres FTS / Elasticsearch — для простоты и скорости
работает прямым ILIKE по нескольким полям. На объёмах 10-100k записей
этого достаточно; при большем объёме переходим на trigram-индексы.
"""

from django.db.models import Q

from core.models import (
    Contract,
    Counterparty,
    FundingCase,
    PaymentRequest,
)


def global_search(query: str, limit: int = 10) -> dict:
    """Ищет везде, возвращает сгруппированные результаты."""
    query = (query or "").strip()
    if len(query) < 2:
        return {"empty": True, "query": query}

    return {
        "empty": False,
        "query": query,
        "contracts": _search_contracts(query, limit),
        "funding_cases": _search_funding_cases(query, limit),
        "counterparties": _search_counterparties(query, limit),
        "payment_requests": _search_payment_requests(query, limit),
    }


def _search_contracts(q: str, limit: int):
    return list(
        Contract.objects
        .filter(Q(number__icontains=q) | Q(name__icontains=q) | Q(counterparty__name__icontains=q))
        .select_related("counterparty", "currency", "funding_case")
        .order_by("-date")[:limit]
    )


def _search_funding_cases(q: str, limit: int):
    return list(
        FundingCase.objects
        .filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(description__icontains=q))
        .select_related("organization", "owner")
        .order_by("-opened_at")[:limit]
    )


def _search_counterparties(q: str, limit: int):
    return list(
        Counterparty.objects
        .filter(Q(name__icontains=q) | Q(inn__icontains=q))
        .order_by("name")[:limit]
    )


def _search_payment_requests(q: str, limit: int):
    return list(
        PaymentRequest.objects
        .filter(Q(number__icontains=q) | Q(comment__icontains=q) | Q(counterparty__name__icontains=q))
        .select_related("counterparty", "contract", "currency")
        .order_by("-request_date")[:limit]
    )
