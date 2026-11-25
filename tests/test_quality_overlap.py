from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from scripts.diagnose_sfm_intrinsics import apply_initial_intrinsics, run_diagnostic
from scripts.prepare_quality_overlap import prepare_overlap
from scripts.quality_sfm_inputs import verify_inputs
from scripts.run_quality_overlap import first_round_failed, run_attempt


def camera_database(path, params=None):
    if params is None:
        params = np.array([1200, 1200, 500, 350, 0, 0, 0, 0], dtype="<f8")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE cameras (camera_id INTEGER, model INTEGER,"
                           " width INTEGER, height INTEGER, params BLOB, prior_focal_length INTEGER)")
        connection.execute("CREATE TABLE images (name TEXT, camera_id INTEGER)")
        connection.execute("INSERT INTO cameras VALUES (1,4,1000,700,?,0)", (params.tobytes(),))
        connection.execute("INSERT INTO images VALUES ('a.jpg',1)")


class IntrinsicsDiagnosticTests(unittest.TestCase):
    def test_only_initial_intrinsics_are_changed_without_accessing_pose(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "database.db"
            params = np.array([1200, 1200, 500, 350, .01, -.02, .003, -.004], dtype="<f8")
            camera_database(database, params)
            reference = {"frames": [{"file_path": "./images/a.jpg", "fl_x": 770, "fl_y": 771,
                                     "cx": 499, "cy": 351, "w": 1000, "h": 700}]}
            rows = apply_initial_intrinsics(database, reference)
            np.testing.assert_array_equal(rows[0]["after"][:4], [770, 771, 499, 351])
            np.testing.assert_array_equal(rows[0]["after"][4:], params[4:])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT prior_focal_length FROM cameras").fetchone()[0], 1)

    def test_initial_intrinsics_reject_wrong_camera_dimensions(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "database.db"
            camera_database(database)
            with self.assertRaisesRegex(ValueError, "dimensions mismatch"):
                apply_initial_intrinsics(database, {"frames": [
                    {"file_path": "a.jpg", "w": 10, "h": 7}]})

    def test_diagnostic_failure_is_terminal_and_preserves_source_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            experiment = root / "experiment"
            source_run = experiment / "runs" / "r1_original"
            source_run.mkdir(parents=True)
            camera_database(source_run / "database.db")
            original = (source_run / "database.db").read_bytes()
            (experiment / "inputs.json").write_text(json.dumps({
                "groups": {"original": {"images": str(root / "images")}}}))
            (experiment / "reference-transforms.json").write_text(json.dumps({"frames": [
                {"file_path": "a.jpg", "fl_x": 770, "fl_y": 771,
                 "cx": 499, "cy": 351, "w": 1000, "h": 700}]}))
            source_run.with_suffix(".json").write_text('{"group":"original"}')
            output = root / "diagnostic"
            with patch("scripts.diagnose_sfm_intrinsics.Pipeline.run",
                       side_effect=RuntimeError("No baseline model")):
                result = run_diagnostic(experiment, "r1_original", output)
            self.assertFalse(result["success"])
            self.assertEqual(json.loads((output / "status.json").read_text())["status"], "failed")
            self.assertEqual((source_run / "database.db").read_bytes(), original)


class OverlapCohortTests(unittest.TestCase):
    def test_first_round_stop_never_counts_unattempted_repetitions_as_success(self):
        self.assertFalse(first_round_failed([{"returncode": 0}] * 4))
        self.assertFalse(first_round_failed([{"returncode": 1}] * 3))
        self.assertTrue(first_round_failed([{"returncode": 0}] * 3 + [{"returncode": 1}]))

    def test_prefix_is_frozen_without_searching_alternative_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "source" / "images"
            images.mkdir(parents=True)
            frames = []
            for index in range(61):
                name = f"frame_{index:03}.jpg"
                Image.new("RGB", (16, 12), (128, 128, 128)).save(images / name)
                frames.append({"file_path": "./images/" + name})
            (images.parent / "transforms.json").write_text(json.dumps({"frames": frames}))
            with patch("scripts.quality_sfm_inputs.provenance",
                       return_value={"files_sha256": {}}):
                manifest = prepare_overlap(images.parent, root / "subset", root / "experiment")
            self.assertEqual(manifest["sampling"]["source_frame_count"], 61)
            self.assertEqual([row["name"] for row in manifest["originals"]],
                             [f"frame_{index:03}.jpg" for index in range(60)])
            self.assertFalse((root / "subset" / "images" / "frame_060.jpg").exists())
            self.assertEqual(manifest["protocol_id"], "continuous_prefix_followup_v1")
            self.assertTrue(manifest["first_round_failure_stop"])
            verify_inputs(root / "experiment", manifest)

    def test_cancellation_is_journalled_and_wrong_manifest_cannot_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "runs").mkdir()
            manifest = {"originals": [{"name": name} for name in ("a", "b", "c")],
                        "groups": {"original": {"names": ["a", "b", "c"], "images": folder}},
                        "configuration": {}, "run_timeout_seconds": 3600,
                        "pose_plan": {"available": False}}
            with patch("scripts.run_quality_overlap.execute", side_effect=KeyboardInterrupt):
                result = run_attempt(output, manifest, 1, "original", "frozen")
            self.assertEqual(result["returncode"], 130)
            self.assertTrue(result["cancelled"])
            self.assertGreaterEqual(result["elapsed_seconds"], 0)
            self.assertTrue((output / "runs" / "r1_original.json").is_file())
            with self.assertRaisesRegex(ValueError, "does not match"):
                run_attempt(output, manifest, 1, "original", "different")


if __name__ == "__main__":
    unittest.main()
