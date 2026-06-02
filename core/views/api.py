"""REST API v1 для интеграций. Token-based auth.

Использование:
    curl -H 'Authorization: Token <uuid>' http://server/api/v1/funding-cases/

Эндпоинты read-only. POST/PUT появятся в v2 — нужны write-токены с
явным согласием пользователя на изменение данных.
"""

from functools import wraps
from datetime import datetime

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from ..models import ApiToken


def _api_error(message: str, status: int = 400):
    return JsonResponse({"error": message}, status=status)


def api_token_required(view_func):
    """Декоратор: ищет токен в Authorization-заголовке, аутентифицирует
    запрос от имени владельца токена. Обновляет last_used_at."""

    @wraps(view_func)
    @csrf_exempt
    def wrapper(request, *args, **kwargs):
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth.startswith("Token "):
            return _api_error("Authorization header required: 'Token <uuid>'", status=401)
        token_str = auth.removeprefix("Token ").strip()
        # Валидируем формат UUID до запроса в БД
        import uuid as _uuid
        try:
            _uuid.UUID(token_str)
        except (ValueError, TypeError):
            return _api_error("Invalid or revoked token", status=401)
        try:
            api_token = ApiToken.objects.select_related("user").get(
                token=token_str, is_active=True
            )
        except ApiToken.DoesNotExist:
            return _api_error("Invalid or revoked token", status=401)
        api_token.last_used_at = timezone.now()
        api_token.save(update_fields=["last_used_at"])
        request.user = api_token.user
        request.api_token = api_token
        return view_func(request, *args, **kwargs)

    return wrapper


@api_token_required
def api_funding_cases(request):
    """GET /api/v1/funding-cases/?status=active&organization_id=N — список кейсов."""
    from ..models import FundingCase, FundingCaseStatus
    qs = FundingCase.objects.select_related("organization", "owner")
    status = request.GET.get("status", "").strip()
    if status in FundingCaseStatus.values:
        qs = qs.filter(status=status)
    org_id = request.GET.get("organization_id")
    if org_id and org_id.isdigit():
        qs = qs.filter(organization_id=int(org_id))

    limit = min(int(request.GET.get("limit") or "50"), 200)
    items = []
    for c in qs.order_by("-opened_at")[:limit]:
        items.append({
            "id": c.id,
            "code": c.code,
            "name": c.name,
            "organization": {"id": c.organization_id, "name": c.organization.name},
            "status": c.status,
            "status_display": c.get_status_display(),
            "owner": c.owner.username if c.owner else None,
            "opened_at": c.opened_at.isoformat(),
            "closed_at": c.closed_at.isoformat() if c.closed_at else None,
            "url": f"/funding/cases/{c.id}/",
        })
    return JsonResponse({"count": len(items), "results": items})


@api_token_required
def api_funding_case_detail(request, case_id: int):
    """GET /api/v1/funding-cases/<id>/ — детальная сводка по кейсу включая overview."""
    from ..models import FundingCase
    from ..services.funding_cases import build_case_overview
    try:
        case = FundingCase.objects.select_related("organization", "owner").get(pk=case_id)
    except FundingCase.DoesNotExist:
        return _api_error("Funding case not found", status=404)

    overview = build_case_overview(case)
    return JsonResponse({
        "id": case.id,
        "code": case.code,
        "name": case.name,
        "description": case.description,
        "organization": {"id": case.organization_id, "name": case.organization.name},
        "status": case.status,
        "status_display": case.get_status_display(),
        "owner": case.owner.username if case.owner else None,
        "opened_at": case.opened_at.isoformat(),
        "closed_at": case.closed_at.isoformat() if case.closed_at else None,
        "totals": {
            "inflow": str(overview["total_inflow"]),
            "outflow": str(overview["total_outflow"]),
            "requested": str(overview["total_requested"]),
            "available_now": str(overview["available_now"]),
            "expected_inflow_remaining": str(overview["expected_inflow_remaining"]),
            "expected_outflow_remaining": str(overview["expected_outflow_remaining"]),
            "projected_balance": str(overview["projected_balance"]),
        },
        "status_label": overview["status_label"],
        "status_class": overview["status_class"],
        "risk_score": overview["risk_score"],
        "risk_label": overview["risk_label"],
        "risk_reasons": overview["risk_reasons"],
        "alerts": overview["alerts"],
    })


@api_token_required
def api_payment_facts(request):
    """GET /api/v1/payment-facts/?from=YYYY-MM-DD&to=YYYY-MM-DD&organization_id=N&direction=inflow — фильтрованный список фактов."""
    from ..models import PaymentFact
    qs = PaymentFact.objects.select_related("article", "counterparty", "contract", "currency", "organization")

    from_str = request.GET.get("from", "").strip()
    to_str = request.GET.get("to", "").strip()
    try:
        if from_str:
            qs = qs.filter(date__gte=datetime.fromisoformat(from_str).date())
        if to_str:
            qs = qs.filter(date__lte=datetime.fromisoformat(to_str).date())
    except ValueError:
        return _api_error("Invalid 'from' or 'to' date format. Use YYYY-MM-DD.")

    org_id = request.GET.get("organization_id")
    if org_id and org_id.isdigit():
        qs = qs.filter(organization_id=int(org_id))
    direction = request.GET.get("direction", "").strip()
    if direction in ("inflow", "outflow"):
        qs = qs.filter(direction=direction)

    limit = min(int(request.GET.get("limit") or "100"), 500)
    items = []
    for f in qs.order_by("-date", "-id")[:limit]:
        items.append({
            "id": f.id,
            "date": f.date.isoformat(),
            "amount": str(f.amount),
            "currency": f.currency.code,
            "direction": f.direction,
            "accounting_kind": f.accounting_kind,
            "article": {"code": f.article.code, "name": f.article.name},
            "counterparty": f.counterparty.name,
            "contract": f.contract.number if f.contract else None,
            "organization": f.organization.name,
            "external_id": f.external_id,
        })
    return JsonResponse({"count": len(items), "results": items})


@api_token_required
def api_exchange_rates(request):
    """GET /api/v1/exchange-rates/?currency=USD&from=YYYY-MM-DD — курсы валют."""
    from ..models import ExchangeRate
    qs = ExchangeRate.objects.select_related("currency")
    currency = request.GET.get("currency", "").strip().upper()
    if currency:
        qs = qs.filter(currency__code=currency)
    from_str = request.GET.get("from", "").strip()
    try:
        if from_str:
            qs = qs.filter(rate_date__gte=datetime.fromisoformat(from_str).date())
    except ValueError:
        return _api_error("Invalid 'from' date format. Use YYYY-MM-DD.")

    limit = min(int(request.GET.get("limit") or "100"), 500)
    items = []
    for r in qs.order_by("-rate_date", "currency__code")[:limit]:
        items.append({
            "currency": r.currency.code,
            "rate_date": r.rate_date.isoformat(),
            "rate_to_rub": str(r.rate_to_rub),
            "source": r.source,
        })
    return JsonResponse({"count": len(items), "results": items})


@api_token_required
def api_root(request):
    """GET /api/v1/ — версия + список доступных эндпоинтов."""
    return JsonResponse({
        "version": "v1",
        "auth": "Token-based (Authorization: Token <uuid>)",
        "endpoints": {
            "funding_cases_list": "/api/v1/funding-cases/",
            "funding_case_detail": "/api/v1/funding-cases/<id>/",
            "payment_facts": "/api/v1/payment-facts/",
            "exchange_rates": "/api/v1/exchange-rates/",
        },
        "user": request.user.username,
    })
