#!/usr/bin/env python3
"""Build a source-preserving quality benchmark from real photos.

Generated transformations are useful for checking implementation and threshold
behavior, but are not human-labeled neighboring camera views. Human-reviewed
negative pairs can be supplied separately and are the only pairs labeled
``different`` by this tool.
"""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import shutil

from recon_workbench.image_quality import pillow
from .evaluate_quality import load_manifest


GROUP_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z')
VARIANTS = ('original', 'exact_copy', 'jpeg_reencode', 'resize', 'crop', 'rotation')


def parse_source(value):
    if not isinstance(value, str) or '=' not in value:
        raise ValueError('--source must be GROUP=IMAGE_DIRECTORY')
    group, path = value.split('=', 1)
    if not GROUP_NAME.fullmatch(group) or not path:
        raise ValueError('Source groups need 1-40 ASCII letters/digits, underscores or hyphens.')
    return group, Path(path).expanduser().resolve()


def parse_mapping(values, allowed, name):
    mapping = {}
    for value in values:
        if '=' not in value:
            raise ValueError('--{} must be GROUP=SPLIT'.format(name))
        group, split = value.split('=', 1)
        if group not in allowed or split not in ('calibration', 'validation'):
            raise ValueError('Invalid {} mapping: {}'.format(name, value))
        if group in mapping:
            raise ValueError('Duplicate {} mapping for group: {}'.format(name, group))
        mapping[group] = split
    return mapping


def source_images(path, limit):
    if not path.is_dir():
        raise ValueError('Source directory does not exist: ' + str(path))
    names = sorted(item for item in path.rglob('*')
                   if item.is_file() and item.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    if len(names) < 1:
        raise ValueError('Source directory has no JPG/PNG files: ' + str(path))
    if len(names) <= limit:
        return names
    if limit == 1:
        return names[:1]
    return [names[index * (len(names) - 1) // (limit - 1)] for index in range(limit)]


def load_negative_pairs(path):
    if path is None:
        return []
    document = json.loads(Path(path).expanduser().resolve().read_text(encoding='utf-8'))
    if not isinstance(document, list):
        raise ValueError('--negative-pairs must contain a JSON array')
    result = []
    for row in document:
        if not isinstance(row, dict) or not isinstance(row.get('a'), str) or not isinstance(row.get('b'), str):
            raise ValueError('Every negative pair needs string fields a and b')
        kind = row.get('kind', 'human_reviewed_negative')
        if not isinstance(kind, str) or not kind:
            raise ValueError('Negative pair kinds must be nonempty strings.')
        result.append({'a': row['a'], 'b': row['b'], 'label': 'different',
                       'kind': kind})
    return result


def variant_names(group, index, suffix):
    stem = '{}_{:03d}'.format(group, index)
    return dict(zip(VARIANTS, (stem + suffix, stem + '_exact' + suffix, stem + '_jpeg.jpg',
                               stem + '_resize.jpg', stem + '_crop.jpg', stem + '_rotate.jpg')))


def plan_sources(output, sources, limit, overrides, split_by_source=False):
    if output.exists():
        raise ValueError('Output directory already exists: ' + str(output))
    if type(limit) is not int or not 1 <= limit <= 83:
        raise ValueError('limit must be an integer from 1 to 83.')
    groups = [parse_source(value) for value in sources]
    if not groups or len({group.casefold() for group, _ in groups}) != len(groups):
        raise ValueError('Source groups must be nonempty and unique, ignoring case.')
    if (not isinstance(overrides, dict) or not set(overrides).issubset(group for group, _ in groups)
            or any(not isinstance(split, str) or split not in ('calibration', 'validation')
                   for split in overrides.values())):
        raise ValueError('Invalid source-group split overrides.')
    roots, hashes, selected, counts = [], {}, [], {'calibration': 0, 'validation': 0}
    for index, (group, source) in enumerate(groups):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError('Output and source directories cannot overlap.')
        if any(source.is_relative_to(root) or root.is_relative_to(source) for root in roots):
            raise ValueError('Source directories cannot overlap or repeat.')
        roots.append(source)
        group_split = overrides.get(group, 'calibration' if index % 2 == 0 else 'validation')
        paths = source_images(source, limit)
        for source_index, path in enumerate(paths):
            split = group_split if group in overrides else (
                ('calibration', 'validation')[source_index % 2] if split_by_source else group_split)
            counts[split] += len(VARIANTS)
            if counts[split] > 500:
                raise ValueError('Generated images cannot exceed 500 in either split.')
            source_group = '{}_{:03d}'.format(group, source_index) if split_by_source else group
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in hashes and hashes[digest] != source_group:
                raise ValueError('Different source groups cannot reuse identical image bytes.')
            hashes[digest] = source_group
            selected.append({'group': group, 'source_group': source_group, 'split': split, 'path': path,
                             'relative_path': path.relative_to(source).as_posix(), 'sha256': digest,
                             'names': variant_names(group, source_index, path.suffix.lower())})
    return selected


def validate_negatives(selected, pairs):
    splits, seen = {}, set()
    for source in selected:
        names = source['names']
        splits.update({name: source['split'] for name in names.values()})
        seen.update(tuple(sorted((names['original'], name)))
                    for kind, name in names.items() if kind != 'original')
    for pair in pairs:
        a, b = pair['a'], pair['b']
        if a == b or a not in splits or b not in splits:
            raise ValueError('Negative pairs must reference two different generated image names.')
        if splits[a] != splits[b]:
            raise ValueError('Negative pairs cannot cross calibration/validation splits.')
        key = tuple(sorted((a, b)))
        if key in seen:
            raise ValueError('Negative pairs cannot repeat or contradict another declared pair.')
        seen.add(key)


def save_variants(source, root):
    Image, ImageOps = pillow()
    names = source['names']
    original = root / names['original']
    shutil.copyfile(source['path'], original)
    if hashlib.sha256(original.read_bytes()).hexdigest() != source['sha256']:
        raise ValueError('Source bytes changed while generating: ' + str(source['path']))
    shutil.copyfile(original, root / names['exact_copy'])
    try:
        with Image.open(original) as opened:
            base = ImageOps.exif_transpose(opened).convert('RGB')
            base.load()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError('Cannot transform image ' + str(source['path']) + ': ' + str(error)) from error
    small_size = (max(1, base.width // 2), max(1, base.height // 2))
    left, top = base.width // 10, base.height // 10
    crop_box = (left, top, base.width - left, base.height - top)
    jpeg_options = {'subsampling': 2, 'optimize': False}
    base.save(root / names['jpeg_reencode'], quality=75, **jpeg_options)
    base.resize(small_size, Image.Resampling.LANCZOS).save(
        root / names['resize'], quality=90, **jpeg_options)
    base.crop(crop_box).save(root / names['crop'], quality=90, **jpeg_options)
    base.rotate(3, expand=False, resample=Image.Resampling.BICUBIC).save(
        root / names['rotation'], quality=90, **jpeg_options)
    transforms = {
        'original': {'kind': 'identity'}, 'exact_copy': {'kind': 'exact_copy'},
        'jpeg_reencode': {'kind': 'jpeg_reencode', 'quality': 75, **jpeg_options},
        'resize': {'kind': 'resize', 'size': list(small_size), 'resampling': 'lanczos',
                   'quality': 90, **jpeg_options},
        'crop': {'kind': 'crop', 'box': list(crop_box), 'quality': 90, **jpeg_options},
        'rotation': {'kind': 'rotation', 'degrees': 3, 'expand': False, 'resampling': 'bicubic',
                     'fill': [0, 0, 0], 'quality': 90, **jpeg_options}}
    images, pairs = [], []
    for kind, name in names.items():
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        images.append({'name': name, 'path': 'images/' + name, 'sha256': digest,
                       'source_group': source['source_group'], 'split': source['split'],
                       'scene': source['group'],
                       'source': {'path': str(source['path']), 'relative_path': source['relative_path'],
                                  'sha256': source['sha256']},
                       'transform': transforms[kind]})
        if kind != 'original':
            label = 'challenge' if kind in ('crop', 'rotation') else (
                'exact_duplicate' if digest == source['sha256'] else 'near_duplicate')
            pairs.append({'a': names['original'], 'b': name, 'label': label, 'kind': kind})
    return images, pairs


def make_benchmark(output, sources, limit=20, split_overrides=None, negative_pairs=None,
                   split_by_source=False):
    output = Path(output).expanduser().resolve()
    selected = plan_sources(output, sources, limit, {} if split_overrides is None else split_overrides,
                            split_by_source=split_by_source)
    negatives = load_negative_pairs(negative_pairs)
    validate_negatives(selected, negatives)
    output.mkdir(parents=True)
    images_root = output / 'images'
    images_root.mkdir()
    images, pairs = [], []
    for source in selected:
        source_images, source_pairs = save_variants(source, images_root)
        images.extend(source_images)
        pairs.extend(source_pairs)
    pairs.extend(negatives)
    manifest = {'version': 1, 'dataset_kind': 'generated_transforms',
                'images': images, 'pairs': pairs,
                'generator': {'name': 'make_quality_benchmark', 'version': 1, 'Pillow': version('Pillow'),
                              'selection': 'Evenly spaced indices in sorted source paths, up to limit.',
                              'split_by_source': bool(split_by_source),
                              'transformed_images': 'EXIF-oriented RGB; copied originals remain unchanged.'},
                'note': 'Generated transforms are not human-labeled camera-view pairs. '
                        'challenge pairs are excluded from main precision/recall. '
                        'different pairs require human review via --negative-pairs.'}
    manifest_path = output / 'pairs.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Validate the complete artifact before reporting success.
    load_manifest(manifest_path)
    return manifest_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', required=True,
                        help='GROUP=directory, repeat for scenes')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--split', action='append', default=[],
                        help='GROUP=calibration or GROUP=validation')
    parser.add_argument('--split-by-source', action='store_true',
                        help='Alternate selected source images between calibration and validation.')
    parser.add_argument('--negative-pairs', type=Path,
                        help='JSON array of human-reviewed {a,b,kind} pairs')
    args = parser.parse_args(argv)
    try:
        groups = [parse_source(value)[0] for value in args.source]
        overrides = parse_mapping(args.split, set(groups), 'split')
        path = make_benchmark(args.output, args.source, args.limit, overrides, args.negative_pairs,
                              split_by_source=args.split_by_source)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, str(error) + '\n')
    print('Benchmark manifest: ' + str(path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
