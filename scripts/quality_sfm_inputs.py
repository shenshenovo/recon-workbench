"""Freeze quality-flag-only SfM inputs before running any reconstruction."""
from datetime import datetime, timezone
from pathlib import Path
import shutil

from scripts.quality_acceptance_data import (
    SFM_CONFIG, evenly_spaced, excluded_by_quality, make_variant, new_output,
    provenance, source_frames, variant_name, write_json)
from scripts.quality_sfm_smoke import analyze_directory, copy_selected, sha256


GROUPS = ("original", "original_filtered", "augmented", "augmented_filtered")


def filter_inputs(images, report, destination):
    """No variant identity or similarity evidence participates in this rule."""
    excluded, selected = [], []
    for item in report["images"]:
        reasons = excluded_by_quality(item)
        if reasons:
            excluded.append({"name": item["name"], "reasons": reasons})
        else:
            selected.append(images / item["name"])
    manifest = copy_selected(selected, destination)
    return manifest, excluded


def pose_plan(original_names, retained_names):
    retained = [name for name in original_names if name in set(retained_names)]
    if len(retained) < 9:
        return {"available": False, "reason": "Need 8 fixed anchors and at least 1 held-out frame.",
                "anchors": [], "evaluation": [], "retained_originals": retained}
    anchors = evenly_spaced(retained, 8)
    return {"available": True, "anchors": anchors,
            "evaluation": [name for name in retained if name not in anchors],
            "retained_originals": retained}


def prepare_experiment(source, output):
    source_root, transforms, paths = source_frames(source, 60)
    output = new_output(output, transforms.parent)
    roots = {name: output / "inputs" / name / "images" for name in GROUPS}
    originals = copy_selected(paths, roots["original"])
    copy_selected([roots["original"] / row["name"] for row in originals], roots["augmented"])
    variants = []
    for index, source_path in enumerate(evenly_spaced(paths, 15)):
        kind, value = (("blur", 6.0) if index < 5 else
                       ("brightness", 0.18) if index < 10 else ("brightness", 3.0))
        name = variant_name(source_path, kind, value)
        variants.append(make_variant(roots["original"] / source_path.name,
                                     roots["augmented"] / name, kind, value))
    original_report = analyze_directory(roots["original"])
    augmented_report = analyze_directory(roots["augmented"])
    groups = {}
    for base in ("original", "augmented"):
        report = original_report if base == "original" else augmented_report
        groups[base] = {"images": str(roots[base]), "names": [row["name"] for row in report["images"]],
                        "files_sha256": {row["name"]: row["sha256"] for row in report["images"]},
                        "excluded": [], "quality_fingerprint": report["fingerprint"]}
        filtered, excluded = filter_inputs(roots[base], report, roots[base + "_filtered"])
        groups[base + "_filtered"] = {
            "images": str(roots[base + "_filtered"]), "names": [row["name"] for row in filtered],
            "files_sha256": {row["name"]: row["sha256"] for row in filtered},
            "excluded": excluded, "quality_fingerprint": report["fingerprint"]}
    original_names = [row["name"] for row in originals]
    retained = set(original_names) & set(groups["original_filtered"]["names"])
    retained_augmented = set(original_names) & set(groups["augmented_filtered"]["names"])
    if retained != retained_augmented:
        raise ValueError("Per-image quality flags drifted between original and augmented inputs.")
    shutil.copyfile(transforms, output / "reference-transforms.json")
    manifest = {"kind": "quality_sfm_four_arm", "created_at": datetime.now(timezone.utc).isoformat(),
                "source": str(source_root), "transforms_sha256": sha256(transforms),
                "originals": originals, "variants": variants, "groups": groups,
                "original_denominator": len(originals), "policy": original_report["policy"],
                "selection_rule": "exclude decode_error, blurry, underexposed, overexposed only",
                "pose_plan": pose_plan(original_names, retained), "configuration": SFM_CONFIG,
                "run_timeout_seconds": 3600, "provenance": provenance()}
    write_json(output / "inputs.json", manifest)
    return manifest


def verify_inputs(output, manifest):
    output = Path(output).resolve()
    if sha256(output / "reference-transforms.json") != manifest["transforms_sha256"]:
        raise ValueError("Reference poses changed after freezing.")
    root = Path(__file__).resolve().parents[1]
    for relative, digest in manifest["provenance"]["files_sha256"].items():
        if sha256(root / relative) != digest:
            raise ValueError("Code/protocol changed after freezing: " + relative)
    for group in manifest["groups"].values():
        images = Path(group["images"]).resolve()
        if not images.is_relative_to(output / "inputs"):
            raise ValueError("Group directory escaped the experiment.")
        actual = {path.name for path in images.iterdir() if path.is_file()}
        if actual != set(group["names"]):
            raise ValueError("Input names changed after freezing.")
        for name, digest in group["files_sha256"].items():
            if sha256(images / name) != digest:
                raise ValueError("Input bytes changed after freezing: " + name)
