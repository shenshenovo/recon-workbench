"""入队前固定输入，文件准备完成后原子发布。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from .. import image_quality
from ..densify import load_source

MAX_FILES = 100000
MAX_BYTES = 100 * 1024**3


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def safe_path(root, value):
    path = root / value
    # 任务输入不接受链接（含父目录链接），防止复制或清理越出工作区。
    absolute = Path(os.path.abspath(path))
    if root not in absolute.parents:
        raise ValueError('输入路径必须位于工作区内。')
    current = root
    for part in absolute.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('任务输入不能包含符号链接。')
    return absolute


def file_hash(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def describe_tree(root, source):
    source = safe_path(root, source)
    if not source.is_dir():
        raise ValueError('输入目录不存在。')
    files, total = [], 0
    for directory, dirs, names in os.walk(source, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError('任务输入不能包含符号链接。')
        for name in sorted(names):
            path = Path(directory) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('任务输入只接受普通文件。')
            total += path.stat().st_size
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise ValueError('输入快照超过 100000 文件或 100 GiB 上限。')
            files.append({'name': path.relative_to(source).as_posix(),
                          'bytes': path.stat().st_size, 'sha256': file_hash(path)})
    return sorted(files, key=lambda item: item['name'])


def describe_input(store, image_set):
    root = store.root
    source = safe_path(root, image_set['path'])
    kind = image_set['input_type']
    if kind == 'images':
        with store.quality_lock:
            quality = store.quality_state(image_set['id'])
            if not quality['report'] or not quality['selection']:
                raise ValueError('请先检测图片质量并保存筛选结果，再开始重建。')
            names = image_quality.validate_selection(quality['report'], quality['selection']['names'], quality['selection']['fingerprint'])
            selected = {row['name']: row for row in quality['report']['images']}
            # 即使旧报告或图片集记录指向链接，也在准备前拒绝。
            for name in names:
                safe_path(root, source / name)
            identity = {'image_set_id': image_set['id'], 'fingerprint': quality['report']['fingerprint'],
                        'names': names, 'files': [{'name': name, 'sha256': selected[name]['sha256']} for name in names]}
            return {'kind': kind, 'source': source, 'quality': quality, 'names': names,
                    'identity': identity, 'fingerprint': digest(identity)}
    if kind == 'dataset':
        manifest = describe_tree(root, source)
        if not (source / 'dataset.json').is_file() or not (source / 'images').is_dir():
            raise ValueError('Brush 输入缺少 dataset.json 或 images。')
        metadata = json.loads((source / 'dataset.json').read_text())
        if metadata.get('status') != 'prepared' or not all((source / 'sparse/0' / name).is_file() for name in ('cameras.bin', 'images.bin', 'points3D.bin')):
            raise ValueError('Brush 输入尚未整理完成或缺少模型。')
        identity = {'kind': kind, 'files': manifest}
        return {'kind': kind, 'source': source, 'manifest': manifest, 'identity': identity, 'fingerprint': digest(identity)}
    _, images, model, config, summary = load_source(source)
    images, model = safe_path(root, images), safe_path(root, model)
    photo_files, model_files = describe_tree(root, images), describe_tree(root, model)
    identity = {'kind': kind, 'config': config, 'summary': summary, 'images': photo_files, 'model': model_files}
    return {'kind': kind, 'source': source, 'images': images, 'model': model, 'config': config,
            'summary': summary, 'photo_files': photo_files, 'model_files': model_files,
            'identity': identity, 'fingerprint': digest(identity)}


def copy_verified(source, destination, manifest):
    destination.mkdir(parents=True)
    for row in manifest:
        original, target = source / row['name'], destination / row['name']
        if original.is_symlink() or not original.is_file() or file_hash(original) != row['sha256']:
            raise ValueError('输入在准备期间已变化：' + row['name'])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        if file_hash(target) != row['sha256']:
            raise ValueError('输入复制期间已变化：' + row['name'])


def prepare(store, task, description, parameters):
    root = store.root
    base = safe_path(root, root / 'data/task_inputs')
    base.mkdir(parents=True, exist_ok=True)
    final = base / task['id']
    staging = base / ('.' + task['id'] + '-' + uuid.uuid4().hex + '.staging')
    staging.mkdir()
    kind = description['kind']
    try:
        if kind == 'images':
            quality = description['quality']
            with store.quality_lock:
                if store.quality_state(task['image_set_id']) != quality:
                    raise ValueError('图片集或筛选结果已变化，请重新确认后启动。')
                files = image_quality.snapshot(root, quality['report'], description['names'], staging / 'images')
            manifest = {'image_set_id': task['image_set_id'], 'fingerprint': quality['report']['fingerprint'],
                        'policy': quality['report']['policy'], 'files': files}
            (staging / 'selection.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            (staging / 'quality.json').write_text(json.dumps(quality['report'], ensure_ascii=False, indent=2))
            parameters.update(selected_images=description['names'], quality_fingerprint=quality['report']['fingerprint'],
                              quality_manifest=str((final / 'selection.json').relative_to(root)))
            input_path = final / 'images'
        elif kind == 'dataset':
            copy_verified(description['source'], staging / 'dataset', description['manifest'])
            input_path = final / 'dataset'
        else:
            copy_verified(description['images'], staging / 'sfm/images', description['photo_files'])
            copy_verified(description['model'], staging / 'sfm/model', description['model_files'])
            config = dict(description['config'], images=str(final / 'sfm/images'))
            summary = dict(description['summary'], selected_model=str(final / 'sfm/model'))
            (staging / 'sfm/config.json').write_text(json.dumps(config))
            (staging / 'sfm/summary.json').write_text(json.dumps(summary))
            input_path = final / 'sfm'
        (staging / 'input.json').write_text(json.dumps({'fingerprint': description['fingerprint'], 'identity': description['identity']}, ensure_ascii=False))
        os.rename(staging, final)
        parameters.update(source_input=parameters['input'], input=str(input_path.relative_to(root)),
                          input_fingerprint=description['fingerprint'], input_manifest=str((final / 'input.json').relative_to(root)))
        return parameters
    finally:
        # 只清理由本次调用创建的 staging，绝不跟随源目录或删原件。
        shutil.rmtree(staging, ignore_errors=True)
