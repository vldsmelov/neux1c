from django.db import transaction
from core.models import (
    AuditAction,
    AuditLog,
    IntegrationRequest,
    IntegrationRequestStatus,
    NotificationKind,
)
from core.services.document_numbers import next_document_number
from core.services.notifications import notify


WORKFLOW_PATH = "/settings/integration-requests/"


@transaction.atomic
def create_integration_request(
    *,
    requested_by,
    integration_name: str,
    target_system: str,
    description: str,
) -> IntegrationRequest:
    if not integration_name.strip():
        raise ValueError("Укажите название интеграции")
    if not target_system.strip():
        raise ValueError("Укажите целевую систему")
    if not description.strip():
        raise ValueError("Описание заявки обязательно")

    request = IntegrationRequest.objects.create(
        number=next_integration_request_number(),
        requested_by=requested_by,
        integration_name=integration_name.strip(),
        target_system=target_system.strip(),
        description=description.strip(),
    )
    AuditLog.objects.create(
        user=requested_by,
        action=AuditAction.CREATE,
        path=WORKFLOW_PATH,
        object_type="IntegrationRequest",
        object_id=str(request.pk),
        message=f"Создана заявка на интеграцию {request.number}",
    )
    return request


@transaction.atomic
def update_integration_request_status(
    *,
    request: IntegrationRequest,
    admin_user,
    status: str,
    admin_comment: str = "",
) -> IntegrationRequest:
    if status not in IntegrationRequestStatus.values:
        raise ValueError("Некорректный статус заявки")
    if status == IntegrationRequestStatus.REJECTED and not admin_comment.strip():
        raise ValueError("Для отклонения заявки нужен комментарий администратора")

    request.status = status
    request.assigned_admin = admin_user
    request.admin_comment = admin_comment.strip()
    request.save(update_fields=["status", "assigned_admin", "admin_comment", "updated_at"])

    AuditLog.objects.create(
        user=admin_user,
        action=AuditAction.UPDATE,
        path=WORKFLOW_PATH,
        object_type="IntegrationRequest",
        object_id=str(request.pk),
        message=f"Статус заявки {request.number} изменен на {request.get_status_display()}",
    )
    # Уведомить инициатора об изменении статуса (не уведомляем самого админа,
    # если он же является инициатором — это его собственное действие).
    if request.requested_by_id and request.requested_by_id != admin_user.id:
        comment_suffix = f" · {admin_comment.strip()}" if admin_comment.strip() else ""
        notify(
            recipient=request.requested_by,
            kind=NotificationKind.INTEGRATION_STATUS_CHANGED,
            title=f"Заявка {request.number}: {request.get_status_display()}",
            text=f"{request.integration_name} · {request.target_system}{comment_suffix}",
            link=WORKFLOW_PATH,
            related=request,
        )
    return request


def next_integration_request_number() -> str:
    return next_document_number("INT")
