"""Held-out pose consistency against published poses, never geometric truth."""
import json
import math
from pathlib import Path

import numpy as np


def quaternion_rotation(q):
    q = np.asarray(q, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all() or np.linalg.norm(q) < 1e-12:
        raise ValueError("Invalid COLMAP quaternion.")
    w, x, y, z = q / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - w*z), 2 * (x*z + w*y)],
        [2 * (x*y + w*z), 1 - 2 * (x*x + z*z), 2 * (y*z - w*x)],
        [2 * (x*z - w*y), 2 * (y*z + w*x), 1 - 2 * (x*x + y*y)]])


def read_colmap_poses(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    index, poses = 0, {}
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("#"):
            index += 1
            continue
        fields = line.split(maxsplit=9)
        if len(fields) != 10:
            raise ValueError("Invalid COLMAP image metadata.")
        rotation = quaternion_rotation([float(value) for value in fields[1:5]])
        translation = np.array([float(value) for value in fields[5:8]])
        if not np.isfinite(translation).all() or fields[9] in poses:
            raise ValueError("Nonfinite or duplicate COLMAP pose.")
        poses[fields[9]] = {"center": -rotation.T @ translation, "rotation": rotation.T}
        index += 2
    return poses


def read_reference_poses(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    poses = {}
    for frame in document["frames"]:
        name = Path(frame["file_path"]).name
        matrix = np.asarray(frame["transform_matrix"], dtype=float)
        if matrix.shape != (4, 4) or not np.isfinite(matrix).all() or name in poses:
            raise ValueError("Invalid or duplicate reference pose.")
        rotation = matrix[:3, :3] @ np.diag([1.0, -1.0, -1.0])
        if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5) or not np.isclose(
                np.linalg.det(rotation), 1.0, atol=1e-5):
            raise ValueError("Reference camera orientation is not a proper rotation.")
        poses[name] = {"center": matrix[:3, 3], "rotation": rotation}
    return poses


def fit_similarity(source, target):
    """Fit target = scale * rotation @ source + translation, det(rotation)=+1."""
    source, target = np.asarray(source, dtype=float), np.asarray(target, dtype=float)
    if source.shape != target.shape or source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("Expected paired Nx3 centers.")
    if len(source) < 3 or not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError("At least three finite alignment anchors are required.")
    x, y = source - source.mean(axis=0), target - target.mean(axis=0)
    if np.linalg.matrix_rank(x, tol=1e-9) < 2 or np.linalg.matrix_rank(y, tol=1e-9) < 2:
        raise ValueError("Degenerate alignment anchors.")
    u, singular, vt = np.linalg.svd(y.T @ x / len(x))
    signs = np.ones(3)
    signs[-1] = 1 if np.linalg.det(u @ vt) > 0 else -1
    rotation = u @ np.diag(signs) @ vt
    scale = float((singular * signs).sum() / np.mean(np.sum(x*x, axis=1)))
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Non-positive alignment scale.")
    translation = target.mean(axis=0) - scale * rotation @ source.mean(axis=0)
    return scale, rotation, translation


def statistics(values):
    if not len(values):
        return None
    values = np.asarray(values, dtype=float)
    return {"count": len(values), "mean": float(values.mean()),
            "median": float(np.median(values)), "rmse": float(np.sqrt(np.mean(values**2))),
            "max": float(values.max())}


def evaluate_poses(estimated, reference, anchors, evaluation):
    if set(anchors) & set(evaluation):
        raise ValueError("Alignment anchors must not be evaluation frames.")
    if len(anchors) != len(set(anchors)) or len(evaluation) != len(set(evaluation)):
        raise ValueError("Duplicate pose evaluation frame.")
    missing_anchors = sorted(set(anchors) - (estimated.keys() & reference.keys()))
    missing_eval = sorted(set(evaluation) - (estimated.keys() & reference.keys()))
    result = {"comparable": False, "alignment_names": anchors, "evaluation_names": evaluation,
              "missing_anchors": missing_anchors, "missing_evaluation": missing_eval,
              "evaluation_denominator": len(evaluation),
              "note": "Published processed poses are a reference, not measured geometric truth."}
    if missing_anchors:
        return dict(result, reason="Missing fixed alignment anchors; no replacement.")
    try:
        target = np.array([reference[name]["center"] for name in anchors])
        scale, rotation, translation = fit_similarity(
            [estimated[name]["center"] for name in anchors], target)
    except ValueError as error:
        return dict(result, reason=str(error))
    normalization = float(np.sqrt(np.mean(np.sum((target - target.mean(axis=0))**2, axis=1))))
    rows = []
    for name in evaluation:
        if name in missing_eval:
            continue
        estimate, ref = estimated[name], reference[name]
        residual = scale * rotation @ estimate["center"] + translation - ref["center"]
        delta = ref["rotation"].T @ rotation @ estimate["rotation"]
        angle = math.degrees(math.acos(float(np.clip((np.trace(delta) - 1) / 2, -1, 1))))
        rows.append({"name": name, "center_error_reference_units": float(np.linalg.norm(residual)),
                     "rotation_error_degrees": angle})
    result.update(comparable=bool(rows), reason=None if rows else "No registered evaluation frames.",
                  scale=scale, alignment_rotation=rotation.tolist(),
                  alignment_translation=translation.tolist(), rows=rows,
                  center_error_reference_units=statistics(
                      [row["center_error_reference_units"] for row in rows]),
                  center_error_normalized=statistics(
                      [row["center_error_reference_units"] / normalization for row in rows]),
                  rotation_error_degrees=statistics([row["rotation_error_degrees"] for row in rows]),
                  normalization_anchor_rms=normalization)
    return result
