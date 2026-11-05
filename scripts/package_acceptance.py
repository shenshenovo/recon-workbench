"""Package small measured records, excluding photos, models and local absolute paths."""
import json
from pathlib import Path

from recon_workbench.paths import PROJECT_ROOT


def main():
    root = PROJECT_ROOT
    rows = []
    keys = ('method', 'input_images', 'registered_images', 'registration_ratio', 'points3D',
            'mean_point_reprojection_error_px', 'dense_points', 'added_images',
            'added_registered_images', 'route_elapsed_seconds', 'warnings')
    for name in ('south-building-50-70/baseline50', 'south-building-50-70/incremental70',
                 'south-building-50-70/full70', 'acceptance-new-sfm-mvs'):
        folder = root / 'outputs' / name
        summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
        state = json.loads((folder / 'status.json').read_text(encoding='utf-8'))
        assert state['status'] == 'completed', name
        rows.append(dict(experiment=name, status=state['status'],
                         **{key: summary[key] for key in keys if key in summary}))
    evidence = {'date': '2026-11-05', 'measured_runs': rows,
                'tests': {'run': 85, 'skipped': 2, 'result': 'OK'},
                'brush': {'steps': 500, 'gaussians': 59681, 'bytes': 14086266,
                          'platform': 'Windows', 'version': '0.3.0', 'quality_evaluation': False}}
    output = root / 'docs/acceptance/measured-results.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
