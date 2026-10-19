#!/usr/bin/env python3
"""Prepare a portable, undistorted COLMAP dataset for Brush (no training)."""

import argparse
import math
from pathlib import Path, PurePosixPath
import shutil
import struct
import sys
import tempfile

from .densify import load_source, positive_int
from .reconstruct import Pipeline, timestamp, write_json


def image_size(path):
    """Read PNG/JPEG dimensions without loading the image or adding dependencies."""
    with path.open("rb") as stream:
        signature = stream.read(24)
        if signature[:8] == b"\x89PNG\r\n\x1a\n" and signature[12:16] == b"IHDR":
            return struct.unpack(">II", signature[16:24])
        if signature[:2] != b"\xff\xd8":
            raise ValueError("仅支持 PNG/JPEG 图片：" + str(path))
        stream.seek(2)
        while True:
            if stream.read(1) != b"\xff":
                raise ValueError("JPEG 文件头损坏：" + str(path))
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if not marker or marker in (b"\xd9", b"\xda"):
                raise ValueError("JPEG 缺少尺寸信息：" + str(path))
            if marker[0] in (0x01, *range(0xD0, 0xD8)):
                continue
            raw = stream.read(2)
            if len(raw) != 2:
                raise ValueError("JPEG 文件截断：" + str(path))
            length = struct.unpack(">H", raw)[0]
            if length < 2:
                raise ValueError("JPEG 段长度无效：" + str(path))
            if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                raw = stream.read(5)
                if len(raw) != 5:
                    raise ValueError("JPEG 尺寸信息截断：" + str(path))
                height, width = struct.unpack(">HH", raw[1:])
                return width, height
            stream.seek(length - 2, 1)


def safe_image_path(root, name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("模型中的图片路径不安全：" + name)
    target = root.joinpath(*relative.parts).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("图片路径越出目录：" + name)
    return target


def validate_dataset(images, model_txt, max_size):
    cameras = {}
    for line in (model_txt / "cameras.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split()
        camera_id, model = int(fields[0]), fields[1]
        width, height = int(fields[2]), int(fields[3])
        params = [float(value) for value in fields[4:]]
        expected = {"PINHOLE": 4, "SIMPLE_PINHOLE": 3}.get(model)
        if expected is None or len(params) != expected:
            raise ValueError("去畸变后必须使用有效的 PINHOLE 相机。")
        if not all(math.isfinite(value) for value in params) or any(value <= 0 for value in params[:expected - 2]):
            raise ValueError("相机焦距或参数无效。")
        if min(width, height) <= 0 or max(width, height) > max_size:
            raise ValueError("相机尺寸超出指定范围。")
        if camera_id in cameras:
            raise ValueError("重复相机 ID。")
        cameras[camera_id] = {"model": model, "width": width, "height": height, "params": params}
    names, image_ids = [], set()
    with (model_txt / "images.txt").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.strip().split(maxsplit=9)
            if len(fields) != 10:
                raise ValueError("COLMAP 图片记录不完整。")
            image_id, camera_id, name = int(fields[0]), int(fields[8]), fields[9]
            if image_id in image_ids or name in names:
                raise ValueError("重复图片 ID 或名称。")
            pose = [float(value) for value in fields[1:8]]
            if not all(math.isfinite(value) for value in pose) or abs(sum(v*v for v in pose[:4]) - 1) > 1e-4:
                raise ValueError("图片位姿无效：" + name)
            if camera_id not in cameras:
                raise ValueError("图片引用了不存在的相机：" + name)
            path = safe_image_path(images, name)
            camera = cameras[camera_id]
            if image_size(path) != (camera["width"], camera["height"]):
                raise ValueError("图片尺寸与相机参数不一致：" + name)
            if stream.readline() == "":
                raise ValueError("COLMAP 图片缺少观测记录。")
            names.append(name)
            image_ids.add(image_id)
    points = 0
    with (model_txt / "points3D.txt").open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 12 or (len(fields) - 8) % 2:
                raise ValueError("稀疏点记录或轨迹无效。")
            if not all(math.isfinite(float(value)) for value in fields[1:4]):
                raise ValueError("稀疏点坐标无效。")
            if any(int(value) not in image_ids for value in fields[8::2]):
                raise ValueError("稀疏点引用了不存在的图片。")
            points += 1
    if len(names) < 2 or not points:
        raise ValueError("数据集至少需要两张已注册照片和非空稀疏点云。")
    return {"registered_images": len(names), "points3D": points,
            "image_names": sorted(names), "cameras": cameras,
            "camera_models": sorted({value["model"] for value in cameras.values()})}


def main(argv=None):
    parser = argparse.ArgumentParser(description="复用 SfM，整理 Brush 的照片、相机位姿和初始稀疏点。")
    parser.add_argument("--sfm-output", required=True, type=Path, help="已有 reconstruct.py 或 densify.py 结果目录")
    parser.add_argument("--output", required=True, type=Path, help="新的数据集目录，必须不存在")
    parser.add_argument("--max-image-size", type=positive_int, default=1200)
    parser.add_argument("--colmap", default="colmap")
    args = parser.parse_args(argv)
    pipeline = None
    try:
        source, images, model, _, original = load_source(args.sfm_output)
        executable = shutil.which(args.colmap)
        if not executable:
            raise ValueError("找不到 COLMAP。")
        output = args.output.expanduser().resolve()
        for protected in (source, images, model):
            if output == protected or protected in output.parents or output in protected.parents:
                raise ValueError("输出目录不能与原始照片或模型目录重叠。")
        output.mkdir(parents=True, exist_ok=False)
        (output / "logs").mkdir()
        write_json(output / "config.json", {"source_sfm": str(source), "images": str(images),
                   "source_model": str(model), "max_image_size": args.max_image_size,
                   "colmap": executable})
        pipeline = Pipeline(output, executable, {"source_sfm": str(source), "task": "prepare_brush"})
        with tempfile.TemporaryDirectory(prefix="design3-brush-") as temp:
            workspace = Path(temp) / "undistorted"
            pipeline.run("01_undistort", ["image_undistorter", "--image_path", images,
                         "--input_path", model, "--output_path", workspace,
                         "--output_type", "COLMAP", "--max_image_size", args.max_image_size])
            text = Path(temp) / "model_txt"
            text.mkdir()
            pipeline.run("02_inspect_model", ["model_converter", "--input_path", workspace / "sparse",
                         "--output_path", text, "--output_type", "TXT"])
            result = validate_dataset(workspace / "images", text, args.max_image_size)
            shutil.copytree(workspace / "images", output / "images")
            shutil.copytree(workspace / "sparse", output / "sparse" / "0")
            # Validate the final copies, not only temporary source files.
            validate_dataset(output / "images", text, args.max_image_size)
        result.update(input_images=original["input_images"],
                      unregistered_images=original["input_images"] - result["registered_images"],
                      source_sfm=str(source), source_model=str(model),
                      max_image_size=args.max_image_size, layout="COLMAP",
                      images_directory="images", model_directory="sparse/0",
                      status="prepared", training_completed=False,
                      evaluation_split="由训练阶段配置；当前保留全部已注册视图")
        write_json(output / "dataset.json", result)
        pipeline.state.update(status="completed", finished_at=timestamp(), summary=result)
        pipeline.save()
        print("\n整理完成：{} 张照片，{} 个初始点\nBrush 输入目录：{}".format(
            result["registered_images"], result["points3D"], output))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        if pipeline:
            pipeline.state.update(status="cancelled" if cancelled else "failed",
                                  finished_at=timestamp(), error="用户中断" if cancelled else str(error))
            pipeline.save()
        print("错误：" + ("用户中断" if cancelled else str(error)), file=sys.stderr)
        return 130 if cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
