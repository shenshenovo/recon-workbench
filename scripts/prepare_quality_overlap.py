#!/usr/bin/env python3
"""Prepare one explicitly scoped, continuous Kitchen follow-up cohort."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality_acceptance_data import new_output, write_json
from scripts.quality_sfm_inputs import prepare_experiment
from scripts.quality_sfm_smoke import copy_selected, read_source, sha256


def prepare_overlap(source, subset_output, experiment_output):
    source_root, transforms, paths = read_source(source)
    if transforms is None or len(paths) < 60:
        raise ValueError("Need at least 60 source frames with transforms.json.")
    document = json.loads(transforms.read_text(encoding="utf-8"))
    if len(paths) != len(document["frames"]):
        raise ValueError("Missing/duplicate source frames; refusing to change the prefix.")
    subset_output = new_output(subset_output, transforms.parent)
    copied = copy_selected(paths[:60], subset_output / "images")
    subset = dict(document, frames=document["frames"][:60])
    write_json(subset_output / "transforms.json", subset)
    source_manifest = {
        "source": str(source_root), "source_transforms_sha256": sha256(transforms),
        "source_frame_count": len(paths), "selection": "first_60_in_transforms_order",
        "frames": copied, "scope": "continuous prefix only, not entire scene"}
    write_json(subset_output / "subset-manifest.json", source_manifest)
    manifest = prepare_experiment(subset_output, experiment_output)
    protocol = REPO_ROOT / "docs" / "QUALITY_OVERLAP_PROTOCOL.md"
    hashes = manifest["provenance"]["files_sha256"]
    for path in (protocol, Path(__file__).resolve(), REPO_ROOT / "scripts" / "run_quality_overlap.py"):
        hashes[path.relative_to(REPO_ROOT).as_posix()] = sha256(path)
    manifest["provenance"]["bundle_sha256"] = hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest.update(protocol_id="continuous_prefix_followup_v1", sampling=source_manifest,
                    first_round_failure_stop=True)
    write_json(Path(experiment_output) / "inputs.json", manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--subset-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = prepare_overlap(args.source, args.subset_output, args.output)
    print(json.dumps({name: len(group["names"]) for name, group in manifest["groups"].items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
