#!/usr/bin/env python3
"""Prepare, review/freeze, and run the separately scoped duplicate experiment."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.quality_acceptance_data import write_json
from scripts.quality_duplicate_inputs import freeze, prepare, verify_frozen
from scripts.quality_duplicate_results import run_order, save_report
from scripts.quality_sfm_benchmark import execute
from scripts.quality_sfm_results import collect_result
from scripts.quality_sfm_smoke import sha256


def run_attempt(output, manifest, repeat, name, digest):
    target = output / 'runs' / ('r{}_{}'.format(repeat, name))
    result_path = target.with_suffix('.json')
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding='utf-8'))
        if result['inputs_manifest_sha256'] != digest:
            raise ValueError('Stored attempt does not match frozen inputs.')
        return result
    if target.exists() or target.with_suffix('.log').exists():
        raise ValueError('Incomplete attempt preserved; inspect before resuming: ' + str(target))
    print('START r{} {} ({} images)'.format(repeat, name, len(manifest['groups'][name]['names'])), flush=True)
    started = time.monotonic()
    try:
        execution = execute(Path(manifest['groups'][name]['images']), target,
                            manifest['configuration'], manifest['run_timeout_seconds'])
    except KeyboardInterrupt:
        execution = dict(returncode=130, elapsed_seconds=round(time.monotonic() - started, 3),
                         summary={}, cancelled=True)
    result = collect_result(target, manifest, name, execution)
    result.update(repeat=repeat, inputs_manifest_sha256=digest)
    write_json(result_path, result)
    return result


def run_suite(output):
    output = Path(output).resolve()
    manifest = json.loads((output / 'inputs.json').read_text(encoding='utf-8'))
    verify_frozen(output, manifest)
    digest = sha256(output / 'inputs.json')
    (output / 'runs').mkdir(exist_ok=True)
    runs, termination = [], 'completed'
    for repeat, name in run_order():
        verify_frozen(output, manifest)
        result = run_attempt(output, manifest, repeat, name, digest)
        runs.append(result)
        save_report(output, manifest, runs)
        print('DONE r{} {} rc={} originals={}/60 elapsed={}s'.format(
            repeat, name, result['returncode'], len(result['registered_original_names']),
            result['elapsed_seconds']), flush=True)
        if result.get('cancelled'):
            termination = 'cancelled'
            break
        if len(runs) == 3 and any(run['returncode'] != 0 for run in runs):
            termination = 'first_round_failure_stop'
            break
    verify_frozen(output, manifest)
    return save_report(output, manifest, runs, termination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'freeze', 'run'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--experiment', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == 'prepare':
        if args.source is None:
            parser.error('prepare requires --source')
        result = prepare(args.source, args.experiment)
        print(json.dumps({'originals': len(result['originals']), 'variants': len(result['variants']),
                          'candidate_pairs': result['candidate_count'], 'counts': result['detector_counts']}))
    elif args.action == 'freeze':
        result = freeze(args.experiment)
        print(json.dumps({name: len(group['names']) for name, group in result['groups'].items()}))
    else:
        result = run_suite(args.experiment)
        return 130 if result['termination'] == 'cancelled' else 0 if result['all_runs_successful'] else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
