"""Сервис N-stage approval workflow.

Используется для:
* подбора подходящей цепочки для документа (заявка/бюджет);
* определения следующего approver-а по текущему stage;
* продвижения документа по этапам при согласовании.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model

from core.models import ApprovalChain, Organization


@dataclass
class StageInfo:
    order: int
    approver_username: str
    approver: object  # User или None если не найден
    comment: str
    min_amount: Decimal | None
    max_amount: Decimal | None


def find_applicable_chain(
    *,
    organization: Organization,
    applies_to_kind: str,
    amount_rub: Decimal,
) -> ApprovalChain | None:
    """Находит наиболее подходящую цепочку для документа.

    Алгоритм:
    1. Среди активных цепочек этой организации и типа документа,
    2. где amount_rub попадает в [min_amount_rub, max_amount_rub],
    3. берём с максимальным min_amount_rub (наиболее специфичная).
    """
    chains = ApprovalChain.objects.filter(
        organization=organization,
        applies_to_kind=applies_to_kind,
        is_active=True,
        min_amount_rub__lte=amount_rub,
    ).order_by("-min_amount_rub")

    for chain in chains:
        if chain.max_amount_rub is None or amount_rub <= chain.max_amount_rub:
            return chain
    return None


def parse_stages(chain: ApprovalChain) -> list[StageInfo]:
    """Парсит JSON-stages, резолвит approver-ов по username."""
    User = get_user_model()
    raw_stages = chain.stages or []
    if not isinstance(raw_stages, list):
        return []

    # Sort by order
    sorted_raw = sorted(
        raw_stages,
        key=lambda s: int(s.get("order", 0)) if isinstance(s, dict) else 0,
    )

    # Bulk resolve usernames
    usernames = [s.get("approver_username", "") for s in sorted_raw if isinstance(s, dict)]
    users_by_name = {u.username: u for u in User.objects.filter(username__in=usernames)}

    result: list[StageInfo] = []
    for s in sorted_raw:
        if not isinstance(s, dict):
            continue
        username = s.get("approver_username", "").strip()
        result.append(StageInfo(
            order=int(s.get("order", 0)),
            approver_username=username,
            approver=users_by_name.get(username),
            comment=str(s.get("comment", "")),
            min_amount=_to_decimal(s.get("min_amount")),
            max_amount=_to_decimal(s.get("max_amount")),
        ))
    return result


def next_stage_after(chain: ApprovalChain, current_order: int) -> StageInfo | None:
    """Возвращает следующий stage после current_order, или None если последний."""
    stages = parse_stages(chain)
    for s in stages:
        if s.order > current_order:
            return s
    return None


def first_stage(chain: ApprovalChain) -> StageInfo | None:
    """Самый первый stage цепочки."""
    stages = parse_stages(chain)
    return stages[0] if stages else None


def _to_decimal(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
