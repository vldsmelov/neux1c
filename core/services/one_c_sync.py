from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from core.integrations.one_c.provider import OneCProvider
from core.models import (
    AdditionalAgreement,
    CashFlowArticle,
    Contract,
    Counterparty,
    Currency,
    Department,
    Nomenclature,
    Organization,
    PaymentFact,
    SourceSystem,
    SyncRun,
    SyncStatus,
)


@dataclass
class SyncResult:
    created: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    updated: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, model_name: str, was_created: bool) -> None:
        bucket = self.created if was_created else self.updated
        bucket[model_name] += 1

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            "created": dict(self.created),
            "updated": dict(self.updated),
        }


def sync_one_c_dataset(provider: OneCProvider, provider_name: str = "mock_1c") -> SyncRun:
    run = SyncRun.objects.create(provider=provider_name, status=SyncStatus.SUCCESS)
    result = SyncResult()
    now = timezone.now()

    try:
        dataset = provider.load_dataset()
        with transaction.atomic():
            currencies = _sync_currencies(dataset.currencies, result, now)
            organizations = _sync_organizations(dataset.organizations, result, now)
            _sync_departments(dataset.departments, result, now)
            articles = _sync_cash_flow_articles(dataset.cash_flow_articles, result, now)
            counterparties = _sync_counterparties(dataset.counterparties, result, now)
            _sync_nomenclature(dataset.nomenclature, result, now)
            contracts = _sync_contracts(dataset.contracts, result, now, counterparties, currencies)
            _sync_additional_agreements(dataset.additional_agreements, result, now, contracts, currencies)
            _sync_payment_facts(
                dataset.payment_facts,
                result,
                now,
                organizations,
                articles,
                counterparties,
                contracts,
                currencies,
            )

        run.stats = result.as_dict()
        run.message = "Mock-данные 1С синхронизированы"
    except Exception as exc:
        run.status = SyncStatus.FAILED
        run.message = str(exc)
        raise
    finally:
        run.finished_at = timezone.now()
        run.save(update_fields=["status", "finished_at", "message", "stats"])

    return run


def _sync_currencies(items, result: SyncResult, now):
    objects = {}
    for item in items:
        obj, created = Currency.objects.update_or_create(
            code=item.code,
            defaults={
                "external_id": item.external_id,
                "name": item.name,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.code] = obj
        result.add("currencies", created)
    return objects


def _sync_organizations(items, result: SyncResult, now):
    objects = {}
    for item in items:
        obj, created = Organization.objects.update_or_create(
            external_id=item.external_id,
            defaults={
                "name": item.name,
                "inn": item.inn,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("organizations", created)
    return objects


def _sync_departments(items, result: SyncResult, now):
    objects = {}
    for item in items:
        obj, created = Department.objects.update_or_create(
            code=item.code,
            defaults={
                "external_id": item.external_id,
                "name": item.name,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("departments", created)
    return objects


def _sync_cash_flow_articles(items, result: SyncResult, now):
    objects = {}
    for item in items:
        obj, created = CashFlowArticle.objects.update_or_create(
            code=item.code,
            defaults={
                "external_id": item.external_id,
                "name": item.name,
                "exists_in_one_c": item.exists_in_one_c,
                "is_internal_turnover": item.is_internal_turnover,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("cash_flow_articles", created)
    return objects


def _sync_counterparties(items, result: SyncResult, now):
    objects = {}
    for item in items:
        obj, created = Counterparty.objects.update_or_create(
            external_id=item.external_id,
            defaults={
                "name": item.name,
                "inn": item.inn,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("counterparties", created)
    return objects


def _sync_nomenclature(items, result: SyncResult, now):
    objects = {}
    for item in items:
        obj, created = Nomenclature.objects.update_or_create(
            code=item.code,
            defaults={
                "external_id": item.external_id,
                "name": item.name,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("nomenclature", created)
    return objects


def _sync_contracts(items, result: SyncResult, now, counterparties, currencies):
    objects = {}

    for item in items:
        obj, created = Contract.objects.update_or_create(
            external_id=item.external_id,
            defaults={
                "number": item.number,
                "date": item.date,
                "name": item.name,
                "kind": item.kind,
                "counterparty": counterparties[item.counterparty_external_id],
                "currency": currencies[item.currency_code],
                "amount": item.amount,
                "manual_exchange_rate": item.manual_exchange_rate,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("contracts", created)

    for item in items:
        if item.parent_customer_contract_external_id:
            contract = objects[item.external_id]
            contract.parent_customer_contract = objects[item.parent_customer_contract_external_id]
            contract.save(update_fields=["parent_customer_contract"])

    return objects


def _sync_additional_agreements(items, result: SyncResult, now, contracts, currencies):
    objects = {}
    for item in items:
        obj, created = AdditionalAgreement.objects.update_or_create(
            external_id=item.external_id,
            defaults={
                "contract": contracts[item.contract_external_id],
                "number": item.number,
                "date": item.date,
                "amount": item.amount,
                "currency": currencies[item.currency_code],
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[item.external_id] = obj
        result.add("additional_agreements", created)
    return objects


def _sync_payment_facts(items, result: SyncResult, now, organizations, articles, counterparties, contracts, currencies):
    objects = {}
    for item in items:
        obj, created = PaymentFact.objects.update_or_create(
            external_id=item.external_id,
            accounting_kind=item.accounting_kind,
            defaults={
                "organization": organizations[item.organization_external_id],
                "date": item.date,
                "account": item.account,
                "direction": item.direction,
                "article": articles[item.article_external_id],
                "counterparty": counterparties[item.counterparty_external_id],
                "contract": contracts.get(item.contract_external_id) if item.contract_external_id else None,
                "amount": item.amount,
                "currency": currencies[item.currency_code],
                "comment": item.comment,
                "source_system": SourceSystem.MOCK_ONE_C,
                "synced_at": now,
            },
        )
        objects[f"{item.external_id}:{item.accounting_kind}"] = obj
        result.add("payment_facts", created)
    return objects
