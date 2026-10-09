#!/usr/bin/env python3
"""COLMAP undistortion + OpenMVS CPU dense/mesh/texture reconstruction."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def resolution_level(value):
    number = int(value)
    if not 0 <= number <= 4:
        raise argparse.ArgumentTypeError("缩放级别必须在 0～4 之间")
    return number


def add_dense_options(parser):
    parser.add_argument("--openmvs-bin", type=Path, help="OpenMVS 可执行文件目录，默认自动查找 .tools/openmvs 和 PATH")
    parser.add_argument("--dense-max-image-size", type=positive_int, default=1200,
                        help="去畸变输出和稠密计算的图像最大边长")
    parser.add_argument("--dense-resolution-level", type=resolution_level, default=1,
                        help="稠密阶段额外缩小 2^级别 倍；0 细节更多、计算更慢")


def required_tools(stage):
    names = ["InterfaceCOLMAP", "DensifyPointCloud"]
    if stage in ("mesh", "textured"):
        names.append("ReconstructMesh")
    if stage == "textured":
        names.append("TextureMesh")
    return names


def inspect_openmvs(args):
    tools = {}
    for name in required_tools(args.stage):
        if args.openmvs_bin:
            executable = shutil.which(str(args.openmvs_bin.expanduser().resolve() / name))
        else:
            root = Path(__file__).resolve().parent / ".tools" / "openmvs"
            candidates = sorted(root.rglob(name)) if root.is_dir() else []
            executable = next((str(path.resolve()) for path in candidates if path.is_file() and os.access(path, os.X_OK)),
                              shutil.which(name))
        if not executable:
            raise RuntimeError("找不到 OpenMVS 工具 {}；请安装官方 macOS arm64 包到 .tools/openmvs，"
                               "或通过 --openmvs-bin 指定目录。".format(name))
        # OpenMVS --help may return 1 and writes its own log to the working folder.
        with tempfile.TemporaryDirectory(prefix="design3-mvs-help-") as directory:
            result = subprocess.run([executable, "--help"], cwd=directory, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace", timeout=30)
        if "--input-file" not in result.stdout or "--output-file" not in result.stdout:
            raise RuntimeError("OpenMVS 启动或参数检查失败：{}\n{}".format(name, result.stdout[-3000:]))
        tools[name] = {"executable": executable, "help": result.stdout}
    return tools


def ply_counts(path):
    """Read counts without loading potentially millions of vertices into RAM."""
    counts = {}
    if not path.is_file():
        raise RuntimeError("缺少重建产物：" + str(path))
    with path.open("rb") as handle:
        if handle.readline().strip() != b"ply":
            raise RuntimeError("无效 PLY 文件：" + str(path))
        while handle.tell() <= 1024 * 1024:
            raw = handle.readline()
            if not raw:
                break
            line = raw.decode("ascii").strip()
            if line.startswith("element "):
                _, element, count = line.split()
                counts[element] = int(count)
            if line == "end_header":
                if counts.get("vertex", 0) <= 0 or not handle.read(1):
                    raise RuntimeError("重建点云或网格为空：" + str(path))
                return counts
    raise RuntimeError("PLY 文件头不完整：" + str(path))


def material_textures(material):
    textures = []
    for line in material.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("map_Kd "):
            texture = material.parent / line.strip().split(maxsplit=1)[1]
            if not texture.is_file() or texture.stat().st_size == 0:
                raise RuntimeError("纹理图片缺失或为空：" + str(texture))
            textures.append(str(texture))
    if not textures:
        raise RuntimeError("材质文件没有引用有效的纹理图片。")
    return textures


def run_dense(pipeline, images, model, args, tools):
    output = pipeline.output
    undistorted = output / "undistorted"
    work = output / "dense"
    work.mkdir()
    for name, tool in tools.items():
        (output / "logs" / (name + "_help.txt")).write_text(tool["help"], encoding="utf-8")
    pipeline.run("06_undistort", ["image_undistorter", "--image_path", images,
                 "--input_path", model, "--output_path", undistorted, "--output_type", "COLMAP",
                 "--max_image_size", args.dense_max_image_size])

    def run(name, tool_name, arguments):
        # Interface archives omit dense data/meshes; preserve native MVS scenes for downstream tools.
        common = ["--working-folder", work, "--max-threads", args.threads, "--archive-type", 1]
        for gpu_option in ("gpu-device", "cuda-device"):
            if re.search(r"--" + gpu_option + r"\b", tools[tool_name]["help"]):
                common.extend(["--" + gpu_option, -2])
                break
        pipeline.run(name, common + arguments,
                     executable=tools[tool_name]["executable"])

    scene = work / "scene.mvs"
    dense_scene = work / "dense.mvs"
    dense_ply = work / "dense.ply"
    run("07_import_mvs", "InterfaceCOLMAP", ["--input-file", undistorted, "--output-file", scene,
        "--image-folder", undistorted / "images"])
    if not scene.is_file():
        raise RuntimeError("OpenMVS 导入未生成 scene.mvs。")
    dense_args = ["--input-file", scene, "--output-file", dense_scene,
                  "--resolution-level", args.dense_resolution_level,
                  "--max-resolution", args.dense_max_image_size,
                  "--min-resolution", min(320, args.dense_max_image_size),
                  "--number-views", 5, "--number-views-fuse", 2, "--crop-to-roi", 0]
    run("08_dense", "DensifyPointCloud", dense_args)
    counts = ply_counts(dense_ply)
    from ply_compatible import convert
    compatible_ply = work / "dense_colmap.ply"
    convert(dense_ply, compatible_ply)
    if not dense_scene.is_file():
        raise RuntimeError("稠密重建未生成 dense.mvs。")
    result = {"dense_pointcloud": str(dense_ply), "dense_points": counts["vertex"],
              "colmap_pointcloud": str(compatible_ply),
              "dense_scene": str(dense_scene), "dense_device": "cpu", "completed_stage": "dense"}
    # Save usable dense output even if a later meshing/texturing stage fails.
    pipeline.state["dense_result"] = result.copy()
    pipeline.save()
    if args.stage in ("mesh", "textured"):
        mesh_scene = work / "mesh.mvs"
        mesh_ply = work / "mesh.ply"
        # Native scenes already carry the point visibility data; re-importing the PLY
        # discards/duplicates that state in some OpenMVS builds.
        run("09_mesh", "ReconstructMesh", ["--input-file", dense_scene, "--output-file", mesh_scene])
        mesh_counts = ply_counts(mesh_ply)
        if mesh_counts.get("face", 0) <= 0 or not mesh_scene.is_file():
            raise RuntimeError("未生成有效表面网格。")
        result.update(mesh=str(mesh_ply), mesh_vertices=mesh_counts["vertex"], mesh_faces=mesh_counts["face"],
                      completed_stage="mesh")
        pipeline.state["dense_result"] = result.copy()
        pipeline.save()
        if args.stage == "textured":
            run("10_texture", "TextureMesh", ["--input-file", mesh_scene,
                "--output-file", work / "textured.mvs", "--export-type", "obj"])
            textured = work / "textured.obj"
            material = work / "textured.mtl"
            if not textured.is_file() or not material.is_file() or textured.stat().st_size == 0:
                raise RuntimeError("纹理阶段没有生成有效 OBJ / MTL 文件。")
            textures = material_textures(material)
            result.update(textured_mesh=str(textured), texture_material=str(material),
                          texture_images=textures, completed_stage="textured")
            pipeline.state["dense_result"] = result.copy()
            pipeline.save()
    return result


def load_source(folder):
    folder = folder.expanduser().resolve()
    config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    images = Path(config["images"])
    model = Path(summary["selected_model"])
    if not images.is_dir():
        raise ValueError("原始图片已移动或丢失：" + str(images))
    if not all((model / name).is_file() for name in ["cameras.bin", "images.bin", "points3D.bin"]):
        raise ValueError("原始稀疏模型已移动或不完整：" + str(model))
    return folder, images, model, config, summary


def main(argv=None):
    from reconstruct import Pipeline, read_help, timestamp, write_json

    parser = argparse.ArgumentParser(description="复用已有 SfM 结果，通过 OpenMVS 在 CPU 上生成稠密点云、网格或纹理模型。")
    parser.add_argument("--sfm-output", type=Path, help="已有 reconstruct.py 结果目录")
    parser.add_argument("--output", type=Path, help="新的结果目录，默认 outputs/dense_时间戳")
    parser.add_argument("--stage", choices=["dense", "mesh", "textured"], default="dense")
    parser.add_argument("--colmap", default="colmap")
    parser.add_argument("--threads", type=positive_int, default=4)
    parser.add_argument("--check-env", action="store_true")
    add_dense_options(parser)
    args = parser.parse_args(argv)
    if not args.check_env and not args.sfm_output:
        parser.error("需要 --sfm-output 已有重建目录，或使用 --check-env")
    pipeline = None
    try:
        executable = shutil.which(args.colmap)
        if not executable:
            raise RuntimeError("找不到 COLMAP；请用 --colmap 指定路径。")
        read_help(executable, "image_undistorter")
        tools = inspect_openmvs(args)
        if args.check_env:
            print("COLMAP 可用：" + executable)
            for name, tool in tools.items():
                print(name + " 可用：" + tool["executable"])
            return 0
        source, images, model, _, sparse_summary = load_source(args.sfm_output)
        output = (args.output or Path("outputs") / datetime.now().strftime("dense_%Y%m%d_%H%M%S_%f")).expanduser().resolve()
        if output == images or images in output.parents or output == source or source in output.parents:
            raise ValueError("输出目录不能位于图片目录或原始 SfM 结果目录内。")
        output.mkdir(parents=True, exist_ok=False)
        (output / "logs").mkdir()
        settings = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
        settings.update(images=str(images), output=str(output), colmap=executable, device="cpu",
                        openmvs={key: value["executable"] for key, value in tools.items()})
        write_json(output / "config.json", settings)
        pipeline = Pipeline(output, executable, {"source_sfm": str(source), "selected_model": str(model),
                            "requested_stage": args.stage, "input_images": sparse_summary["input_images"]})
        print("复用稀疏模型：{}\n输出：{}\n计算：CPU，{} 线程".format(model, output, args.threads), flush=True)
        result = run_dense(pipeline, images, model, args, tools)
        summary = dict(sparse_summary, **result)
        summary.update(source_sfm=str(source),
                       sparse_pointcloud=sparse_summary.get("sparse_pointcloud", sparse_summary["pointcloud"]),
                       pointcloud=result["dense_pointcloud"], source_sfm_elapsed_seconds=sparse_summary["elapsed_seconds"],
                       elapsed_seconds=round(time.monotonic() - pipeline.started, 3))
        write_json(output / "summary.json", summary)
        pipeline.state.update(status="completed", finished_at=timestamp(), summary=summary)
        pipeline.save()
        print("\n完成：{} 个稠密点\n点云：{}".format(result["dense_points"], result["dense_pointcloud"]), flush=True)
        if result.get("mesh"):
            print("网格：" + result["mesh"], flush=True)
        if result.get("textured_mesh"):
            print("纹理模型：" + result["textured_mesh"], flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        if pipeline:
            pipeline.state.update(status="cancelled" if cancelled else "failed", finished_at=timestamp(),
                                  error="用户中断" if cancelled else str(error))
            pipeline.save()
        print("错误：" + ("用户中断；已有产物已保留" if cancelled else str(error)), file=sys.stderr)
        return 130 if cancelled else 1


if __name__ == "__main__":
    sys.exit(main())
