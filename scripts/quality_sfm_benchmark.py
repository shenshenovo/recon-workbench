#!/usr/bin/env python3
"""Prepare, freeze, then run the four-arm quality-filtering experiment."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality_acceptance_data import write_json
from scripts.quality_sfm_inputs import GROUPS, prepare_experiment, verify_inputs
from scripts.quality_sfm_results import collect_result, save_report
from scripts.quality_sfm_smoke import sha256


def run_order():
    return [(repeat + 1, name) for repeat in range(3)
            for name in GROUPS[repeat:] + GROUPS[:repeat]]


def stop_process(process):
    """Give the pipeline a chance to cancel its own COLMAP process group."""
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        # Pipeline and COLMAP have separate sessions; enumerate descendants before killing.
        descendants = {process.pid}
        changed = True
        while changed:
            changed = False
            for path in Path("/proc").glob("[0-9]*/stat"):
                try:
                    fields = path.read_text().rsplit(")", 1)[1].split()
                    if int(fields[1]) in descendants and int(path.parent.name) not in descendants:
                        descendants.add(int(path.parent.name))
                        changed = True
                except (OSError, ValueError, IndexError):
                    continue
        for pid in sorted(descendants, reverse=True):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def execute(images, output, config, timeout):
    command = [sys.executable, "-u", "-m", "recon_workbench.reconstruct",
               "--images", str(images), "--output", str(output)]
    for option in ("camera_model", "max_image_size", "max_features", "threads", "matcher"):
        command.extend(["--" + option.replace("_", "-"), str(config[option])])
    log_path = output.with_suffix(".log")
    started = time.monotonic()
    timed_out = False
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=REPO_ROOT, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process(process)
            returncode = 124
        except BaseException:
            stop_process(process)
            raise
    summary_path = output / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    return {"command": command, "returncode": returncode, "timed_out": timed_out,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "summary": summary, "output": str(output), "log": str(log_path)}


def run_suite(output):
    output = Path(output).resolve()
    manifest = json.loads((output / "inputs.json").read_text(encoding="utf-8"))
    verify_inputs(output, manifest)
    manifest_digest = sha256(output / "inputs.json")
    runs_root = output / "runs"
    runs_root.mkdir(exist_ok=True)
    runs = []
    for repeat, name in run_order():
        target = runs_root / ("r{}_{}".format(repeat, name))
        result_path = target.with_suffix(".json")
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result["inputs_manifest_sha256"] != manifest_digest:
                raise ValueError("Stored run belongs to a different frozen input manifest.")
        else:
            if target.exists():
                raise ValueError("Incomplete run retained; inspect before resuming: " + str(target))
            group = manifest["groups"][name]
            print("START r{} {} ({} images)".format(repeat, name, len(group["names"])), flush=True)
            if len(group["names"]) < 3:
                execution = {"returncode": 2, "elapsed_seconds": 0.0, "summary": {},
                             "error": "Fewer than 3 retained images; no replacement."}
            else:
                execution = execute(Path(group["images"]), target, manifest["configuration"],
                                    manifest["run_timeout_seconds"])
            result = collect_result(target, manifest, name, execution)
            result.update(repeat=repeat, inputs_manifest_sha256=manifest_digest)
            write_json(result_path, result)
        runs.append(result)
        save_report(output, manifest, runs)
        print("DONE r{} {} rc={} originals={}/60 elapsed={}s".format(
            repeat, name, result["returncode"], len(result["registered_original_names"]),
            result["elapsed_seconds"]), flush=True)
    verify_inputs(output, manifest)
    return save_report(output, manifest, runs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--experiment", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == "prepare":
        manifest = prepare_experiment(args.source, args.output)
        print(json.dumps({name: {"input": len(group["names"]), "excluded": len(group["excluded"])}
                          for name, group in manifest["groups"].items()}), flush=True)
        return 0
    report = run_suite(args.experiment)
    return 0 if report["all_runs_successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
