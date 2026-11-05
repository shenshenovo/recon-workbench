"""Explicit review decisions and source-pair accounting for duplicate inputs."""
import csv
from itertools import combinations
from pathlib import Path

from recon_workbench.image_quality import compare_pair


REVIEW_FIELDS = ('candidate_id', 'a', 'b', 'decision', 'exclude', 'retain', 'reviewer', 'notes')


def candidate_pairs(report):
    images = sorted(report['images'], key=lambda row: row['name'])
    rows = []
    for image in images:
        if image['duplicate_of']:
            rows.append(dict(a=image['duplicate_of'], b=image['name'], evidence='sha256',
                             dhash_distance=0, phash_distance=0))
    unique = [row for row in images if row['decodable'] and not row['duplicate_of']]
    for left, right in combinations(unique, 2):
        pair = compare_pair(left, right, report['policy'])
        if pair['match']:
            rows.append(dict(pair, evidence='dhash_and_phash'))
    return [dict(row, candidate_id='D{:03}'.format(index))
            for index, row in enumerate(rows, 1)]


def write_review(path, candidates):
    with Path(path).open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for pair in candidates:
            writer.writerow({key: pair.get(key, '') for key in REVIEW_FIELDS})


def reviewed_exclusions(path, candidates, image_names, original_names):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(REVIEW_FIELDS):
            raise ValueError('Review CSV fields changed.')
        rows = list(reader)
    expected = {row['candidate_id']: row for row in candidates}
    if len(rows) != len(expected) or len({row['candidate_id'] for row in rows}) != len(rows):
        raise ValueError('Review must contain every candidate exactly once.')
    excluded = {}
    for row in rows:
        pair = expected.get(row['candidate_id'])
        if not pair or (row['a'], row['b']) != (pair['a'], pair['b']):
            raise ValueError('Candidate identity changed.')
        if row['decision'] not in ('keep_both', 'exclude_one'):
            raise ValueError('Every candidate needs a review decision.')
        if not row['reviewer'].strip() or not row['notes'].strip():
            raise ValueError('Review attribution and reason are required.')
        if row['decision'] == 'keep_both':
            if row['exclude'] or row['retain']:
                raise ValueError('keep_both cannot declare an exclusion.')
            continue
        if {row['exclude'], row['retain']} != {pair['a'], pair['b']}:
            raise ValueError('Exclusion and reference must identify the candidate pair.')
        if row['exclude'] in original_names:
            raise ValueError('The fixed original views cannot be excluded in this protocol.')
        record = dict(name=row['exclude'], reference=row['retain'], reasons=[row['notes']],
                      reviewer=row['reviewer'], candidate_id=row['candidate_id'])
        if record['name'] in excluded and excluded[record['name']]['reference'] != record['reference']:
            raise ValueError('Conflicting retained references for one excluded image.')
        excluded[record['name']] = record
    for row in excluded.values():
        if row['reference'] in excluded:
            raise ValueError('An exclusion reference is also excluded.')
        if row['reference'] not in image_names:
            raise ValueError('Retained reference is absent from the input.')
    return [excluded[name] for name in sorted(excluded)]


def source_pair_accounting(report, variants, excluded=()):
    images = {row['name']: row for row in report['images']}
    removed = {row['name'] for row in excluded}
    records, counts = [], {}
    for variant in variants:
        source, copy = images[variant['source_name']], images[variant['name']]
        pair = compare_pair(source, copy, report['policy'])
        detected = (source['sha256'] == copy['sha256'] if variant['kind'] == 'exact_copy'
                    else pair['match'])
        record = dict(variant, detected_from_source=detected, removed=variant['name'] in removed,
                      dhash_distance=pair['dhash_distance'], phash_distance=pair['phash_distance'],
                      exclusion_reasons=pair['exclusion_reasons'])
        records.append(record)
        group = counts.setdefault(variant['kind'], dict(total=0, detected=0, removed=0))
        group['total'] += 1
        group['detected'] += int(detected)
        group['removed'] += int(record['removed'])
    return dict(by_kind=counts, variants=records,
                note='Known source-pair detection only; other natural pairs need separate labels.')
