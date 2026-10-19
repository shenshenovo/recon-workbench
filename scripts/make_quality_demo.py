#!/usr/bin/env python3
"""Create disposable image-quality test inputs without changing source photos."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import random
import shutil
import struct

from recon_workbench.image_quality import pillow


def generate(output, source=None):
    Image, ImageOps = pillow()
    from PIL import ImageEnhance, ImageFilter
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError('输出目录已存在，请选择新目录。')
    candidates = []
    if source:
        source = Path(source).expanduser().resolve()
        if not source.exists():
            raise ValueError('样例来源不存在。')
        candidates = [source] if source.is_file() else sorted(path for path in source.rglob('*')
            if path.is_file() and path.suffix.lower() in ('.jpg','.jpeg','.png'))[:8]
        if not candidates:
            raise ValueError('来源目录中没有 JPG/PNG。')
        if output == source or source in output.parents or output in source.parents:
            raise ValueError('样例输出不能与来源目录重叠。')
    output.mkdir(parents=True)
    if candidates:
        originals = []
        for i, path in enumerate(candidates):
            name = 'view_{:02d}'.format(i) + path.suffix.lower()
            shutil.copyfile(path, output / name)
            originals.append(name)
        with Image.open(output / originals[0]) as image:
            base = ImageOps.exif_transpose(image).convert('RGB')
            base.thumbnail((1200,1200), Image.Resampling.LANCZOS)
    else:
        rng = random.Random(42)
        base = Image.frombytes('L',(512,384),bytes(rng.randrange(40,216) for _ in range(512*384))).convert('RGB')
        base.save(output / 'view_00.png')
        base.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(output / 'view_01.png')
        originals = ['view_00.png','view_01.png']
    duplicate = 'zz_exact_copy' + Path(originals[0]).suffix
    shutil.copyfile(output / originals[0], output / duplicate)
    base.filter(ImageFilter.GaussianBlur(12)).save(output / 'zz_blur.jpg', quality=95)
    ImageEnhance.Brightness(base).enhance(0.1).save(output / 'zz_dark.jpg', quality=95)
    base.point(lambda value: min(255, round(value * 0.08 + 235))).save(output / 'zz_bright.jpg', quality=95)
    base.save(output / 'zz_similar.jpg', quality=70)
    (output / 'zz_broken.png').write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I',13) + b'IHDR' + struct.pack('>II',64,48))
    info = {'source':str(source) if source else None, 'originals': originals,
            'examples': {'blurry':'zz_blur.jpg','underexposed':'zz_dark.jpg','overexposed':'zz_bright.jpg',
                         'exact_duplicate':duplicate,'near_duplicate_candidate':'zz_similar.jpg','decode_error':'zz_broken.png'},
            'note':'相似哈希只是提示；质量阈值并非所有场景的统一标准。' if candidates else
                   '合成样例仅验证质量检测，不用于评价真实三维重建效果。'}
    (output / 'demo.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    return info


def main():
    parser = argparse.ArgumentParser(description='生成质量检测样例：原图副本、模糊、曝光、重复和损坏文件。')
    parser.add_argument('--source', type=Path, help='可选的真实照片或照片目录；不提供则使用合成纹理')
    parser.add_argument('--output', type=Path, default=Path('data') / datetime.now().strftime('quality-demo_%Y%m%d_%H%M%S'))
    args = parser.parse_args()
    try:
        info = generate(args.output, args.source)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print('样例目录：' + str(args.output.resolve()))
    print(info['note'])


if __name__ == '__main__':
    main()
