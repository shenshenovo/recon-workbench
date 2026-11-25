#!/usr/bin/env python3
"""Evaluate a completed scene-review CSV at preselected thresholds."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import sys

from importlib.metadata import version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recon_workbench import image_quality as quality
from scripts.evaluate_quality import compare_labeled_pairs, summaries


FIELDS = {'pair_id', 'split', 'image_a', 'image_b', 'label', 'reviewer', 'notes'}
LABELS = {'near_duplicate', 'different', 'challenge'}
THRESHOLDS = {
    'default_4_6': {'near_distance': 4, 'phash_distance': 6},
    'candidate_8_16': {'near_distance': 8, 'phash_distance': 16},
}


def read_labels(candidate_path, labels_path):
    candidates = json.loads(candidate_path.read_text(encoding='utf-8'))
    expected = {row['id']: row for row in candidates.get('pairs', [])}
    if not expected or len(expected) != len(candidates['pairs']):
        raise ValueError('Candidate manifest has no pairs or duplicate IDs.')
    with labels_path.open(newline='', encoding='utf-8-sig') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or set(reader.fieldnames) != FIELDS:
            raise ValueError('Reviewed CSV has an unexpected header.')
        rows = list(reader)
    if len(rows) != len(expected):
        raise ValueError(f'Expected {len(expected)} reviewed rows, got {len(rows)}.')
    seen = set()
    for row in rows:
        pair_id = row['pair_id']
        if pair_id not in expected or pair_id in seen:
            raise ValueError('CSV pair IDs are unknown or duplicated.')
        candidate = expected[pair_id]
        if (row['split'] != 'validation' or row['image_a'] != candidate['source_a']
                or row['image_b'] != candidate['source_b']):
            raise ValueError('CSV pair identity or validation split was changed: ' + pair_id)
        if row['label'] not in LABELS:
            raise ValueError('Every row needs near_duplicate, different, or challenge: ' + pair_id)
        seen.add(pair_id)
    return candidates, rows


def build_features(candidate_path, candidates):
    features = {}
    hashes = {}
    image_root = candidate_path.parent
    settings = quality.policy()
    for pair in candidates['pairs']:
        for key, source_key in (('a', 'source_a'), ('b', 'source_b')):
            name, relative = pair[source_key], pair[key]
            path = PurePosixPath(relative)
            if path.is_absolute() or '..' in path.parts or '\\' in relative:
                raise ValueError('Unsafe candidate image path: ' + relative)
            image_path = (image_root / Path(*path.parts)).resolve()
            if not image_path.is_relative_to(image_root.resolve()) or not image_path.is_file():
                raise ValueError('Candidate image is missing or outside the review directory: ' + relative)
            data = quality.read_image_bytes(image_path)
            digest = hashlib.sha256(data).hexdigest()
            if name in hashes and hashes[name] != digest:
                raise ValueError('One source image name maps to different bytes: ' + name)
            if name not in features:
                features[name] = dict(quality.metrics(data, settings), name=name,
                                      decodable=True, sha256=digest)
                hashes[name] = digest
    return features, hashes


def evaluate(candidate_path, labels_path):
    candidates, labels = read_labels(candidate_path, labels_path)
    features, hashes = build_features(candidate_path, candidates)
    pair_rows = []
    for row in labels:
        pair_rows.append({
            'a': row['image_a'], 'b': row['image_b'], 'label': row['label'],
            'kind': 'human_reviewed_cross_scene', 'split': 'validation',
        })

    results = {}
    for name, threshold in THRESHOLDS.items():
        settings = quality.policy(threshold)
        compared = compare_labeled_pairs(pair_rows, features, settings)
        results[name] = {'thresholds': threshold, 'summary': summaries(compared),
                         'pairs': compared}

    return {
        'version': 1,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'scene': candidates.get('scene'),
        'dataset_kind': 'human_labeled_cross_scene_validation',
        'source_image_count': candidates.get('source_image_count'),
        'candidate_pair_count': len(pair_rows),
        'label_counts': dict(Counter(row['label'] for row in labels)),
        'candidate_manifest': str(candidate_path),
        'candidate_manifest_sha256': hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        'reviewed_csv': str(labels_path),
        'reviewed_csv_sha256': hashlib.sha256(labels_path.read_bytes()).hexdigest(),
        'image_sha256': hashes,
        'threshold_results': results,
        'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                        'Pillow': version('Pillow'), 'ImageHash': version('ImageHash')},
        'notes': [
            'This is a fixed-threshold external validation set; no threshold selection is performed.',
            'The 4/6 default and 8/16 candidate are compared on the same human-reviewed pairs.',
            'Challenge labels are reported separately and excluded from precision/recall.',
            'Sampling is stratified, not a random sample of all possible scene pairs.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', type=Path, required=True)
    parser.add_argument('--labels', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='New output directory; existing reports are never overwritten.')
    args = parser.parse_args(argv)
    candidates = args.candidates.expanduser().resolve()
    labels = args.labels.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error('Output directory already exists.')
    if not candidates.is_file() or not labels.is_file():
        parser.error('Candidates manifest and reviewed CSV must both exist.')
    try:
        report = evaluate(candidates, labels)
        output.mkdir(parents=True)
        report_path = output / 'scene-quality-evaluation.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                          allow_nan=False) + '\n', encoding='utf-8')
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, str(error) + '\n')
    print('Cross-scene evaluation:', report_path)
    for name, value in report['threshold_results'].items():
        combined = value['summary']['near_duplicate_methods']['combined']
        print('{}: TP={} FP={} FN={} TN={} precision={} recall={}'.format(
            name, combined['tp'], combined['fp'], combined['fn'], combined['tn'],
            combined['precision'], combined['recall']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
