from django.contrib import admin

from .models import (
    AdditionalAgreement,
    AuditLog,
    BudgetLimitAdjustment,
    BudgetLimitAdjustmentMonth,
    BudgetLimitMonth,
    BudgetLimitPlan,
    CashFlowArticle,
    Contract,
    Counterparty,
    Currency,
    Department,
    ExternalPaymentDocument,
    Nomenclature,
    Organization,
    PaymentFact,
    PaymentRequest,
    PaymentRequestControlSettings,
    SyncRun,
    UiThemeSettings,
    UserProfile,
)


class IntegrationTrackedAdmin(admin.ModelAdmin):
    list_display = ("__str__", "source_system", "external_id", "synced_at")
    list_filter = ("source_system",)


@admin.register(Currency)
class CurrencyAdmin(IntegrationTrackedAdmin):
    search_fields = ("code", "name", "external_id")


@admin.register(Organization)
class OrganizationAdmin(IntegrationTrackedAdmin):
    search_fields = ("name", "inn", "external_id")


@admin.register(Department)
class DepartmentAdmin(IntegrationTrackedAdmin):
    search_fields = ("code", "name", "external_id")


@admin.register(CashFlowArticle)
class CashFlowArticleAdmin(IntegrationTrackedAdmin):
    search_fields = ("code", "name", "external_id")
    list_filter = ("source_system", "exists_in_one_c", "is_internal_turnover")


@admin.register(Counterparty)
class CounterpartyAdmin(IntegrationTrackedAdmin):
    search_fields = ("name", "inn", "external_id")


@admin.register(Nomenclature)
class NomenclatureAdmin(IntegrationTrackedAdmin):
    search_fields = ("code", "name", "external_id")


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "kind", "counterparty", "amount", "currency", "source_system")
    list_filter = ("kind", "source_system", "currency")
    search_fields = ("number", "name", "counterparty__name", "external_id")


@admin.register(AdditionalAgreement)
class AdditionalAgreementAdmin(admin.ModelAdmin):
    list_display = ("number", "date", "contract", "amount", "currency", "source_system")
    list_filter = ("source_system", "currency")
    search_fields = ("number", "contract__number", "external_id")


@admin.register(PaymentFact)
class PaymentFactAdmin(admin.ModelAdmin):
    list_display = ("date", "account", "direction", "accounting_kind", "article", "counterparty", "amount", "currency")
    list_filter = ("account", "direction", "accounting_kind", "source_system")
    search_fields = ("external_id", "article__name", "counterparty__name", "contract__number")


@admin.register(ExternalPaymentDocument)
class ExternalPaymentDocumentAdmin(admin.ModelAdmin):
    list_display = ("number", "payment_date", "contract", "accountant", "amount", "currency", "status")
    list_filter = ("status", "currency", "payment_date")
    search_fields = ("number", "contract__number", "contract__counterparty__name", "accountant__username")
    readonly_fields = ("payment_fact_bu", "payment_fact_nu", "created_at")


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "request_date",
        "request_kind",
        "counterparty",
        "amount",
        "currency",
        "status",
        "limit_exceeded",
        "approver",
    )
    list_filter = ("request_kind", "status", "currency", "limit_exceeded")
    search_fields = ("number", "invoice_number", "counterparty__name", "contract__number", "do_external_id")
    readonly_fields = ("amount_rub", "limit_remaining_before_rub", "limit_remaining_after_rub", "approved_at", "transferred_at")


@admin.register(PaymentRequestControlSettings)
class PaymentRequestControlSettingsAdmin(admin.ModelAdmin):
    list_display = ("name", "control_mode", "is_active", "updated_at")
    list_editable = ("control_mode", "is_active")


class BudgetLimitMonthInline(admin.TabularInline):
    model = BudgetLimitMonth
    extra = 0


@admin.register(BudgetLimitPlan)
class BudgetLimitPlanAdmin(admin.ModelAdmin):
    list_display = ("number", "planning_year", "department", "article", "annual_amount", "currency", "status", "version")
    list_filter = ("status", "planning_year", "department", "currency")
    search_fields = ("number", "article__name", "department__name", "author__username", "approver__username")
    inlines = [BudgetLimitMonthInline]


class BudgetLimitAdjustmentMonthInline(admin.TabularInline):
    model = BudgetLimitAdjustmentMonth
    extra = 0


@admin.register(BudgetLimitAdjustment)
class BudgetLimitAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("number", "base_plan", "new_annual_amount", "status", "version", "approver", "approved_at")
    list_filter = ("status", "version", "approver")
    search_fields = ("number", "base_plan__number", "reason", "author__username", "approver__username")
    inlines = [BudgetLimitAdjustmentMonthInline]


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("provider", "status", "started_at", "finished_at")
    list_filter = ("provider", "status")
    readonly_fields = ("provider", "status", "started_at", "finished_at", "message", "stats")


@admin.register(UiThemeSettings)
class UiThemeSettingsAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "primary_color", "background_color", "surface_color", "updated_at")
    list_editable = ("is_active",)
    fieldsets = (
        ("Основное", {"fields": ("name", "is_active")}),
        (
            "Палитра",
            {
                "fields": (
                    "primary_color",
                    "primary_hover",
                    "primary_soft",
                    "background_color",
                    "surface_color",
                    "sidebar_color",
                    "border_color",
                    "text_color",
                    "muted_text_color",
                    "warning_color",
                    "danger_color",
                )
            },
        ),
    )


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "department", "is_app_access_enabled", "updated_at")
    list_filter = ("role", "is_app_access_enabled", "department")
    search_fields = ("user__username", "user__email")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "path", "method", "status_code")
    list_filter = ("action", "method", "status_code")
    search_fields = ("user__username", "path", "message", "object_type", "object_id")
    readonly_fields = (
        "user",
        "action",
        "path",
        "method",
        "status_code",
        "object_type",
        "object_id",
        "message",
        "ip_address",
        "user_agent",
        "created_at",
    )
