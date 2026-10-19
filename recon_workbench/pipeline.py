#!/usr/bin/env python3
"""Unified local entry point for SfM, MVS and Brush 3DGS routes."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import shlex
import sys
import time

from .reconstruct import Pipeline, positive_int, timestamp, write_json, validate_camera_params
from .paths import PROJECT_ROOT


def get_parser():
    parser = argparse.ArgumentParser(description="统一三维重建入口：sfm / mvs / 3dgs。")
    parser.add_argument("--method", required=True, choices=("sfm", "mvs", "3dgs"))
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--images", type=Path, help="从照片开始执行")
    inputs.add_argument("--sfm-output", type=Path, help="复用 SfM，仅用于 mvs / 3dgs")
    inputs.add_argument("--dataset", type=Path, help="已整理的 Brush 数据集，仅用于 3dgs")
    parser.add_argument("--output", type=Path, help="新的任务目录，默认 outputs/方法_时间戳")
    parser.add_argument("--colmap", default="colmap")
    parser.add_argument("--brush")
    parser.add_argument("--openmvs-bin", type=Path)
    parser.add_argument("--camera-model", choices=("SIMPLE_RADIAL", "PINHOLE", "OPENCV", "OPENCV_FISHEYE"), default="SIMPLE_RADIAL")
    parser.add_argument("--single-camera", action="store_true")
    parser.add_argument("--camera-params", help="已知内参，英文逗号分隔，使用原图像素尺寸")
    parser.add_argument("--matcher", choices=("exhaustive", "sequential"), default="exhaustive")
    parser.add_argument("--max-image-size", type=positive_int, default=1200)
    parser.add_argument("--threads", type=positive_int, default=4)
    parser.add_argument("--mvs-stage", choices=("dense", "mesh", "textured"), default="textured")
    parser.add_argument("--dense-resolution-level", type=int, choices=range(5), default=1)
    parser.add_argument("--steps", type=positive_int, default=5000)
    parser.add_argument("--max-splats", type=positive_int, default=1000000)
    parser.add_argument("--export-every", type=positive_int, default=1000)
    parser.add_argument("--eval-split-every", type=positive_int)
    parser.add_argument("--eval-every", type=positive_int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--with-viewer", action="store_true")
    parser.add_argument("--save-eval-images", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="打印各阶段命令，不创建目录或执行")
    return parser


def build_plan(args, output):
    """Dispatch only; algorithm-specific execution stays in the existing scripts."""
    plan = []
    if args.method == "sfm" and not args.images:
        raise ValueError("sfm 路线需要 --images；已有 SfM 可直接选择 mvs 或 3dgs。")
    if args.dataset and args.method != "3dgs":
        raise ValueError("--dataset 仅用于 3dgs 路线。")
    if args.method == "3dgs" and (args.eval_split_every == 1 or args.seed < 0):
        raise ValueError("评估间隔至少为 2，seed 必须非负。")
    source = args.sfm_output
    if args.images:
        camera_params = validate_camera_params(args.camera_model, args.camera_params)
        source = output / "sfm"
        command = ["-m", "recon_workbench.reconstruct", "--images", args.images, "--output", source,
                   "--colmap", args.colmap, "--stage", "sparse", "--camera-model", args.camera_model,
                   "--matcher", args.matcher, "--max-image-size", args.max_image_size, "--threads", args.threads]
        if args.single_camera:
            command.append("--single-camera")
        if camera_params:
            command.extend(["--camera-params", camera_params])
        plan.append(("01_sfm", command, source))
    if args.method == "mvs":
        result = output / "mvs"
        command = ["-m", "recon_workbench.densify", "--sfm-output", source, "--output", result,
                   "--stage", args.mvs_stage, "--colmap", args.colmap, "--threads", args.threads,
                   "--dense-max-image-size", args.max_image_size,
                   "--dense-resolution-level", args.dense_resolution_level]
        if args.openmvs_bin:
            command.extend(["--openmvs-bin", args.openmvs_bin])
        plan.append(("02_mvs", command, result))
    elif args.method == "3dgs":
        result = output / "3dgs"
        command = ["-m", "recon_workbench.train_3dgs", "--output", result, "--colmap", args.colmap,
                   "--steps", args.steps, "--max-resolution", args.max_image_size,
                   "--max-splats", args.max_splats, "--export-every", args.export_every, "--seed", args.seed]
        command.extend(["--dataset", args.dataset] if args.dataset else ["--sfm-output", source])
        if args.brush:
            command.extend(["--brush", args.brush])
        if args.eval_split_every:
            command.extend(["--eval-split-every", args.eval_split_every, "--eval-every", args.eval_every])
        if args.with_viewer:
            command.append("--with-viewer")
        if args.save_eval_images:
            if not args.eval_split_every:
                raise ValueError("保存评估图片需要 --eval-split-every。")
            command.append("--save-eval-images")
        plan.append(("02_3dgs", command, result))
    return [(name, [str(value) for value in command], folder) for name, command, folder in plan]


def main(argv=None):
    args = get_parser().parse_args(argv)
    pipeline = None
    try:
        for key in ("images", "sfm_output", "dataset", "openmvs_bin"):
            value = getattr(args, key)
            if value is not None:
                setattr(args, key, value.expanduser().resolve())
        source = args.images or args.sfm_output or args.dataset
        if not source.is_dir():
            raise ValueError("输入目录不存在：" + str(source))
        output = (args.output or Path("outputs") / datetime.now().strftime(args.method + "_%Y%m%d_%H%M%S_%f")).expanduser().resolve()
        if output == source or source in output.parents or output in source.parents:
            raise ValueError("输出目录不能与输入目录重叠。")
        if output.exists():
            raise ValueError("输出目录已存在，请选择新目录：" + str(output))
        plan = build_plan(args, output)
        for name, command, _ in plan:
            print("{}：{}".format(name, shlex.join([sys.executable] + command)), flush=True)
        if args.dry_run:
            return 0
        output.mkdir(parents=True, exist_ok=False)
        (output / "logs").mkdir()
        write_json(output / "config.json", {key: str(value) if isinstance(value, Path) else value
                   for key, value in vars(args).items()})
        pipeline = Pipeline(output, sys.executable, {"method": args.method, "input": str(source)})
        stage_results = []
        for name, command, folder in plan:
            pipeline.run(name, command, cwd=PROJECT_ROOT)
            summary_path = folder / "summary.json"
            stage_results.append({"name": name, "output": str(folder), "summary": str(summary_path)})
            pipeline.state["results"] = stage_results.copy()
            pipeline.save()
        result = json.loads((plan[-1][2] / "summary.json").read_text(encoding="utf-8"))
        result.update(method=args.method, task_output=str(output), stages=stage_results,
                      route_elapsed_seconds=round(time.monotonic() - pipeline.started, 3))
        write_json(output / "summary.json", result)
        pipeline.state.update(status="completed", finished_at=timestamp(), summary=result)
        pipeline.save()
        print("\n{} 路线完成；结果目录：{}".format(args.method, plan[-1][2]), flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        if pipeline:
            pipeline.state.update(status="cancelled" if cancelled else "failed", finished_at=timestamp(),
                                  error="用户中断" if cancelled else str(error))
            pipeline.save()
        print("错误：" + ("用户中断；已完成阶段保留" if cancelled else str(error)), file=sys.stderr)
        return 130 if cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
