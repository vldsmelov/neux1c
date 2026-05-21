"""User guide, instruction assets, healthcheck and design styleguide.

These views don't depend on the domain workflow — they serve static-ish content
(markdown rendering, image assets, health probe, design-system reference).
"""

import html
import re
from mimetypes import guess_type
from pathlib import Path

from django.db import connection
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.safestring import mark_safe

from ..access import role_required
from ..models import UserRole


BASE_DIR = Path(__file__).resolve().parent.parent.parent
USER_GUIDE_DIR = BASE_DIR / "docs" / "user-guide"


@login_required
def instruction(request):
    guide_path = USER_GUIDE_DIR / "instruction.md"
    try:
        markdown_text = guide_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Http404("Руководство не найдено") from exc

    return render(
        request,
        "core/instruction.html",
        {
            "active_section": "instruction",
            "guide_html": mark_safe(_render_user_guide_markdown(markdown_text)),
        },
    )


@login_required
def instruction_asset(request, asset_path: str):
    asset_root = USER_GUIDE_DIR.resolve()
    requested_path = Path(asset_path)
    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise Http404("Некорректный путь")

    resolved_path = (USER_GUIDE_DIR / requested_path).resolve()
    if asset_root not in resolved_path.parents:
        raise Http404("Файл не найден")
    if not resolved_path.is_file():
        raise Http404("Файл не найден")

    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if resolved_path.suffix.lower() not in allowed_suffixes:
        raise Http404("Тип файла не поддерживается")

    content_type = guess_type(str(resolved_path))[0] or "application/octet-stream"
    return FileResponse(resolved_path.open("rb"), content_type=content_type)


def healthz(request):
    database = "ok"
    try:
        connection.ensure_connection()
    except Exception:
        database = "error"
    status = 200 if database == "ok" else 503
    return JsonResponse(
        {"status": "ok" if status == 200 else "error", "database": database},
        status=status,
    )


@role_required(UserRole.ADMINISTRATOR)
def styleguide(request):
    """Internal visual reference for the design system. Admin only."""
    return render(
        request,
        "core/styleguide.html",
        {
            "active_section": "settings",
            "swatches": [
                ("Accent",        "var(--color-accent)",        "--color-accent"),
                ("Accent hover",  "var(--color-accent-hover)",  "--color-accent-hover"),
                ("Accent soft",   "var(--color-accent-soft)",   "--color-accent-soft"),
                ("Surface",       "var(--color-surface)",       "--color-surface"),
                ("Background",    "var(--color-bg)",            "--color-bg"),
                ("Border",        "var(--color-border)",        "--color-border"),
                ("Text",          "var(--color-text)",          "--color-text"),
                ("Text muted",    "var(--color-text-muted)",    "--color-text-muted"),
                ("Success",       "var(--color-success)",       "--color-success"),
                ("Warning",       "var(--color-warning)",       "--color-warning"),
                ("Danger",        "var(--color-danger)",        "--color-danger"),
            ],
        },
    )


# --- Minimal markdown renderer used by the instruction page ---------------

_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_ORDERED_LIST_PATTERN = re.compile(r"\d+\.\s+(.*)")
_UNORDERED_LIST_PATTERN = re.compile(r"-\s+(.*)")


def _render_user_guide_markdown(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    chunks = ['<article class="guide-content">']
    index = 0

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            index += 1
            continue

        if stripped.startswith("### "):
            chunks.append(f"<h3>{_render_inline_markdown(stripped[4:])}</h3>")
            index += 1
            continue
        if stripped.startswith("## "):
            chunks.append(f"<h2>{_render_inline_markdown(stripped[3:])}</h2>")
            index += 1
            continue
        if stripped.startswith("# "):
            chunks.append(f"<h1>{_render_inline_markdown(stripped[2:])}</h1>")
            index += 1
            continue
        if stripped == "---":
            chunks.append("<hr>")
            index += 1
            continue

        image_match = _IMAGE_PATTERN.fullmatch(stripped)
        if image_match:
            alt_text = _render_inline_markdown(image_match.group(1))
            image_src = image_match.group(2).strip()
            image_url = reverse("instruction_asset", kwargs={"asset_path": image_src})
            chunks.append(
                '<figure class="guide-figure">'
                f'<img src="{image_url}" alt="{html.escape(image_match.group(1))}" loading="lazy">'
                f"<figcaption>{alt_text}</figcaption>"
                "</figure>"
            )
            index += 1
            continue

        if _is_table_line(stripped):
            table_lines = []
            while index < len(lines) and _is_table_line(lines[index].strip()):
                table_lines.append(lines[index].strip())
                index += 1
            chunks.append(_render_markdown_table(table_lines))
            continue

        ordered_match = _ORDERED_LIST_PATTERN.match(stripped)
        if ordered_match:
            list_items = []
            while index < len(lines):
                current_line = lines[index].strip()
                match = _ORDERED_LIST_PATTERN.match(current_line)
                if not match:
                    break
                list_items.append(f"<li>{_render_inline_markdown(match.group(1))}</li>")
                index += 1
            chunks.append("<ol>" + "".join(list_items) + "</ol>")
            continue

        unordered_match = _UNORDERED_LIST_PATTERN.match(stripped)
        if unordered_match:
            list_items = []
            while index < len(lines):
                current_line = lines[index].strip()
                match = _UNORDERED_LIST_PATTERN.match(current_line)
                if not match:
                    break
                list_items.append(f"<li>{_render_inline_markdown(match.group(1))}</li>")
                index += 1
            chunks.append("<ul>" + "".join(list_items) + "</ul>")
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < len(lines):
            current_line = lines[index].strip()
            if not current_line:
                break
            if _starts_block(current_line):
                break
            paragraph_lines.append(current_line)
            index += 1
        paragraph = " ".join(paragraph_lines)
        chunks.append(f"<p>{_render_inline_markdown(paragraph)}</p>")

    chunks.append("</article>")
    return "".join(chunks)


def _render_markdown_table(table_lines: list[str]) -> str:
    if not table_lines:
        return ""

    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table_lines]
    if not rows:
        return ""

    separator_index = None
    if len(rows) > 1 and all(set(cell.replace(":", "").replace("-", "").strip()) == set() for cell in rows[1]):
        separator_index = 1

    headers = rows[0]
    data_rows = rows[2:] if separator_index is not None else rows[1:]

    head_html = "".join(f"<th>{_render_inline_markdown(header)}</th>" for header in headers)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_render_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
        for row in data_rows
    )
    return (
        '<div class="data-table-wrap guide-table-wrap"><table class="data-table guide-table">'
        f"<thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></div>"
    )


def _render_inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _is_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def _starts_block(line: str) -> bool:
    return (
        line.startswith("#")
        or line == "---"
        or bool(_IMAGE_PATTERN.fullmatch(line))
        or bool(_ORDERED_LIST_PATTERN.match(line))
        or bool(_UNORDERED_LIST_PATTERN.match(line))
        or _is_table_line(line)
    )
