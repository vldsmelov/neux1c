"""Backfill: create a baseline PlanningScenario per (organization, year) for
every existing BudgetPlan / BudgetLimitPlan, and attach the documents to it.
"""

from django.db import migrations


def _ensure_baseline(Scenario, organization_id, year):
    scenario = Scenario.objects.filter(
        organization_id=organization_id,
        year=year,
        is_baseline=True,
    ).first()
    if scenario is not None:
        return scenario
    scenario, _ = Scenario.objects.get_or_create(
        organization_id=organization_id,
        year=year,
        name="Базовый",
        defaults={"kind": "base", "is_baseline": True},
    )
    if not scenario.is_baseline:
        scenario.is_baseline = True
        scenario.save(update_fields=["is_baseline"])
    return scenario


def forwards(apps, schema_editor):
    Scenario = apps.get_model("core", "PlanningScenario")
    BudgetPlan = apps.get_model("core", "BudgetPlan")
    BudgetLimitPlan = apps.get_model("core", "BudgetLimitPlan")

    for plan in BudgetPlan.objects.filter(scenario__isnull=True):
        plan.scenario = _ensure_baseline(Scenario, plan.organization_id, plan.budget_year)
        plan.save(update_fields=["scenario"])

    for limit in BudgetLimitPlan.objects.filter(scenario__isnull=True):
        limit.scenario = _ensure_baseline(Scenario, limit.organization_id, limit.planning_year)
        limit.save(update_fields=["scenario"])


def backwards(apps, schema_editor):
    Scenario = apps.get_model("core", "PlanningScenario")
    Scenario.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_planningscenario_budgetlimitplan_scenario_and_more"),
    ]
    operations = [migrations.RunPython(forwards, backwards)]
