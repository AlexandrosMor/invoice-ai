"""Compare extraction strategies on one ordered, manually labeled dataset.

Datasets and reports may contain sensitive invoice information. Keep private
evaluation data and generated reports outside version control.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.evaluate_invoices import evaluate_dataset


SUMMARY_FIELDS = (
    "overall_field_accuracy",
    "whole_invoice_exact_match_rate",
    "supplier_accuracy",
    "invoice_number_accuracy",
    "date_accuracy",
    "currency_accuracy",
    "financial_total_accuracy",
    "vat_breakdown_accuracy",
    "line_item_accuracy",
    "critical_false_accept_rate",
    "manual_review_rate",
    "extraction_failure_rate",
    "average_inference_time_ms",
    "average_strategy_time_ms",
    "p50_processing_time_ms",
    "p95_processing_time_ms",
)


def compare(dataset: Path, output_dir: Path, strategies: list[str]) -> dict:
    reports = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for strategy in strategies:
        report = evaluate_dataset(dataset, output_dir / f"{strategy}.json", strategy=strategy)
        reports[strategy] = {
            "report_file": f"{strategy}.json",
            **{field: report.get(field) for field in SUMMARY_FIELDS},
            "dataset_size": report["dataset_size"],
            "model": report["model"],
            "pipeline_version": report["pipeline_version"],
            "runtime": report["runtime"],
        }
    result = {
        "comparison_version": "1.0.0",
        "dataset": str(dataset.resolve()),
        "dataset_size": next(iter(reports.values()))["dataset_size"] if reports else 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "methodology": "Each selected strategy processes the same sorted ground-truth dataset once; no report is a claim about other supplier populations.",
        "strategies": reports,
    }
    (output_dir / "comparison.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="Private local dataset containing invoices/ and ground_truth/.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory for detailed per-strategy reports and comparison.json.")
    parser.add_argument("--strategies", default="deterministic,ollama,hybrid", help="Comma-separated subset of deterministic,ollama,hybrid.")
    args = parser.parse_args()
    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    allowed = {"deterministic", "ollama", "hybrid"}
    if not strategies or len(set(strategies)) != len(strategies) or not set(strategies) <= allowed:
        parser.error("--strategies must be a nonempty, duplicate-free list of deterministic,ollama,hybrid")
    result = compare(args.dataset, args.output_dir, strategies)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
