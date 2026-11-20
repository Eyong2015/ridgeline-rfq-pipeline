import json
from pathlib import Path

from extractors import extract_rfq


def main() -> None:
    source_dir = Path(__file__).resolve().parent / "rfqs"
    sources = sorted(
        path for path in source_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in {
            ".txt", ".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff"
        }
    )
    if not sources:
        raise RuntimeError("No supported RFQ files were found in {}".format(source_dir))

    results = []
    for source in sources:
        rfq = extract_rfq(source)
        if not rfq.lines:
            raise AssertionError("{} produced no extracted lines".format(source.name))
        for line in rfq.lines:
            if (
                line.ridgeline_part_number is not None
                or line.match_method is not None
                or line.confidence != 0.0
                or not line.needs_review
            ):
                raise AssertionError("Matching defaults were not preserved")
            if "stub" in line.flags and (
                len(rfq.lines) != 1 or not line.note or "OCR" not in line.note
            ):
                raise AssertionError("Image stub must be a single OCR-required line")
        results.append(
            {
                "source": source.name,
                "rfq_id": rfq.rfq_id,
                "customer_id": rfq.customer_id,
                "line_count": len(rfq.lines),
                "review_count": sum(line.needs_review for line in rfq.lines),
                "flags": sorted({flag for line in rfq.lines for flag in line.flags}),
            }
        )
    print(json.dumps({"rfq_count": len(results), "results": results}, indent=2))


if __name__ == "__main__":
    main()
