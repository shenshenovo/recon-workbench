"""Deterministic synthetic pairs for smoke tests, not real-view accuracy claims."""
import json
from pathlib import Path
import random

from recon_workbench.image_quality import pillow


def synthetic_scene(seed, size=(320, 240)):
    Image, _ = pillow()
    from PIL import ImageDraw, ImageFilter
    rng = random.Random(seed)
    pixels = bytes(rng.randrange(35, 220) for _ in range(size[0] * size[1]))
    image = Image.frombytes('L', size, pixels).filter(ImageFilter.GaussianBlur(1.2)).convert('RGB')
    draw = ImageDraw.Draw(image)
    for _ in range(24):
        x, y = rng.randrange(size[0] - 30), rng.randrange(size[1] - 30)
        color = tuple(rng.randrange(35, 220) for _ in range(3))
        draw.rectangle((x, y, x + rng.randrange(8, 30), y + rng.randrange(8, 30)), fill=color)
    return image


def make_synthetic_manifest(output, groups=4):
    """Keep all variants of one generated scene in exactly one split."""
    Image, _ = pillow()
    if isinstance(groups, bool) or not isinstance(groups, int) or groups < 2:
        raise ValueError('Synthetic smoke tests need at least two source groups.')
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'images').mkdir()
    images, pairs = [], []
    for index in range(groups):
        base = synthetic_scene(42 + index)
        prefix = 'scene_{:02d}'.format(index)
        split = 'calibration' if index % 2 == 0 else 'validation'
        source_name = prefix + '_original.png'
        base.save(output / 'images' / source_name)
        exact_name = prefix + '_exact.png'
        (output / 'images' / exact_name).write_bytes((output / 'images' / source_name).read_bytes())
        jpeg_name, resize_name = prefix + '_jpeg.jpg', prefix + '_resize.png'
        base.save(output / 'images' / jpeg_name, quality=75)
        base.resize((160, 120), Image.Resampling.LANCZOS).save(output / 'images' / resize_name)
        # A shifted foreground patch imitates a local view change, not real camera geometry.
        adjacent = base.copy()
        patch = base.crop((90, 65, 180, 155))
        adjacent.paste(patch, (110, 65))
        adjacent_name = prefix + '_synthetic_parallax.png'
        adjacent.save(output / 'images' / adjacent_name)
        for name in (source_name, exact_name, jpeg_name, resize_name, adjacent_name):
            images.append({'name': name, 'path': 'images/' + name,
                           'source_group': prefix, 'split': split})
        for name, label, kind in (
                (exact_name, 'exact_duplicate', 'exact_copy'),
                (jpeg_name, 'near_duplicate', 'jpeg_reencode'),
                (resize_name, 'near_duplicate', 'resize'),
                (adjacent_name, 'different', 'synthetic_parallax')):
            pairs.append({'a': source_name, 'b': name, 'label': label, 'kind': kind})
    manifest = {'version': 1, 'dataset_kind': 'synthetic_smoke', 'images': images, 'pairs': pairs,
                'note': 'Generated source/transform labels only. Synthetic parallax is not a '
                        'human-labeled neighboring camera view. Do not claim real-world accuracy.'}
    path = output / 'pairs.json'
    path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return path
