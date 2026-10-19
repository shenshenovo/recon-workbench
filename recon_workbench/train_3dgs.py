#!/usr/bin/env python3
"""Run Brush training with checked COLMAP inputs, logs and result metadata."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .densify import load_source
from .prepare_brush import validate_dataset
from .reconstruct import Pipeline, positive_int, timestamp, write_json
from .paths import PROJECT_ROOT


def inspect_brush(requested=None):
    if requested:
        executable = shutil.which(str(Path(requested).expanduser()))
    else:
        local = PROJECT_ROOT / ".tools/brush/brush_app"
        executable = shutil.which(str(local)) or shutil.which("brush") or shutil.which("brush_app")
    if not executable:
        raise ValueError("找不到 Brush；请安装到 .tools/brush/brush_app，或指定 --brush 路径。")
    outputs = []
    for flag in ("--help", "--version"):
        result = subprocess.run([executable, flag], capture_output=True, text=True,
                                errors="replace", timeout=30)
        if result.returncode != 0:
            raise RuntimeError("Brush 启动检查失败（{}）：{}".format(result.returncode, result.stderr))
        outputs.append(result.stdout)
    options = set(re.findall(r"--([\w-]+)", outputs[0]))
    step_option = next((name for name in ("total-steps", "total-train-iters") if name in options), None)
    required = {"max-resolution", "max-splats", "export-every", "export-path", "export-name",
                "eval-split-every", "eval-every", "seed", "with-viewer"}
    if not step_option or not required.issubset(options):
        raise ValueError("Brush CLI 参数不兼容；请检查该版本的 --help。")
    return str(Path(executable).resolve()), outputs[1].strip(), outputs[0], step_option


def training_arguments(args, dataset, exports, step_option):
    command = [str(dataset), "--" + step_option, str(args.steps),
               "--max-resolution", str(args.max_resolution), "--max-splats", str(args.max_splats),
               "--export-every", str(args.export_every), "--export-path", str(exports),
               "--export-name", "export_{iter}.ply", "--seed", str(args.seed)]
    if args.eval_split_every:
        command.extend(["--eval-split-every", str(args.eval_split_every),
                        "--eval-every", str(args.eval_every)])
    if args.with_viewer:
        command.append("--with-viewer")
    if getattr(args, "save_eval_images", False):
        command.append("--eval-save-to-disk")
    return command


def gaussian_ply_count(path):
    """Validate Brush's binary Gaussian PLY schema and payload length."""
    properties, count, fmt = {}, 0, None
    with path.open("rb") as stream:
        if stream.readline().strip() != b"ply":
            raise ValueError("无效 PLY：" + str(path))
        while stream.tell() < 65536:
            line = stream.readline()
            if not line:
                raise ValueError("PLY 文件头截断。")
            fields = line.decode("ascii").strip().split()
            if not fields:
                continue
            if fields[0] == "format":
                fmt = fields[1]
            elif fields[0] == "element":
                if fields[1] != "vertex":
                    raise ValueError("预期 Gaussian PLY 只包含 vertex。")
                count = int(fields[2])
            elif fields[0] == "property":
                if len(fields) != 3 or fields[1] not in ("float", "float32", "double", "float64"):
                    raise ValueError("Gaussian PLY 属性类型不受支持。")
                properties[fields[2]] = 4 if fields[1] in ("float", "float32") else 8
            elif fields[0] == "end_header":
                break
        else:
            raise ValueError("PLY 文件头过长。")
        required = {"x", "y", "z", "opacity", "scale_0", "scale_1", "scale_2",
                    "rot_0", "rot_1", "rot_2", "rot_3", "f_dc_0", "f_dc_1", "f_dc_2"}
        if fmt != "binary_little_endian" or count < 1 or not required.issubset(properties):
            raise ValueError("导出文件不是完整的 Brush Gaussian PLY。")
        expected = count * sum(properties.values())
        if path.stat().st_size - stream.tell() != expected:
            raise ValueError("Gaussian PLY 数据长度不匹配或文件截断。")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description="调用 Brush 训练 3DGS，复用已有 SfM 或已整理数据集。")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dataset", type=Path, help="prepare_brush.py 整理的目录")
    source.add_argument("--sfm-output", type=Path, help="已有 SfM 结果；自动整理后训练")
    parser.add_argument("--output", type=Path, help="新的结果目录，默认 outputs/3dgs_时间戳")
    parser.add_argument("--brush", help="Brush 可执行文件路径，默认查找项目 .tools 和 PATH")
    parser.add_argument("--colmap", default="colmap")
    parser.add_argument("--steps", type=positive_int, default=5000)
    parser.add_argument("--max-resolution", type=positive_int, default=1200)
    parser.add_argument("--max-splats", type=positive_int, default=1000000)
    parser.add_argument("--export-every", type=positive_int, default=1000)
    parser.add_argument("--eval-split-every", type=positive_int, help="每 N 张留出一张评估，N 至少为 2")
    parser.add_argument("--eval-every", type=positive_int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--with-viewer", action="store_true", help="训练时打开 Brush 实时查看窗口")
    parser.add_argument("--save-eval-images", action="store_true", help="保存留出视图的渲染图片（需启用评估）")
    parser.add_argument("--check-env", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只检查环境和输入，打印命令，不训练或创建目录")
    args = parser.parse_args(argv)
    if not args.check_env and not (args.dataset or args.sfm_output):
        parser.error("需要 --dataset 或 --sfm-output")
    if args.eval_split_every == 1 or args.seed < 0:
        parser.error("评估间隔至少为 2，seed 必须非负")
    if args.save_eval_images and not args.eval_split_every:
        parser.error("--save-eval-images 需要 --eval-split-every")
    pipeline = None
    try:
        brush, version, help_text, step_option = inspect_brush(args.brush)
        colmap = shutil.which(args.colmap)
        if not colmap:
            raise ValueError("找不到 COLMAP，输入校验需要 model_converter。")
        if args.check_env:
            print("Brush：{}（{}）\nCOLMAP：{}".format(brush, version, colmap))
            return 0
        output = (args.output or Path("outputs") / datetime.now().strftime("3dgs_%Y%m%d_%H%M%S_%f")).expanduser().resolve()
        if args.dataset:
            dataset = args.dataset.expanduser().resolve()
            metadata = json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
            if metadata.get("status") != "prepared":
                raise ValueError("数据集未完成整理。")
            protected = [dataset]
        else:
            original, images, model, _, _ = load_source(args.sfm_output)
            protected = [original, images, model]
            dataset = output / "dataset"
        if any(output == path or path in output.parents or output in path.parents for path in protected):
            raise ValueError("输出目录不能与输入数据或源 SfM 目录重叠。")
        if output.exists():
            raise ValueError("输出目录已存在，请使用新目录：" + str(output))
        command = training_arguments(args, dataset, output / "gaussians", step_option)
        if args.dry_run:
            if args.dataset:
                for name in ("cameras.bin", "images.bin", "points3D.bin"):
                    if not (dataset / "sparse/0" / name).is_file():
                        raise ValueError("数据集模型不完整：" + name)
            import shlex
            if args.sfm_output:
                print("训练前会自动执行 prepare_brush.py 整理输入。")
            print(shlex.join([brush] + command))
            return 0
        output.mkdir(parents=True, exist_ok=False)
        (output / "logs").mkdir()
        settings = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
        settings.update(brush=brush, brush_version=version, dataset=str(dataset),
                        output=str(output), training_command=[brush] + command)
        write_json(output / "config.json", settings)
        (output / "logs/brush_help.txt").write_text(help_text, encoding="utf-8")
        pipeline = Pipeline(output, brush, {"task": "train_3dgs", "dataset": str(dataset), "steps": args.steps})
        if args.sfm_output:
            pipeline.run("01_prepare", ["-m", "recon_workbench.prepare_brush",
                         "--sfm-output", original, "--output", dataset,
                         "--max-image-size", args.max_resolution, "--colmap", colmap], executable=sys.executable,
                         cwd=PROJECT_ROOT)
        # Check actual binary model and final photos before GPU training.
        with tempfile.TemporaryDirectory(prefix="design3-train-check-") as temp:
            pipeline.run("02_validate", ["model_converter", "--input_path", dataset / "sparse/0",
                         "--output_path", temp, "--output_type", "TXT"], executable=colmap)
            data = validate_dataset(dataset / "images", Path(temp), 2**31 - 1)
        if args.max_splats < data["points3D"]:
            raise ValueError("--max-splats 不能小于初始化稀疏点数 {}。".format(data["points3D"]))
        if (dataset / "args.txt").exists() or (dataset / "init.ply").exists():
            raise ValueError("输入包含 args.txt 或 init.ply；当前入口要求使用 prepare_brush.py 的标准输入。")
        if args.eval_split_every and data["registered_images"] < 3:
            raise ValueError("留出评估至少需要 3 张照片。")
        (output / "gaussians").mkdir()
        from .evaluate import make_split, parse_brush_metrics, read_log
        split = make_split(dataset, data["image_names"], args.eval_split_every, args.max_resolution, args.seed)
        write_json(output / "split.json", split)
        print("训练：{} 张照片，{} 步；日志可在 logs/03_train.log 查看。".format(data["registered_images"], args.steps), flush=True)
        environment = os.environ.copy()
        environment.setdefault("RUST_LOG", "info")
        # Brush 0.3 hides its progress/evaluation display when stdout is a plain file.
        pipeline.run("03_train", command, env=environment, terminal=(os.name == "posix"))
        exports = []
        for path in (output / "gaussians").glob("export_*.ply"):
            match = re.fullmatch(r"export_(\d+)\.ply", path.name)
            if match:
                exports.append({"step": int(match[1]), "path": str(path),
                                "splats": gaussian_ply_count(path), "bytes": path.stat().st_size})
        exports.sort(key=lambda value: value["step"])
        if not exports or exports[-1]["step"] != args.steps:
            raise ValueError("Brush 未导出请求的最后一步结果，请检查训练日志。")
        evaluations = parse_brush_metrics(read_log(output / "logs/03_train.log")) if args.eval_split_every else []
        evaluation = {"protocol": split["protocol"], "train_views": len(split["train_images"]),
                      "eval_views": len(split["eval_images"]), "curve": evaluations,
                      "latest": evaluations[-1] if evaluations else None,
                      "status": "measured" if evaluations else "unavailable"}
        write_json(output / "evaluation.json", evaluation)
        summary = {"method": "3dgs", "engine": "brush", "brush_version": version,
                   "dataset": str(dataset), "registered_images": data["registered_images"],
                   "initial_points": data["points3D"], "steps": args.steps,
                   "evaluation_enabled": bool(args.eval_split_every), "eval_split_every": args.eval_split_every,
                   "gaussian_ply": exports[-1]["path"], "splats": exports[-1]["splats"],
                   "exports": exports, "elapsed_seconds": round(time.monotonic() - pipeline.started, 3)}
        summary.update(evaluation=evaluation, split_file=str(output / "split.json"))
        write_json(output / "summary.json", summary)
        pipeline.state.update(status="completed", finished_at=timestamp(), summary=summary)
        pipeline.save()
        print("\n训练完成：{} 个高斯\n结果：{}".format(summary["splats"], summary["gaussian_ply"]))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        if pipeline:
            pipeline.state.update(status="cancelled" if cancelled else "failed", finished_at=timestamp(),
                                  error="用户中断" if cancelled else str(error))
            pipeline.save()
        print("错误：" + ("用户中断，已有导出和日志保留" if cancelled else str(error)), file=sys.stderr)
        return 130 if cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
