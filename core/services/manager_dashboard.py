from django.db.models import Count

from core.models import PaymentRequest, PaymentRequestStatus
from core.services.plan_fact_report import PlanFactFilters, build_plan_fact_report


def build_manager_dashboard(*, year: int) -> dict:
    rows, summary = build_plan_fact_report(PlanFactFilters(year=year))
    sorted_rows = sorted(rows, key=lambda row: row["balance_bu"])

    top_overruns = [row for row in sorted_rows if row["has_limit_overrun"]][:5]
    limit_residuals = sorted_rows[:12]

    statuses_raw = {
        row["status"]: row["total"]
        for row in PaymentRequest.objects.filter(request_date__year=year)
        .values("status")
        .annotate(total=Count("id"))
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
