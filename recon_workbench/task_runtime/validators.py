"""只读校验任务输入、阶段状态和输出产物。

该模块不执行算法，也不依据 ``Path.exists`` 单独判定阶段可复用；所有可
复用的文件都要同时通过来源清单和 SHA-256 校验。
"""
from pathlib import Path
import json
import os
import platform
import shutil
import sys

from .snapshots import digest, file_hash, safe_path


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None


def _files(root, exclude=()):
    root = Path(root)
    result = []
    if not root.is_dir() or root.is_symlink():
        return result
    excluded = {str(Path(item)) for item in exclude}
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        current = Path(directory)
        for name in dirs:
            if (current / name).is_symlink():
                raise ValueError('任务产物包含符号链接，无法生成可复用清单。')
        for name in sorted(names):
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('任务产物包含非普通文件，无法生成可复用清单。')
            relative = str(path.relative_to(root)).replace(os.sep, '/')
            if relative in excluded or relative in ('artifact_manifest.json', '.artifact_manifest.tmp'):
                continue
            result.append({'path': relative, 'bytes': path.stat().st_size, 'sha256': file_hash(path)})
    return result


def capture_output_manifest(root, output, *, status=None):
    """Write a self-contained manifest after a stage attempt.

    The manifest is best-effort for failed runs. It never makes a failed run
    successful and is only consumed by the explicit recovery preflight.
    """
    output = Path(output)
    if not output.is_dir() or output.is_symlink():
        return None
    payload = {
        'format_version': 1,
        'status': status,
        'files': _files(output),
    }
    payload['manifest_fingerprint'] = digest(payload)
    temporary = output / '.artifact_manifest.tmp'
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(output / 'artifact_manifest.json')
        return payload
    finally:
        temporary.unlink(missing_ok=True)


def capture_output_manifests(root, output):
    """Capture every route output that owns a status.json, children first."""
    output = Path(output)
    if not output.is_dir() or output.is_symlink():
        return []
    owners = [output]
    owners.extend(path.parent for path in output.rglob('status.json')
                  if path.is_file() and not path.is_symlink() and path.parent != output)
    owners = sorted(set(owners), key=lambda path: (-len(path.parts), str(path)))
    manifests = []
    for owner in owners:
        state = read_json(owner / 'status.json') or {}
        if not isinstance(state, dict):
            state = {}
        value = capture_output_manifest(root, owner, status=state.get('status'))
        if value:
            manifests.append((str(owner.relative_to(root)), value['manifest_fingerprint']))
    return manifests


def validate_output_manifest(output):
    output = Path(output)
    current = Path(output.anchor)
    for part in output.parts[1:]:
        current = current / part
        if current.is_symlink():
            return {'valid': False, 'reason': '阶段输出路径包含符号链接。', 'manifest': None}
    manifest = read_json(output / 'artifact_manifest.json')
    if not isinstance(manifest, dict) or manifest.get('format_version') != 1:
        return {'valid': False, 'reason': '缺少或不支持产物清单。', 'manifest': manifest}
    expected = manifest.get('manifest_fingerprint')
    body = {key: value for key, value in manifest.items() if key != 'manifest_fingerprint'}
    if not isinstance(expected, str) or digest(body) != expected:
        return {'valid': False, 'reason': '产物清单指纹不匹配。', 'manifest': manifest}
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        return {'valid': False, 'reason': '产物清单为空或格式无效。', 'manifest': manifest}
    recorded = set()
    for item in files:
        relative = item.get('path') if isinstance(item, dict) else None
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            return {'valid': False, 'reason': '产物清单包含非法路径。', 'manifest': manifest}
        path = output / relative
        if relative in recorded:
            return {'valid': False, 'reason': '产物清单存在重复路径。', 'manifest': manifest}
        recorded.add(relative)
        if any(parent.is_symlink() for parent in path.parents):
            return {'valid': False, 'reason': '产物路径包含符号链接。', 'manifest': manifest}
        if path.is_symlink() or not path.is_file():
            return {'valid': False, 'reason': '产物缺失：' + relative, 'manifest': manifest}
        if path.stat().st_size != item.get('bytes') or file_hash(path) != item.get('sha256'):
            return {'valid': False, 'reason': '产物校验失败：' + relative, 'manifest': manifest}
    return {'valid': True, 'reason': None, 'manifest': manifest}


def manifest_covers(check, output, paths):
    recorded = {item['path'] for item in check['manifest']['files']}
    return all(Path(path).relative_to(output).as_posix() in recorded for path in paths)


def validate_input_snapshot(workspace, task):
    """Check the immutable task input snapshot and its recorded hashes."""
    root = Path(workspace.root)
    parameters = task.get('parameters') or {}
    relative = parameters.get('input_manifest')
    fingerprint = task.get('input_fingerprint') or parameters.get('input_fingerprint')
    if not isinstance(relative, str) or not fingerprint:
        return {'valid': False, 'reason': '任务缺少输入快照指纹。'}
    try:
        manifest_path = safe_path(root, relative)
    except ValueError as error:
        return {'valid': False, 'reason': str(error)}
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get('fingerprint') != fingerprint:
        return {'valid': False, 'reason': '输入快照清单缺失或指纹不匹配。'}
    identity = manifest.get('identity') or {}
    if not isinstance(identity, dict):
        return {'valid': False, 'reason': '输入快照身份格式无效。'}
    if digest(identity) != fingerprint:
        return {'valid': False, 'reason': '输入快照身份内容与指纹不匹配。'}

    def check_file(item, base):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            return '输入快照清单格式无效。'
        try:
            path = safe_path(root, Path(base) / item['name'])
        except ValueError as error:
            return str(error)
        if path.is_symlink() or not path.is_file():
            return '输入快照文件缺失：' + item['name']
        if item.get('bytes') is not None and path.stat().st_size != item['bytes']:
            return '输入快照文件大小变化：' + item['name']
        if item.get('sha256') and file_hash(path) != item['sha256']:
            return '输入快照文件哈希变化：' + item['name']
        return None

    input_base = parameters.get('input')
    if not isinstance(input_base, str):
        return {'valid': False, 'reason': '任务输入路径缺失。'}
    checks = []
    for field in ('files', 'images', 'model', 'photo_files', 'model_files'):
        if not isinstance(identity.get(field, []), list):
            return {'valid': False, 'reason': '输入快照文件清单格式无效。'}
    for item in identity.get('files', []):
        checks.append((item, input_base))
    for item in identity.get('images', []):
        checks.append((item, str(Path(input_base) / 'images')))
    for item in identity.get('model', []):
        checks.append((item, str(Path(input_base) / 'model')))
    for item in identity.get('photo_files', []):
        checks.append((item, str(Path(input_base) / 'images')))
    for item in identity.get('model_files', []):
        checks.append((item, str(Path(input_base) / 'model')))
    if not checks:
        return {'valid': False, 'reason': '输入快照清单没有文件。'}
    for item, base in checks:
        try:
            error = check_file(item, base)
        except OSError as exception:
            return {'valid': False, 'reason': '输入快照读取失败：' + str(exception)}
        if error:
            return {'valid': False, 'reason': error}
    return {'valid': True, 'reason': None, 'manifest': manifest}


def validate_sfm_output(output):
    output = Path(output)
    status, _ = stage_state(output)
    summary = read_json(output / 'summary.json')
    check = validate_output_manifest(output)
    if status.get('status') != 'completed' or not isinstance(summary, dict) or not check['valid']:
        return {'valid': False, 'reason': check['reason'] or 'SfM 阶段未完成或缺少摘要。'}
    selected = summary.get('selected_model')
    if not isinstance(selected, str):
        return {'valid': False, 'reason': 'SfM 摘要缺少选定模型路径。'}
    model = Path(selected)
    if not model.is_absolute():
        model = output / model
    try:
        model = model.resolve(strict=True)
        model.relative_to(output.resolve(strict=True))
    except (OSError, ValueError):
        return {'valid': False, 'reason': 'SfM 选定模型缺失或越出阶段目录。'}
    if any((model / name).is_symlink() or not (model / name).is_file() or (model / name).stat().st_size == 0
           for name in ('cameras.bin', 'images.bin', 'points3D.bin')):
        return {'valid': False, 'reason': 'SfM 稀疏模型不完整。'}
    required = [output / 'status.json', output / 'summary.json']
    required += [model / name for name in ('cameras.bin', 'images.bin', 'points3D.bin')]
    if not manifest_covers(check, output, required):
        return {'valid': False, 'reason': 'SfM 模型或状态不在校验清单中。'}
    return {'valid': True, 'reason': None, 'summary': summary,
            'manifest_fingerprint': check['manifest']['manifest_fingerprint']}


def validate_mvs_output(output, completed_stage):
    output = Path(output)
    status, _ = stage_state(output)
    check = validate_output_manifest(output)
    result = status.get('dense_result') if isinstance(status.get('dense_result'), dict) else {}
    if not check['valid']:
        return {'valid': False, 'reason': check['reason'] or 'MVS 产物清单无效。'}
    if result.get('completed_stage') not in ('dense', 'mesh', 'textured'):
        return {'valid': False, 'reason': '没有已完成的 MVS 阶段可复用。'}
    order = ('dense', 'mesh', 'textured')
    if completed_stage not in order or order.index(result['completed_stage']) < order.index(completed_stage):
        return {'valid': False, 'reason': 'MVS 阶段尚未完成。'}
    work = output / 'dense'
    dense = ('scene.mvs', 'dense.mvs', 'dense.ply', 'dense_colmap.ply')
    required = list(dense)
    if completed_stage in ('mesh', 'textured'):
        required += ['mesh.mvs', 'mesh.ply']
    for name in required:
        path = work / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            return {'valid': False, 'reason': 'MVS 产物缺失或为空：' + name}
    if not manifest_covers(check, output, [output / 'status.json'] + [work / name for name in required]):
        return {'valid': False, 'reason': 'MVS 依赖不在产物清单中。'}
    undistorted = output / 'undistorted/images'
    photos = [path for path in undistorted.rglob('*') if path.is_file()]
    if not undistorted.is_dir() or not photos:
        return {'valid': False, 'reason': 'MVS 去畸变照片缺失。'}
    if not manifest_covers(check, output, photos):
        return {'valid': False, 'reason': 'MVS 去畸变照片不在产物清单中。'}
    try:
        from ..densify import ply_counts
        dense_counts = ply_counts(work / 'dense.ply')
        if dense_counts.get('vertex', 0) <= 0:
            return {'valid': False, 'reason': 'MVS 稠密点云为空。'}
        if completed_stage in ('mesh', 'textured'):
            mesh_counts = ply_counts(work / 'mesh.ply')
            if mesh_counts.get('face', 0) <= 0:
                return {'valid': False, 'reason': 'MVS 网格没有有效面。'}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return {'valid': False, 'reason': 'MVS PLY 校验失败：' + str(error)}
    return {'valid': True, 'reason': None,
            'completed_stage': completed_stage,
            'dense_points': dense_counts.get('vertex'),
            'manifest_fingerprint': check['manifest']['manifest_fingerprint']}


def stage_state(output):
    status = read_json(Path(output) / 'status.json') or {}
    if not isinstance(status, dict):
        status = {}
    stages = status.get('stages') if isinstance(status.get('stages'), list) else []
    return status, [stage for stage in stages if isinstance(stage, dict)]


def validate_prepared_dataset(output):
    output = Path(output)
    check = validate_output_manifest(output)
    metadata = read_json(output / 'dataset.json')
    if not check['valid'] or not isinstance(metadata, dict) or metadata.get('status') != 'prepared':
        return {'valid': False, 'reason': check['reason'] or '3DGS 数据准备未完成。'}
    required = [output / 'dataset.json'] + [output / 'sparse/0' / name
                for name in ('cameras.bin', 'images.bin', 'points3D.bin')]
    photos = [p for p in (output / 'images').rglob('*') if p.is_file()]
    if not photos or not all(p.is_file() and p.stat().st_size for p in required):
        return {'valid': False, 'reason': '3DGS 数据集模型或照片缺失。'}
    if not manifest_covers(check, output, required + photos):
        return {'valid': False, 'reason': '3DGS 数据集依赖不在产物清单中。'}
    return {'valid': True, 'manifest_fingerprint': check['manifest']['manifest_fingerprint']}


def execution_fingerprint(workspace, method, parameters=None):
    """Fingerprint runtime source and external executables used by a route."""
    root = Path(workspace)
    modules = ['pipeline.py', 'reconstruct.py', 'densify.py', 'prepare_brush.py',
               'train_3dgs.py', 'fast3r.py', 'task_runtime/recovery.py',
               'task_runtime/recovery_worker.py', 'task_runtime/validators.py']
    code = {}
    for relative in modules:
        path = root / 'recon_workbench' / relative
        code[relative] = file_hash(path) if path.is_file() else None
    parameters = parameters or {}
    configured = {'colmap': parameters.get('colmap', 'colmap')}
    if method == '3dgs':
        configured['brush'] = parameters.get('brush') or str(root / '.tools/brush/brush_app')
    if method == 'mvs':
        from ..densify import required_tools
        configured_dir = parameters.get('openmvs_bin')
        local = root / '.tools/openmvs'
        for name in required_tools(parameters.get('mvs_stage', 'textured')):
            candidates = [Path(configured_dir).expanduser() / name] if configured_dir else (
                sorted(local.rglob(name)) if local.is_dir() else [])
            resolved = next((path.resolve() for path in candidates
                             if path.is_file() and os.access(path, os.X_OK)), None)
            if resolved is None:
                discovered = shutil.which(name)
                resolved = Path(discovered).resolve() if discovered else None
            configured[name] = str(resolved) if resolved else name
    executables = {}
    for name, requested in configured.items():
        resolved = shutil.which(str(requested))
        if resolved is None and Path(str(requested)).is_file():
            resolved = str(Path(str(requested)).resolve())
        if resolved is None:
            executables[name] = {'available': False}
        else:
            binary = Path(resolved)
            executables[name] = {'bytes': binary.stat().st_size, 'sha256': file_hash(binary)}
    return digest({'method': method, 'python': sys.version, 'platform': platform.platform(),
                   'code': code, 'executables': executables,
                   'model': parameters.get('fast3r_model') if method == 'fast3r' else None})
