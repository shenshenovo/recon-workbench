"""Read sampled XYZ/RGB from PLY for the in-page WebGL point viewer."""
import json
import math
from pathlib import Path
import struct

from ply_compatible import TYPES, read_exact


def cloud_packet(path, limit=300000):
    properties, count, element, encoding = [], None, None, None
    with Path(path).open('rb') as stream:
        if stream.readline().strip() != b'ply':
            raise ValueError('不是 PLY 文件。')
        while stream.tell() < 65536:
            raw = stream.readline()
            if not raw:
                raise ValueError('PLY 文件头截断。')
            fields = raw.decode('ascii').split()
            if not fields:
                continue
            if fields[0] == 'format':
                encoding = fields[1]
            elif fields[0] == 'element':
                element = fields[1]
                if element == 'vertex':
                    count = int(fields[2])
            elif fields[0] == 'property' and element == 'vertex':
                if fields[1] == 'list':
                    properties.append((fields[4], TYPES[fields[2]], TYPES[fields[3]]))
                else:
                    properties.append((fields[2], TYPES[fields[1]], None))
            elif fields[0] == 'end_header':
                break
        else:
            raise ValueError('PLY 文件头过长。')
        if count is None or not 0 < count <= 100000000 or encoding not in ('binary_little_endian', 'binary_big_endian', 'ascii'):
            raise ValueError('不支持的 PLY 格式或点数。')
        names = [name for name, _, item in properties if item is None]
        if not {'x', 'y', 'z'}.issubset(names):
            raise ValueError('缺少 XYZ 坐标。')
        if 'opacity' in names and 'f_dc_0' in names:
            raise ValueError('这是 Gaussian PLY，请用 Brush 查看高斯渲染；不能当作普通点云。')
        endian = '>' if encoding == 'binary_big_endian' else '<'
        fixed = struct.Struct(endian + ''.join(code for _, code, _ in properties)) if all(item is None for _, _, item in properties) else None
        stride = max(1, math.ceil(count / limit))
        output = bytearray()
        minimum, maximum = [math.inf] * 3, [-math.inf] * 3
        start = stream.tell()
        if fixed and encoding != 'ascii' and Path(path).stat().st_size < start + count * fixed.size:
            raise ValueError('PLY 数据截断。')
        indices = range(0, count, stride) if fixed and encoding != 'ascii' else range(count)
        for index in indices:
            if encoding == 'ascii':
                fields = stream.readline().split()
                values, offset = {}, 0
                for name, code, item in properties:
                    if offset >= len(fields):
                        raise ValueError('PLY 数据截断。')
                    value = float(fields[offset])
                    offset += 1
                    if item is None:
                        values[name] = value
                    else:
                        offset += int(value)
                if offset != len(fields):
                    raise ValueError('PLY 属性数量不匹配。')
            elif fixed:
                stream.seek(start + index * fixed.size)
                values = dict(zip(names, fixed.unpack(read_exact(stream, fixed.size))))
            else:
                values = {}
                for name, code, item in properties:
                    value = struct.unpack(endian + code, read_exact(stream, struct.calcsize(code)))[0]
                    if item is None:
                        values[name] = value
                    else:
                        if not 0 <= value <= 1000000:
                            raise ValueError('PLY 列表长度异常。')
                        read_exact(stream, value * struct.calcsize(item))
            if index % stride:
                continue
            xyz = [values[name] for name in ('x', 'y', 'z')]
            if not all(math.isfinite(value) for value in xyz):
                continue
            rgb = [max(0, min(255, values.get(name, 180))) / 255 for name in ('red', 'green', 'blue')]
            output.extend(struct.pack('<ffffff', *(xyz + rgb)))
            for axis in range(3):
                minimum[axis] = min(minimum[axis], xyz[axis])
                maximum[axis] = max(maximum[axis], xyz[axis])
        if not output:
            raise ValueError('点云没有有效坐标。')
        metadata = {'original_points': count, 'display_points': len(output) // 24,
                    'min': minimum, 'max': maximum, 'sampled': stride > 1}
        # Frame the central 98% of points so isolated outliers don't shrink the scene.
        axes = [sorted(row[axis] for row in struct.iter_unpack('<ffffff', output)) for axis in range(3)]
        n = len(axes[0])
        metadata['fit_min'] = [axis[int((n-1)*.01)] for axis in axes]
        metadata['fit_max'] = [axis[int((n-1)*.99)] for axis in axes]
        header = json.dumps(metadata).encode()
        header += b' ' * ((-len(header)) % 4)
        return struct.pack('<I', len(header)) + header + output
