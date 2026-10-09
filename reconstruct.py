#!/usr/bin/env python3
"""Local COLMAP SfM and optional OpenMVS CPU reconstruction. Python 3.9+."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def get_parser():
    from densify import add_dense_options
    parser = argparse.ArgumentParser(
        description="使用 COLMAP 完成 CPU SfM，可继续调用 OpenMVS 生成稠密点云、网格和纹理。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--images", type=Path, help="图片目录，递归读取 JPG/PNG/TIFF/BMP")
    parser.add_argument("--output", type=Path, help="新的结果目录，默认 outputs/时间戳")
    parser.add_argument("--colmap", default="colmap", help="COLMAP 可执行文件名称或路径")
    parser.add_argument("--check-env", action="store_true", help="只检查 COLMAP 和 CPU 参数")
    parser.add_argument("--matcher", choices=["exhaustive", "sequential"], default="exhaustive",
                        help="全部图片两两匹配，或按文件名顺序匹配相邻图片")
    parser.add_argument("--overlap", type=positive_int, default=10, help="顺序匹配的邻近图片数量")
    parser.add_argument("--max-image-size", type=positive_int, default=1600,
                        help="特征提取时的图像最大边长，原图不改写")
    parser.add_argument("--max-features", type=positive_int, default=8192, help="每张图片的 SIFT 特征数量上限")
    parser.add_argument("--threads", type=positive_int, default=min(8, os.cpu_count() or 1), help="CPU 线程数")
    parser.add_argument("--camera-model", choices=["SIMPLE_RADIAL", "PINHOLE", "OPENCV", "OPENCV_FISHEYE"],
                        default="SIMPLE_RADIAL", help="相机模型")
    parser.add_argument("--single-camera", action="store_true",
                        help="所有图片共享内参；仅用于相同镜头、焦距、分辨率的照片")
    parser.add_argument("--stage", choices=["sparse", "dense", "mesh", "textured"], default="sparse",
                        help="最终输出阶段；dense/mesh/textured 需要 OpenMVS")
    add_dense_options(parser)
    return parser


def read_help(executable, command):
    result = subprocess.run([executable, command, "-h"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", timeout=30)
    if result.returncode != 0:
        raise RuntimeError("COLMAP 命令检查失败：{}\n{}".format(command, result.stdout[-3000:]))
    return result.stdout


def select_option(help_text, *names):
    available = set(re.findall(r"--([A-Za-z0-9_.]+)", help_text))
    for name in names:
        if name in available:
            return "--" + name
    raise RuntimeError("COLMAP 版本缺少所需参数：" + " / ".join(names))


def inspect_colmap(args):
    executable = shutil.which(args.colmap)
    if not executable:
        raise RuntimeError("找不到 COLMAP。macOS 请先运行 brew install colmap，或用 --colmap 指定路径。")
    extraction = read_help(executable, "feature_extractor")
    matching = read_help(executable, args.matcher + "_matcher")
    mapping = read_help(executable, "mapper")
    # COLMAP 3.x used SiftExtraction/SiftMatching for these shared options.
    options = {
        "extract_gpu": select_option(extraction, "FeatureExtraction.use_gpu", "SiftExtraction.use_gpu"),
        "extract_size": select_option(extraction, "FeatureExtraction.max_image_size", "SiftExtraction.max_image_size"),
        "extract_threads": select_option(extraction, "FeatureExtraction.num_threads", "SiftExtraction.num_threads"),
        "match_gpu": select_option(matching, "FeatureMatching.use_gpu", "SiftMatching.use_gpu"),
        "match_threads": select_option(matching, "FeatureMatching.num_threads", "SiftMatching.num_threads"),
        "mapper_threads": select_option(mapping, "Mapper.num_threads"),
    }
    if args.matcher == "sequential":
        options["overlap"] = select_option(matching, "SequentialMatching.overlap")
    for name in ["image_list_path", "ImageReader.camera_model", "ImageReader.single_camera",
                 "SiftExtraction.max_num_features"]:
        select_option(extraction, name)
    return executable, options


def collect_images(root):
    if not root.is_dir():
        raise ValueError("图片目录不存在：" + str(root))
    names = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
                   and not any(part.startswith(".") for part in p.relative_to(root).parts))
    if len(names) < 3:
        raise ValueError("至少需要 3 张 JPG/PNG/TIFF/BMP 图片；建议使用 20～40 张重叠照片。HEIC 请先转换。")
    if any("\n" in name or "\r" in name for name in names):
        raise ValueError("图片文件名不能包含换行符。")
    return names


def model_count(path):
    with path.open("rb") as handle:
        header = handle.read(8)
    if len(header) != 8:
        raise RuntimeError("模型文件不完整：" + str(path))
    return struct.unpack("<Q", header)[0]


def select_model(sparse):
    models = []
    for folder in sorted(sparse.iterdir()):
        if folder.is_dir() and all((folder / name).is_file()
                                   for name in ["images.bin", "points3D.bin", "cameras.bin"]):
            models.append({"path": str(folder), "registered_images": model_count(folder / "images.bin"),
                           "points3D": model_count(folder / "points3D.bin")})
    usable = [model for model in models if model["registered_images"] >= 2 and model["points3D"] > 0]
    if not usable:
        raise RuntimeError("未生成有效稀疏模型。请检查照片重叠、清晰度、纹理和视角变化；详情见 mapper 日志。")
    best = max(usable, key=lambda model: (model["registered_images"], model["points3D"]))
    return best, models


def point_statistics(path):
    count, error_sum, observations = 0, 0.0, 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split()
            count += 1
            error_sum += float(fields[7])
            observations += (len(fields) - 8) // 2
    return {"points3D": count, "mean_point_reprojection_error_px": error_sum / count if count else None,
            "observations": observations, "mean_track_length": observations / count if count else None}


class Pipeline:
    def __init__(self, output, executable, metadata):
        self.output = output
        self.executable = executable
        self.started = time.monotonic()
        self.state = dict(metadata, status="running", started_at=timestamp(), stages=[])
        self.save()

    def save(self):
        self.state["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        write_json(self.output / "status.json", self.state)

    def run(self, name, arguments, executable=None, env=None, terminal=False):
        command = [executable or self.executable] + [str(value) for value in arguments]
        log_path = self.output / "logs" / (name + ".log")
        stage = {"name": name, "status": "running", "command": command, "log": str(log_path),
                 "started_at": timestamp()}
        self.state["stages"].append(stage)
        self.state["current_stage"] = name
        self.save()
        print("\n[{}] 开始；日志：{}".format(name, log_path), flush=True)
        started = time.monotonic()
        process = None
        master = slave = None
        reader = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                log.write("$ " + shlex.join(command) + "\n\n")
                log.flush()
                if terminal:
                    import pty
                    master, slave = pty.openpty()
                process = subprocess.Popen(command, stdout=slave if terminal else log, stderr=subprocess.STDOUT,
                                           cwd=str(self.output), start_new_session=True, env=env)
                if terminal:
                    os.close(slave)
                    slave = None

                    def copy_terminal():
                        while True:
                            try:
                                data = os.read(master, 65536)
                            except OSError:
                                break
                            if not data:
                                break
                            try:
                                log.write(data.decode("utf-8", errors="replace"))
                                log.flush()
                            except (OSError, ValueError):
                                break

                    reader = threading.Thread(target=copy_terminal, daemon=True)
                    reader.start()
                while True:
                    try:
                        returncode = process.wait(timeout=5)
                        break
                    except subprocess.TimeoutExpired:
                        self.save()
                stage["returncode"] = returncode
                if reader:
                    reader.join(timeout=5)
            if returncode != 0:
                with log_path.open("rb") as log:
                    log.seek(max(0, log_path.stat().st_size - 4000))
                    tail = log.read().decode("utf-8", errors="replace")
                raise RuntimeError("{} 退出码 {}，日志末尾：\n{}".format(name, returncode, tail))
            stage["status"] = "completed"
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            stage["status"] = "cancelled"
            raise
        except Exception:
            stage["status"] = "failed"
            raise
        finally:
            if reader:
                reader.join(timeout=5)
            for descriptor in (master, slave):
                if descriptor is not None:
                    os.close(descriptor)
            stage["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.save()
        print("[{}] 完成，耗时 {:.1f} 秒".format(name, stage["elapsed_seconds"]), flush=True)


def reconstruct(args, executable, options, dense_tools=None):
    images = args.images.expanduser().resolve()
    names = collect_images(images)
    output = (args.output or Path("outputs") / datetime.now().strftime("%Y%m%d_%H%M%S_%f")).expanduser().resolve()
    if output == images or images in output.parents:
        raise ValueError("结果目录不能放在输入图片目录内。")
    if output.exists():
        raise ValueError("结果目录已存在，请指定新目录，避免覆盖或混用数据：" + str(output))
    output.mkdir(parents=True)
    for directory in ["logs", "sparse", "model_txt"]:
        (output / directory).mkdir()
    image_list = output / "image_list.txt"
    image_list.write_text("\n".join(names) + "\n", encoding="utf-8")
    settings = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    settings.update(images=str(images), output=str(output), colmap=executable, device="cpu", options=options)
    write_json(output / "config.json", settings)
    pipeline = Pipeline(output, executable, {"input_images": len(names), "images_path": str(images),
                        "requested_stage": args.stage})
    database = output / "database.db"
    print("图片：{} 张\n输出：{}\n计算：CPU，{} 线程".format(len(names), output, args.threads), flush=True)
    if args.matcher == "exhaustive" and len(names) > 100:
        print("提示：两两匹配工作量随图片数平方增长；有序拍摄数据可考虑 --matcher sequential。", flush=True)
    try:
        # Save actual command help for reproducibility across COLMAP releases.
        (output / "colmap_help.txt").write_text(read_help(executable, "feature_extractor"), encoding="utf-8")
        pipeline.run("01_features", ["feature_extractor", "--database_path", database,
                     "--image_path", images, "--image_list_path", image_list,
                     "--ImageReader.camera_model", args.camera_model,
                     "--ImageReader.single_camera", int(args.single_camera),
                     options["extract_gpu"], 0, options["extract_size"], args.max_image_size,
                     options["extract_threads"], args.threads, "--SiftExtraction.max_num_features", args.max_features])
        matching = [args.matcher + "_matcher", "--database_path", database,
                    options["match_gpu"], 0, options["match_threads"], args.threads]
        if args.matcher == "sequential":
            matching.extend([options["overlap"], args.overlap])
        pipeline.run("02_matching", matching)
        pipeline.run("03_mapper", ["mapper", "--database_path", database, "--image_path", images,
                     "--output_path", output / "sparse", options["mapper_threads"], args.threads])
        best, models = select_model(output / "sparse")
        pipeline.run("04_export_txt", ["model_converter", "--input_path", best["path"],
                     "--output_path", output / "model_txt", "--output_type", "TXT"])
        pipeline.run("05_export_ply", ["model_converter", "--input_path", best["path"],
                     "--output_path", output / "sparse.ply", "--output_type", "PLY"])
        if not (output / "sparse.ply").is_file() or (output / "sparse.ply").stat().st_size == 0:
            raise RuntimeError("PLY 导出未生成有效文件。")
        statistics = point_statistics(output / "model_txt" / "points3D.txt")
        if statistics["points3D"] == 0:
            raise RuntimeError("导出的点云为空。")
        warnings = []
        if best["registered_images"] < len(names):
            warnings.append("所选模型只注册了 {}/{} 张图片；其余图片可能重建失败或属于其他模型。".format(
                best["registered_images"], len(names)))
        if len(models) > 1:
            warnings.append("产生多个独立模型；导出注册图片最多的模型，其余保留在 sparse/ 中。")
        summary = dict(statistics, input_images=len(names), registered_images=best["registered_images"],
                       registration_ratio=best["registered_images"] / len(names), selected_model=best["path"],
                       models=models, warnings=warnings, elapsed_seconds=round(time.monotonic() - pipeline.started, 3),
                       pointcloud=str(output / "sparse.ply"), camera_model=args.camera_model, device="cpu",
                       completed_stage="sparse")
        write_json(output / "summary.json", summary)
        pipeline.state["sparse_summary"] = summary.copy()
        if args.stage != "sparse":
            from densify import run_dense
            if dense_tools is None:
                raise RuntimeError("稠密阶段缺少 OpenMVS 环境检查结果。")
            summary.update(run_dense(pipeline, images, Path(best["path"]), args, dense_tools))
            summary["sparse_pointcloud"] = str(output / "sparse.ply")
            summary["pointcloud"] = summary["dense_pointcloud"]
        summary["completed_stage"] = args.stage
        summary["elapsed_seconds"] = round(time.monotonic() - pipeline.started, 3)
        write_json(output / "summary.json", summary)
        pipeline.state.update(status="completed", finished_at=timestamp(), summary=summary)
        pipeline.save()
        print("\n重建完成：{}/{} 张图片，{} 个三维点".format(
            best["registered_images"], len(names), statistics["points3D"]), flush=True)
        print("点云：{}\n统计：{}".format(summary["pointcloud"], output / "summary.json"), flush=True)
        if summary.get("dense_points"):
            print("稠密点数：{}".format(summary["dense_points"]), flush=True)
        if summary.get("mesh"):
            print("网格：" + summary["mesh"], flush=True)
        if summary.get("textured_mesh"):
            print("纹理模型：" + summary["textured_mesh"], flush=True)
        for warning in warnings:
            print("提示：" + warning, flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        pipeline.state.update(status="cancelled" if cancelled else "failed", finished_at=timestamp(),
                              error="用户中断" if cancelled else str(error))
        pipeline.save()
        print("\n{}。中间结果和日志已保留：{}".format("任务已中断" if cancelled else "重建失败", output), file=sys.stderr)
        if not cancelled:
            print(str(error), file=sys.stderr)
        return 130 if cancelled else 1


def main(argv=None):
    parser = get_parser()
    args = parser.parse_args(argv)
    if not args.check_env and args.images is None:
        parser.error("需要 --images 图片目录；或使用 --check-env 检查环境")
    try:
        executable, options = inspect_colmap(args)
        dense_tools = None
        if args.stage != "sparse":
            from densify import inspect_openmvs
            read_help(executable, "image_undistorter")
            dense_tools = inspect_openmvs(args)
        if args.check_env:
            print("COLMAP 可用：" + executable)
            print("CPU 参数检查通过：" + json.dumps(options, ensure_ascii=False))
            if dense_tools:
                for name, tool in dense_tools.items():
                    print(name + " 可用：" + tool["executable"])
            return 0
        return reconstruct(args, executable, options, dense_tools)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("错误：" + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
