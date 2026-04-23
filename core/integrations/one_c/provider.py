from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class CurrencyDTO:
    external_id: str
    code: str
    name: str


@dataclass(frozen=True)
class OrganizationDTO:
    external_id: str
    name: str
    inn: str


@dataclass(frozen=True)
class DepartmentDTO:
    external_id: str
    code: str
    name: str


@dataclass(frozen=True)
class CashFlowArticleDTO:
    external_id: str
    code: str
    name: str
    exists_in_one_c: bool = True
    is_internal_turnover: bool = False


@dataclass(frozen=True)
class CounterpartyDTO:
    external_id: str
    name: str
    inn: str


@dataclass(frozen=True)
class NomenclatureDTO:
    external_id: str
    code: str
    name: str


@dataclass(frozen=True)
class ContractDTO:
    external_id: str
    number: str
    date: date
    name: str
    kind: str
    counterparty_external_id: str
    currency_code: str
    amount: Decimal
    manual_exchange_rate: Decimal = Decimal("1")
    parent_customer_contract_external_id: str | None = None


@dataclass(frozen=True)
class AdditionalAgreementDTO:
    external_id: str
    contract_external_id: str
    number: str
    date: date
    amount: Decimal
    currency_code: str


@dataclass(frozen=True)
class PaymentFactDTO:
    external_id: str
    organization_external_id: str
    date: date
    account: str
    direction: str
    accounting_kind: str
    article_external_id: str
    counterparty_external_id: str
    amount: Decimal
    currency_code: str
    contract_external_id: str | None = None
    comment: str = ""


@dataclass(frozen=True)
class OneCDataset:
    currencies: list[CurrencyDTO]
    organizations: list[OrganizationDTO]
    departments: list[DepartmentDTO]
    cash_flow_articles: list[CashFlowArticleDTO]
    counterparties: list[CounterpartyDTO]
    nomenclature: list[NomenclatureDTO]
    contracts: list[ContractDTO]
    additional_agreements: list[AdditionalAgreementDTO]
    payment_facts: list[PaymentFactDTO]


class OneCProvider(Protocol):
    def load_dataset(self) -> OneCDataset:
        """Return an immutable snapshot of data received from 1C or its mock."""
