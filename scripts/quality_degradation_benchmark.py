#!/usr/bin/env python3
"""Measure advisory detector responses, not real-camera classification accuracy."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality_acceptance_data import (
    QUALITY_FLAGS, VARIANTS, make_controls, make_variant, new_output,
    provenance, source_frames, variant_name, write_json)
from scripts.quality_sfm_smoke import analyze_directory, copy_selected, sha256


def response_groups(rows, items):
    indexed = {item["name"]: item for item in items}
    groups = {}
    for row in rows:
        key = (row["split"], row["kind"], row["level"])
        group = groups.setdefault(key, [])
        group.append(indexed[row["name"]])
    result = []
    for (split, kind, level), group in groups.items():
        counts = {flag: sum(flag in item["flags"] for item in group) for flag in QUALITY_FLAGS}
        result.append({"split": split, "kind": kind, "level": level, "count": len(group),
                       "alert_counts": counts,
                       "alert_rates": {flag: count / len(group) for flag, count in counts.items()}})
    return result


def write_review_template(path, rows):
    fields = ["name", "source_name", "split", "blur_label", "underexposed_label",
              "overexposed_label", "review_status", "notes"]
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{key: row[key] for key in ("name", "source_name", "split")},
                             "review_status": "unreviewed"})


def write_measurements(path, rows, items):
    indexed = {item["name"]: item for item in items}
    metrics = ["laplacian_variance", "mean_luminance", "luminance_std", "dark_ratio",
               "bright_ratio"]
    fields = ["name", "source_name", "split", "kind", "level", *metrics, *QUALITY_FLAGS]
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            item = indexed[row["name"]]
            writer.writerow({**{key: row[key] for key in fields[:5]},
                             **{key: item.get(key) for key in metrics},
                             **{flag: int(flag in item["flags"]) for flag in QUALITY_FLAGS}})


def run_experiment(source, output):
    source_root, transforms, paths = source_frames(source, 24)
    output = new_output(output, transforms.parent)
    images = output / "samples" / "images"
    copied = copy_selected(paths, images)
    rows = []
    for index, row in enumerate(copied):
        split = "calibration" if index < 12 else "validation"
        rows.append({**row, "source_name": row["name"], "split": split,
                     "kind": "original", "level": 1.0})
        for kind, value in VARIANTS:
            name = variant_name(row["name"], kind, value)
            variant = make_variant(images / row["name"], images / name, kind, value)
            rows.append({**variant, "split": split})
    frozen = {"created_at": datetime.now(timezone.utc).isoformat(),
              "source": str(source_root), "transforms_sha256": sha256(transforms),
              "source_count": 24, "samples": rows, "provenance": provenance(),
              "no_threshold_search": True, "ground_truth": None}
    write_json(output / "inputs.json", frozen)
    report = analyze_directory(images)
    controls = output / "controls" / "images"
    controls.parent.mkdir()
    make_controls(controls)
    control_report = analyze_directory(controls)
    response = {"kind": "controlled_detector_response", "created_at": frozen["created_at"],
                "sample_count": len(rows), "policy": report["policy"],
                "groups": response_groups(rows, report["images"]),
                "controls": control_report["images"], "classification_accuracy": None,
                "limitations": [
                    "Original images are unlabelled, not known normal negatives.",
                    "Controlled transformations are not human camera-quality ground truth.",
                    "Calibration and validation share one scene; neither tunes thresholds.",
                    "Flat/noise controls are probes, not camera-image accuracy samples."]}
    write_json(output / "response.json", response)
    write_measurements(output / "measurements.csv", rows, report["images"])
    write_review_template(output / "human-quality-labels.csv", rows)
    lines = ["# Controlled quality-detector response", "",
             "Not precision/recall: real-image quality labels are unavailable.", "",
             "| Split | Transform | Level | N | Blur | Under | Over |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for group in response["groups"]:
        rates = group["alert_rates"]
        lines.append("| {} | {} | {} | {} | {:.1%} | {:.1%} | {:.1%} |".format(
            group["split"], group["kind"], group["level"], group["count"],
            *(rates[flag] for flag in QUALITY_FLAGS)))
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    response = run_experiment(args.source, args.output)
    print(json.dumps({"output": str(args.output.resolve()),
                      "samples": response["sample_count"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
