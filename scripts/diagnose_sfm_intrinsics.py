#!/usr/bin/env python3
"""Exploratory mapper-only diagnostic; never a replacement for a frozen SfM run."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import time

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recon_workbench.reconstruct import Pipeline, point_statistics, select_model, timestamp
from scripts.quality_acceptance_data import write_json
from scripts.quality_sfm_smoke import sha256


def apply_initial_intrinsics(database, reference):
    """Use only fx/fy/cx/cy; preserve the original zero distortion initialization."""
    frames = {Path(frame["file_path"]).name: frame for frame in reference["frames"]}
    connection = sqlite3.connect(database)
    rows = []
    try:
        cameras = connection.execute(
            "SELECT images.name,cameras.camera_id,cameras.model,cameras.width,cameras.height,"
            "cameras.params FROM images JOIN cameras USING(camera_id)").fetchall()
        if len({row[1] for row in cameras}) != len(cameras):
            raise ValueError("Diagnostic requires independent cameras.")
        for name, camera_id, model, width, height, blob in cameras:
            frame = frames[name]
            params = np.frombuffer(blob, dtype="<f8").copy()
            if model != 4 or len(params) != 8 or (width, height) != (frame["w"], frame["h"]):
                raise ValueError("Camera model or dimensions mismatch.")
            before = params.tolist()
            params[:4] = [frame[key] for key in ("fl_x", "fl_y", "cx", "cy")]
            if not np.isfinite(params).all() or min(params[:2]) <= 0:
                raise ValueError("Invalid initial intrinsics.")
            connection.execute("UPDATE cameras SET params=?,prior_focal_length=1 WHERE camera_id=?",
                               (params.astype("<f8").tobytes(), camera_id))
            rows.append({"name": name, "before": before, "after": params.tolist()})
        connection.commit()
    finally:
        connection.close()
    return rows


def run_diagnostic(experiment, source_run, output):
    experiment, output = Path(experiment).resolve(), Path(output).resolve()
    source_run = experiment / "runs" / source_run
    source_database = source_run / "database.db"
    if output.exists() or output.is_relative_to(experiment):
        raise ValueError("Use a new diagnostic output outside the frozen experiment.")
    manifest = json.loads((experiment / "inputs.json").read_text())
    source_hash = sha256(source_database)
    output.mkdir(parents=True)
    for name in ("logs", "sparse", "model_txt"):
        (output / name).mkdir()
    database = output / "database.db"
    with closing(sqlite3.connect(source_database.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(database)) as destination:
            source.backup(destination)
    reference = json.loads((experiment / "reference-transforms.json").read_text())
    rows = apply_initial_intrinsics(database, reference)
    write_json(output / "diagnostic-inputs.json", {
        "kind": "exploratory_mapper_only", "source_database_sha256": source_hash,
        "intrinsics": rows, "ground_truth_poses_imported": False,
        "features_and_verified_matches_reused": True,
        "warning": "Old verified geometries are reused; this only diagnoses initialization sensitivity.",
        "code_sha256": sha256(Path(__file__))})
    group = json.loads(source_run.with_suffix(".json").read_text())["group"]
    images = manifest["groups"][group]["images"]
    pipeline = Pipeline(output, shutil.which("colmap"), {"diagnostic": True})
    started = time.monotonic()
    try:
        pipeline.run("mapper", ["mapper", "--database_path", database, "--image_path", images,
                               "--output_path", output / "sparse", "--Mapper.num_threads", 4])
        best, models = select_model(output / "sparse")
        pipeline.run("export", ["model_converter", "--input_path", best["path"],
                               "--output_path", output / "model_txt", "--output_type", "TXT"])
        result = {"success": True, "registered_images": best["registered_images"], "models": models,
                  **point_statistics(output / "model_txt" / "points3D.txt")}
    except KeyboardInterrupt:
        result = {"success": False, "cancelled": True, "error": "Diagnostic cancelled."}
    except (RuntimeError, OSError) as error:
        result = {"success": False, "error": str(error)}
    result.update(mapper_only_elapsed_seconds=time.monotonic() - started,
                  source_database_unchanged=sha256(source_database) == source_hash,
                  not_part_of_frozen_benchmark=True)
    terminal = "completed" if result["success"] else "cancelled" if result.get("cancelled") else "failed"
    pipeline.state.update(status=terminal, finished_at=timestamp(), summary=result)
    pipeline.save()
    write_json(output / "diagnostic-result.json", result)
    print(json.dumps(result), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--source-run", default="r1_original")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run_diagnostic(args.experiment, args.source_run, args.output)
    return 130 if result.get("cancelled") else 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
