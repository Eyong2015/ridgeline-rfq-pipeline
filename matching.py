import csv
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from schemas import RFQ, RFQLine


_ROOT = Path(__file__).resolve().parent
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
    "cs": "CS",
    "case": "CS",
    "cases": "CS",
    "pail": "PAIL",
    "pails": "PAIL",
    "gal": "GAL",
    "gallon": "GAL",
    "gallons": "GAL",
}
_UNIT_PATTERN = "|".join(sorted(_UNIT_ALIASES, key=len, reverse=True))


def _norm_unit(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _UNIT_ALIASES.get(value.casefold(), value.strip().upper())


def _contains(value: str, identifier: str) -> bool:
    return bool(
        re.search(
            r"(?<![A-Z0-9]){}(?![A-Z0-9])".format(re.escape(identifier)),
            value,
            re.IGNORECASE,
        )
    )


def _load_products() -> List[Dict[str, str]]:
    with (_ROOT / "products.csv").open(newline="", encoding="utf-8-sig") as source:
        return [
            {
                key: (row.get(key) or "").strip()
                for key in ("part_number", "description", "unit", "status", "replaced_by")
            }
            for row in csv.DictReader(source)
        ]


def _load_cross_references() -> List[Dict[str, str]]:
    with (_ROOT / "cross_reference.csv").open(
        newline="", encoding="utf-8-sig"
    ) as source:
        return [
            {
                key: (row.get(key) or "").strip()
                for key in ("competitor_part_number", "ridgeline_part_number", "notes")
            }
            for row in csv.DictReader(source)
        ]


def _add_flag(line: RFQLine, flag: str, note: Optional[str] = None) -> None:
    if flag not in line.flags:
        line.flags.append(flag)
    if note:
        line.note = "{}; {}".format(line.note, note) if line.note else note
    line.needs_review = True


def _reset_match(line: RFQLine) -> None:
    line.ridgeline_part_number = None
    line.match_method = None
    line.confidence = 0.0
    line.product_reference = None
    line.needs_review = True


def _unit_conflict(line: RFQLine, product: Dict[str, str]) -> bool:
    requested = _norm_unit(line.unit)
    catalog = _norm_unit(product.get("unit"))
    return bool(requested and catalog and requested != catalog)


def _specifications(text: str, quantity: Optional[float]) -> set:
    value = text
    value = re.sub(
        r"\bqty\.?\s*\d+(?:\.\d+)?\s*(?:" + _UNIT_PATTERN + r")?\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:" + _UNIT_PATTERN + r")\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    if quantity is not None:
        number = re.escape(str(int(quantity))) if quantity.is_integer() else re.escape(str(quantity))
        value = re.sub(r"^\s*[-*]?\s*" + number + r"\b", " ", value)
        value = re.sub(r"(?:\bx|×)\s*" + number + r"\s*$", " ", value, flags=re.I)
        value = re.sub(r"[-–,]\s*" + number + r"\s*$", " ", value)

    values = set()
    patterns = (
        r"\b[A-Z]{1,5}\d+[A-Z0-9-]*\b",
        r"\b\d{4,}(?:-[A-Z0-9]+)*\b",
        r"\b\d+(?:/\d+)?\s*(?:HP|RPM|MICRON|PHASE|MM|INCH(?:ES)?|#|[\"'])\b",
        r"\b\d+(?:/\d+)?\s*[x×]\s*\d+(?:/\d+)?(?:\s*[x×]\s*\d+(?:/\d+)?)?(?:-\d+)?\b",
    )
    for pattern in patterns:
        values.update(
            re.sub(r"\s+", "", match).casefold()
            for match in re.findall(pattern, value, re.IGNORECASE)
        )
    return values


def _description_text(line: RFQLine) -> str:
    value = " ".join(
        part for part in (line.product_reference or "", line.as_written) if part
    )
    value = re.sub(r"\b[A-Z]{1,8}(?:-[A-Z0-9]+)+\b", " ", value, flags=re.I)
    value = re.sub(r"\bRL\d+\b", " ", value, flags=re.I)
    value = re.sub(
        r"\bqty\.?\s*\d+(?:\.\d+)?\s*(?:" + _UNIT_PATTERN + r")?\b",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:" + _UNIT_PATTERN + r")\b", " ", value, flags=re.I
    )
    if line.quantity is not None:
        number = re.escape(str(int(line.quantity))) if line.quantity.is_integer() else re.escape(str(line.quantity))
        value = re.sub(r"^\s*[-*]?\s*" + number + r"\b", " ", value)
        value = re.sub(r"(?:\bx|×)\s*" + number + r"\s*$", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def _normalized_description(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", value.casefold())
    normalized = []
    for word in words:
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith(("ches", "shes", "sses", "xes", "zes")):
            word = word[:-2]
        elif word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 3:
            word = word[:-1]
        normalized.append(word)
    return " ".join(normalized)


def _previous_order_unavailable(line: RFQLine) -> bool:
    return bool(
        re.search(
            r"\b(?:same|as per|like)\b.{0,60}\b(?:last|previous|prior)\s+"
            r"(?:order|purchase|quote)\b",
            line.as_written,
            re.IGNORECASE,
        )
    )


def _set_match(
    line: RFQLine,
    product: Dict[str, str],
    method: str,
    confidence: float,
    threshold: float,
) -> None:
    line.ridgeline_part_number = product["part_number"]
    line.product_reference = product["part_number"]
    line.match_method = method
    line.confidence = max(0.0, min(1.0, confidence))
    line.needs_review = bool(line.flags or line.note or line.confidence < threshold)


def _match_line(
    line: RFQLine,
    products: Sequence[Dict[str, str]],
    cross_references: Sequence[Dict[str, str]],
    threshold: float,
) -> None:
    product_reference = line.product_reference
    _reset_match(line)
    if "stub" in line.flags or "extraction_error" in line.flags:
        return
    if _previous_order_unavailable(line):
        _add_flag(
            line,
            "previous_order_unavailable",
            "The referenced prior order is unavailable; no product was inferred.",
        )
        return
    if "revised" in line.flags or "ambiguous_reference" in line.flags:
        _add_flag(
            line,
            "revision_requires_review" if "revised" in line.flags else "reference_requires_review",
        )
        return

    source_text = " ".join(
        part for part in (product_reference or "", line.as_written) if part
    )
    references = [
        row for row in cross_references
        if row["competitor_part_number"]
        and _contains(source_text, row["competitor_part_number"])
    ]
    if references:
        targets = {row["ridgeline_part_number"] for row in references if row["ridgeline_part_number"]}
        if len(targets) != 1:
            _add_flag(
                line,
                "ambiguous_cross_reference",
                "The cross-reference identifier maps to competing Ridgeline products.",
            )
            return
        caveats = sorted({row["notes"] for row in references if row["notes"]})
        if caveats:
            _add_flag(
                line,
                "cross_reference_caveat",
                "Cross-reference requires review: {}.".format("; ".join(caveats)),
            )
            return
        target = next(iter(targets))
        candidates = [p for p in products if p["part_number"].casefold() == target.casefold()]
        if len(candidates) != 1:
            _add_flag(line, "unresolved_cross_reference")
            return
        product = candidates[0]
        if product["status"].casefold() != "active":
            _add_flag(line, "inactive_product")
            return
        if _unit_conflict(line, product):
            _add_flag(
                line,
                "unit_mismatch",
                "Requested and catalog units differ; packaging was not assumed equivalent.",
            )
            return
        _set_match(line, product, "cross_reference", 1.0, threshold)
        return

    exact_numbers = {
        product["part_number"].casefold()
        for product in products
        if product["part_number"] and _contains(source_text, product["part_number"])
    }
    if len(exact_numbers) > 1:
        _add_flag(line, "ambiguous_identifier")
        return
    if exact_numbers:
        part_number = next(iter(exact_numbers))
        candidates = [
            product for product in products
            if product["part_number"].casefold() == part_number
        ]
        if len(candidates) != 1:
            _add_flag(line, "ambiguous_identifier")
            return
        product = candidates[0]
        if product["status"].casefold() != "active":
            _add_flag(line, "inactive_product")
            return
        if _unit_conflict(line, product):
            _add_flag(line, "unit_mismatch", "Requested and catalog units differ.")
            return
        _set_match(line, product, "ridgeline_part_number", 1.0, threshold)
        return

    line.product_reference = product_reference
    query = _description_text(line)
    if not query:
        _add_flag(line, "no_match")
        return
    active_products = [p for p in products if p["status"].casefold() == "active"]
    scored: List[Tuple[float, Dict[str, str]]] = []
    for product in active_products:
        description = product["description"]
        if not description:
            continue
        normalized_query = _normalized_description(query)
        normalized_description = _normalized_description(description)
        score = max(
            fuzz.token_set_ratio(normalized_query, normalized_description),
            fuzz.partial_token_set_ratio(normalized_query, normalized_description),
        ) / 100.0
        scored.append((score, product))
    if not scored:
        _add_flag(line, "no_match")
        return
    scored.sort(key=lambda result: result[0], reverse=True)
    best_score, best_product = scored[0]
    line.confidence = best_score
    line.match_method = "description"
    line.product_reference = best_product["part_number"]
    if best_score < threshold:
        _add_flag(line, "low_confidence")
        return
    if len(scored) > 1 and best_score - scored[1][0] < 0.05:
        _add_flag(
            line,
            "competing_candidates",
            "Description candidates are too close to distinguish confidently.",
        )
        return
    requested_specs = _specifications(query, line.quantity)
    candidate_specs = _specifications(best_product["description"], None)
    if requested_specs and candidate_specs and requested_specs.isdisjoint(candidate_specs):
        _add_flag(
            line,
            "conflicting_specifications",
            "The best description candidate conflicts with requested specifications.",
        )
        return
    if _unit_conflict(line, best_product):
        _add_flag(
            line,
            "unit_mismatch",
            "Requested and catalog units differ; packaging was not assumed equivalent.",
        )
        return
    if best_score >= threshold:
        _set_match(line, best_product, "description", best_score, threshold)


def match_rfq(rfq: RFQ, threshold: float = 0.8) -> RFQ:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    products = _load_products()
    cross_references = _load_cross_references()
    for line in rfq.lines:
        _match_line(line, products, cross_references, threshold)
    return rfq
