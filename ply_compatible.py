#!/usr/bin/env python3
"""Export OpenMVS binary PLY as XYZ/RGB PLY readable by COLMAP."""

import argparse
from pathlib import Path
import struct


TYPES = {"char": "b", "int8": "b", "uchar": "B", "uint8": "B",
         "short": "h", "int16": "h", "ushort": "H", "uint16": "H",
         "int": "i", "int32": "i", "uint": "I", "uint32": "I",
         "float": "f", "float32": "f", "double": "d", "float64": "d"}
FIELDS = ("x", "y", "z", "red", "green", "blue")


def read_exact(handle, size):
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("PLY 数据截断，无法完成转换。")
    return data


def convert(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists() or source.resolve() == destination.resolve():
        raise ValueError("请使用新的输出文件，避免覆盖已有点云。")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        raise ValueError("转换临时文件已存在：" + str(temporary))
    try:
        with source.open("rb") as handle:
            if handle.readline().strip() != b"ply":
                raise ValueError("输入不是 PLY 文件。")
            properties, count, element, encoding = [], None, None, None
            while handle.tell() < 1024 * 1024:
                raw = handle.readline()
                if not raw:
                    raise ValueError("PLY 文件头截断。")
                parts = raw.decode("ascii").split()
                if not parts:
                    continue
                if parts[0] == "format":
                    encoding = parts[1]
                elif parts[0] == "element":
                    element = parts[1]
                    if element == "vertex":
                        count = int(parts[2])
                    elif int(parts[2]) != 0:
                        raise ValueError("此转换器仅处理点云，请将网格交给 MeshLab 查看。")
                elif parts[0] == "property" and element == "vertex":
                    if parts[1] == "list":
                        properties.append((parts[4], TYPES[parts[2]], TYPES[parts[3]]))
                    else:
                        properties.append((parts[2], TYPES[parts[1]], None))
                elif parts[0] == "end_header":
                    break
            else:
                raise ValueError("PLY 文件头过长。")
            if encoding != "binary_little_endian" or not count:
                raise ValueError("需要非空 binary_little_endian 点云（OpenMVS 默认格式）。")
            if not set(FIELDS).issubset({name for name, _, item in properties if item is None}):
                raise ValueError("点云缺少坐标或 RGB 颜色属性。")
            scalar = None
            if all(item is None for _, _, item in properties):
                scalar = struct.Struct("<" + "".join(code for _, code, _ in properties))
            output_record = struct.Struct("<fffBBB")
            with temporary.open("xb") as out:
                out.write(("ply\nformat binary_little_endian 1.0\nelement vertex {}\n"
                           "property float x\nproperty float y\nproperty float z\n"
                           "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n".format(count)).encode("ascii"))
                for _ in range(count):
                    if scalar:
                        values = dict(zip((p[0] for p in properties), scalar.unpack(read_exact(handle, scalar.size))))
                    else:
                        values = {}
                        for name, code, item in properties:
                            value = struct.unpack("<" + code, read_exact(handle, struct.calcsize(code)))[0]
                            if item is None:
                                values[name] = value
                            else:
                                if value < 0 or value > count:
                                    raise ValueError("PLY 列表属性长度无效。")
                                read_exact(handle, value * struct.calcsize(item))
                    out.write(output_record.pack(*(values[name] for name in FIELDS)))
        temporary.replace(destination)
        return count
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出 COLMAP 兼容的 XYZ/RGB 点云，保留原始文件。")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print("已转换 {} 个点：{}".format(convert(args.input, args.output), args.output))
