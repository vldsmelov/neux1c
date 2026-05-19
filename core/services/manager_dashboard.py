from django.db.models import Count

from core.models import PaymentRequest, PaymentRequestStatus
from core.services.plan_fact_report import PlanFactFilters, build_plan_fact_report


def build_manager_dashboard(*, year: int, organization_id: int | None = None) -> dict:
    rows, summary = build_plan_fact_report(PlanFactFilters(year=year, organization_id=organization_id))
    article_rows = _aggregate_by_article(rows)
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
    status_rows = [
        {
            "status": status,
            "label": label,
            "total": statuses_raw.get(status, 0),
        }
        for status, label in PaymentRequestStatus.choices
    ]

    return {
        "summary": summary,
        "top_overruns": top_overruns,
        "limit_residuals": limit_residuals,
        "status_rows": status_rows,
        "requests_total": sum(item["total"] for item in status_rows),
    }


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
