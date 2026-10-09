#!/usr/bin/env python3
"""Collect measured reconstruction metrics and export comparison JSON/CSV."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from reconstruct import write_json


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default if default is not None else {}


def clean_terminal(text):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "\n")


def parse_brush_metrics(text):
    number = r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    pattern = r"Eval iter\s+(\d+):\s*PSNR\s+" + number + r",\s*ssim\s+" + number
    results = {}
    for match in re.finditer(pattern, clean_terminal(text), flags=re.I):
        step, psnr, ssim = int(match[1]), float(match[2]), float(match[3])
        if math.isfinite(psnr) and math.isfinite(ssim):
            results[step] = {"step": step, "psnr_db": psnr, "ssim": ssim}
    return [results[key] for key in sorted(results)]


def read_log(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def make_split(dataset, names, every, resolution, seed):
    """Brush 0.3 sorts filenames and holds out zero-based indices divisible by N."""
    names = sorted(names)
    evaluation = [name for index, name in enumerate(names) if every and index % every == 0]
    digest = hashlib.sha256()
    for name in ("cameras.bin", "images.bin"):
        path = Path(dataset) / "sparse/0" / name
        if path.is_file():
            digest.update(path.read_bytes())
    return {"protocol": "known_pose_view_holdout", "sorting": "filename_ascending",
            "eval_split_every": every, "train_images": [name for name in names if name not in evaluation],
            "eval_images": evaluation, "max_resolution": resolution, "seed": seed,
            "pose_fingerprint": digest.hexdigest(),
            "note": "相机与初始稀疏点来自全体照片的 SfM；留出视图不参与高斯优化。"}


def collect_run(folder):
    folder = Path(folder).resolve()
    summary = read_json(folder / "summary.json")
    if not summary:
        candidates = []
        for path in folder.glob("export_*.ply"):
            match = re.fullmatch(r"export_(\d+)\.ply", path.name)
            if match:
                candidates.append((int(match[1]), path))
        if not candidates:
            raise ValueError("缺少有效 summary.json：" + str(folder))
        from train_3dgs import gaussian_ply_count
        step, path = max(candidates)
        summary = {"method": "3dgs", "steps": step, "gaussian_ply": str(path),
                   "splats": gaussian_ply_count(path), "evaluation_enabled": False,
                   "warnings": ["手动训练产物缺少项目配置记录；耗时、输入和评估划分未知。"]}
    config = read_json(folder / "config.json")
    method = summary.get("method") or ("3dgs" if summary.get("gaussian_ply") else "mvs" if summary.get("dense_points") else "sfm")
    detail = folder / "3dgs" if (folder / "3dgs/summary.json").is_file() else folder
    if method == "3dgs":
        train_config = read_json(detail / "config.json")
        split = read_json(detail / "split.json")
        if not split and summary.get("dataset"):
            manifest = read_json(Path(summary["dataset"]) / "dataset.json")
            if manifest.get("image_names"):
                split = make_split(summary["dataset"], manifest["image_names"],
                                   train_config.get("eval_split_every"), train_config.get("max_resolution"),
                                   train_config.get("seed", 42))
        evaluations = parse_brush_metrics(read_log(detail / "logs/03_train.log"))
        if not summary.get("evaluation_enabled"):
            evaluations = []
    else:
        split, evaluations, train_config = {}, [], {}
    artifact_keys = ("gaussian_ply", "textured_mesh", "mesh", "colmap_pointcloud", "dense_pointcloud", "sparse_pointcloud", "pointcloud")
    artifacts = []
    for key in artifact_keys:
        value = summary.get(key)
        if value and not any(item["path"] == value for item in artifacts):
            path = Path(value)
            artifacts.append({"kind": key, "path": str(path), "bytes": path.stat().st_size if path.is_file() else None})
    for value in [summary.get("texture_material")] + summary.get("texture_images", []):
        if value:
            path = Path(value)
            artifacts.append({"kind": "texture", "path": str(path), "bytes": path.stat().st_size if path.is_file() else None})
    input_images = summary.get("input_images")
    registered = summary.get("registered_images")
    warnings = list(summary.get("warnings", []))
    if method == "3dgs" and not evaluations:
        warnings.append("没有可用的留出视图评估记录，PSNR/SSIM 不可用于效果结论。")
    if method != "3dgs":
        warnings.append("未提供对齐的几何真值，未计算几何精度或完整度；点数不代表精度。")
    latest = evaluations[-1] if evaluations else {}
    if latest and latest["step"] != summary.get("steps"):
        warnings.append("最后一条评估对应的步数与最终模型步数不同。")
    if method == "3dgs" and not split:
        warnings.append("缺少可核验的评估划分，不能确认与其他训练的可比性。")
    group = None
    if split.get("eval_images"):
        protocol = {key: split.get(key) for key in ("eval_images", "max_resolution", "pose_fingerprint")}
        protocol["brush_version"] = summary.get("brush_version")
        group = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()[:12]
    input_path = config.get("images") or config.get("sfm_output") or summary.get("source_sfm") or summary.get("dataset")
    return {"name": folder.name, "path": str(folder), "method": method,
            "input": input_path, "status": read_json(folder / "status.json").get("status", "completed"),
            "input_images": input_images, "registered_images": registered,
            "registration_ratio": summary.get("registration_ratio", registered / input_images if input_images and registered is not None else None),
            "reprojection_error_px": summary.get("mean_point_reprojection_error_px"),
            "sparse_points": summary.get("points3D", summary.get("initial_points")),
            "dense_points": summary.get("dense_points"), "mesh_faces": summary.get("mesh_faces"),
            "splats": summary.get("splats"), "steps": summary.get("steps"),
            "elapsed_seconds": summary.get("route_elapsed_seconds", summary.get("elapsed_seconds")),
            "timing_scope": "unified_route" if "route_elapsed_seconds" in summary else "current_script",
            "source_sfm_seconds": summary.get("source_sfm_elapsed_seconds"),
            "max_resolution": split.get("max_resolution", config.get("dense_max_image_size", config.get("max_image_size"))),
            "artifact_bytes": sum(item["bytes"] or 0 for item in artifacts), "artifacts": artifacts,
            "psnr_db": latest.get("psnr_db"), "ssim": latest.get("ssim"), "evaluated_step": latest.get("step"),
            "train_views": len(split["train_images"]) if split else None,
            "eval_views": len(split["eval_images"]) if split else None,
            "comparison_group": group, "evaluation_curve": evaluations,
            "split": split, "seed": train_config.get("seed"), "warnings": warnings}


def discover_runs(root):
    """Top-level task summaries take precedence over their stage summaries."""
    folders = {path.parent for path in Path(root).glob("*/summary.json")}
    folders.update(path.parent for path in Path(root).glob("*/export_*.ply"))
    return sorted(folders)


CSV_FIELDS = ["name", "method", "input", "status", "input_images", "registered_images",
              "registration_ratio", "reprojection_error_px", "sparse_points", "dense_points", "mesh_faces",
              "splats", "steps", "elapsed_seconds", "timing_scope", "source_sfm_seconds", "max_resolution",
              "artifact_bytes", "psnr_db", "ssim", "evaluated_step", "train_views", "eval_views", "comparison_group"]


def export_report(folders, output):
    rows = [collect_run(path) for path in folders]
    output = Path(output).expanduser().resolve()
    for folder in folders:
        source = Path(folder).resolve()
        if source == output or source in output.parents or output in source.parents:
            raise ValueError("报告目录不能与任务目录重叠。")
    output.mkdir(parents=True, exist_ok=False)
    report = {"runs": rows, "notes": ["PSNR/SSIM 为 Brush 在留出视图上测得的均值。",
              "仅相同 comparison_group 的 3DGS 结果具备已记录的相同位姿、视图划分、分辨率和版本条件。",
              "耗时需同时查看 timing_scope；文件大小仅计列出的结果文件，不含中间数据。",
              "普通点云顶点数与高斯数量含义不同；未使用真值的几何质量不作排名。"]}
    write_json(output / "comparison.json", report)
    with (output / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            cells = {key: row.get(key) for key in CSV_FIELDS}
            for key, value in cells.items():
                if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
                    cells[key] = "'" + value
            writer.writerow(cells)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="汇总已完成任务，导出评估与对比 JSON/CSV。")
    parser.add_argument("--runs", type=Path, nargs="+")
    parser.add_argument("--scan", type=Path, default=Path("outputs"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    folders = args.runs or discover_runs(args.scan)
    if not folders:
        parser.error("没有找到已完成任务。")
    output = args.output or Path("outputs/reports") / datetime.now().strftime("comparison_%Y%m%d_%H%M%S_%f")
    report = export_report(folders, output)
    print("已汇总 {} 个任务；报告：{}".format(len(report["runs"]), output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
