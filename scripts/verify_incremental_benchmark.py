"""Validate a completed real SfM comparison without requiring GUI tooling."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import struct

from recon_workbench.incremental import parent_info
from recon_workbench.pointcloud import cloud_packet


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify(root):
    rows = []
    for label, count in [('baseline50', 50), ('incremental70', 70), ('full70', 70)]:
        folder = root / label
        parent_info(folder)
        summary = read(folder / 'summary.json')
        assert read(folder / 'status.json')['status'] == 'completed', label
        assert summary['input_images'] == count, label
        assert summary['registration_ratio'] >= .9, label
        packet = cloud_packet(summary['pointcloud'])
        length = struct.unpack('<I', packet[:4])[0]
        metadata = json.loads(packet[4:4 + length])
        assert metadata['original_points'] == summary['points3D'], label
        assert metadata['display_points'] > 0, label
        rows.append({key: summary[key] for key in ('input_images', 'registered_images',
                     'points3D', 'mean_point_reprojection_error_px', 'route_elapsed_seconds')})
        rows[-1].update(label=label, cloud_valid=True)
    configs = [read(root / label / 'sfm/config.json') for label in ('baseline50', 'incremental70', 'full70')]
    for key in ('threads', 'max_image_size', 'camera_model', 'single_camera', 'matcher', 'max_features'):
        assert all(config[key] == configs[0][key] for config in configs), key
    old = read(root / 'baseline50/sfm/input_manifest.json')
    child = read(root / 'incremental70/sfm/input_manifest.json')
    full = read(root / 'full70/sfm/input_manifest.json')
    assert all(child.get(name) == value for name, value in old.items())
    assert Counter(child.values()) == Counter(full.values()), '70-image inputs differ'
    records = []
    for label in ('baseline50', 'incremental70'):
        with sqlite3.connect((root / label / 'sfm/database.db').resolve().as_uri() + '?mode=ro', uri=True) as db:
            records.append(db.execute('SELECT image_id,name FROM images ORDER BY image_id').fetchall())
    assert records[0] == records[1][:50], 'old image IDs changed'
    assert read(root / 'incremental70/summary.json')['added_registered_images'] == 20
    result = dict(passed=True, rows=rows, input_hashes_equal=True, old_image_ids_preserved=True,
                  config_equal=True, visual_inspection='pending; file validation is not visual acceptance')
    (root / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    print(json.dumps(verify(parser.parse_args().output), ensure_ascii=False, indent=2))
