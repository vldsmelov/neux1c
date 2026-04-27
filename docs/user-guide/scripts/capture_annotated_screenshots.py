from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright


BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("/app/docs/user-guide/images")
RAW_DIR = OUTPUT_DIR / "raw"
ANNOTATED_DIR = OUTPUT_DIR / "annotated"


ROLE_CREDENTIALS = {
    "admin": ("admin", "demo12345"),
    "economist": ("economist", "demo12345"),
    "manager": ("manager", "demo12345"),
    "accountant": ("accountant", "demo12345"),
}


SCREENSHOTS = [
    {
        "name": "01_login",
        "role": None,
        "path": "/login/",
        "notes": [
            ("input[name='username']", "Введите логин роли"),
            ("input[name='password']", "Введите пароль"),
            ("button[type='submit']", "Нажмите Войти"),
        ],
    },
    {
        "name": "02_economist_planning_limits",
        "role": "economist",
        "path": "/planning/limits/",
        "notes": [
            ("#planning_year", "Выберите год планирования"),
            ("#annual_amount", "Укажите годовую сумму"),
            ("button[form='create-plan-form']", "Сохраните план"),
            ("form:has(input[name='action'][value='submit']) button[type='submit']", "Отправьте черновик на утверждение"),
        ],
    },
    {
        "name": "03_economist_payment_requests",
        "role": "economist",
        "path": "/payments/requests/",
        "notes": [
            ("#request_kind", "Выберите тип заявки"),
            ("#contract_id", "Укажите договор"),
            ("#payment_purpose", "Заполните назначение платежа"),
            ("button[form='create-request-form']", "Сохраните заявку"),
            ("form:has(input[name='action'][value='submit']) button[type='submit']", "Отправьте на согласование"),
        ],
    },
    {
        "name": "04_manager_payment_approval",
        "role": "manager",
        "path": "/payments/requests/",
        "notes": [
            ("form:has(input[name='action'][value='approve']) button[type='submit']", "Согласуйте заявку"),
            ("form:has(input[name='action'][value='reject']) input[name='approver_comment']", "Введите причину отклонения"),
            ("form:has(input[name='action'][value='reject']) button[type='submit']", "Отклоните при необходимости"),
        ],
    },
    {
        "name": "05_manager_plan_approval",
        "role": "manager",
        "path": "/planning/limits/",
        "notes": [
            ("form:has(input[name='action'][value='approve']) button[type='submit']", "Утвердите план или корректировку"),
        ],
    },
    {
        "name": "06_economist_payment_facts_adjust",
        "role": "economist",
        "path": "/payments/facts/",
        "notes": [
            ("form:has(input[name='action'][value='adjust']) input[name='new_amount']", "Измените сумму факта"),
            ("form:has(input[name='action'][value='adjust']) input[name='reason']", "Укажите причину изменения"),
            ("form:has(input[name='action'][value='adjust']) button[type='submit']", "Сохраните корректировку"),
        ],
    },
    {
        "name": "07_economist_report_templates",
        "role": "economist",
        "path": "/reports/plan-fact/",
        "notes": [
            ("#year", "Настройте фильтры отчета"),
            ("#template_name", "Задайте имя шаблона"),
            ("form:has(input[name='action'][value='save_template']) button[type='submit']", "Сохраните шаблон"),
            ("#template_id_apply", "Выберите шаблон для применения"),
            ("form:has(input[name='action'][value='apply_template']) button[type='submit']", "Примените шаблон"),
        ],
    },
    {
        "name": "08_manager_dashboard",
        "role": "manager",
        "path": "/reports/manager/",
        "notes": [
            (".metric-strip .metric-cell:nth-child(1)", "Контролируйте сводные KPI"),
            ("table.data-table tbody tr:first-child", "Проверьте статьи с риском перерасхода"),
        ],
    },
    {
        "name": "09_accountant_external_system",
        "role": "accountant",
        "path": "/external/accounting/",
        "notes": [
            ("form.inline-form-row input[name='amount']", "Укажите сумму оплаты"),
            ("form.inline-form-row button[type='submit']", "Проведите оплату"),
            (".app-title", "Внешний контур доступен только бухгалтеру"),
        ],
    },
    {
        "name": "10_admin_integration_requests",
        "role": "admin",
        "path": "/settings/integration-requests/",
        "notes": [
            ("#integration_name", "Введите название интеграции"),
            ("#target_system", "Укажите целевую систему"),
            ("#description", "Опишите задачу"),
            ("form#integration-request-form button[type='submit']", "Создайте заявку"),
            ("form:has(input[name='action'][value='set_status']) select[name='status']", "Измените статус заявки"),
            ("form:has(input[name='action'][value='set_status']) button[type='submit']", "Сохраните решение"),
        ],
    },
]


def login(page, username: str, password: str) -> None:
    page.goto(f"{BASE_URL}/login/", wait_until="networkidle")
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", password)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle")


def collect_boxes(page, notes):
    items = []
    for selector, label in notes:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            box = locator.bounding_box()
            if not box:
                continue
            items.append(
                {
                    "selector": selector,
                    "label": label,
                    "x": box["x"],
                    "y": box["y"],
                    "w": box["width"],
                    "h": box["height"],
                }
            )
        except Exception:
            continue
    return items


def annotate_image(raw_path: Path, target_path: Path, boxes) -> None:
    image = Image.open(raw_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
    try:
        font = ImageFont.truetype(font_path, 16)
        badge_font = ImageFont.truetype(font_path, 15)
    except Exception:
        font = ImageFont.load_default()
        badge_font = ImageFont.load_default()

    for index, box in enumerate(boxes, start=1):
        x0 = int(box["x"])
        y0 = int(box["y"])
        x1 = int(box["x"] + box["w"])
        y1 = int(box["y"] + box["h"])

        draw.rectangle([x0, y0, x1, y1], outline=(18, 108, 255), width=4)

        badge_x0 = max(4, x0 - 10)
        badge_y0 = max(4, y0 - 24)
        badge_x1 = badge_x0 + 28
        badge_y1 = badge_y0 + 20
        draw.rounded_rectangle([badge_x0, badge_y0, badge_x1, badge_y1], radius=6, fill=(18, 108, 255))
        draw.text((badge_x0 + 9, badge_y0 + 3), str(index), fill=(255, 255, 255), font=badge_font)

        text = f"{index}. {box['label']}"
        text_x = min(image.width - 400, x0 + 8)
        text_y = min(image.height - 28, y1 + 4)
        draw.rounded_rectangle([text_x, text_y, text_x + 388, text_y + 22], radius=6, fill=(255, 255, 255))
        draw.text((text_x + 7, text_y + 3), text, fill=(26, 43, 71), font=font)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(target_path)


def main():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        contexts = {}
        pages = {}

        for role, creds in ROLE_CREDENTIALS.items():
            context = browser.new_context(viewport={"width": 1720, "height": 980})
            page = context.new_page()
            login(page, creds[0], creds[1])
            contexts[role] = context
            pages[role] = page

        for shot in SCREENSHOTS:
            role = shot["role"]
            if role is None:
                context = browser.new_context(viewport={"width": 1720, "height": 980})
                page = context.new_page()
            else:
                context = contexts[role]
                page = pages[role]

            page.goto(f"{BASE_URL}{shot['path']}", wait_until="networkidle")
            page.wait_for_timeout(600)

            raw_path = RAW_DIR / f"{shot['name']}.png"
            annotated_path = ANNOTATED_DIR / f"{shot['name']}.png"
            page.screenshot(path=str(raw_path), full_page=True)
            boxes = collect_boxes(page, shot["notes"])
            annotate_image(raw_path, annotated_path, boxes)

            if role is None:
                context.close()

        for role in contexts:
            contexts[role].close()
        browser.close()

    print(f"Done. Annotated screenshots are in: {ANNOTATED_DIR}")


if __name__ == "__main__":
    main()
