#!/usr/bin/env python3
"""Минимальный компилятор .po → .mo (формат GNU gettext).

Используется когда msgfmt из gettext недоступен в среде. Реализует
только то, что нужно Django: оригинал → перевод, без plural форм.

Usage: python scripts/compile_po.py locale/en/LC_MESSAGES/django.po
"""

import struct
import sys
from pathlib import Path


def parse_po(text: str) -> dict[str, str]:
    """Парсит .po-файл, возвращает {msgid: msgstr}."""
    entries: dict[str, str] = {}
    current_id = None
    current_str = None
    mode = None  # 'id' or 'str'

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            # Сохраняем накопленную пару при переходе через blank/comment
            if current_id is not None and current_str is not None:
                entries[current_id] = current_str
                current_id = None
                current_str = None
                mode = None
            continue
        if line.startswith("msgid "):
            if current_id is not None and current_str is not None:
                entries[current_id] = current_str
            current_id = _unquote(line[len("msgid "):])
            current_str = None
            mode = "id"
        elif line.startswith("msgstr "):
            current_str = _unquote(line[len("msgstr "):])
            mode = "str"
        elif line.startswith('"') and line.endswith('"'):
            # Продолжение строки
            chunk = _unquote(line)
            if mode == "id" and current_id is not None:
                current_id += chunk
            elif mode == "str" and current_str is not None:
                current_str += chunk

    # Финальная пара
    if current_id is not None and current_str is not None:
        entries[current_id] = current_str

    return entries


def _unquote(s: str) -> str:
    """Снимает кавычки и обрабатывает escape-последовательности
    (\\n, \\t, \\\", \\\\), не ломая UTF-8."""
    if not (s.startswith('"') and s.endswith('"')):
        return s
    inner = s[1:-1]
    # Обрабатываем escape вручную, чтобы не ломать UTF-8 через unicode_escape
    result = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt == "n":
                result.append("\n")
            elif nxt == "t":
                result.append("\t")
            elif nxt == "r":
                result.append("\r")
            elif nxt == '"':
                result.append('"')
            elif nxt == "\\":
                result.append("\\")
            else:
                result.append(c + nxt)
            i += 2
        else:
            result.append(c)
            i += 1
    return "".join(result)


def compile_mo(entries: dict[str, str], output_path: Path) -> None:
    """Генерирует .mo файл из словаря переводов.

    Формат:
      magic uint32 = 0x950412de
      version uint32 = 0
      num uint32
      orig_table_offset uint32
      trans_table_offset uint32
      hash_size uint32 = 0
      hash_offset uint32 = 0
      orig_table: (len, offset) pairs
      trans_table: (len, offset) pairs
      strings (null-terminated)
    """
    # Включаем пустую строку с metadata-заголовком — стандарт gettext
    if "" not in entries:
        entries[""] = (
            "Content-Type: text/plain; charset=UTF-8\n"
            "MIME-Version: 1.0\n"
            "Content-Transfer-Encoding: 8bit\n"
        )

    keys = sorted(entries.keys())
    n = len(keys)
    orig_bytes = [k.encode("utf-8") for k in keys]
    trans_bytes = [entries[k].encode("utf-8") for k in keys]

    # Header is 7 uint32 = 28 bytes; tables follow
    header_size = 28
    orig_table_offset = header_size
    trans_table_offset = orig_table_offset + 8 * n
    strings_offset = trans_table_offset + 8 * n

    orig_offsets: list[tuple[int, int]] = []
    trans_offsets: list[tuple[int, int]] = []
    strings_blob = b""
    cur = strings_offset
    for raw in orig_bytes:
        orig_offsets.append((len(raw), cur))
        strings_blob += raw + b"\x00"
        cur += len(raw) + 1
    for raw in trans_bytes:
        trans_offsets.append((len(raw), cur))
        strings_blob += raw + b"\x00"
        cur += len(raw) + 1

    out = struct.pack(
        "<IIIIIII",
        0x950412DE,  # magic
        0,           # version
        n,
        orig_table_offset,
        trans_table_offset,
        0,           # hash_size
        0,           # hash_offset
    )
    for length, offset in orig_offsets:
        out += struct.pack("<II", length, offset)
    for length, offset in trans_offsets:
        out += struct.pack("<II", length, offset)
    out += strings_blob

    output_path.write_bytes(out)


def main():
    if len(sys.argv) < 2:
        print("Usage: compile_po.py path/to/django.po")
        sys.exit(1)
    po_path = Path(sys.argv[1])
    if not po_path.exists():
        print(f"File not found: {po_path}")
        sys.exit(1)
    mo_path = po_path.with_suffix(".mo")
    text = po_path.read_text(encoding="utf-8")
    entries = parse_po(text)
    print(f"Parsed {len(entries)} entries from {po_path}")
    compile_mo(entries, mo_path)
    print(f"Wrote {mo_path} ({mo_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
