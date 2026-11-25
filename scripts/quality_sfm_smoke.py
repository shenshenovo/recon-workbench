#!/usr/bin/env python3
"""Run a small, traceable image-quality and SfM smoke experiment.

This is deliberately a workflow check, not a reproduction of an older
experiment.  It keeps all selected normal views, injects known disposable
variants, and compares a noisy input with the same input after an explicitly
confirmed selection.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recon_workbench import image_quality as quality


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def frame_count(value):
    number = positive_int(value)
    if number < 3 or number > 30:
        raise argparse.ArgumentTypeError("图片数必须是 3～30 的整数")
    return number


def read_source(source):
    """Return an image root and the ordered frame paths from a scene directory."""
    source = Path(source).expanduser().resolve()
    image_root = source / "images" if (source / "images").is_dir() else source
    if not image_root.is_dir():
        raise ValueError("图片目录不存在：" + str(image_root))
    transforms = image_root.parent / "transforms.json"
    ordered = []
    if transforms.is_file():
        try:
            document = json.loads(transforms.read_text(encoding="utf-8"))
            for frame in document.get("frames", []):
                value = frame.get("file_path")
                if not isinstance(value, str):
                    continue
                relative = value[2:] if value.startswith("./") else value
                candidate = (transforms.parent / relative).resolve()
                if candidate.is_file() and candidate.parent == image_root.resolve():
                    ordered.append(candidate)
        except (OSError, ValueError, TypeError):
            ordered = []
    if not ordered:
        ordered = sorted(path for path in image_root.rglob("*")
                         if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png"))
    unique = []
    seen = set()
    for path in ordered:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    if len(unique) < 3:
        raise ValueError("至少需要 3 张可用照片。")
    return image_root, transforms if transforms.is_file() else None, unique


def choose_frames(source, count):
    image_root, transforms, paths = read_source(source)
    if type(count) is not int or not 3 <= count <= 30:
        raise ValueError("图片数必须是 3～30 的整数。")
    count = min(count, len(paths))
    # Consecutive transforms.json frames preserve the intended overlapping path.
    selected = paths[:count]
    return image_root, transforms, selected


def copy_selected(paths, destination):
    destination = Path(destination)
    names = [path.name for path in paths]
    if len(set(names)) != len(names):
        raise ValueError("抽取的图片存在重名，无法安全复制到平面实验目录。")
    destination.mkdir(parents=True, exist_ok=False)
    rows = []
    for path in paths:
        target = destination / path.name
        shutil.copyfile(path, target)
        rows.append({"name": target.name, "source": str(path), "sha256": sha256(path),
                     "bytes": path.stat().st_size})
    return rows


def inject_variants(clean_root, destination, base_name):
    """Create same-size, disposable variants; the source image is untouched."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted(clean_root.iterdir()):
        shutil.copyfile(path, destination / path.name)
    base_path = destination / base_name
    suffix = base_path.suffix.lower() or ".jpg"
    stem = "zz_quality_" + base_path.stem
    names = {
        "exact_copy": stem + "_exact" + suffix,
        "jpeg_reencode": stem + "_reencode.jpg",
        "blur": stem + "_blur.jpg",
        "dark": stem + "_dark.jpg",
        "bright": stem + "_bright.jpg",
    }
    with Image.open(base_path) as opened:
        base = ImageOps.exif_transpose(opened).convert("RGB")
        base.load()
        shutil.copyfile(base_path, destination / names["exact_copy"])
        base.save(destination / names["jpeg_reencode"], format="JPEG", quality=75,
                  subsampling=2, optimize=False)
        base.filter(ImageFilter.GaussianBlur(8)).save(
            destination / names["blur"], format="JPEG", quality=90)
        ImageEnhance.Brightness(base).enhance(0.18).save(
            destination / names["dark"], format="JPEG", quality=90)
        ImageEnhance.Brightness(base).enhance(1.8).save(
            destination / names["bright"], format="JPEG", quality=90)
    variants = []
    parameters = {
        "exact_copy": {"kind": "exact_copy"},
        "jpeg_reencode": {"kind": "jpeg_reencode", "quality": 75, "subsampling": 2},
        "blur": {"kind": "gaussian_blur", "radius": 8, "quality": 90},
        "dark": {"kind": "brightness", "factor": 0.18, "quality": 90},
        "bright": {"kind": "brightness", "factor": 1.8, "quality": 90},
    }
    for kind, name in names.items():
        path = destination / name
        variants.append({"name": name, "base": base_name, "sha256": sha256(path),
                         "bytes": path.stat().st_size, "parameters": parameters[kind]})
    return variants


def image_entries(root):
    return [{"name": path.name, "path": path.name, "bytes": path.stat().st_size}
            for path in sorted(root.iterdir())
            if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png")]


def quality_report_path(root):
    return Path(root).parent / "quality.json"


def analyze_directory(root):
    report = quality.analyze(root, image_entries(root))
    quality_report_path(root).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def make_selection(injected_root, report, normal_names, excluded_names, output_root):
    """Validate a human-confirmed list and copy immutable selected bytes."""
    selected = sorted(normal_names)
    quality.validate_selection(report, selected, report["fingerprint"])
    selection_root = Path(output_root)
    manifest = quality.snapshot(injected_root, report, selected, selection_root)
    document = {
        "version": 1,
        "policy": "keep_all_selected_normal_views; remove_only_known_injected_variants",
        "selected": manifest,
        "excluded_known_variants": sorted(excluded_names),
        "report_fingerprint": report["fingerprint"],
        "note": "Quality flags are advisory and were not used to remove normal views.",
    }
    selection_root.parent.joinpath("selection.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return document


def registered_image_names(path):
    """Read COLMAP's two-line-per-image TXT export, including blank observation lines."""
    path = Path(path)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines) and (
            not lines[index].strip() or lines[index].lstrip().startswith("#")):
        index += 1
    names = []
    while index < len(lines):
        metadata = lines[index].strip()
        if not metadata:
            index += 1
            continue
        fields = metadata.split(maxsplit=9)
        if len(fields) != 10:
            raise ValueError("COLMAP images.txt 包含无法识别的注册图像记录：" + str(path))
        names.append(fields[9])
        index += 2
    return names


def run_sfm(images, output, args, normal_names):
    output = Path(output)
    command = [sys.executable, "-m", "recon_workbench.reconstruct",
               "--images", str(Path(images).resolve()), "--output", str(output.resolve()),
               "--camera-model", args.camera_model, "--max-image-size", str(args.max_image_size),
               "--max-features", str(args.max_features), "--threads", str(args.threads),
               "--matcher", args.matcher, "--overlap", str(args.overlap)]
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    elapsed = round(time.perf_counter() - started, 3)
    log = output.parent / (output.name + ".log")
    log.write_text(completed.stdout, encoding="utf-8")
    summary_path = output / "summary.json"
    summary = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            summary = {}
    registered_names = registered_image_names(output / "model_txt" / "images.txt")
    registered_normal_names = sorted(set(registered_names) & set(normal_names))
    return {
        "name": output.name,
        "images": str(Path(images).resolve()),
        "output": str(output.resolve()),
        "command": command,
        "returncode": completed.returncode,
        "elapsed_seconds": elapsed,
        "summary": summary,
        "registered_original_images": len(registered_normal_names),
        "registered_original_names": registered_normal_names,
        "log": str(log.resolve()),
    }


def versions():
    result = {"python": platform.python_version(), "platform": platform.platform()}
    for package in ("Pillow", "ImageHash", "numpy", "scipy"):
        try:
            from importlib.metadata import version
            result[package] = version(package)
        except Exception:
            result[package] = None
    return result


def run_experiment(args):
    source_root, transforms, selected_paths = choose_frames(args.source, args.count)
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise ValueError("输出目录已存在，请选择新目录：" + str(output))
    if output == source_root or source_root in output.parents:
        raise ValueError("输出目录不能位于源图片目录内。")
    output.mkdir(parents=True)
    clean_root = output / "inputs" / "clean" / "images"
    injected_root = output / "inputs" / "injected" / "images"
    filtered_root = output / "inputs" / "filtered" / "images"
    clean_rows = copy_selected(selected_paths, clean_root)
    variants = inject_variants(clean_root, injected_root, clean_rows[0]["name"])
    normal_names = {row["name"] for row in clean_rows}
    excluded_names = {row["name"] for row in variants}
    clean_report = analyze_directory(clean_root)
    injected_report = analyze_directory(injected_root)
    selection = make_selection(injected_root, injected_report, normal_names, excluded_names, filtered_root)
    clean_report_file = quality_report_path(clean_root)
    injected_report_file = quality_report_path(injected_root)
    (output / "inputs.json").write_text(json.dumps({
        "source": str(source_root),
        "transforms": str(transforms) if transforms else None,
        "selected_frames": clean_rows,
        "variants": variants,
        "clean_report": str(clean_report_file.resolve()),
        "injected_report": str(injected_report_file.resolve()),
        "selection": selection,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    runs_root = output / "runs"
    runs_root.mkdir()
    runs = [
        run_sfm(clean_root, runs_root / "clean", args, normal_names),
        run_sfm(injected_root, runs_root / "injected", args, normal_names),
        run_sfm(filtered_root, runs_root / "filtered", args, normal_names),
    ]
    scene_name = source_root.parent.name if source_root.name.lower() == "images" else source_root.name
    frame_order = "transforms.json" if transforms else "sorted_filename"
    report = {
        "version": 1,
        "kind": "quality_sfm_smoke",
        "success": all(run["returncode"] == 0 for run in runs),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"scene": scene_name, "images": str(source_root),
                   "transforms": str(transforms) if transforms else None,
                   "frame_order": frame_order,
                   "source_sha256": {row["name"]: row["sha256"] for row in clean_rows}},
        "selection": {"count": len(clean_rows), "names": sorted(normal_names),
                      "excluded_known_variants": sorted(excluded_names),
                      "policy": "quality warnings remain advisory; remove only known injected variants"},
        "quality": {
            "algorithm_version": quality.VERSION,
            "policy": injected_report["policy"],
            "clean_counts": clean_report["counts"],
            "injected_counts": injected_report["counts"],
            "clean_report": str(clean_report_file.resolve()),
            "injected_report": str(injected_report_file.resolve()),
        },
        "sfm": {"runs": runs, "configuration": {
            "camera_model": args.camera_model,
            "single_camera": False,
            "camera_params_supplied": False,
            "poses_imported": False,
            "max_image_size": args.max_image_size,
            "max_features": args.max_features,
            "threads": args.threads,
            "matcher": args.matcher,
            "overlap": args.overlap,
        }},
        "environment": versions(),
        "notes": [
            "This is a small workflow smoke, not a strict reproduction of an older experiment.",
            "Frame order follows {}. COLMAP uses {} with independent cameras; no intrinsics or poses are "
            "supplied from transforms.json.".format(frame_order, args.camera_model),
            "Normal overlapping views are retained in every clean/filtered run. Only known injected variants "
            "are removed from the filtered run.",
            "Registration, sparse point count and elapsed time are process observations, not geometric accuracy.",
            "One noisy/filtered run is not evidence of a general speed or reconstruction-quality improvement.",
        ],
    }
    (output / "quality-sfm-smoke.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=Path("data/datasets/nerfstudio/kitchen"),
                        help="场景根目录或 images 目录")
    parser.add_argument("--output", type=Path, required=True,
                        help="新的实验目录；不会覆盖已有目录")
    parser.add_argument("--count", type=frame_count, default=10)
    parser.add_argument("--max-image-size", type=positive_int, default=640)
    parser.add_argument("--max-features", type=positive_int, default=4096)
    parser.add_argument("--threads", type=positive_int, default=2)
    parser.add_argument("--matcher", choices=("sequential", "exhaustive"), default="sequential")
    parser.add_argument("--overlap", type=positive_int, default=5)
    parser.add_argument("--camera-model", choices=("OPENCV", "PINHOLE", "SIMPLE_RADIAL"),
                        default="OPENCV")
    args = parser.parse_args(argv)
    try:
        report = run_experiment(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
    print("质量/SfM smoke 报告：" + str(Path(args.output).resolve() / "quality-sfm-smoke.json"))
    for run in report["sfm"]["runs"]:
        summary = run["summary"]
        print("{}: rc={}, registered={}/{}, registered_originals={}, points={}, elapsed={}s".format(
            run["name"], run["returncode"], summary.get("registered_images"),
            summary.get("input_images"), run["registered_original_images"],
            summary.get("points3D"), run["elapsed_seconds"]))
    if not report.get("success", all(run["returncode"] == 0 for run in report["sfm"]["runs"])):
        print("SfM smoke 未全部成功；请检查报告与各运行日志。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
