import csv
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from extractors import extract_rfq
from matching import match_rfq
from schemas import RFQ, RFQLine


ROOT = Path(__file__).resolve().parent
SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
OUTPUT_LINE_FIELDS = (
    "line",
    "as_written",
    "quantity",
    "unit",
    "ridgeline_part_number",
    "match_method",
    "confidence",
    "needs_review",
    "flags",
)
logger = logging.getLogger(__name__)


def _customer_names() -> Dict[str, str]:
    with (ROOT / "customers.csv").open(newline="", encoding="utf-8-sig") as source:
        return {
            row["customer_id"]: row["name"]
            for row in csv.DictReader(source)
            if row.get("customer_id") and row.get("name")
        }


def extract_and_match(source: Path, threshold: float = 0.8) -> RFQ:
    try:
        rfq = extract_rfq(source)
    except Exception as error:
        logger.error(
            "Extraction failed for %s (%s); routing to human review.",
            source.name,
            type(error).__name__,
        )
        rfq = RFQ(
            rfq_id=source.stem,
            lines=[
                RFQLine(
                    line=1,
                    as_written="Extraction failed; manual review required",
                    flags=["extraction_error"],
                    needs_review=True,
                    note="Automatic extraction failed; inspect the source manually.",
                )
            ],
        )
    return match_rfq(rfq, threshold=threshold)


def _serialize(rfq: RFQ, source: Path, customer_names: Dict[str, str]) -> Dict[str, object]:
    lines = []
    for line in rfq.lines:
        entry = {
            key: getattr(line, key)
            for key in OUTPUT_LINE_FIELDS
        }
        if line.note is not None:
            entry["note"] = line.note
        lines.append(entry)
    return {
        "rfq_id": source.stem,
        "customer_id": rfq.customer_id,
        "lines": lines,
        "customer_name": customer_names.get(rfq.customer_id) if rfq.customer_id else None,
        "non_item_rows": list(rfq.non_item_rows),
    }


def process_all(
    source_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    threshold: float = 0.8,
) -> List[Path]:
    source_dir = source_dir or ROOT / "rfqs"
    output_dir = output_dir or ROOT / "outputs"
    sources = sorted(
        path for path in source_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
    )
    if not sources:
        raise RuntimeError("No supported RFQ files found in {}".format(source_dir))
    output_dir.mkdir(parents=True, exist_ok=True)
    customer_names = _customer_names()
    written = []
    for source in sources:
        rfq = extract_and_match(source, threshold=threshold)
        destination = output_dir / "{}.json".format(source.stem)
        payload = _serialize(rfq, source, customer_names)
        destination.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        written.append(destination)
    return written


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    for path in process_all():
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
