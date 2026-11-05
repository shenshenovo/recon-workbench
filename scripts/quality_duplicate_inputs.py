"""Prepare and freeze the three-group duplicate-copy experiment."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time

from PIL import Image, ImageOps

from recon_workbench.image_quality import analyze, policy
from scripts.quality_acceptance_data import SFM_CONFIG, evenly_spaced, provenance, write_json
from scripts.quality_duplicate_review import (
    candidate_pairs, reviewed_exclusions, source_pair_accounting, write_review)
from scripts.quality_sfm_inputs import pose_plan, verify_inputs
from scripts.quality_sfm_smoke import copy_selected, image_entries, read_source, sha256


PROTOCOL_ID = 'duplicate_copies_three_groups_v1'
GROUPS = ('original', 'augmented', 'filtered')
PROTOCOL = 'docs/DUPLICATE_SFM_PROTOCOL.md'


def read_prefix(source):
    image_root, transforms, paths = read_source(source)
    if transforms is None:
        raise ValueError('Ordered source transforms.json is required.')
    document = json.loads(transforms.read_text(encoding='utf-8'))
    if len(paths) != len(document.get('frames', [])) or len(paths) < 60:
        raise ValueError('Source frame order is incomplete or has fewer than 60 images.')
    return image_root, transforms, dict(document, frames=document['frames'][:60]), paths[:60]


def inject_copies(original, augmented, source_names):
    variants = []
    kinds = ('exact_copy', 'jpeg_reencode', 'resize')
    for index, name in enumerate(evenly_spaced(source_names, 15)):
        kind = kinds[index % len(kinds)]
        source = original / name
        suffix = source.suffix if kind == 'exact_copy' else '.jpg' if kind == 'jpeg_reencode' else '.png'
        target = augmented / ('zz_dup_' + source.stem + '_' + kind + suffix)
        parameters = {}
        if kind == 'exact_copy':
            shutil.copyfile(source, target)
        else:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert('RGB')
                if kind == 'jpeg_reencode':
                    parameters = dict(quality=75, subsampling=2, optimize=False)
                    image.save(target, format='JPEG', **parameters)
                else:
                    size = tuple(max(1, int(value * .75 + .5)) for value in image.size)
                    parameters = dict(scale=.75, size=list(size), resampling='LANCZOS', cropped=False)
                    image.resize(size, Image.Resampling.LANCZOS).save(target, format='PNG')
        variants.append(dict(name=target.name, source_name=name, kind=kind,
                             parameters=parameters, sha256=sha256(target)))
    return variants


def analyze_to_file(images, target, thresholds=None):
    started = time.monotonic()
    report = analyze(images, image_entries(images), thresholds)
    elapsed = time.monotonic() - started
    write_json(target, report)
    return report, round(elapsed, 3)


def prepare(source, output):
    started = time.monotonic()
    output = Path(output).resolve()
    image_root, transforms, reference, paths = read_prefix(source)
    if output == image_root or image_root in output.parents:
        raise ValueError('Output cannot be inside the source images.')
    output.mkdir(parents=True, exist_ok=False)
    original, augmented = (output / 'inputs' / name / 'images' for name in GROUPS[:2])
    originals = copy_selected(paths, original)
    copy_selected([original / row['name'] for row in originals], augmented)
    variants = inject_copies(original, augmented, [row['name'] for row in originals])
    write_json(output / 'reference-transforms.json', reference)
    shutil.copyfile(Path(__file__).resolve().parents[1] / PROTOCOL, output / 'protocol.md')
    original_report, original_time = analyze_to_file(original, output / 'original-report.json')
    default, default_time = analyze_to_file(augmented, output / 'default-report.json')
    candidate, candidate_time = analyze_to_file(
        augmented, output / 'candidate-report.json', dict(near_distance=8, phash_distance=16))
    candidates = candidate_pairs(default)
    write_json(output / 'candidates.json', candidates)
    write_review(output / 'review.csv', candidates)
    prepared = dict(protocol_id=PROTOCOL_ID, created_at=datetime.now(timezone.utc).isoformat(),
                    source=str(image_root), source_transforms_sha256=sha256(transforms),
                    originals=originals, variants=variants, policy=default['policy'],
                    detector_counts=default['counts'], candidate_detector_counts=candidate['counts'],
                    analysis_seconds=dict(original=original_time, default=default_time,
                                          candidate=candidate_time),
                    preparation_seconds=round(time.monotonic() - started, 3),
                    transforms_sha256=sha256(output / 'reference-transforms.json'),
                    candidate_count=len(candidates), original_fingerprint=original_report['fingerprint'])
    write_json(output / 'prepared.json', prepared)
    return prepared


def group_record(images, excluded, fingerprint):
    entries = image_entries(images)
    return dict(images=str(images), names=[row['name'] for row in entries],
                files_sha256={row['name']: sha256(images / row['name']) for row in entries},
                excluded=excluded, quality_fingerprint=fingerprint)


def verify_prepared(output, prepared, report):
    if prepared.get('protocol_id') != PROTOCOL_ID:
        raise ValueError('Wrong prepared experiment protocol.')
    if report['policy'] != policy() or prepared['policy'] != policy():
        raise ValueError('The default detection policy changed before freezing.')
    if sha256(output / 'protocol.md') != sha256(Path(__file__).resolve().parents[1] / PROTOCOL):
        raise ValueError('Protocol changed between preparation and freezing.')
    if sha256(output / 'reference-transforms.json') != prepared['transforms_sha256']:
        raise ValueError('Reference metadata changed before freezing.')
    expected = {row['name']: row['sha256'] for row in prepared['originals'] + prepared['variants']}
    if {row['name']: row['sha256'] for row in report['images']} != expected:
        raise ValueError('Detection report differs from the prepared files.')
    for group, names in [('original', prepared['originals']),
                         ('augmented', prepared['originals'] + prepared['variants'])]:
        images = output / 'inputs' / group / 'images'
        if {row['name'] for row in image_entries(images)} != {row['name'] for row in names}:
            raise ValueError('Prepared file list changed.')
        if any(sha256(images / row['name']) != row['sha256'] for row in names):
            raise ValueError('Prepared image bytes changed.')
    for row in prepared['originals']:
        if sha256(row['source']) != row['sha256']:
            raise ValueError('An original source image changed.')


def freeze(output):
    output = Path(output).resolve()
    if (output / 'inputs.json').exists():
        raise ValueError('This experiment is already frozen.')
    prepared = json.loads((output / 'prepared.json').read_text(encoding='utf-8'))
    report = json.loads((output / 'default-report.json').read_text(encoding='utf-8'))
    verify_prepared(output, prepared, report)
    candidates = json.loads((output / 'candidates.json').read_text(encoding='utf-8'))
    if candidates != candidate_pairs(report):
        raise ValueError('Candidate list changed after detection.')
    original_names = [row['name'] for row in prepared['originals']]
    names = [row['name'] for row in report['images']]
    excluded = reviewed_exclusions(output / 'review.csv', candidates, names, original_names)
    removed = {row['name'] for row in excluded}
    augmented = output / 'inputs' / 'augmented' / 'images'
    copy_selected([augmented / name for name in names if name not in removed],
                  output / 'inputs' / 'filtered' / 'images')
    screening = source_pair_accounting(report, prepared['variants'], excluded)
    screening.update(input_count=len(names), retained_count=len(names) - len(removed),
                     removed_count=len(removed), excluded=excluded,
                     natural_view_exclusions=0, quality_flags_used_for_selection=False,
                     review_method='User-authorized assistant visual review of detector candidates.')
    write_json(output / 'screening.json', screening)
    manifest = make_manifest(output, prepared, report, excluded)
    write_json(output / 'inputs.json', manifest)
    verify_frozen(output, manifest)
    return manifest


def make_manifest(output, prepared, report, excluded):
    evidence = provenance()
    repo = Path(__file__).resolve().parents[1]
    evidence['files_sha256'][PROTOCOL] = sha256(repo / PROTOCOL)
    evidence['bundle_sha256'] = hashlib.sha256(
        json.dumps(evidence['files_sha256'], sort_keys=True).encode()).hexdigest()
    artifact_names = ('prepared.json', 'original-report.json', 'default-report.json',
                      'candidate-report.json', 'candidates.json', 'review.csv', 'screening.json', 'protocol.md')
    groups = {name: group_record(output / 'inputs' / name / 'images',
                                excluded if name == 'filtered' else [], report['fingerprint'])
              for name in GROUPS}
    groups['original']['quality_fingerprint'] = prepared['original_fingerprint']
    return dict(prepared, frozen_at=datetime.now(timezone.utc).isoformat(), groups=groups,
                original_denominator=60, configuration=dict(SFM_CONFIG), run_timeout_seconds=3600,
                pose_plan=pose_plan([row['name'] for row in prepared['originals']], groups['filtered']['names']),
                selection_rule='Default detector candidates followed by explicit review; original views fixed.',
                provenance=evidence, first_round_failure_stop=True,
                frozen_artifact_sha256={name: sha256(output / name) for name in artifact_names})


def verify_frozen(output, manifest):
    output = Path(output).resolve()
    if manifest.get('protocol_id') != PROTOCOL_ID:
        raise ValueError('Wrong frozen duplicate protocol.')
    if manifest['configuration'] != SFM_CONFIG:
        raise ValueError('Frozen CPU/independent-camera configuration changed.')
    verify_inputs(output, manifest)
    for name, digest in manifest['frozen_artifact_sha256'].items():
        if sha256(output / name) != digest:
            raise ValueError('Frozen review or evidence changed: ' + name)
    for row in manifest['originals']:
        if sha256(row['source']) != row['sha256']:
            raise ValueError('An original source changed after freezing.')
