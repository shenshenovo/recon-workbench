"""Optional CUDA Fast3R inference adapter; base CLI/tests need no torch."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import time

from .paths import PROJECT_ROOT
from .reconstruct import Pipeline, positive_int, timestamp, write_json

DEFAULT_MODEL = "jedyang97/Fast3R_ViT_Large_512"


def get_parser():
    parser = argparse.ArgumentParser(description="使用预训练 Fast3R 输出彩色点云；需要独立 CUDA 环境。")
    parser.add_argument('--images', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--model', default=DEFAULT_MODEL, help='Hugging Face 模型名或本机模型目录')
    parser.add_argument('--image-size', type=int, choices=(224, 512), default=512)
    parser.add_argument('--max-views', type=positive_int, default=8, help='输入数量保护限制，不自动截取照片')
    parser.add_argument('--precision', choices=('fp16', 'bf16', 'fp32'), default='bf16')
    parser.add_argument('--confidence-percentile', type=float, default=50,
                        help='每视图丢弃低于该置信度百分位的点；不是准确率')
    parser.add_argument('--max-points', type=positive_int, default=300000, help='过滤后导出点数上限')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--check-env', action='store_true', help='检查 CUDA 和导入，不下载模型')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    return parser


def validate_options(args):
    if not math.isfinite(args.confidence_percentile) or not 0 <= args.confidence_percentile < 100:
        raise ValueError('置信度百分位必须在 0～100 之间，不包含 100。')
    if not 2 <= args.max_views <= 500 or not 1 <= args.max_points <= 5000000 or not 0 <= args.seed < 2**32:
        raise ValueError('Fast3R 输入上限需为 2～500、点数上限为 1～5000000，seed 需为非负 32 位整数。')
    if not isinstance(args.model, str) or not args.model.strip() or len(args.model) > 1024:
        raise ValueError('请指定模型名或本机模型目录。')


def image_names(images, max_views):
    if not images.is_dir():
        raise ValueError('图片目录不存在。')
    names = sorted(path.relative_to(images).as_posix() for path in images.rglob('*')
                   if path.is_file() and path.suffix.lower() in ('.jpg', '.jpeg', '.png')
                   and not any(part.startswith('.') for part in path.relative_to(images).parts))
    if not 2 <= len(names) <= max_views:
        raise ValueError(f'Fast3R 当前允许 2～{max_views} 张照片，实际 {len(names)} 张。'
                         '请先保存更小的筛选；8GB 显存建议从 2～4 张开始，不会自动截断输入。')
    if any('\n' in name or '\r' in name for name in names):
        raise ValueError('图片名称不能含换行符。')
    return names


def write_cloud(path, rows):
    """Write bounded XYZ/RGB rows atomically, rejecting invalid coordinates."""
    temporary = path.with_suffix('.ply.part')
    payload = path.with_suffix('.vertices.part')
    count = 0
    try:
        with payload.open('xb') as stream:
            for xyz, rgb in rows:
                if len(xyz) != 3 or len(rgb) != 3 or not all(math.isfinite(x) and abs(x) <= 3.4e38 for x in xyz):
                    raise ValueError('点云包含无效坐标。')
                if not all(isinstance(x, int) and 0 <= x <= 255 for x in rgb):
                    raise ValueError('RGB 必须为 0～255 的整数。')
                stream.write(struct.pack('<fffBBB', *xyz, *rgb))
                count += 1
        if not count:
            raise ValueError('置信度过滤后点云为空，请检查预测或适当降低过滤百分位。')
        with temporary.open('xb') as stream, payload.open('rb') as source:
            stream.write(('ply\nformat binary_little_endian 1.0\n'
                          f'element vertex {count}\nproperty float x\nproperty float y\nproperty float z\n'
                          'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n').encode('ascii'))
            for block in iter(lambda: source.read(1024**2), b''):
                stream.write(block)
        if path.exists():
            raise ValueError('拒绝覆盖已有点云。')
        temporary.rename(path)
        return count
    finally:
        temporary.unlink(missing_ok=True)
        payload.unlink(missing_ok=True)


def inspect_environment():
    try:
        import torch
    except ImportError as error:
        raise RuntimeError('缺少 PyTorch；请在 GPU 电脑按 docs/FAST3R.md 配置独立环境。') from error
    if not torch.cuda.is_available():
        raise RuntimeError('Fast3R 路线需要 CUDA GPU；当前 Python 未检测到可用 CUDA，请检查所用环境。')
    try:
        from fast3r.models.fast3r import Fast3R
        from fast3r.dust3r.inference_multiview import inference
        from fast3r.dust3r.utils.image import load_images
    except ImportError as error:
        raise RuntimeError(f'Fast3R 依赖导入失败：{error}。安装说明见 docs/FAST3R.md。') from error
    prop = torch.cuda.get_device_properties(0)
    metadata = {'torch_version': torch.__version__, 'cuda_runtime': torch.version.cuda,
                'gpu': prop.name, 'gpu_total_memory_bytes': prop.total_memory,
                'source_file': str(Path(sys.modules[Fast3R.__module__].__file__).resolve())}
    try:
        result = subprocess.run(['git', '-C', str(Path(metadata['source_file']).parents[2]), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=5)
        metadata['fast3r_commit'] = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        metadata['fast3r_commit'] = None
    return torch, Fast3R, inference, load_images, metadata


def load_model(Fast3R, requested):
    from huggingface_hub import hf_hub_download
    local = Path(requested).expanduser()
    if local.is_dir():
        config_path = local / 'config.json'
        source, revision = str(local.resolve()), None
    else:
        config_path = Path(hf_hub_download(requested, 'config.json'))
        source, revision = requested, config_path.parent.name
    config = json.loads(config_path.read_text())
    # Override attention through the documented constructor configuration, not monkey patches.
    encoder, decoder = dict(config['encoder_args']), dict(config['decoder_args'])
    encoder['attn_implementation'] = decoder['attn_implementation'] = 'pytorch_auto'
    kwargs = {'encoder_args': encoder, 'decoder_args': decoder, 'head_args': config['head_args']}
    if revision:
        kwargs['revision'] = revision
    model = Fast3R.from_pretrained(source, **kwargs)
    return model, {'model_source': source, 'model_revision': revision,
                   'model_config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                   'attention': 'pytorch_auto'}


def prediction_rows(output, percentile, max_points, statistics):
    """Export only globally referenced point maps; local maps are not concatenated."""
    import numpy as np
    predictions, views = output['preds'], output['views']
    if len(predictions) != len(views) or not views:
        raise ValueError('Fast3R 返回的点图与视图数量不一致。')
    statistics.update(predicted_points=0, confidence_filtered_points=0, per_view=[])
    for index, (pred, view) in enumerate(zip(predictions, views)):
        points = pred['pts3d_in_other_view'].detach().float().cpu().numpy()
        confidence = pred['conf'].detach().float().cpu().numpy()
        image = view['img'].detach().float().cpu().numpy()
        if points.ndim != 4 or points.shape[0] != 1 or points.shape[-1] != 3:
            raise ValueError('Fast3R 全局点图维度不受支持。')
        height, width = points.shape[1:3]
        if image.shape != (1, 3, height, width) or confidence.shape not in ((1, height, width), (1, height, width, 1)):
            raise ValueError('点图、置信度和颜色像素未对齐。')
        points, confidence = points.reshape(-1, 3), confidence.reshape(-1)
        colors = image[0].transpose(1, 2, 0).reshape(-1, 3)
        finite = np.isfinite(points).all(axis=1) & np.isfinite(confidence) & np.isfinite(colors).all(axis=1)
        finite &= (np.abs(points) <= 3.4e38).all(axis=1)
        threshold = float(np.percentile(confidence[finite], percentile)) if finite.any() else None
        indices = np.flatnonzero(finite & (confidence >= threshold)) if threshold is not None else np.array([], dtype=int)
        eligible = len(indices)
        quota = max_points // len(views) + int(index < max_points % len(views))
        if len(indices) > quota:
            indices = indices[np.linspace(0, len(indices)-1, quota, dtype=int)] if quota else indices[:0]
        rgb = np.clip(np.rint((colors[indices] + 1) * 127.5), 0, 255).astype(np.uint8)
        statistics['predicted_points'] += len(points)
        statistics['confidence_filtered_points'] += eligible
        statistics['per_view'].append({'index': index, 'width': width, 'height': height,
                                       'threshold': threshold, 'eligible_points': eligible, 'exported_points': len(indices)})
        for xyz, color in zip(points[indices], rgb):
            yield xyz.tolist(), color.tolist()


def run_worker(args):
    torch, Fast3R, inference, load_images, environment = inspect_environment()
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    if args.precision == 'bf16' and not torch.cuda.is_bf16_supported():
        raise RuntimeError('当前 GPU 不支持 BF16；可尝试 --precision fp16。')
    manifest = json.loads((args.output / 'input.json').read_text())
    files = []
    for item in manifest['files']:
        path = args.images / item['name']
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                raise ValueError('输入图片发生变化：' + item['name'])
        files.append(str(path))
    print('设备：' + environment['gpu'] + '；加载预训练模型（首次可能需要下载权重）。', flush=True)
    started = time.monotonic()
    model, model_info = load_model(Fast3R, args.model)
    model = model.to('cuda').eval()
    model.max_parallel_views_for_head = 1
    model_seconds = time.monotonic() - started
    print(f'模型加载完成；预处理 {len(files)} 张照片，size={args.image_size}。', flush=True)
    started = time.monotonic()
    images = load_images(files, size=args.image_size, verbose=True)
    if len(images) != len(files):
        raise ValueError('预处理未返回全部选中照片。')
    preprocess_seconds = time.monotonic() - started
    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    started = time.monotonic()
    print('执行 Fast3R 前向推理；不进行场景训练。', flush=True)
    precision = {'bf16': 'bf16-mixed', 'fp16': '16-mixed', 'fp32': '32'}[args.precision]
    with torch.inference_mode():
        output = inference(images, model, torch.device('cuda'), dtype=precision, verbose=True, profiling=False)
    # Be tolerant of official versions returning an optional profiling tuple.
    if isinstance(output, tuple):
        output = output[0]
    if len(output['views']) != len(files) or len(output['preds']) != len(files):
        raise ValueError('Fast3R 返回的视图数与实际输入不一致，拒绝记录为完成。')
    torch.cuda.synchronize()
    inference_seconds = time.monotonic() - started
    peak = torch.cuda.max_memory_allocated()
    del model
    torch.cuda.empty_cache()
    print('推理完成；过滤并导出公共坐标系中的彩色点云。', flush=True)
    statistics = {}
    pointcloud = args.output / 'pointcloud.ply'
    count = write_cloud(pointcloud, prediction_rows(output, args.confidence_percentile, args.max_points, statistics))
    summary = dict(environment, **model_info, method='fast3r', input_images=len(files), processed_views=len(files),
                   dense_points=count, dense_pointcloud=str(pointcloud), pointcloud=str(pointcloud),
                   max_resolution=args.image_size, precision=args.precision,
                   model_load_seconds=round(model_seconds, 3), preprocessing_seconds=round(preprocess_seconds, 3),
                   inference_seconds=round(inference_seconds, 3), peak_gpu_allocated_bytes=peak,
                   confidence_percentile=args.confidence_percentile, max_points=args.max_points,
                   predicted_points=statistics['predicted_points'], confidence_filtered_points=statistics['confidence_filtered_points'],
                   warnings=['输出为置信度过滤并限点后的预测点图汇集，未做表面融合；点数不是几何精度。',
                             '当前适配器未导出相机位姿，不计算 SfM 注册率或重投影误差，不直接衔接 MVS/3DGS。'])
    write_json(args.output / 'pointcloud_stats.json', statistics)
    write_json(args.output / 'summary.json', summary)
    print(f'已导出 {count} 个点：{pointcloud}', flush=True)


def main(argv=None):
    args = get_parser().parse_args(argv)
    pipeline = None
    try:
        validate_options(args)
        if args.check_env:
            print(json.dumps(inspect_environment()[-1], ensure_ascii=False, indent=2))
            return 0
        if args.images is None:
            raise ValueError('需要 --images，或使用 --check-env。')
        args.images = args.images.expanduser().resolve()
        if Path(args.model).expanduser().is_dir():
            args.model = str(Path(args.model).expanduser().resolve())
        names = image_names(args.images, args.max_views)
        args.output = (args.output or PROJECT_ROOT / 'outputs' / datetime.now().strftime('fast3r_%Y%m%d_%H%M%S_%f')).expanduser().resolve()
        if args.output == args.images or args.images in args.output.parents or args.output in args.images.parents:
            raise ValueError('输出目录不能与输入目录重叠。')
        if args.worker:
            if not (args.output / 'config.json').is_file() or not (args.output / 'input.json').is_file():
                raise ValueError('推理子进程缺少父任务配置与输入清单。')
            run_worker(args)
            return 0
        if args.output.exists():
            raise ValueError('输出目录已存在，请选择新目录。')
        args.output.mkdir(parents=True)
        (args.output / 'logs').mkdir()
        write_json(args.output / 'config.json', {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()})
        files = []
        for name in names:
            with (args.images / name).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files.append({'name': name, 'sha256': digest})
        write_json(args.output / 'input.json', {'files': files})
        pipeline = Pipeline(args.output, sys.executable, {'method': 'fast3r', 'input_images': len(names)})
        command = ['-u', '-m', 'recon_workbench.fast3r', '--worker', '--images', args.images, '--output', args.output,
                   '--model', args.model, '--image-size', args.image_size, '--max-views', args.max_views,
                   '--precision', args.precision, '--confidence-percentile', args.confidence_percentile,
                   '--max-points', args.max_points, '--seed', args.seed]
        pipeline.run('01_inference', command, cwd=PROJECT_ROOT)
        summary = json.loads((args.output / 'summary.json').read_text())
        summary['elapsed_seconds'] = round(time.monotonic() - pipeline.started, 3)
        write_json(args.output / 'summary.json', summary)
        pipeline.state.update(status='completed', finished_at=timestamp(), summary=summary)
        pipeline.save()
        return 0
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        message = '用户中断' if cancelled else str(error)
        if 'out of memory' in message.lower():
            message = 'CUDA 显存不足：先减少到 2～4 张，或尝试 224 预处理尺寸；不会自动分组或省略图片。\n' + message
        if pipeline:
            pipeline.state.update(status='cancelled' if cancelled else 'failed', finished_at=timestamp(), error=message)
            pipeline.save()
        print('Fast3R 失败：' + message, file=sys.stderr, flush=True)
        return 130 if cancelled else 1


if __name__ == '__main__':
    sys.exit(main())
