#!/usr/bin/env python3
"""Build a blinded, cross-scene pair review batch for fixed-threshold validation."""
import argparse
import csv
import hashlib
import html
import json
import random
import shutil
from itertools import combinations
from pathlib import Path

from recon_workbench.image_quality import metrics, policy, read_image_bytes
from recon_workbench.image_similarity import compare_pair


ROOT = Path(__file__).resolve().parents[1]
QUOTAS = (('sequence_neighbor', 20), ('hash_or_threshold', 10), ('broad_negative', 20))
FIELDS = ('pair_id', 'split', 'image_a', 'image_b', 'label', 'reviewer', 'notes')


def choose(pool, amount, rng, used):
    candidates = [row for row in pool if row['key'] not in used]
    rng.shuffle(candidates)
    selected = candidates[:amount]
    used.update(row['key'] for row in selected)
    return selected


def candidate_pairs(source, seed):
    files = sorted(path for path in source.iterdir()
                   if path.is_file() and path.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    if len(files) < 10:
        raise ValueError('Need at least 10 source images for a cross-scene review batch.')

    settings = policy()
    wider = policy({'near_distance': 8, 'phash_distance': 16})
    images = []
    for index, path in enumerate(files):
        data = read_image_bytes(path)
        features = metrics(data, settings)
        images.append({**features, 'name': path.name, 'path': path,
                       'sha256': hashlib.sha256(data).hexdigest(), 'source_index': index})

    pools = {name: [] for name, _ in QUOTAS}
    used = set()
    for left, right in combinations(images, 2):
        pair = compare_pair(left, right, wider)
        key = tuple(sorted((left['name'], right['name'])))
        row = {'left': left, 'right': right, 'pair': pair, 'key': key}
        gap = abs(left['source_index'] - right['source_index'])
        default_pair = compare_pair(left, right, settings)
        if pair['eligible'] and gap <= 1:
            pools['sequence_neighbor'].append(row)
        elif pair['eligible'] and (
                pair['dhash_match'] != pair['phash_match']
                or default_pair['match'] or pair['match']
                or pair['dhash_distance'] <= wider['near_distance'] + 3
                or pair['phash_distance'] <= wider['phash_distance'] + 3):
            pools['hash_or_threshold'].append(row)
        elif pair['eligible'] and gap >= max(8, len(images) // 3):
            pools['broad_negative'].append(row)

    rng = random.Random(seed)
    selected = []
    selected_pool_counts = {}
    for group, amount in QUOTAS:
        chosen = choose(pools[group], amount, rng, used)
        selected.extend(chosen)
        selected_pool_counts[group] = len(chosen)
    missing = sum(amount for _, amount in QUOTAS) - len(selected)
    for group in ('broad_negative', 'sequence_neighbor', 'hash_or_threshold'):
        if not missing:
            break
        chosen = choose(pools[group], missing, rng, used)
        selected.extend(chosen)
        selected_pool_counts[group] += len(chosen)
        missing -= len(chosen)
    if len(selected) < sum(amount for _, amount in QUOTAS):
        raise ValueError(f'Only {len(selected)} eligible pairs are available.')
    return (selected, images, {name: len(rows) for name, rows in pools.items()},
            selected_pool_counts)


def review_page(rows, scene, storage_key):
    payload = json.dumps(rows, ensure_ascii=True).replace('<', '\\u003c')
    title = html.escape(scene)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} 视角复核</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, "Microsoft YaHei", sans-serif; color: #202a32; background: #f3f5f4; }}
    * {{ box-sizing: border-box; }} body {{ margin: 0; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 12px 18px; background: #fff; border-bottom: 1px solid #d8dedb; }}
    .bar {{ max-width: 1160px; margin: auto; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
    h1 {{ margin: 0; font-size: 19px; }} .controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    button, select {{ min-height: 38px; border: 1px solid #aebbb5; border-radius: 4px; background: #fff; color: inherit; padding: 7px 12px; font: inherit; cursor: pointer; }}
    button:hover, select:hover {{ background: #eef3f0; }} button[aria-pressed="true"] {{ color: #fff; background: #176b57; border-color: #176b57; }}
    #export {{ color: #fff; background: #244d63; border-color: #244d63; }}
    main {{ max-width: 1160px; margin: 0 auto; padding: 14px 16px 48px; }}
    .intro {{ margin: 0 0 12px; color: #52615a; font-size: 14px; }}
    .pair {{ padding: 14px 0 18px; border-top: 1px solid #ccd5d0; }}
    .pair-head {{ display: flex; justify-content: space-between; margin-bottom: 8px; color: #53615b; font-size: 14px; }}
    .photos {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    figure {{ margin: 0; background: #e3e8e5; }} img {{ display: block; width: 100%; height: min(52vh, 420px); object-fit: contain; }}
    .answers {{ display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }} .answers button {{ flex: 1 1 190px; }}
    @media (max-width: 620px) {{ header {{ padding: 10px 12px; }} main {{ padding: 12px 10px 36px; }} img {{ height: 30vh; min-height: 150px; }} .answers button {{ flex-basis: 100%; }} }}
  </style>
</head>
<body>
  <header><div class="bar"><h1>{title} 照片对复核</h1><div class="controls">
    <span id="progress" aria-live="polite"></span>
    <select id="filter" aria-label="显示哪些照片对"><option value="unreviewed">只看未判断</option><option value="all">查看全部</option></select>
    <button id="export">导出复核结果</button>
  </div></div></header>
  <main><p class="intro">判断两张图是否为几乎相同的相机视角。只看画面，不看文件顺序或算法结果；不确定可选“拿不准”。</p><div id="pairs"></div></main>
  <script>
    const pairs = {payload};
    const key = {json.dumps(storage_key)};
    let answers = {{}};
    try {{ answers = JSON.parse(localStorage.getItem(key) || "{{}}"); }} catch {{}}
    const container = document.querySelector("#pairs");
    const progress = document.querySelector("#progress");
    const filter = document.querySelector("#filter");
    const labels = [["near_duplicate", "几乎同一视角"], ["different", "不同视角"], ["challenge", "拿不准"]];
    function save() {{ localStorage.setItem(key, JSON.stringify(answers)); render(); }}
    function render() {{
      progress.textContent = `已判断 ${{Object.keys(answers).filter(id => pairs.some(pair => pair.id === id)).length}} / ${{pairs.length}}`;
      container.replaceChildren();
      for (const pair of pairs.filter(row => filter.value === "all" || !answers[row.id])) {{
        const section = document.createElement("section"); section.className = "pair";
        const heading = document.createElement("div"); heading.className = "pair-head";
        const id = document.createElement("span"); id.textContent = pair.id;
        const state = document.createElement("span"); state.textContent = answers[pair.id] ? labels.find(row => row[0] === answers[pair.id])[1] : "未判断";
        heading.append(id, state);
        const photos = document.createElement("div"); photos.className = "photos";
        for (const path of [pair.a, pair.b]) {{ const figure = document.createElement("figure"); const image = document.createElement("img"); image.loading = "lazy"; image.src = path; image.alt = "待比较照片"; figure.append(image); photos.append(figure); }}
        const buttons = document.createElement("div"); buttons.className = "answers";
        for (const [value, text] of labels) {{ const button = document.createElement("button"); button.textContent = text; button.setAttribute("aria-pressed", String(answers[pair.id] === value)); button.addEventListener("click", () => {{ answers[pair.id] = value; save(); }}); buttons.append(button); }}
        section.append(heading, photos, buttons); container.append(section);
      }}
    }}
    filter.addEventListener("change", render);
    document.querySelector("#export").addEventListener("click", () => {{
      const escape = value => '"' + String(value ?? "").replaceAll('"', '""') + '"';
      const rows = [{json.dumps(list(FIELDS))}];
      for (const pair of pairs) rows.push([pair.id, "validation", pair.source_a, pair.source_b, answers[pair.id] || "", "", ""]);
      const csv = "\\ufeff" + rows.map(row => row.map(escape).join(",")).join("\\r\\n");
      const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([csv], {{ type: "text/csv;charset=utf-8" }})); link.download = "pair_labels_reviewed.csv"; link.click(); URL.revokeObjectURL(link.href);
    }});
    render();
  </script>
</body>
</html>
'''


def build(source, output, scene, pair_count, seed):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('Output directory already exists: ' + str(output))
    selected, images, pool_counts, selected_pool_counts = candidate_pairs(source, seed)
    if pair_count != sum(amount for _, amount in QUOTAS):
        raise ValueError('The current stratified review template produces exactly 50 pairs.')

    output.mkdir(parents=True)
    image_root = output / 'images'
    image_root.mkdir()
    copied = {}
    pairs, csv_rows = [], []
    for index, candidate in enumerate(selected, start=1):
        row = {'id': f'P{index:03d}'}
        for side, key in (('left', 'a'), ('right', 'b')):
            image = candidate[side]
            if image['name'] not in copied:
                safe_name = image['sha256'][:16] + image['path'].suffix.lower()
                shutil.copyfile(image['path'], image_root / safe_name)
                copied[image['name']] = 'images/' + safe_name
            row[key] = copied[image['name']]
        row.update(source_a=candidate['left']['name'], source_b=candidate['right']['name'])
        pairs.append(row)
        csv_rows.append({'pair_id': row['id'], 'split': 'validation',
                         'image_a': row['source_a'], 'image_b': row['source_b'],
                         'label': '', 'reviewer': '', 'notes': ''})

    (output / 'review.html').write_text(
        review_page(pairs, scene, f'image-quality-review-{scene}-{seed}'), encoding='utf-8')
    with (output / 'pair_labels.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows)
    metadata = {
        'scene': scene, 'source_directory': source.relative_to(ROOT).as_posix(),
        'source_image_count': len(images),
        'source_manifest_sha256': hashlib.sha256(
            (source.parent / 'download-manifest.json').read_bytes()).hexdigest()
            if (source.parent / 'download-manifest.json').is_file() else None,
        'pair_count': len(pairs), 'split': 'validation', 'seed': seed,
        'target_quotas': dict(QUOTAS), 'selected_pool_counts': selected_pool_counts,
        'available_pool_counts': pool_counts,
        'pairs': pairs,
        'thresholds_are_not_shown_to_reviewer': True,
        'thresholds_to_evaluate_after_labels': [{'near_distance': 4, 'phash_distance': 6},
                                                {'near_distance': 8, 'phash_distance': 16}],
    }
    (output / 'review-candidates.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    selected_description = '、'.join(
        f'{group} {count} 对' for group, count in selected_pool_counts.items() if count)
    readme = f'''# {scene} 独立场景复核

这是固定阈值的外部验证集，只用于验证，不用它重新选择阈值。来源是 {len(images)} 张覆盖整段序列的均匀抽样原图；候选对分层包含相邻抽样视图、哈希分歧/阈值附近图片对，以及较远视图。

打开 `review.html`，判断 50 对图片。请选择“几乎同一视角”“不同视角”或“拿不准”。只根据画面内容判断，不参考文件顺序、文件名或算法提示。结果自动保存在当前浏览器；完成后点“导出复核结果”，把导出的 `pair_labels_reviewed.csv` 放回本目录。

所有图片都是原图逐字节副本。该集合的预测结果将在标签固定后分别按 dHash/pHash 4/6 与 8/16 统计；不要用此验证集调整阈值。

本批实际抽样组成：{selected_description}。阈值附近/哈希分歧候选池只有 {pool_counts['hash_or_threshold']} 对，因此不足目标配额的部分从较远视图候选中补齐；候选池和实际抽样数保存在 `review-candidates.json`。

将导出的 CSV 放回本目录后，在仓库根目录运行：

```bash
python -m scripts.evaluate_scene_quality_review \\
  --candidates data/quality/dozer-review-20261103-external-validation-final/review-candidates.json \\
  --labels data/quality/dozer-review-20261103-external-validation-final/pair_labels_reviewed.csv \\
  --output outputs/quality-dozer-validation
```

输出目录必须是新目录。报告会保留本批标签、图片哈希，并分别给出 4/6 与 8/16 的混淆矩阵和 precision/recall；不修改应用阈值。
'''
    (output / 'README.md').write_text(readme, encoding='utf-8')
    return len(pairs), len(copied), pool_counts, selected_pool_counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/datasets/nerfstudio/dozer/images')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'data/quality/dozer-review-20261103-external-validation-final')
    parser.add_argument('--scene', default='Dozer')
    parser.add_argument('--pair-count', type=int, default=50)
    parser.add_argument('--seed', type=int, default=20261103)
    args = parser.parse_args(argv)
    try:
        pairs, images, pools, selected_pools = build(
            args.source, args.output, args.scene, args.pair_count, args.seed)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(f'Created {pairs} blind review pairs from {images} images: {args.output}')
    print('Available candidate pool counts:', json.dumps(pools, sort_keys=True))
    print('Selected pair counts:', json.dumps(selected_pools, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
