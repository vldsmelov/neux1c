"""Driver-based planning: вычисление плана как функции нескольких драйверов.

Пример формулы: Revenue = Volume × Price × Seasonality
Каждый драйвер хранит 12 помесячных значений; перемножение / сложение
делается помесячно — на каждый месяц получаем результирующее значение
плана для этого периода.

Поддерживаемые операции: multiply, add, subtract.
"""

from dataclasses import dataclass
from decimal import Decimal

from core.models import PlanDriver


@dataclass
class DriverFormulaResult:
    monthly_values: list[Decimal]  # 12 чисел
    annual_total: Decimal
    annual_average: Decimal
    driver_summaries: list[dict]


def compute_formula(driver_ids_with_ops: list[dict]) -> DriverFormulaResult:
    """Вычисляет план по формуле из нескольких драйверов.

    driver_ids_with_ops — список словарей вида:
    [{"driver_id": 1, "op": "multiply"}, {"driver_id": 2, "op": "multiply"}, ...]

    Первый элемент задаёт начальные значения; последующие применяют op
    помесячно (multiply / add / subtract).
    """
    if not driver_ids_with_ops:
        return DriverFormulaResult(
            monthly_values=[Decimal("0")] * 12,
            annual_total=Decimal("0"),
            annual_average=Decimal("0"),
            driver_summaries=[],
        )

    driver_ids = [d["driver_id"] for d in driver_ids_with_ops if "driver_id" in d]
    drivers_by_id = {d.id: d for d in PlanDriver.objects.filter(id__in=driver_ids)}

    monthly = [Decimal("0")] * 12
    summaries: list[dict] = []

    for i, step in enumerate(driver_ids_with_ops):
        driver = drivers_by_id.get(step["driver_id"])
        if not driver:
            continue
        values = _normalize_values(driver.monthly_values)
        op = step.get("op", "multiply")
        if i == 0:
            monthly = list(values)
        else:
            new_monthly = []
            for cur, val in zip(monthly, values):
                if op == "multiply":
                    new_monthly.append(cur * val)
                elif op == "add":
                    new_monthly.append(cur + val)
                elif op == "subtract":
                    new_monthly.append(cur - val)
                else:
                    new_monthly.append(cur)
            monthly = new_monthly
        summaries.append({
            "driver": driver,
            "op": op,
            "annual_total": sum(values, Decimal("0")),
        })

    # Округляем до 2 знаков для отображения
    quantized = [v.quantize(Decimal("0.01")) for v in monthly]
    total = sum(quantized, Decimal("0"))
    avg = (total / Decimal(12)).quantize(Decimal("0.01")) if quantized else Decimal("0")

    return DriverFormulaResult(
        monthly_values=quantized,
        annual_total=total.quantize(Decimal("0.01")),
        annual_average=avg,
        driver_summaries=summaries,
    )


def _normalize_values(raw) -> list[Decimal]:
    """Приводит monthly_values к list[Decimal] из 12 чисел."""
    if not isinstance(raw, list):
        return [Decimal("0")] * 12
    out = []
    for v in raw[:12]:
        try:
            out.append(Decimal(str(v)))
        except Exception:
            out.append(Decimal("0"))
    while len(out) < 12:
        out.append(Decimal("0"))
    return out
