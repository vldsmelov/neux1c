"""Public model API for the `core` app.

Domain-driven split of the historical monolithic ``core/models.py``. Import
order below follows the dependency direction (enums → nsi → contracts →
payment_facts/budgets → payment_requests/external → system) so foreign keys
resolve without circular imports. External callers keep using
``from core.models import X`` unchanged, and migrations keep referencing
``core.<Model>`` because every model still lives in the ``core`` app.
"""

from .enums import (  # noqa: F401
    AccountingKind,
    AuditAction,
    BudgetPeriodicity,
    CashFlowDirection,
    BudgetPlanStatus,
    BudgetScope,
    ContractKind,
    ExternalPaymentStatus,
    IntegrationRequestStatus,
    IntegrationTrackedModel,
    NotificationKind,
    PaymentDirection,
    PaymentLimitControlMode,
    PaymentRequestKind,
    PaymentRequestStatus,
    PlanningScenarioKind,
    ReportTemplateType,
    SourceSystem,
    SyncStatus,
    UiThemeMode,
    UserRole,
    hex_color_validator,
)
from .nsi import (  # noqa: F401
    CashFlowArticle,
    Counterparty,
    Currency,
    Department,
    Nomenclature,
    Organization,
)
from .contracts import AdditionalAgreement, Contract  # noqa: F401
from .payment_facts import (  # noqa: F401
    PaymentFact,
    PaymentFactAdjustment,
    PaymentFactControlSettings,
)
from .budgets import (  # noqa: F401
    BudgetDepartmentAllocation,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    BudgetLimitMonth,
    BudgetLimitPlan,
    BudgetPlan,
    PlanningScenario,
)
from .payment_requests import PaymentRequest, PaymentRequestControlSettings  # noqa: F401
from .external import ExternalPaymentDocument  # noqa: F401
from .system import (  # noqa: F401
    AuditLog,
    DocumentSequence,
    IntegrationRequest,
    Notification,
    ReportTemplate,
    SyncRun,
    UiThemeSettings,
    UserProfile,
)
