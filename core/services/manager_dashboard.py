from django.db.models import Count

from core.models import PaymentRequest, PaymentRequestStatus
from core.services.plan_fact_report import PlanFactFilters, build_plan_fact_report


def build_manager_dashboard(*, year: int, organization_id: int | None = None) -> dict:
    rows, summary = build_plan_fact_report(PlanFactFilters(year=year, organization_id=organization_id))
    article_rows = _aggregate_by_article(rows)
    for row in article_rows:
        _annotate_usage(row)
    sorted_rows = sorted(article_rows, key=lambda row: row["balance_bu"])

    top_overruns = [row for row in sorted_rows if row["has_limit_overrun"]][:5]
    limit_residuals = sorted_rows[:12]

    request_query = PaymentRequest.objects.filter(request_date__year=year)
    if organization_id:
        request_query = request_query.filter(organization_id=organization_id)
    statuses_raw = {
        row["status"]: row["total"]
        for row in request_query.values("status").annotate(total=Count("id"))
    }
    requests_total = sum(statuses_raw.values())
    status_rows = []
    for status, label in PaymentRequestStatus.choices:
        total = statuses_raw.get(status, 0)
        share = (total / requests_total * 100) if requests_total else 0
        status_rows.append({
            "status": status,
            "label": label,
            "total": total,
            "share_pct": round(share, 1),
            "kind": _status_kind(status),
        })

    return {
        "summary": summary,
        "top_overruns": top_overruns,
        "limit_residuals": limit_residuals,
        "status_rows": status_rows,
        "requests_total": requests_total,
    }


def _annotate_usage(row: dict) -> None:
    """Attach effective_limit, used, usage_pct (capped 100), overrun_pct, usage_level."""
    effective = (row.get("plan") or 0) + (row.get("adjustments") or 0)
    used = (row.get("reserved") or 0) + (row.get("requested") or 0) + (row.get("fact_bu") or 0)
    row["effective_limit"] = effective
    row["used"] = used
    if effective > 0:
        pct = float(used) / float(effective) * 100
    else:
        pct = 100.0 if used > 0 else 0.0
    row["usage_pct"] = round(min(pct, 100.0), 1)
    row["overrun_pct"] = round(max(pct - 100.0, 0.0), 1)
    if pct >= 100:
        row["usage_level"] = "danger"
    elif pct >= 80:
        row["usage_level"] = "warn"
    else:
        row["usage_level"] = "ok"


def _status_kind(status: str) -> str:
    # Map PaymentRequestStatus codes to badge variants used in templates
    if status in ("approved", "transferred"):
        return "success"
    if status == "pending_approval":
        return "info"
    if status == "rejected":
        return "danger"
    return "neutral"


def _aggregate_by_article(rows: list[dict]) -> list[dict]:
    by_article = {}
    for row in rows:
        article_id = row["article"].id
        if article_id not in by_article:
            by_article[article_id] = {
                "article": row["article"],
                "plan": row["plan"],
                "adjustments": row["adjustments"],
                "reserved": row["reserved"],
                "requested": row["requested"],
                "fact_bu": row["fact_bu"],
                "fact_nu": row["fact_nu"],
                "balance_bu": row["balance_bu"],
                "balance_nu": row["balance_nu"],
                "has_limit_overrun": row["has_limit_overrun"],
                "is_internal_turnover": row["is_internal_turnover"],
                "missing_in_one_c": row["missing_in_one_c"],
            }
            continue

        aggregate = by_article[article_id]
        aggregate["plan"] += row["plan"]
        aggregate["adjustments"] += row["adjustments"]
        aggregate["reserved"] += row["reserved"]
        aggregate["requested"] += row["requested"]
        aggregate["fact_bu"] += row["fact_bu"]
        aggregate["fact_nu"] += row["fact_nu"]
        aggregate["balance_bu"] += row["balance_bu"]
        aggregate["balance_nu"] += row["balance_nu"]
        aggregate["has_limit_overrun"] = aggregate["balance_bu"] < 0

    return list(by_article.values())
