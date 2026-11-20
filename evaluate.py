import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from pipeline import ROOT, SUPPORTED_EXTENSIONS, extract_and_match


THRESHOLDS = (0.7, 0.8, 0.9)
MATCH_FIELDS = (
    "ridgeline_part_number",
    "expected_part_number",
    "part_number",
)
TEXT_FIELDS = ("as_written", "source_text", "original_text", "description", "text")
FILE_FIELDS = (
    "source_file", "filename", "file", "rfq_file", "input_file", "source", "rfq_id"
)


def _stem(value: Any) -> str:
    return Path(str(value)).stem


def _lines(document: Any) -> Optional[List[Any]]:
    if isinstance(document, dict) and isinstance(document.get("lines"), list):
        return document["lines"]
    if isinstance(document, list):
        return document
    return None


def _document_key(document: Dict[str, Any], fallback: str) -> str:
    for field in FILE_FIELDS:
        if document.get(field):
            return _stem(document[field])
    return fallback


def _store_document(
    result: Dict[str, Dict[str, Any]], key: str, document: Any
) -> None:
    lines = _lines(document)
    if lines is not None:
        body = document if isinstance(document, dict) else {"lines": lines}
        result[key] = {"lines": lines, **{
            field: body[field] for field in ("rfq_id", "customer_id") if field in body
        }}


def _read_json(path: Path, result: Dict[str, Dict[str, Any]]) -> None:
    with path.open(encoding="utf-8") as source:
        document = json.load(source)
    if isinstance(document, dict) and _lines(document) is not None:
        _store_document(result, _document_key(document, path.stem), document)
        return
    if isinstance(document, dict):
        records = document.get("rfqs")
        if isinstance(records, list):
            for index, record in enumerate(records):
                if isinstance(record, dict):
                    _store_document(
                        result,
                        _document_key(record, "{}_{}".format(path.stem, index)),
                        record,
                    )
        for key, value in document.items():
            if key == "rfqs":
                continue
            if isinstance(value, (dict, list)):
                _store_document(result, _stem(key), value)
    elif isinstance(document, list):
        grouped: Dict[str, List[Any]] = {}
        for record in document:
            if not isinstance(record, dict):
                continue
            key = _document_key(record, path.stem)
            if _lines(record) is not None:
                _store_document(result, key, record)
            else:
                grouped.setdefault(key, []).append(record)
        for key, rows in grouped.items():
            _store_document(result, key, rows)


def _read_csv(path: Path, result: Dict[str, Dict[str, Any]]) -> None:
    with path.open(newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = next((row[field] for field in FILE_FIELDS if row.get(field)), path.stem)
        grouped.setdefault(_stem(key), []).append(row)
    for key, values in grouped.items():
        _store_document(result, key, values)


def _read_jsonl(path: Path, result: Dict[str, Dict[str, Any]]) -> None:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    with path.open(encoding="utf-8") as source:
        for raw in source:
            if raw.strip():
                record = json.loads(raw)
                if isinstance(record, dict):
                    key = _document_key(record, path.stem)
                    grouped.setdefault(key, []).append(record)
    for key, rows in grouped.items():
        _store_document(result, key, rows)


def _load_expected() -> Dict[str, Dict[str, Any]]:
    answer_dir = ROOT / "answer_key"
    if not answer_dir.is_dir():
        raise FileNotFoundError("Required answer-key directory is missing.")
    result: Dict[str, Dict[str, Any]] = {}
    for path in sorted(answer_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.casefold() == ".json":
            _read_json(path, result)
        elif path.suffix.casefold() == ".csv":
            _read_csv(path, result)
        elif path.suffix.casefold() == ".jsonl":
            _read_jsonl(path, result)
    if not result:
        raise ValueError("No line-level JSON, JSONL, or CSV answer-key records were found.")
    return result


def _expected_part(line: Any) -> Optional[str]:
    if not isinstance(line, dict):
        return None
    for field in MATCH_FIELDS:
        value = line.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _normalized_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def _unit(value: Any) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    aliases = {
        "each": "EA", "ea": "EA", "pc": "EA", "pcs": "EA", "piece": "EA",
        "pair": "PR", "pairs": "PR", "pr": "PR", "box": "BOX", "boxes": "BOX",
        "case": "CS", "cases": "CS", "cs": "CS", "pail": "PAIL", "pails": "PAIL",
    }
    value = str(value).strip()
    return aliases.get(value.casefold(), value.upper())


def _expected_value(line: Any, keys: Sequence[str]) -> Tuple[bool, Any]:
    if not isinstance(line, dict):
        return False, None
    for key in keys:
        if key in line:
            return True, line[key]
    return False, None


def _line_extraction_fields(expected: Any, actual: Dict[str, Any]) -> Tuple[int, int]:
    correct = total = 0
    has_line, expected_line = _expected_value(expected, ("line",))
    if has_line:
        total += 1
        correct += int(expected_line == actual.get("line"))
    has_text, expected_text = _expected_value(expected, TEXT_FIELDS)
    if has_text:
        total += 1
        expected_normalized = _normalized_text(expected_text)
        actual_normalized = _normalized_text(actual.get("as_written"))
        text_score = fuzz.token_set_ratio(expected_normalized, actual_normalized)
        correct += int(bool(expected_normalized) and text_score >= 85)
    has_quantity, expected_quantity = _expected_value(expected, ("quantity",))
    if has_quantity:
        total += 1
        actual_quantity = actual.get("quantity")
        if expected_quantity is None:
            correct += int(actual_quantity is None)
        elif actual_quantity is not None:
            correct += int(
                math.isclose(
                    _as_float(expected_quantity),
                    float(actual_quantity),
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
            )
    has_unit, expected_unit = _expected_value(expected, ("unit",))
    if has_unit:
        total += 1
        correct += int(_unit(expected_unit) == _unit(actual.get("unit")))
    return correct, total


def _expected_field_count(line: Any) -> int:
    count = int(_expected_value(line, ("line",))[0])
    count += int(any(_expected_value(line, (field,))[0] for field in TEXT_FIELDS))
    count += int(_expected_value(line, ("quantity",))[0])
    count += int(_expected_value(line, ("unit",))[0])
    return max(1, count)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _precision(tp: int, fp: int) -> float:
    return tp / float(tp + fp) if tp + fp else 0.0


def _recall(tp: int, fn: int) -> float:
    return tp / float(tp + fn) if tp + fn else 0.0


def _evaluate_threshold(
    sources: Sequence[Path],
    expected: Dict[str, Dict[str, Any]],
    threshold: float,
) -> Dict[str, Any]:
    true_positives = false_positives = false_negatives = 0
    extraction_correct = extraction_fields = expected_lines = predicted_lines = 0
    review_lines = accepted_matches = accepted_true_positives = 0

    for source in sources:
        key = source.stem
        reference = expected.get(key)
        if reference is None:
            raise KeyError(
                "No answer-key record for {}. Discovered keys: {}".format(
                    source.name, ", ".join(sorted(expected))
                )
            )
        gold_lines = _lines(reference) or []
        rfq = extract_and_match(source, threshold=threshold)
        predicted = rfq.lines
        expected_lines += len(gold_lines)
        predicted_lines += len(predicted)
        expected_field_average = (
            sum(_expected_field_count(line) for line in gold_lines) / float(len(gold_lines))
            if gold_lines else 1.0
        )
        for index in range(max(len(gold_lines), len(predicted))):
            gold = gold_lines[index] if index < len(gold_lines) else None
            actual = predicted[index] if index < len(predicted) else None
            if gold is not None and actual is not None:
                correct, fields = _line_extraction_fields(gold, actual.model_dump())
                extraction_correct += correct
                extraction_fields += fields
            elif gold is not None:
                extraction_fields += _expected_field_count(gold)
            elif actual is not None:
                extraction_fields += expected_field_average
            expected_part = _expected_part(gold)
            actual_part = actual.ridgeline_part_number if actual is not None else None
            if expected_part:
                if actual_part == expected_part:
                    true_positives += 1
                    if not actual.needs_review:
                        accepted_true_positives += 1
                else:
                    false_negatives += 1
                    if actual_part:
                        false_positives += 1
            elif actual_part:
                false_positives += 1
            if actual is not None:
                review_lines += int(actual.needs_review)
                if actual_part and not actual.needs_review:
                    accepted_matches += 1

    extraction_accuracy = (
        extraction_correct / float(extraction_fields) if extraction_fields else 0.0
    )
    return {
        "threshold": threshold,
        "extraction_accuracy": extraction_accuracy,
        "match_precision": _precision(true_positives, false_positives),
        "match_recall": _recall(true_positives, false_negatives),
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "review_rate": review_lines / float(predicted_lines) if predicted_lines else 0.0,
        "accepted_match_precision": (
            accepted_true_positives / float(accepted_matches) if accepted_matches else 0.0
        ),
    }


def main() -> None:
    sources = sorted(
        path for path in (ROOT / "rfqs").iterdir()
        if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
    )
    if not sources:
        raise RuntimeError("No supported RFQs found.")
    expected = _load_expected()
    for source in sources:
        if source.stem not in expected:
            extracted = extract_and_match(source, threshold=0.8)
            if extracted.rfq_id in expected:
                expected[source.stem] = expected[extracted.rfq_id]
    evaluated_sources = [source for source in sources if source.stem in expected]
    unscored_sources = [source.name for source in sources if source.stem not in expected]
    if not evaluated_sources:
        raise ValueError("No RFQs have line-level answer-key records.")
    results = [
        _evaluate_threshold(evaluated_sources, expected, threshold)
        for threshold in THRESHOLDS
    ]
    baseline = next(result for result in results if result["threshold"] == 0.8)
    output = {
        "threshold": baseline["threshold"],
        "extraction_accuracy": baseline["extraction_accuracy"],
        "match_precision": baseline["match_precision"],
        "match_recall": baseline["match_recall"],
        "additional_metrics": {
            "true_positives": baseline["true_positives"],
            "false_positives": baseline["false_positives"],
            "false_negatives": baseline["false_negatives"],
            "review_rate": baseline["review_rate"],
            "accepted_match_precision": baseline["accepted_match_precision"],
            "matching_precision": baseline["match_precision"],
            "matching_recall": baseline["match_recall"],
            "evaluated_rfq_count": len(evaluated_sources),
            "unscored_rfqs": unscored_sources,
            "threshold_comparison": results,
        },
    }
    destination = ROOT / "evaluation" / "results.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(output, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
