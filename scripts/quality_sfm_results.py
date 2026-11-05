"""Aggregate repeated SfM observations without treating them as ground truth."""
from pathlib import Path
import statistics

from scripts.quality_acceptance_data import write_json
from scripts.quality_pose_reference import evaluate_poses, read_colmap_poses, read_reference_poses
from scripts.quality_sfm_inputs import GROUPS


def collect_result(output, manifest, group_name, execution):
    group = manifest["groups"][group_name]
    model = Path(output) / "model_txt" / "images.txt"
    estimated = read_colmap_poses(model) if model.is_file() else {}
    original_names = {row["name"] for row in manifest["originals"]}
    registered = set(estimated)
    retained = original_names & set(group["names"])
    pose = manifest["pose_plan"]
    consistency = {"comparable": False, "reason": pose.get("reason", "No exported model.")}
    if pose["available"]:
        consistency = evaluate_poses(
            estimated, read_reference_poses(Path(output).parents[1] / "reference-transforms.json"),
            pose["anchors"], pose["evaluation"])
    return {**execution, "group": group_name,
            "original_denominator": len(original_names),
            "retained_original_names": sorted(retained),
            "excluded_original_names": sorted(original_names - retained),
            "registered_original_names": sorted(original_names & registered),
            "unregistered_retained_original_names": sorted(retained - registered),
            "registered_variant_names": sorted(registered - original_names),
            "original_coverage": len(original_names & registered) / len(original_names),
            "registered_input_names": sorted(registered), "pose_reference_consistency": consistency}


def median_range(values):
    if not values:
        return None
    return {"median": statistics.median(values), "min": min(values), "max": max(values),
            "count": len(values)}


def aggregate(runs, manifest):
    groups = {}
    for name in GROUPS:
        selected = [run for run in runs if run["group"] == name]
        successful = [run for run in selected if run["returncode"] == 0]
        metrics = {"elapsed_seconds_all_attempts": median_range([run["elapsed_seconds"] for run in selected])}
        for key in ("registered_images", "points3D", "mean_point_reprojection_error_px",
                    "observations", "mean_track_length"):
            metrics[key] = median_range([run["summary"][key] for run in successful
                                        if run["summary"].get(key) is not None])
        metrics["original_coverage"] = median_range([run["original_coverage"] for run in successful])
        for key in ("center_error_normalized", "rotation_error_degrees"):
            values = [run["pose_reference_consistency"].get(key) for run in successful]
            metrics[key + "_rmse"] = median_range([value["rmse"] for value in values if value])
        groups[name] = {"attempts": len(selected), "successful": len(successful),
                        "input_count": len(manifest["groups"][name]["names"]),
                        "excluded_count": len(manifest["groups"][name]["excluded"]),
                        "metrics_successful_only": metrics}
    return {"kind": "quality_sfm_four_arm", "original_denominator": manifest["original_denominator"],
            "expected_runs": 12, "completed_attempts": len(runs), "groups": groups, "runs": runs,
            "complete": len(runs) == 12,
            "all_runs_successful": len(runs) == 12 and all(run["returncode"] == 0 for run in runs),
            "limitations": [
                "One scene only; source images have no human blur/exposure labels.",
                "Flag-only exclusion is an experimental ablation, not product auto-deletion.",
                "Internal errors and sparse point counts are not geometric accuracy.",
                "Published processed camera poses are a reference, not measured ground truth.",
                "Pose metrics use registered held-out frames; missing frames are reported per run."]}


def display(metric, precision=3):
    if metric is None:
        return "n/a"
    return ("{median:." + str(precision) + "f} [{min:." + str(precision)
            + "f}, {max:." + str(precision) + "f}]").format(**metric)


def save_report(output, manifest, runs):
    output = Path(output)
    report = aggregate(runs, manifest)
    write_json(output / "quality-sfm-benchmark.json", report)
    lines = ["# Quality filtering: repeated SfM comparison", "",
             "Values: median [min, max]. Fixed original denominator: 60.",
             "Failed runs are retained in JSON; successful-run metrics are conditional.", "",
             "| Group | Input | Success | Original coverage | Time(s), all attempts | Points | Internal error(px) |",
             "|---|---:|---:|---|---|---|---|"]
    for name, group in report["groups"].items():
        m = group["metrics_successful_only"]
        lines.append("| {} | {} | {}/{} | {} | {} | {} | {} |".format(
            name, group["input_count"], group["successful"], group["attempts"],
            display(m["original_coverage"]), display(m["elapsed_seconds_all_attempts"]),
            display(m["points3D"], 0), display(m["mean_point_reprojection_error_px"])))
    lines += ["", "## Published-pose consistency (not geometric truth)", "",
              "| Group | Normalized center RMSE | Rotation RMSE(deg) |",
              "|---|---|---|"]
    for name, group in report["groups"].items():
        m = group["metrics_successful_only"]
        lines.append("| {} | {} | {} |".format(name, display(m["center_error_normalized_rmse"]),
                                               display(m["rotation_error_degrees_rmse"])))
    lines += ["", "## Limitations", ""] + ["- " + note for note in report["limitations"]]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
