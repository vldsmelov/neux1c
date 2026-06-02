"""UI цепочек согласования /settings/approval-chains/."""

import json

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from ..access import role_required
from ..models import ApprovalChain, Organization, UserRole
from ..services.approval_chains import parse_stages
from ._shared import parse_optional_int


@role_required(UserRole.ADMINISTRATOR, UserRole.MANAGER)
def approval_chains_index(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                _create_chain(request)
                messages.success(request, "Цепочка создана")
            elif action == "toggle":
                chain = get_object_or_404(
                    ApprovalChain,
                    pk=parse_optional_int(request.POST.get("chain_id")),
                )
                chain.is_active = not chain.is_active
                chain.save(update_fields=["is_active"])
                messages.success(
                    request,
                    f"Цепочка «{chain.name}» {'активирована' if chain.is_active else 'отключена'}",
                )
            elif action == "delete":
                chain = get_object_or_404(
                    ApprovalChain,
                    pk=parse_optional_int(request.POST.get("chain_id")),
                )
                name = chain.name
                chain.delete()
                messages.success(request, f"Цепочка «{name}» удалена")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("approval_chains_index")

    chains_qs = ApprovalChain.objects.select_related(
        "organization", "created_by"
    ).order_by("organization__name", "min_amount_rub")

    # Парсим stages для отображения
    chains_with_stages = []
    for ch in chains_qs:
        chains_with_stages.append({
            "chain": ch,
            "stages": parse_stages(ch),
        })

    return render(
        request,
        "core/approval_chains.html",
        {
            "active_section": "settings",
            "chains_with_stages": chains_with_stages,
            "organizations": Organization.objects.order_by("name"),
            "applies_choices": ApprovalChain.APPLIES_CHOICES,
        },
    )


def _create_chain(request) -> ApprovalChain:
    name = (request.POST.get("name") or "").strip()
    if not name:
        raise ValueError("Укажите название цепочки")
    organization = get_object_or_404(
        Organization, pk=parse_optional_int(request.POST.get("organization_id"))
    )
    applies_to_kind = request.POST.get("applies_to_kind") or ApprovalChain.APPLIES_PAYMENT_REQUEST
    if applies_to_kind not in {k for k, _ in ApprovalChain.APPLIES_CHOICES}:
        applies_to_kind = ApprovalChain.APPLIES_PAYMENT_REQUEST

    min_amount = request.POST.get("min_amount_rub") or "0"
    max_amount = (request.POST.get("max_amount_rub") or "").strip() or None

    # Парсим stages из формы
    stages_json_raw = (request.POST.get("stages_json") or "").strip()
    if stages_json_raw:
        try:
            stages = json.loads(stages_json_raw)
            if not isinstance(stages, list):
                raise ValueError("stages должен быть массивом")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Невалидный JSON: {exc}") from exc
    else:
        # Альтернативный режим: набор полей stage_1_username, stage_2_username
        stages = []
        for i in range(1, 6):
            username = (request.POST.get(f"stage_{i}_username") or "").strip()
            if username:
                stages.append({
                    "order": i,
                    "approver_username": username,
                    "comment": "",
                })

    if not stages:
        raise ValueError("Цепочка должна содержать хотя бы один этап")

    return ApprovalChain.objects.create(
        name=name,
        organization=organization,
        applies_to_kind=applies_to_kind,
        min_amount_rub=min_amount,
        max_amount_rub=max_amount,
        stages=stages,
        comment=(request.POST.get("comment") or "").strip(),
        created_by=request.user,
    )
