"""Report duplicate filtering without confusing file count with view coverage."""
from pathlib import Path

from scripts.quality_acceptance_data import write_json
from scripts.quality_duplicate_inputs import GROUPS
from scripts.quality_sfm_results import display, median_range


def run_order():
    return [(repeat + 1, name) for repeat in range(3)
            for name in GROUPS[repeat:] + GROUPS[:repeat]]


def group_metrics(selected):
    successful = [run for run in selected if run['returncode'] == 0]
    metrics = {'elapsed_seconds': median_range([run['elapsed_seconds'] for run in selected])}
    for key in ('registered_images', 'registration_ratio', 'points3D',
                'mean_point_reprojection_error_px', 'mean_track_length'):
        metrics[key] = median_range([run['summary'][key] for run in successful
                                     if run['summary'].get(key) is not None])
    metrics['original_coverage'] = median_range([run['original_coverage'] for run in successful])
    metrics['registered_originals'] = median_range([len(run['registered_original_names']) for run in successful])
    metrics['model_count'] = median_range([len(run['summary'].get('models', [])) for run in successful])
    for key in ('center_error_normalized', 'rotation_error_degrees'):
        values = [run['pose_reference_consistency'].get(key) for run in successful]
        metrics[key + '_rmse'] = median_range([value['rmse'] for value in values if value])
    return metrics


def paired_comparisons(runs):
    indexed = {(run['repeat'], run['group']): run for run in runs}
    comparisons = []
    for repeat in range(1, 4):
        before, after = indexed.get((repeat, 'augmented')), indexed.get((repeat, 'filtered'))
        if not before or not after:
            continue
        left, right = set(before['registered_original_names']), set(after['registered_original_names'])
        comparisons.append(dict(repeat=repeat, before_success=before['returncode'] == 0,
                                after_success=after['returncode'] == 0,
                                lost_originals=sorted(left - right), gained_originals=sorted(right - left),
                                common_registered_originals=sorted(left & right),
                                before_seconds=before['elapsed_seconds'], after_seconds=after['elapsed_seconds']))
    return comparisons


def aggregate(runs, manifest):
    groups = {}
    for name in GROUPS:
        selected = [run for run in runs if run['group'] == name]
        groups[name] = dict(attempts=len(selected), successful=sum(run['returncode'] == 0 for run in selected),
                            input_count=len(manifest['groups'][name]['names']),
                            excluded_count=len(manifest['groups'][name]['excluded']),
                            metrics=group_metrics(selected))
    return dict(kind='duplicate_copies_three_groups', original_denominator=manifest['original_denominator'],
                expected_runs=9, completed_attempts=len(runs), complete=len(runs) == 9,
                all_runs_successful=len(runs) == 9 and all(run['returncode'] == 0 for run in runs),
                groups=groups, runs=runs, paired_comparisons=paired_comparisons(runs),
                limitations=[
                    'One fixed Kitchen prefix with known copied/reencoded/resized inputs; not natural-view redundancy.',
                    'Original coverage uses the same 60-image denominator and the largest exported model.',
                    'All submodel sizes are retained in per-run summaries; disconnected models are not merged.',
                    'No positive finding is required. Timing ranges describe three serial repetitions, not significance.',
                    'Internal point counts/errors and published-pose consistency are not measured geometric accuracy.',
                    'Detection and review are outside the SfM timing; this is an assisted-review workflow.'])


def save_report(output, manifest, runs, termination='running'):
    output = Path(output)
    report = aggregate(runs, manifest)
    report.update(protocol_id=manifest['protocol_id'], termination=termination,
                  analysis_seconds=manifest['analysis_seconds'])
    write_json(output / 'duplicate-sfm-benchmark.json', report)
    lines = ['# Duplicate-input SfM comparison', '',
             'Median [min, max]. Original-view denominator: 60. Counts refer to the largest exported model.', '',
             '| Group | Input | Success | Registered originals | Time (s) | Models | Points | Internal error (px) |',
             '|---|---:|---:|---|---|---|---|---|']
    for name, group in report['groups'].items():
        metrics = group['metrics']
        lines.append('| {} | {} | {}/{} | {} | {} | {} | {} | {} |'.format(
            name, group['input_count'], group['successful'], group['attempts'],
            display(metrics['registered_originals'], 0), display(metrics['elapsed_seconds']),
            display(metrics['model_count'], 0), display(metrics['points3D'], 0),
            display(metrics['mean_point_reprojection_error_px'])))
    lines += ['', 'Termination: ' + termination + '.', '', '## Limits', '']
    lines += ['- ' + text for text in report['limitations']]
    (output / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report
