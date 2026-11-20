import csv
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz
import openpyxl

from schemas import RFQ, RFQLine


_UNIT_ALIASES = {
    "ea": "EA",
    "each": "EA",
    "pc": "EA",
    "pcs": "EA",
    "piece": "EA",
    "pieces": "EA",
    "pr": "PR",
    "pair": "PR",
    "pairs": "PR",
    "box": "BOX",
    "boxes": "BOX",
    "case": "CS",
    "cases": "CS",
    "cs": "CS",
    "pail": "PAIL",
    "pails": "PAIL",
    "gal": "GAL",
    "gallon": "GAL",
    "gallons": "GAL",
}
_UNIT_PATTERN = "|".join(sorted(_UNIT_ALIASES, key=len, reverse=True))
_QUANTITY = r"(\d+(?:\.\d+)?)"


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _customers() -> List[Dict[str, str]]:
    customer_file = Path(__file__).resolve().parent / "customers.csv"
    with customer_file.open(newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def _identify_customer(text: str) -> Optional[str]:
    normalized_text = _normalized(text)
    candidates = set()
    email_domains = re.findall(r"[\w.+-]+@([\w.-]+)", text.casefold())
    domain_names = {_normalized(domain.split(".")[0]) for domain in email_domains}

    for customer in _customers():
        name = _normalized(customer["name"])
        if name and (name in normalized_text or name in domain_names):
            candidates.add(customer["customer_id"])
    return next(iter(candidates)) if len(candidates) == 1 else None


def _rfq_id(text: str) -> Optional[str]:
    match = re.search(
        r"\b(?:RFQ|Bid)\s*(?:#|No\.?)\s*:?\s*([A-Z0-9][A-Z0-9-]+)",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _normalize_unit(value: Optional[str]) -> Optional[str]:
    return _UNIT_ALIASES.get(value.casefold()) if value else None


def _quantity_and_unit(text: str) -> Tuple[Optional[float], Optional[str], List[str]]:
    searchable = re.sub(r"\([^)]*\)", " ", text).strip()
    searchable = re.sub(r"^\s*[-*]\s*", "", searchable)
    quantity: Optional[float] = None
    unit: Optional[str] = None
    match = re.search(
        r"\bqty\.?\s*[:=]?\s*" + _QUANTITY + r"\s*(" + _UNIT_PATTERN + r")?\b",
        searchable,
        re.IGNORECASE,
    )
    if match:
        quantity = float(match.group(1))
        unit = _normalize_unit(match.group(2))

    if quantity is None:
        for match in re.finditer(
            r"\b" + _QUANTITY + r"\s*(" + _UNIT_PATTERN + r")\b",
            searchable,
            re.IGNORECASE,
        ):
            quantity = float(match.group(1))
            unit = _normalize_unit(match.group(2))

    if quantity is None:
        match = re.search(r"(?:\bx\s*|×\s*)" + _QUANTITY + r"\s*$", searchable, re.I)
        if match:
            quantity = float(match.group(1))

    if quantity is None:
        match = re.search(r"[-–]\s*" + _QUANTITY + r"\s*$", searchable)
        if match:
            quantity = float(match.group(1))

    if quantity is None:
        match = re.match(r"\s*" + _QUANTITY + r"\s+[A-Za-z]", searchable)
        if match:
            quantity = float(match.group(1))

    if unit is None:
        ambiguous_unit = re.search(
            r"\b(?:a few|several|some|a couple of)\s+(" + _UNIT_PATTERN + r")\b",
            searchable,
            re.IGNORECASE,
        )
        if ambiguous_unit:
            unit = _normalize_unit(ambiguous_unit.group(1))

    flags = []
    if quantity is None:
        flags.append("ambiguous_quantity" if re.search(
            r"\b(?:a few|several|some|a couple of)\b", searchable, re.IGNORECASE
        ) else "missing_quantity")
    if unit is None:
        flags.append("missing_unit")
    return quantity, unit, flags


def _make_line(
    line_number: int,
    as_written: str,
    quantity: Optional[float],
    unit: Optional[str],
    flags: Optional[List[str]] = None,
    note: Optional[str] = None,
) -> RFQLine:
    line_flags = list(flags or [])
    details = as_written.casefold()
    if re.search(r"\b(?:same as above|see above|as above|as previous)\b", details):
        line_flags.append("ambiguous_reference")
        note = _append_note(note, "The request refers to an earlier line; its details were not inferred.")
    if re.search(r"\bif available\b", details):
        line_flags.append("conditional")
    if re.search(r"\bwhatever you have\b|\bequivalent\b", details):
        line_flags.append("ambiguous_equivalent")
    if quantity is None and not any(
        flag in line_flags for flag in ("missing_quantity", "ambiguous_quantity", "stub")
    ):
        line_flags.append("missing_quantity")
    if unit is None and "missing_unit" not in line_flags and "stub" not in line_flags:
        line_flags.append("missing_unit")
    return RFQLine(
        line=line_number,
        as_written=as_written,
        quantity=quantity,
        unit=unit,
        flags=list(dict.fromkeys(line_flags)),
        note=note,
    )


def _append_note(current: Optional[str], addition: str) -> str:
    return "{}; {}".format(current, addition) if current else addition


def _text_body(text: str) -> str:
    lines = text.splitlines()
    first_body_line = 0
    if lines and any(
        line.casefold().startswith(("from:", "to:", "date:", "subject:"))
        for line in lines[:8]
    ):
        for index, line in enumerate(lines):
            if not line.strip():
                first_body_line = index + 1
                break
    return "\n".join(lines[first_body_line:])


def _text_item_strings(text: str) -> List[str]:
    lines = _text_body(text).splitlines()
    bulleted = [line.strip() for line in lines if re.match(r"^\s*[-*]\s+\S", line)]
    if bulleted:
        return bulleted

    candidates = []
    for line in lines:
        value = line.strip()
        if not value or re.match(r"^(?:from|to|date|subject):", value, re.I):
            continue
        if re.search(
            r"\b(?:make|bump|change|increase|reduce|set)\b.*\b(?:not|to)\b",
            value,
            re.I,
        ):
            continue
        searchable = re.sub(r"\([^)]*\)", " ", value).strip()
        has_quantity = re.search(
            r"\bqty\b|\bx\s*\d|\b\d+(?:\.\d+)?\s*(?:" + _UNIT_PATTERN + r")\b"
            r"|[-–]\s*\d+(?:\.\d+)?\s*$|\b(?:a few|several|some)\s+",
            searchable,
            re.I,
        )
        has_identifier = re.search(
            r"\b(?:[A-Z]{1,8}-\d[A-Z0-9-]*|\d{3,}(?:-[A-Z0-9]+)*)\b",
            searchable,
            re.I,
        )
        if has_quantity or has_identifier:
            candidates.append(value)
    return candidates


def _revision_target(item: str, target: str) -> bool:
    words = [word.casefold() for word in re.findall(r"[A-Za-z]+", target)]
    words = [word[:-1] if word.endswith("s") and len(word) > 3 else word for word in words]
    words = [word for word in words if word not in {"the", "a", "an", "to"}]
    item_words = re.findall(r"[a-z]+", item.casefold())
    return bool(words) and all(
        word in item_words or word + "s" in item_words for word in words
    )


def _apply_revisions(text: str, lines: List[RFQLine]) -> None:
    revisions = []
    for raw in text.splitlines():
        value = raw.strip()
        match = re.search(
            r"\b(?:make|change|set)\s+(?:the\s+)?(?P<target>[\w -]+?)\s+"
            r"(?P<new>[\w.'\"/-]+)\s+not\s+(?P<old>[\w.'\"/-]+)",
            value,
            re.IGNORECASE,
        )
        if match:
            revisions.append((match.group("target"), match.group(0), None))
        match = re.search(
            r"\bbump\s+(?:the\s+)?(?P<target>[\w -]+?)\s+to\s+"
            r"(?P<quantity>\d+(?:\.\d+)?)\b",
            value,
            re.IGNORECASE,
        )
        if match:
            revisions.append(
                (match.group("target"), match.group(0), float(match.group("quantity")))
            )

    for target, revision_text, quantity in revisions:
        matches = [
            line for line in lines if _revision_target(line.as_written, target)
        ]
        if len(matches) == 1:
            line = matches[0]
            if quantity is not None:
                line.quantity = quantity
            line.flags = list(dict.fromkeys(line.flags + ["revised"]))
            line.note = _append_note(line.note, "Forwarded revision: {}".format(revision_text))
        elif len(matches) > 1:
            for line in matches:
                line.flags = list(dict.fromkeys(line.flags + ["ambiguous_revision"]))
                line.note = _append_note(
                    line.note,
                    "A forwarded revision could refer to more than one line: {}".format(
                        revision_text
                    ),
                )


def _extract_text(text: str) -> List[RFQLine]:
    items = _text_item_strings(text)
    result = []
    for index, raw in enumerate(items, start=1):
        quantity, unit, flags = _quantity_and_unit(raw)
        result.append(_make_line(index, raw, quantity, unit, flags))
    _apply_revisions(text, result)
    return result


def _extract_pdf(path: Path) -> Tuple[str, List[RFQLine]]:
    with fitz.open(path) as document:
        text = "\n".join(page.get_text() for page in document)
    raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
    table_start = next(
        (index for index, value in enumerate(raw_lines) if value.casefold() == "line"),
        None,
    )
    result = []
    if table_start is not None:
        index = table_start + 1
        while index < len(raw_lines):
            if not re.fullmatch(r"\d+", raw_lines[index]):
                index += 1
                continue
            source_line = int(raw_lines[index])
            cells = raw_lines[index + 1:index + 5]
            if len(cells) < 4:
                break
            part_number, description, quantity_text, unit_text = cells
            if not re.fullmatch(r"\d+(?:\.\d+)?", quantity_text):
                index += 1
                continue
            as_written = "{} | {} | Qty {} {}".format(
                part_number, description, quantity_text, unit_text
            )
            result.append(
                _make_line(
                    source_line,
                    as_written,
                    float(quantity_text),
                    _normalize_unit(unit_text),
                )
            )
            index += 5
    return text, result


def _header_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold()) if value is not None else ""


def _extract_xlsx(path: Path) -> Tuple[str, List[RFQLine]]:
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    extracted_text = []
    result = []
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            header_row = None
            indices: Dict[str, int] = {}
            for row_number, row in enumerate(rows):
                normalized = [_header_name(value) for value in row]
                names = {
                    "item": {"item", "itemno", "itemnumber", "part", "partno"},
                    "description": {"desc", "description"},
                    "quantity": {"qty", "quantity"},
                    "unit": {"uom", "unit"},
                    "notes": {"note", "notes"},
                }
                found = {
                    key: next(
                        (index for index, value in enumerate(normalized) if value in aliases),
                        -1,
                    )
                    for key, aliases in names.items()
                }
                if found["description"] >= 0 and found["quantity"] >= 0 and found["unit"] >= 0:
                    header_row = row_number
                    indices = found
                    break
            if header_row is None:
                continue

            for row in rows:
                def cell(key: str) -> Any:
                    column = indices.get(key, -1)
                    return row[column] if column >= 0 and column < len(row) else None

                item, description = cell("item"), cell("description")
                quantity_value, unit_value, note_value = (
                    cell("quantity"),
                    cell("unit"),
                    cell("notes"),
                )
                if all(value is None or str(value).strip() == "" for value in row):
                    continue
                has_item = item is not None and str(item).strip() != ""
                if description is None and not has_item:
                    continue
                if not has_item and quantity_value is None:
                    continue

                parts = [str(value).strip() for value in (item, description) if value is not None]
                if quantity_value is not None:
                    parts.append("Qty {}".format(quantity_value))
                if unit_value is not None:
                    parts.append(str(unit_value).strip())
                if note_value is not None and str(note_value).strip():
                    parts.append(str(note_value).strip())
                as_written = " | ".join(parts)
                try:
                    quantity = float(quantity_value) if quantity_value is not None else None
                except (TypeError, ValueError):
                    quantity = None
                unit = _normalize_unit(str(unit_value).strip()) if unit_value is not None else None
                result.append(
                    _make_line(len(result) + 1, as_written, quantity, unit)
                )
            extracted_text.extend(
                str(value) for row in sheet.iter_rows(values_only=True) for value in row if value is not None
            )
    finally:
        workbook.close()
    return "\n".join(extracted_text), result


def extract_rfq(path: Any) -> RFQ:
    source = Path(path)
    extension = source.suffix.casefold()
    if extension == ".txt":
        text = source.read_text(encoding="utf-8-sig", errors="replace")
        lines = _extract_text(text)
    elif extension == ".pdf":
        text, lines = _extract_pdf(source)
    elif extension == ".xlsx":
        text, lines = _extract_xlsx(source)
    elif extension in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        text = ""
        lines = [
            RFQLine(
                line=1,
                as_written="Handwritten request image; OCR required",
                flags=["stub"],
                note="OCR is required to extract this handwritten request.",
            )
        ]
    else:
        raise ValueError("Unsupported RFQ file type: {}".format(extension or "(none)"))

    return RFQ(
        rfq_id=_rfq_id(text) or source.stem,
        customer_id=_identify_customer(text),
        lines=lines,
    )
