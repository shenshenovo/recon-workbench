#!/usr/bin/env python3
"""Run the separately frozen overlap cohort, stopping after a failed first round."""
import argparse
import json
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality_acceptance_data import write_json
from scripts.quality_sfm_benchmark import execute, run_order
from scripts.quality_sfm_inputs import verify_inputs
from scripts.quality_sfm_results import collect_result, save_report
from scripts.quality_sfm_smoke import sha256


def first_round_failed(runs):
    return len(runs) == 4 and any(run["returncode"] != 0 for run in runs)


def run_attempt(output, manifest, repeat, name, digest):
    target = output / "runs" / ("r{}_{}".format(repeat, name))
    result_path = target.with_suffix(".json")
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["inputs_manifest_sha256"] != digest:
            raise ValueError("Stored run does not match frozen inputs.")
        return result
    if target.exists():
        raise ValueError("Incomplete output retained for inspection: " + str(target))
    group = manifest["groups"][name]
    print("START r{} {} ({} images)".format(repeat, name, len(group["names"])), flush=True)
    started = time.monotonic()
    try:
        if len(group["names"]) < 3:
            execution = {"returncode": 2, "elapsed_seconds": 0, "summary": {},
                         "error": "Fewer than 3 input images."}
        else:
            execution = execute(Path(group["images"]), target, manifest["configuration"],
                                manifest["run_timeout_seconds"])
    except KeyboardInterrupt:
        execution = {"returncode": 130, "elapsed_seconds": round(time.monotonic() - started, 3),
                     "summary": {}, "cancelled": True}
    result = collect_result(target, manifest, name, execution)
    result.update(repeat=repeat, inputs_manifest_sha256=digest)
    write_json(result_path, result)
    return result


def run_suite(output):
    output = Path(output).resolve()
    manifest = json.loads((output / "inputs.json").read_text(encoding="utf-8"))
    if manifest.get("protocol_id") != "continuous_prefix_followup_v1":
        raise ValueError("This runner is only for the separately frozen overlap protocol.")
    verify_inputs(output, manifest)
    digest = sha256(output / "inputs.json")
    (output / "runs").mkdir(exist_ok=True)
    runs, termination = [], "completed"
    for repeat, name in run_order():
        result = run_attempt(output, manifest, repeat, name, digest)
        runs.append(result)
        save_report(output, manifest, runs)
        print("DONE r{} {} rc={} originals={}/60 elapsed={}s".format(
            repeat, name, result["returncode"], len(result["registered_original_names"]),
            result["elapsed_seconds"]), flush=True)
        if result.get("cancelled"):
            termination = "cancelled"
            break
        if first_round_failed(runs):
            termination = "first_round_baseline_failure_stop"
            break
    verify_inputs(output, manifest)
    report = save_report(output, manifest, runs)
    report.update(protocol_id=manifest["protocol_id"], termination=termination)
    write_json(output / "quality-sfm-benchmark.json", report)
    with (output / "REPORT.md").open("a", encoding="utf-8") as stream:
        stream.write("\nTermination: {}. No unattempted runs are counted as successful.\n".format(termination))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    args = parser.parse_args(argv)
    report = run_suite(args.experiment)
    if report["termination"] == "cancelled":
        return 130
    return 0 if report["all_runs_successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
