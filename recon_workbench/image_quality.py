"""Explainable image checks and immutable, selected inputs for reconstruction."""
import hashlib
import io
from importlib.metadata import version
import json
import math
from pathlib import Path
import shutil


VERSION = 'quality-v1'
DEFAULTS = {'max_side': 512, 'blur_threshold': 60.0, 'dark_mean': 40.0,
            'bright_mean': 215.0, 'clipped_ratio': 0.6, 'near_distance': 4}
MAX_BYTES = 64 * 1024**2


def pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise ValueError('图片检测需要 Pillow，请使用项目虚拟环境并安装 requirements.txt。') from None
    return Image, ImageOps


def policy(values=None):
    result = dict(DEFAULTS)
    values = values or {}
    if not isinstance(values, dict) or not set(values) <= {'blur_threshold', 'dark_mean', 'bright_mean', 'near_distance'}:
        raise ValueError('检测参数无效。')
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('检测阈值必须是有限数字。')
        result[key] = value
    if not 0 <= result['blur_threshold'] <= 100000 or not 0 <= result['dark_mean'] < result['bright_mean'] <= 255:
        raise ValueError('模糊阈值需在 0～100000，亮度阈值需满足 0 ≤ 暗阈值 < 亮阈值 ≤ 255。')
    if not isinstance(result['near_distance'], int) or not 0 <= result['near_distance'] <= 8:
        raise ValueError('相似哈希距离需为 0～8 的整数。')
    return result


def read_image_bytes(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValueError('图片为空或超过 64 MB。')
    return data


def metrics(data, settings):
    Image, ImageOps = pillow()
    with Image.open(io.BytesIO(data)) as source:
        if source.format not in ('JPEG', 'PNG') or source.width * source.height > 40_000_000:
            raise ValueError('仅支持 JPG/PNG，图片像素数不能超过 4000 万。')
        source.load()  # Read pixel payload, not just the header.
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
        gray = oriented.convert('L')
        gray.thumbnail((settings['max_side'], settings['max_side']), Image.Resampling.LANCZOS)
    w, h = gray.size
    pixels = gray.tobytes()
    histogram = gray.histogram()
    count = len(pixels)
    mean = sum(i * n for i, n in enumerate(histogram)) / count
    std = math.sqrt(max(0, sum((i - mean)**2 * n for i, n in enumerate(histogram)) / count))
    total = squares = 0
    interior = max(0, w - 2) * max(0, h - 2)
    for y in range(1, h - 1):
        offset = y * w
        for x in range(1, w - 1):
            i = offset + x
            value = pixels[i-1] + pixels[i+1] + pixels[i-w] + pixels[i+w] - 4 * pixels[i]
            total += value
            squares += value * value
    sharpness = max(0, squares / interior - (total / interior)**2) if interior else 0.0
    small = gray.resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (small[y*9+x] > small[y*9+x+1])
    return {'width': width, 'height': height, 'analysis_width': w, 'analysis_height': h,
            'laplacian_variance': round(sharpness, 4), 'mean_luminance': round(mean, 4),
            'luminance_std': round(std, 4), 'dark_ratio': round(sum(histogram[:21]) / count, 6),
            'bright_ratio': round(sum(histogram[235:]) / count, 6), 'dhash': format(bits, '016x')}


def analyze(root, images, thresholds=None):
    Image, _ = pillow()  # Missing dependency is a request error, not 500 decode errors.
    settings = policy(thresholds)
    if not images or len(images) > 500:
        raise ValueError('请选择含 1～500 张 JPG/PNG 的原始图片集。')
    root = Path(root).resolve()
    results, hashes, candidates = [], {}, []
    for image in sorted(images, key=lambda item: item['name']):
        item = {'name': image['name'], 'path': image['path'], 'sha256': None,
                'bytes': None, 'decodable': False, 'flags': [], 'duplicate_of': None, 'similar_to': None}
        try:
            path = (root / image['path']).resolve()
            path.relative_to(root)
            data = read_image_bytes(path)
            item.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))
            item.update(metrics(data, settings), decodable=True)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
            item['flags'].append('decode_error')
            item['error'] = str(error)
            results.append(item)
            continue
        if item['laplacian_variance'] < settings['blur_threshold']:
            item['flags'].append('blurry')
        if item['mean_luminance'] < settings['dark_mean'] or item['dark_ratio'] >= settings['clipped_ratio']:
            item['flags'].append('underexposed')
        if item['mean_luminance'] > settings['bright_mean'] or item['bright_ratio'] >= settings['clipped_ratio']:
            item['flags'].append('overexposed')
        if item['sha256'] in hashes:
            item['flags'].append('exact_duplicate')
            item['duplicate_of'] = hashes[item['sha256']]
        else:
            hashes[item['sha256']] = item['name']
            # Low texture and very different exposure/aspect ratios are unreliable matches.
            if item['luminance_std'] >= 8:
                for previous in candidates:
                    if abs(item['width']/item['height'] / (previous['width']/previous['height']) - 1) > 0.02:
                        continue
                    distance = (int(item['dhash'], 16) ^ int(previous['dhash'], 16)).bit_count()
                    if distance <= settings['near_distance'] and abs(item['mean_luminance'] - previous['mean_luminance']) <= 10:
                        item['flags'].append('near_duplicate')
                        item['similar_to'] = previous['name']
                        item['hash_distance'] = distance
                        break
                candidates.append(item)
        results.append(item)
    identity = {'version': VERSION, 'pillow_version': version('Pillow'), 'policy': settings,
                'files': [(item['name'], item['sha256']) for item in results]}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return {'version': VERSION, 'pillow_version': identity['pillow_version'], 'policy': settings, 'fingerprint': fingerprint, 'images': results,
            'counts': {'total': len(results), 'decodable': sum(item['decodable'] for item in results),
                       **{flag: sum(flag in item['flags'] for item in results) for flag in
                          ('decode_error', 'blurry', 'underexposed', 'overexposed', 'exact_duplicate', 'near_duplicate')}}}


def validate_selection(report, names, fingerprint):
    if not report or report['fingerprint'] != fingerprint:
        raise ValueError('检测报告已变化，请重新查看检测结果。')
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError('图片名单必须是没有重复名称的数组。')
    available = {item['name']: item for item in report['images']}
    if len(names) < 2:
        raise ValueError('至少选择两张可解码图片；实际重建通常需要更多有重叠的视角。')
    if any(name not in available or not available[name]['decodable'] for name in names):
        raise ValueError('选中了未知或无法解码的图片，请重新筛选。')
    return sorted(names)


def snapshot(root, report, names, destination):
    """Copy exactly reviewed files, checking bytes as copied; never link mutable input."""
    root, destination = Path(root).resolve(), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    chosen = {item['name']: item for item in report['images']}
    manifest = []
    try:
        for name in names:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name:
                raise ValueError('图片名称无效。')
            item = chosen[name]
            source = (root / item['path']).resolve()
            source.relative_to(root)
            data = read_image_bytes(source)
            if hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('图片已变化，请重新检测并保存筛选：' + name)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest.append({'name': name, 'source': item['path'], 'sha256': item['sha256'], 'bytes': len(data)})
        return manifest
    except BaseException:
        shutil.rmtree(destination)
        raise
