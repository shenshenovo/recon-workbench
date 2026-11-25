"""Shared, deterministic data preparation for the frozen image-quality experiments."""
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from scripts.quality_sfm_smoke import read_source, sha256, versions


QUALITY_FLAGS = ("blurry", "underexposed", "overexposed")
VARIANTS = [("blur", radius) for radius in (0.75, 2.0, 6.0)] + [
    ("brightness", factor) for factor in (0.18, 0.5, 0.8, 1.25, 1.8, 3.0)]
SFM_CONFIG = {"camera_model": "OPENCV", "max_image_size": 1000,
              "max_features": 4096, "threads": 4, "matcher": "exhaustive",
              "single_camera": False, "poses_imported": False,
              "camera_params_supplied": False, "repetitions": 3}


def write_json(path, document):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
                         + "\n", encoding="utf-8")
    temporary.replace(path)


def evenly_spaced(items, count):
    if count < 2 or len(items) < count:
        raise ValueError("Not enough source frames for the frozen sample size.")
    indices = [index * (len(items) - 1) // (count - 1) for index in range(count)]
    return [items[index] for index in indices]


def source_frames(source, count):
    root, transforms, paths = read_source(source)
    if transforms is None:
        raise ValueError("This experiment requires transforms.json and its frame order.")
    document = json.loads(transforms.read_text(encoding="utf-8"))
    declared = document.get("frames", [])
    if len(paths) != len(declared):
        raise ValueError("Missing, duplicate or invalid source frame; do not silently resample.")
    return root, transforms, evenly_spaced(paths, count)


def new_output(output, source):
    output, source = Path(output).resolve(), Path(source).resolve()
    if output == source or source in output.parents:
        raise ValueError("Experiment output cannot be inside the source scene.")
    output.mkdir(parents=True, exist_ok=False)
    return output


def provenance():
    root = Path(__file__).resolve().parents[1]
    files = [root / "docs" / "QUALITY_ACCEPTANCE.md"]
    files += sorted((root / "scripts").glob("quality_*.py"))
    files += [root / "recon_workbench" / name for name in
              ("image_quality.py", "image_similarity.py", "reconstruct.py")]
    hashes = {path.relative_to(root).as_posix(): sha256(path) for path in files}
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    environment = versions()
    for label, command in (("git_head", ["git", "rev-parse", "HEAD"]),
                           ("git_status", ["git", "status", "--short"]),
                           ("colmap_version", ["colmap", "-h"])):
        completed = subprocess.run(command, cwd=root, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=30)
        environment[label] = completed.stdout.strip() if label != "colmap_version" else (
            completed.stdout.strip().splitlines()[:3])
    return {"files_sha256": hashes, "bundle_sha256": identity, "environment": environment}


def variant_name(path, kind, value):
    return "zz_{}_{}_{}.png".format(Path(path).stem, kind, format(value, "g").replace(".", "p"))


def make_variant(source, target, kind, value):
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        if kind == "blur":
            image = image.filter(ImageFilter.GaussianBlur(value))
        elif kind == "brightness":
            image = ImageEnhance.Brightness(image).enhance(value)
        else:
            raise ValueError("Unknown controlled transformation: " + kind)
        image.save(target, format="PNG")
    return {"name": Path(target).name, "source_name": Path(source).name,
            "kind": kind, "level": value, "sha256": sha256(target)}


def make_controls(root):
    """Non-photographic probes; never mix these with camera-image accuracy."""
    root.mkdir()
    rng = np.random.default_rng(20261103)
    arrays = {
        "flat_black": np.zeros((384, 512), dtype=np.uint8),
        "flat_gray": np.full((384, 512), 128, dtype=np.uint8),
        "flat_white": np.full((384, 512), 255, dtype=np.uint8),
        "gray_noise": np.clip(128 + rng.normal(0, 24, (384, 512)), 0, 255).astype(np.uint8),
    }
    for name, array in arrays.items():
        Image.fromarray(array).save(root / (name + ".png"))


def excluded_by_quality(item):
    if not item.get("decodable", False):
        return ["decode_error"]
    return [flag for flag in QUALITY_FLAGS if flag in item["flags"]]
