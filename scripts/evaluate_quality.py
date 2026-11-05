#!/usr/bin/env python3
"""Compare gated dHash, pHash and their combination on declared labeled pairs."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path, PurePosixPath
import platform
import statistics
import time

from recon_workbench import image_quality as quality
from .quality_benchmark_data import make_synthetic_manifest


LABELS = {'exact_duplicate', 'near_duplicate', 'different', 'challenge'}
DATASET_KINDS = {'synthetic_smoke', 'human_labeled', 'generated_transforms'}
SPLITS = {'calibration', 'validation'}
METHOD_FIELDS = {'dhash': 'dhash_match', 'phash': 'phash_match', 'combined': 'match'}


def image_path(root, value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Image paths must be nonempty relative POSIX paths.')
    relative = PurePosixPath(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Image paths cannot leave the manifest directory.')
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Image is missing or outside the manifest directory: ' + value)
    return path


def load_manifest(path):
    path = Path(path).expanduser().resolve()
    content = path.read_bytes()
    document = json.loads(content)
    if (not isinstance(document, dict) or type(document.get('version')) is not int
            or document['version'] != 1):
        raise ValueError('Expected a version 1 pair manifest.')
    dataset_kind = document.get('dataset_kind')
    if not isinstance(dataset_kind, str) or dataset_kind not in DATASET_KINDS:
        raise ValueError('Unsupported dataset_kind.')
    if not isinstance(document.get('images'), list) or not isinstance(document.get('pairs'), list):
        raise ValueError('Manifest images and pairs must be arrays.')
    images, groups, paths = {}, {}, set()
    for row in document['images']:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name']:
            raise ValueError('Every image needs a nonempty unique name.')
        name, group, split = row['name'], row.get('source_group'), row.get('split')
        if (name in images or not isinstance(group, str) or not group
                or not isinstance(split, str) or split not in SPLITS):
            raise ValueError('Invalid image name, source_group or split.')
        resolved = image_path(path.parent, row.get('path'))
        if resolved in paths:
            raise ValueError('The same image path is registered more than once.')
        if group in groups and groups[group] != split:
            raise ValueError('One source_group cannot cross calibration/validation splits.')
        digest = row.get('sha256')
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                   or any(character not in '0123456789abcdef' for character in digest)):
            raise ValueError('Image sha256 must be a lowercase 64-character hexadecimal string.')
        groups[group], images[name] = split, dict(row, resolved=resolved)
        paths.add(resolved)
    pairs, seen = [], set()
    for row in document['pairs']:
        if (not isinstance(row, dict) or not isinstance(row.get('label'), str)
                or row['label'] not in LABELS):
            raise ValueError('Unknown pair label.')
        a, b = row.get('a'), row.get('b')
        if not isinstance(a, str) or not isinstance(b, str) or a == b or a not in images or b not in images:
            raise ValueError('Pairs must reference two different registered images.')
        key = tuple(sorted((a, b)))
        if key in seen:
            raise ValueError('Duplicate or contradictory pair labels are not allowed.')
        if images[a]['split'] != images[b]['split']:
            raise ValueError('A pair cannot cross calibration/validation splits.')
        if not isinstance(row.get('kind'), str) or not row['kind']:
            raise ValueError('Every pair needs a nonempty kind.')
        seen.add(key)
        pairs.append(dict(row, split=images[a]['split']))
    if not images or not pairs:
        raise ValueError('Manifest needs at least one image pair.')
    return {'images': images, 'pairs': pairs, 'path': path, 'dataset_kind': document['dataset_kind'],
            'sha256': hashlib.sha256(content).hexdigest()}


def classification_counts(rows, field, exact=False):
    counts = {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0}
    for row in rows:
        expected = row['label'] == ('exact_duplicate' if exact else 'near_duplicate')
        predicted = bool(row[field])
        key = ('tp' if expected else 'fp') if predicted else ('fn' if expected else 'tn')
        counts[key] += 1
    tp, fp, fn, tn = (counts[key] for key in ('tp', 'fp', 'fn', 'tn'))
    return dict(counts, pairs=len(rows), precision=tp / (tp + fp) if tp + fp else None,
                recall=tp / (tp + fn) if tp + fn else None,
                false_positive_rate=fp / (fp + tn) if fp + tn else None,
                false_negative_rate=fn / (tp + fn) if tp + fn else None)


def challenge_summary(rows):
    reasons = sorted({reason for row in rows for reason in row['exclusion_reasons']})
    return {'pairs': len(rows), 'eligible': sum(row['eligible'] for row in rows),
            'excluded': sum(not row['eligible'] for row in rows),
            'match_counts': {name: sum(bool(row[field]) for row in rows)
                             for name, field in METHOD_FIELDS.items()},
            'exclusion_reasons': {reason: sum(reason in row['exclusion_reasons'] for row in rows)
                                  for reason in reasons}}


def summaries(rows):
    near = [row for row in rows if row['label'] in ('near_duplicate', 'different')]
    scored = [row for row in rows if row['label'] != 'challenge']
    challenges = [row for row in rows if row['label'] == 'challenge']
    methods = {}
    for name, field in METHOD_FIELDS.items():
        methods[name] = classification_counts(near, field)
        methods[name]['by_kind'] = {
            kind: classification_counts([row for row in near if row['kind'] == kind], field)
            for kind in sorted({row['kind'] for row in near})}
    return {'near_duplicate_methods': methods,
            'exact_sha256': classification_counts(scored, 'exact_match', exact=True),
            'challenge_pairs': {
                'pairs': len(challenges),
                'by_kind': {
                    kind: challenge_summary([row for row in challenges if row['kind'] == kind])
                    for kind in sorted({row['kind'] for row in challenges})
                }}}


def timing_summary(values):
    return {'runs_seconds': values, 'median_seconds': statistics.median(values),
            'min_seconds': min(values), 'max_seconds': max(values)}


def extract_features(images, names, settings):
    Image, _ = quality.pillow()
    result = {}
    for name in names:
        try:
            data = quality.read_image_bytes(images[name]['resolved'])
            digest = hashlib.sha256(data).hexdigest()
            expected = images[name].get('sha256')
            if expected is not None and expected != digest:
                raise ValueError('Image bytes disagree with the manifest sha256.')
            result[name] = dict(quality.metrics(data, settings), name=name, decodable=True, sha256=digest)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as error:
            raise ValueError('Cannot score image ' + name + ': ' + str(error)) from error
    return result


def compare_labeled_pairs(pairs, features, settings):
    result = []
    for pair in pairs:
        a, b, label = pair['a'], pair['b'], pair['label']
        exact = features[a]['sha256'] == features[b]['sha256']
        if label != 'challenge' and (label == 'exact_duplicate') != exact:
            raise ValueError('SHA-256 contradicts the exact/non-exact label for: ' + a + ', ' + b)
        compared = quality.compare_pair(features[a], features[b], settings)
        result.append(dict(compared, label=label, kind=pair['kind'], split=pair['split'], exact_match=exact))
    return result


def benchmark_hashes(images, names, settings, repeats, hashes):
    Image, ImageOps = quality.pillow()
    grays = []
    for name in names:
        data = quality.read_image_bytes(images[name]['resolved'])
        if hashlib.sha256(data).hexdigest() != hashes[name]:
            raise ValueError('Input bytes changed before hash-only timings.')
        with Image.open(io.BytesIO(data)) as source:
            gray = ImageOps.exif_transpose(source).convert('L')
            gray.thumbnail((settings['max_side'], settings['max_side']), Image.Resampling.LANCZOS)
            grays.append(gray)
    result = {}
    for name, function in (('dhash', quality.dhash), ('phash', quality.phash)):
        durations = []
        for _ in range(repeats):
            started = time.perf_counter()
            for gray in grays:
                function(gray)
            durations.append(time.perf_counter() - started)
        result[name] = timing_summary(durations)
    return result


def evaluate(manifest, repeats=3, split='validation', thresholds=None):
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 50:
        raise ValueError('repeats must be an integer from 1 to 50.')
    if not isinstance(split, str) or split not in SPLITS:
        raise ValueError('Unknown split.')
    settings = quality.policy(thresholds)
    pairs = [pair for pair in manifest['pairs'] if pair['split'] == split]
    if not pairs:
        raise ValueError('No pairs in the requested split.')
    names = sorted({pair[key] for pair in pairs for key in ('a', 'b')})
    if len(names) > 500:
        raise ValueError('A benchmark split cannot exceed the 500-image detection limit.')
    feature_times, compare_times, rows = [], [], None
    hashes = None
    for _ in range(repeats):
        started = time.perf_counter()
        features = extract_features(manifest['images'], names, settings)
        feature_times.append(time.perf_counter() - started)
        current_hashes = {name: features[name]['sha256'] for name in names}
        if hashes is not None and hashes != current_hashes:
            raise ValueError('Input bytes changed between benchmark runs.')
        hashes = current_hashes
        started = time.perf_counter()
        current = compare_labeled_pairs(pairs, features, settings)
        compare_times.append(time.perf_counter() - started)
        if rows is not None and rows != current:
            raise ValueError('Pair decisions changed between identical benchmark runs.')
        rows = current
    return {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'algorithm_version': quality.VERSION, 'policy': settings,
            'dataset_kind': manifest['dataset_kind'], 'manifest': str(manifest['path']),
            'manifest_sha256': manifest['sha256'], 'split': split,
            'image_sha256': hashes, 'images': len(names), 'pairs': rows, 'summary': summaries(rows),
            'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                            'Pillow': version('Pillow'), 'ImageHash': version('ImageHash')},
            'timing': {'repeats': repeats, 'cold_first_run_included': True,
                       'shared_feature_extraction': timing_summary(feature_times),
                       'shared_pair_comparison': timing_summary(compare_times),
                       'hash_only': benchmark_hashes(manifest['images'], names, settings, repeats, hashes)},
            'notes': ['Feature time includes reads, decoding, metrics and both hashes; pair time '
                      'includes shared gates and both comparisons. These are not separate method timings.',
                      'Hash-only timings reuse decoded grayscale thumbnails and warmed libraries; '
                      'they exclude reads, decoding and resizing to the analysis scale.',
                      'Only declared pairs are evaluated, not an all-pairs prevalence estimate.',
                      'Exact SHA-256 pairs are excluded from near-duplicate accuracy summaries.',
                      'Unreviewed challenge pairs are excluded from all accuracy metrics and are '
                      'reported separately as counts, without a correctness claim.',
                      'Undefined metrics are null. No threshold tuning is performed here.',
                      'Synthetic smoke and generated transforms do not establish real-view '
                      'accuracy or SfM quality.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', type=Path)
    source.add_argument('--synthetic-smoke', action='store_true')
    parser.add_argument('--output', type=Path, required=True, help='New report directory; never overwritten')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--split', choices=sorted(SPLITS), default='validation')
    parser.add_argument('--near-distance', type=int)
    parser.add_argument('--phash-distance', type=int)
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        if output.exists():
            raise ValueError('Output directory already exists.')
        if not 1 <= args.repeats <= 50:
            raise ValueError('repeats must be from 1 to 50.')
        thresholds = {key: value for key, value in (
            ('near_distance', args.near_distance), ('phash_distance', args.phash_distance)) if value is not None}
        quality.policy(thresholds)
        if args.manifest:
            manifest = load_manifest(args.manifest)
            if output == manifest['path'].parent or output in manifest['path'].parents:
                raise ValueError('Report directory cannot contain the source manifest.')
            report = evaluate(manifest, args.repeats, args.split, thresholds)
            output.mkdir(parents=True, exist_ok=False)
        else:
            output.mkdir(parents=True, exist_ok=False)
            manifest = load_manifest(make_synthetic_manifest(output / 'synthetic'))
            report = evaluate(manifest, args.repeats, args.split, thresholds)
        (output / 'quality-evaluation.json').write_text(
            json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print('Pair evaluation report: ' + str(output / 'quality-evaluation.json'))
    print('Data kind: ' + report['dataset_kind'] + '; split: ' + report['split'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
