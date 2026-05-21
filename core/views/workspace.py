"""Workspace landing page + role-aware «рабочий день» tasks pipeline."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse

from ..access import user_has_role
from ..models import (
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlan,
    BudgetPlanStatus,
    CashFlowArticle,
    Contract,
    IntegrationRequest,
    IntegrationRequestStatus,
    PaymentFact,
    PaymentRequest,
    PaymentRequestStatus,
    SyncRun,
    UserRole,
)
from ._permissions import (
    can_approve_budgets,
    can_approve_payment_requests,
    can_approve_plans,
    can_create_budgets,
    can_edit_plans,
    can_manage_payment_requests,
)
from ._shared import is_administrator, working_organization


@login_required
def workspace(request):
    profile = getattr(request.user, "profile", None)
    if profile and profile.role == UserRole.ACCOUNTANT:
        return redirect("external_accounting")
    if not user_has_role(request.user, {UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER}):
        raise PermissionDenied
    modules = [
        {"name": "Планирование", "document": "Бюджет и лимит", "state": "В работе", "check": "Бюджет, резерв и контроль лимитов"},
        {"name": "НСИ", "document": "Синхронизация Mock-1С", "state": "Готово", "check": "Данные загружены"},
        {"name": "Договоры", "document": "Дерево договоров", "state": "НСИ готова", "check": "Резерв считается"},
        {"name": "Заявки", "document": "Заявка на оплату", "state": "В работе", "check": "Лимит + согласование + передача в 1С:ДО"},
        {"name": "Отчетность", "document": "Дашборд руководителя", "state": "Готово", "check": "Остатки + превышения + статусы"},
        {"name": "Настройки", "document": "Заявки на интеграцию", "state": "В работе", "check": "Запрос администратору"},
    ]
    counts = {
        "articles": CashFlowArticle.objects.count(),
        "contracts": Contract.objects.count(),
        "payment_facts": PaymentFact.objects.count(),
        "sync_runs": SyncRun.objects.count(),
    }
    org = working_organization(request)
    workday = _build_workday_context(request.user, org)
    return render(
        request,
        "core/workspace.html",
        {
            "modules": modules,
            "counts": counts,
            "workday": workday,
            "active_section": "workspace",
        },
    )


# --- Workday tasks pipeline ----------------------------------------------

def _build_workday_context(user, organization) -> dict:
    profile = getattr(user, "profile", None)
    role = profile.role if profile else ""
    tasks = []

    if organization is None:
        tasks.append(
            _task_row(
                "Подготовить компании",
                "Перед работой с бюджетами и лимитами синхронизируйте или заведите компанию.",
                "НСИ",
                reverse("nsi_dashboard") if user.is_superuser or role in [UserRole.ADMINISTRATOR, UserRole.ECONOMIST] else reverse("workspace"),
                "Открыть",
                "high",
            )
        )
        return {
            "role_label": _workday_role_label(role),
            "headline": _workday_headline(role),
            "rhythm": _workday_rhythm(role),
            "tasks": tasks,
            "task_count": len(tasks),
            "high_count": 1,
            "primary_action": _workday_primary_action(role),
        }

    if can_approve_budgets(user):
        for budget in BudgetPlan.objects.filter(
            organization=organization,
            status=BudgetPlanStatus.PENDING_APPROVAL,
        ).select_related("currency")[:5]:
            tasks.append(
                _task_row(
                    "Согласовать бюджет",
                    f"{budget.number} · {budget.budget_year} · {budget.total_amount} {budget.currency.code}",
                    "Планирование",
                    reverse("document_action_wizard", args=["budget", budget.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )

    if can_approve_plans(user):
        plan_query = BudgetLimitPlan.objects.filter(
            organization=organization,
            status=BudgetPlanStatus.PENDING_APPROVAL,
        ).select_related("department", "article", "currency", "approver")
        adjustment_query = BudgetLimitAdjustment.objects.filter(
            base_plan__organization=organization,
            status=BudgetPlanStatus.PENDING_APPROVAL,
        ).select_related("base_plan", "base_plan__currency", "article", "approver")
        if not user.is_superuser:
            plan_query = plan_query.filter(approver=user)
            adjustment_query = adjustment_query.filter(approver=user)
        for plan in plan_query[:5]:
            tasks.append(
                _task_row(
                    "Утвердить лимит",
                    f"{plan.number} · {plan.department.name} · {plan.annual_amount} {plan.currency.code}",
                    "Лимиты",
                    reverse("document_action_wizard", args=["limit", plan.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )
        for adjustment in adjustment_query[:5]:
            tasks.append(
                _task_row(
                    "Утвердить корректировку",
                    f"{adjustment.number} · {adjustment.base_plan.number} · v{adjustment.version}",
                    "Лимиты",
                    reverse("document_action_wizard", args=["limit-adjustment", adjustment.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )

    if can_approve_payment_requests(user):
        payment_query = PaymentRequest.objects.filter(
            organization=organization,
            status=PaymentRequestStatus.PENDING_APPROVAL,
        ).select_related("counterparty", "currency", "approver")
        if not user.is_superuser:
            payment_query = payment_query.filter(approver=user)
        for payment_request in payment_query[:7]:
            tasks.append(
                _task_row(
                    "Согласовать платеж",
                    f"{payment_request.number} · {payment_request.counterparty.name} · {payment_request.amount} {payment_request.currency.code}",
                    "Платежи",
                    reverse("document_action_wizard", args=["payment-request", payment_request.id, "approve"]),
                    "Открыть wizard",
                    "high",
                )
            )

    if can_manage_payment_requests(user):
        editable_requests = PaymentRequest.objects.filter(
            Q(status=PaymentRequestStatus.REJECTED) | Q(status=PaymentRequestStatus.DRAFT),
            organization=organization,
        ).select_related("counterparty", "currency", "author")
        if not user.is_superuser:
            editable_requests = editable_requests.filter(author=user)
        for payment_request in editable_requests[:5]:
            action_url = (
                reverse("payment_request_edit_wizard", args=[payment_request.id])
                if payment_request.status == PaymentRequestStatus.REJECTED
                else reverse("document_action_wizard", args=["payment-request", payment_request.id, "submit"])
            )
            tasks.append(
                _task_row(
                    "Доработать заявку" if payment_request.status == PaymentRequestStatus.REJECTED else "Отправить черновик",
                    f"{payment_request.number} · {payment_request.counterparty.name} · {payment_request.get_status_display()}",
                    "Платежи",
                    action_url,
                    "Открыть wizard",
                    "medium",
                )
            )

        for payment_request in PaymentRequest.objects.filter(
            organization=organization,
            status=PaymentRequestStatus.APPROVED,
        ).select_related("counterparty", "currency")[:7]:
            tasks.append(
                _task_row(
                    "Передать в 1С:ДО",
                    f"{payment_request.number} · {payment_request.counterparty.name} · {payment_request.amount} {payment_request.currency.code}",
                    "Платежи",
                    reverse("document_action_wizard", args=["payment-request", payment_request.id, "transfer"]),
                    "Открыть wizard",
                    "medium",
                )
            )

    if can_edit_plans(user):
        draft_limits = BudgetLimitPlan.objects.filter(
            organization=organization,
            status=BudgetPlanStatus.DRAFT,
        ).select_related("department", "article", "currency", "author")
        draft_adjustments = BudgetLimitAdjustment.objects.filter(
            base_plan__organization=organization,
            status=BudgetPlanStatus.DRAFT,
        ).select_related("base_plan", "base_plan__currency", "author")
        if not user.is_superuser:
            draft_limits = draft_limits.filter(author=user)
            draft_adjustments = draft_adjustments.filter(author=user)
        for plan in draft_limits[:4]:
            tasks.append(
                _task_row(
                    "Отправить лимит",
                    f"{plan.number} · {plan.department.name} · {plan.annual_amount} {plan.currency.code}",
                    "Планирование",
                    reverse("document_action_wizard", args=["limit", plan.id, "submit"]),
                    "Открыть wizard",
                    "medium",
                )
            )
        for adjustment in draft_adjustments[:4]:
            tasks.append(
                _task_row(
                    "Отправить корректировку",
                    f"{adjustment.number} · {adjustment.base_plan.number} · v{adjustment.version}",
                    "Планирование",
                    reverse("document_action_wizard", args=["limit-adjustment", adjustment.id, "submit"]),
                    "Открыть wizard",
                    "medium",
                )
            )

    if is_administrator(user):
        for item in IntegrationRequest.objects.filter(status__in=[
            IntegrationRequestStatus.NEW,
            IntegrationRequestStatus.IN_PROGRESS,
        ]).select_related("requested_by")[:6]:
            tasks.append(
                _task_row(
                    "Вести интеграцию",
                    f"{item.number} · {item.integration_name} · {item.target_system}",
                    "Настройки",
                    reverse("integration_request_status_wizard", args=[item.id]),
                    "Открыть wizard",
                    "medium",
                )
            )

    if not tasks:
        tasks.append(
            _task_row(
                "Создать рабочий документ",
                "Начните с первичного бюджета, корректировки лимита или платежной заявки.",
                "Старт",
                reverse("planning_wizard") if can_create_budgets(user) else reverse("payment_requests"),
                "Начать",
                "low",
            )
        )

    tasks = tasks[:12]
    return {
        "role_label": _workday_role_label(role),
        "headline": _workday_headline(role),
        "rhythm": _workday_rhythm(role),
        "tasks": tasks,
        "task_count": len(tasks),
        "high_count": sum(1 for task in tasks if task["priority"] == "high"),
        "primary_action": _workday_primary_action(role),
    }


def _task_row(title: str, text: str, area: str, url: str, action_label: str, priority: str) -> dict:
    return {
        "title": title,
        "text": text,
        "area": area,
        "url": url,
        "action_label": action_label,
        "priority": priority,
    }


def _workday_role_label(role: str) -> str:
    labels = {
        UserRole.ADMINISTRATOR: "Администратор",
        UserRole.ECONOMIST: "Экономист",
        UserRole.MANAGER: "Руководитель",
        UserRole.ACCOUNTANT: "Бухгалтер",
    }
    return labels.get(role, "Пользователь")


def _workday_headline(role: str) -> str:
    if role == UserRole.MANAGER:
        return "Утром проверьте документы на утверждении, затем отклонения и превышения."
    if role == UserRole.ADMINISTRATOR:
        return "Начните с зависших согласований и интеграционных заявок, затем проверьте журналы."
    if role == UserRole.ECONOMIST:
        return "Сначала доработайте возвраты и отправьте черновики, затем создавайте новые документы."
    return "Начните с документов, которые требуют действия сегодня."


def _workday_rhythm(role: str) -> list[dict]:
    if role == UserRole.MANAGER:
        return [
            {"time": "09:00", "title": "Очередь согласований", "text": "Бюджеты, лимиты, корректировки и платежные заявки."},
            {"time": "12:00", "title": "Контроль лимитов", "text": "Проверка превышений, резервов и спорных заявок."},
            {"time": "16:00", "title": "Отчеты", "text": "План-факт и управленческий дашборд."},
        ]
    if role == UserRole.ADMINISTRATOR:
        return [
            {"time": "09:00", "title": "Зависшие операции", "text": "Документы без движения и интеграционные запросы."},
            {"time": "13:00", "title": "НСИ и доступы", "text": "Синхронизация, роли, справочники."},
            {"time": "17:00", "title": "Аудит", "text": "Проверка журнала действий и качества данных."},
        ]
    return [
        {"time": "09:00", "title": "Возвраты и черновики", "text": "Исправить отклоненные заявки и отправить готовые документы."},
        {"time": "11:00", "title": "Новый ввод", "text": "Бюджеты, лимиты, платежные заявки и корректировки через wizard."},
        {"time": "15:00", "title": "Контроль журналов", "text": "Статусы, передача в 1С:ДО, факты и план-факт."},
    ]


def _workday_primary_action(role: str) -> dict:
    if role == UserRole.MANAGER:
        return {"label": "Открыть дашборд", "url": reverse("manager_dashboard")}
    if role == UserRole.ADMINISTRATOR:
        return {"label": "Интеграции", "url": reverse("integration_requests")}
    return {"label": "Новая заявка", "url": reverse("payment_request_wizard")}
