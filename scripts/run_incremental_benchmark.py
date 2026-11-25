"""Run the fixed 50/20/70 SfM comparison from an extracted real image directory."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from recon_workbench.incremental import manifest
from recon_workbench.reconstruct import collect_images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    names = collect_images(args.images)[:70]
    if len(names) != 70:
        parser.error('需要至少 70 张真实照片')
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'manifest.json').write_text(json.dumps(manifest(args.images, names), indent=2))
    for group, selected in [('base', names[:50]), ('added', names[50:]), ('all', names)]:
        for name in selected:
            target = args.output / group / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.images / name, target)
    summaries = []
    for label, group, base in [('baseline50', 'base', None), ('incremental70', 'added', 'baseline50'), ('full70', 'all', None)]:
        command = [sys.executable, 'main.py', '--method', 'sfm', '--images', str(args.output / group), '--output', str(args.output / label), '--threads', '4', '--max-image-size', '1200', '--camera-model', 'SIMPLE_RADIAL', '--single-camera']
        if base:
            command += ['--base-sfm', str(args.output / base)]
        with (args.output / (label + '.log')).open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        summary = json.loads((args.output / label / 'summary.json').read_text())
        summaries.append(dict(label=label, **summary))
        (args.output / 'comparison.json').write_text(json.dumps(summaries, indent=2))
        if label == 'baseline50' and summary['registration_ratio'] < .9:
            raise RuntimeError('基线注册率低于 90%，先检查数据与参数。')


if __name__ == '__main__':
    main()
