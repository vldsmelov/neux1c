"""Public view API for the `core` app.

Domain-driven split of the historical monolithic ``core/views.py``. Each module
hosts a coherent slice of views; this ``__init__.py`` re-exports the public
surface so external callers (``config/urls.py``, tests) keep using
``from core.views import X`` regardless of where ``X`` actually lives.
"""

from .audit import audit_log  # noqa: F401
from .auth import RoleAwareLoginView, set_theme_mode, set_working_organization  # noqa: F401
from .contracts import contracts_register, contracts_reservations, contracts_tree  # noqa: F401
from .external import external_accounting, external_payment_wizard  # noqa: F401
from .instruction import healthz, instruction, instruction_asset, styleguide  # noqa: F401
from .integrations import (  # noqa: F401
    integration_request_status_wizard,
    integration_request_wizard,
    integration_requests,
)
from .notifications import notification_follow, notifications  # noqa: F401
from .nsi import counterparty_360, nsi_dashboard, nsi_directory  # noqa: F401
from .payments import (  # noqa: F401
    payment_fact_adjustment_wizard,
    payment_fact_history,
    payment_facts,
    payment_request_edit_wizard,
    payment_request_justification_download,
    payment_request_wizard,
    payment_requests,
)
from .planning import (  # noqa: F401
    budget_limit_history,
    limit_adjustment_wizard,
    planning_budgets,
    planning_limits,
    planning_wizard,
)
from .reports import executive_dashboard, manager_dashboard, plan_fact_report, profit_loss_report  # noqa: F401
from .funding import funding_case_detail, funding_case_print, funding_cases_index  # noqa: F401
from .exchange_rates import exchange_rates  # noqa: F401
from .period_close import period_close  # noqa: F401
from .scenarios import planning_scenarios  # noqa: F401
from .template_builder import planning_template_builder  # noqa: F401
from .workflow import document_action_wizard  # noqa: F401
from .workspace import global_search_view, workspace  # noqa: F401
