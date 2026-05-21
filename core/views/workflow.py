"""Universal document action wizard (submit / approve / reject / transfer).

Один view + конфигурация в виде словаря на (document_type, action). По сравнению
с раздельными view-функциями это даёт единый шаблон UX подтверждения, а вся
домен-специфика (как достать документ, кто может выполнить, какой success message)
описана декларативно.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ..access import role_required
from ..models import (
    BudgetLimitAdjustment,
    BudgetLimitPlan,
    BudgetPlan,
    PaymentRequest,
    UserRole,
)
from ..services.budget_planning import (
    approve_budget_plan,
    approve_limit_adjustment,
    submit_budget_plan,
    submit_limit_adjustment,
)
from ..services.budgets import approve_budget, submit_budget
from ..services.payment_requests import (
    approve_payment_request,
    reject_payment_request,
    submit_payment_request,
    transfer_payment_request_to_do,
)
from ._permissions import (
    can_approve_budgets,
    can_approve_payment_requests,
    can_approve_plans,
    can_create_budgets,
    can_edit_limit_adjustments,
    can_edit_plans,
    can_manage_payment_requests,
)


@role_required(UserRole.ADMINISTRATOR, UserRole.ECONOMIST, UserRole.MANAGER)
def document_action_wizard(request, document_type: str, document_id: int, action: str):
    config = _document_action_config(document_type, action, document_id)
    if config is None:
        raise Http404("Document action not found")

    document = config["document"]
    can_execute = config["can_execute"](request.user, document)
    submitted: dict = {}
    if request.method == "POST":
        try:
            if not can_execute:
                raise ValueError(config["permission_error"])
            comment = request.POST.get("comment", "")
            config["execute"](document, request.user, comment)
            messages.success(request, config["success_message"].format(number=document.number))
            return redirect(config["return_route"])
        except ValueError as exc:
            messages.error(request, str(exc))
            submitted = request.POST

    return render(
        request,
        "core/document_action_wizard.html",
        {
            "active_section": config["active_section"],
            "document": document,
            "document_kind": config["document_kind"],
            "details": config["details"](document),
            "action_label": config["action_label"],
            "action_text": config["action_text"],
            "submit_label": config["submit_label"],
            "comment_label": config.get("comment_label", "Комментарий"),
            "comment_placeholder": config.get("comment_placeholder", ""),
            "comment_required": config.get("comment_required", False),
            "can_execute": can_execute,
            "return_url": reverse(config["return_route"]),
            "submitted": submitted,
        },
    )


# --- Configuration table -------------------------------------------------

def _document_action_config(document_type: str, action: str, document_id: int):
    def payment_document():
        return get_object_or_404(
            PaymentRequest.objects.select_related(
                "organization", "article", "counterparty", "contract",
                "additional_agreement", "currency", "approver", "author",
            ),
            pk=document_id,
        )

    configs = {
        ("budget", "submit"): {
            "document": lambda: get_object_or_404(BudgetPlan.objects.select_related("organization", "currency", "author"), pk=document_id),
            "active_section": "planning",
            "return_route": "planning_budgets",
            "document_kind": "Бюджет",
            "action_label": "Отправка бюджета",
            "action_text": "Бюджет будет отправлен на утверждение и останется доступен в журнале бюджетов.",
            "submit_label": "Отправить на утверждение",
            "permission_error": "Отправлять бюджет может экономист или администратор",
            "can_execute": lambda user, document: can_create_budgets(user),
            "execute": lambda document, user, comment: submit_budget(document, user),
            "success_message": "Бюджет {number} отправлен на утверждение",
            "details": _budget_action_details,
        },
        ("budget", "approve"): {
            "document": lambda: get_object_or_404(BudgetPlan.objects.select_related("organization", "currency", "author"), pk=document_id),
            "active_section": "planning",
            "return_route": "planning_budgets",
            "document_kind": "Бюджет",
            "action_label": "Утверждение бюджета",
            "action_text": "После утверждения бюджет станет базой контроля лимитов.",
            "submit_label": "Утвердить бюджет",
            "permission_error": "Утверждать бюджеты может руководитель или администратор",
            "can_execute": lambda user, document: can_approve_budgets(user),
            "execute": lambda document, user, comment: approve_budget(document, user),
            "success_message": "Бюджет {number} утвержден",
            "details": _budget_action_details,
        },
        ("limit", "submit"): {
            "document": lambda: get_object_or_404(
                BudgetLimitPlan.objects.select_related("organization", "budget", "department", "article", "currency", "approver", "author"),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Лимит",
            "action_label": "Отправка лимита",
            "action_text": "Лимит будет передан назначенному руководителю на утверждение.",
            "submit_label": "Отправить на утверждение",
            "permission_error": "Отправлять лимиты может экономист или администратор",
            "can_execute": lambda user, document: can_edit_plans(user),
            "execute": lambda document, user, comment: submit_budget_plan(document, user),
            "success_message": "Лимит {number} отправлен на утверждение",
            "details": _limit_action_details,
        },
        ("limit", "approve"): {
            "document": lambda: get_object_or_404(
                BudgetLimitPlan.objects.select_related("organization", "budget", "department", "article", "currency", "approver", "author"),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Лимит",
            "action_label": "Утверждение лимита",
            "action_text": "После утверждения лимит участвует в контроле платежных заявок.",
            "submit_label": "Утвердить лимит",
            "permission_error": "Утверждать лимиты может назначенный руководитель или администратор",
            "can_execute": lambda user, document: can_approve_plans(user),
            "execute": lambda document, user, comment: approve_budget_plan(document, user),
            "success_message": "Лимит {number} утвержден",
            "details": _limit_action_details,
        },
        ("limit-adjustment", "submit"): {
            "document": lambda: get_object_or_404(
                BudgetLimitAdjustment.objects.select_related(
                    "base_plan", "base_plan__organization", "base_plan__department",
                    "base_plan__currency", "target_plan", "target_organization",
                    "article", "approver", "author",
                ),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Корректировка лимита",
            "action_label": "Отправка корректировки",
            "action_text": "Заявка на корректировку лимита будет передана руководителю.",
            "submit_label": "Отправить на утверждение",
            "permission_error": "Отправлять корректировки может экономист или администратор",
            "can_execute": lambda user, document: can_edit_limit_adjustments(user),
            "execute": lambda document, user, comment: submit_limit_adjustment(document, user),
            "success_message": "Корректировка {number} отправлена на утверждение",
            "details": _limit_adjustment_action_details,
        },
        ("limit-adjustment", "approve"): {
            "document": lambda: get_object_or_404(
                BudgetLimitAdjustment.objects.select_related(
                    "base_plan", "base_plan__organization", "base_plan__department",
                    "base_plan__currency", "target_plan", "target_organization",
                    "article", "approver", "author",
                ),
                pk=document_id,
            ),
            "active_section": "planning",
            "return_route": "planning_limits",
            "document_kind": "Корректировка лимита",
            "action_label": "Утверждение корректировки",
            "action_text": "После утверждения новая сумма и месячная разбивка обновят базовый лимит.",
            "submit_label": "Утвердить корректировку",
            "permission_error": "Утверждать корректировку может назначенный руководитель или администратор",
            "can_execute": lambda user, document: can_approve_plans(user),
            "execute": lambda document, user, comment: approve_limit_adjustment(document, user),
            "success_message": "Корректировка {number} утверждена",
            "details": _limit_adjustment_action_details,
        },
        ("payment-request", "submit"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Отправка заявки",
            "action_text": "Заявка будет отправлена назначенному руководителю на согласование.",
            "submit_label": "Отправить на согласование",
            "permission_error": "Отправлять заявки может экономист или администратор",
            "can_execute": lambda user, document: can_manage_payment_requests(user),
            "execute": lambda document, user, comment: submit_payment_request(document, user),
            "success_message": "Заявка {number} отправлена на согласование",
            "details": _payment_request_action_details,
        },
        ("payment-request", "approve"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Согласование заявки",
            "action_text": "После согласования заявка будет готова к передаче во внешний документооборот.",
            "submit_label": "Согласовать заявку",
            "permission_error": "Согласовывать заявки может назначенный руководитель",
            "can_execute": lambda user, document: can_approve_payment_requests(user),
            "execute": lambda document, user, comment: approve_payment_request(document, user),
            "success_message": "Заявка {number} согласована",
            "details": _payment_request_action_details,
        },
        ("payment-request", "reject"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Отклонение заявки",
            "action_text": "Заявка вернется автору на исправление. Комментарий обязателен.",
            "submit_label": "Отклонить заявку",
            "permission_error": "Отклонять заявки может назначенный руководитель",
            "can_execute": lambda user, document: can_approve_payment_requests(user),
            "execute": lambda document, user, comment: reject_payment_request(document, user, comment),
            "success_message": "Заявка {number} отклонена",
            "comment_required": True,
            "comment_label": "Причина отклонения",
            "comment_placeholder": "Что нужно исправить в заявке",
            "details": _payment_request_action_details,
        },
        ("payment-request", "transfer"): {
            "document": payment_document,
            "active_section": "payments",
            "return_route": "payment_requests",
            "document_kind": "Заявка на оплату",
            "action_label": "Передача в 1С:ДО",
            "action_text": "Заявка будет передана во внешний документооборот и получит внешний идентификатор.",
            "submit_label": "Передать в 1С:ДО",
            "permission_error": "Передавать заявки может экономист или администратор",
            "can_execute": lambda user, document: can_manage_payment_requests(user),
            "execute": lambda document, user, comment: transfer_payment_request_to_do(document, user),
            "success_message": "Заявка {number} передана в 1С:ДО",
            "details": _payment_request_action_details,
        },
    }
    config = configs.get((document_type, action))
    if config is None:
        return None
    resolved = dict(config)
    resolved["document"] = config["document"]()
    return resolved


# --- Details renderers per document kind ----------------------------------

def _budget_action_details(budget):
    return [
        {"label": "Номер", "value": budget.number},
        {"label": "Компания", "value": budget.organization.name if budget.organization else "Не указана"},
        {"label": "Год", "value": budget.budget_year},
        {"label": "Вид", "value": budget.get_scope_display()},
        {"label": "Сумма", "value": f"{budget.total_amount} {budget.currency.code}"},
        {"label": "Статус", "value": budget.get_status_display()},
        {"label": "Автор", "value": budget.author.get_full_name() or budget.author.username},
    ]


def _limit_action_details(plan):
    return [
        {"label": "Номер", "value": plan.number},
        {"label": "Компания", "value": plan.organization.name if plan.organization else "Не указана"},
        {"label": "Бюджет", "value": plan.budget.number if plan.budget else "Без бюджета"},
        {"label": "ЦФО", "value": plan.department.name},
        {"label": "Статья", "value": plan.article.name},
        {"label": "Сумма", "value": f"{plan.annual_amount} {plan.currency.code}"},
        {"label": "Согласующий", "value": plan.approver.get_full_name() or plan.approver.username},
    ]


def _limit_adjustment_action_details(adjustment):
    return [
        {"label": "Номер", "value": adjustment.number},
        {"label": "Базовый лимит", "value": adjustment.base_plan.number},
        {"label": "Компания-источник", "value": adjustment.base_plan.organization.name if adjustment.base_plan.organization else "Не указана"},
        {"label": "Компания-получатель", "value": adjustment.target_organization.name if adjustment.target_organization else "Внутри компании"},
        {"label": "ЦФО", "value": adjustment.base_plan.department.name},
        {"label": "Статья", "value": adjustment.article.name},
        {"label": "Новая сумма", "value": f"{adjustment.new_annual_amount} {adjustment.base_plan.currency.code}"},
        {"label": "Версия", "value": f"v{adjustment.version}"},
    ]


def _payment_request_action_details(payment_request):
    return [
        {"label": "Номер", "value": payment_request.number},
        {"label": "Компания", "value": payment_request.organization.name},
        {"label": "Тип", "value": payment_request.get_request_kind_display()},
        {"label": "Контрагент", "value": payment_request.counterparty.name},
        {"label": "Сумма", "value": f"{payment_request.amount} {payment_request.currency.code}"},
        {"label": "Статус", "value": payment_request.get_status_display()},
        {"label": "Согласующий", "value": payment_request.approver.get_full_name() or payment_request.approver.username},
    ]
