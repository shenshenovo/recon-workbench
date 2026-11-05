"""Immutable inputs and validation for COLMAP continuation."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(stream.read()).hexdigest()


def manifest(images, names):
    return {name: digest(images / name) for name in names}


def parent_info(folder):
    folder = Path(folder).resolve()
    if (folder / 'sfm').is_dir():
        folder = folder / 'sfm'
    try:
        summary = json.loads((folder / 'summary.json').read_text())
        state = json.loads((folder / 'status.json').read_text())
        saved = json.loads((folder / 'input_manifest.json').read_text())
        images = Path(summary['images_path'])
        model = Path(summary['selected_model'])
        if state['status'] != 'completed' or not saved:
            raise ValueError('父版本未完成或输入清单为空')
        for name, fingerprint in saved.items():
            path = (images / name).resolve()
            if images.resolve() not in path.parents or digest(path) != fingerprint:
                raise ValueError('父版本照片已变化：' + name)
        for name in ('cameras.bin', 'images.bin', 'points3D.bin'):
            if not (model / name).is_file():
                raise ValueError('父版本缺少模型文件：' + name)
        if not (folder / 'database.db').is_file():
            raise ValueError('父版本缺少数据库')
        return folder, images, model, saved
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError('父版本缺少可验证的模型、数据库或输入快照，请重新生成基线。') from error


def prepare(base, additions, names, output):
    folder, old_images, model, saved = parent_info(base)
    hashes = set(saved.values())
    new = manifest(additions, names)
    if not new or any(value in hashes for value in new.values()) or len(set(new.values())) != len(new):
        raise ValueError('补图为空或包含内容重复的照片。')
    images = output / 'inputs'
    images.mkdir()
    for name in saved:
        target = images / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_images / name, target)
    mapped = []
    for index, name in enumerate(names):
        target = 'added_' + new[name][:16] + '_' + str(index) + Path(name).suffix.lower()
        while target in saved:
            target = 'new_' + target
        shutil.copy2(additions / name, images / target)
        saved[target] = new[name]
        mapped.append(target)
    if manifest(images, list(saved)) != saved:
        raise ValueError('输入照片在复制期间发生变化。')
    # SQLite backup also captures a consistent snapshot when a WAL exists.
    with sqlite3.connect((folder / 'database.db').as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(output / 'database.db') as destination:
            source.backup(destination)
    shutil.copytree(model, output / 'base_model')
    return images, saved, mapped
