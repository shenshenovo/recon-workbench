import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from scripts.quality_acceptance_data import evenly_spaced, excluded_by_quality, make_variant
from scripts.quality_degradation_benchmark import (
    response_groups, run_experiment, write_review_template)
from scripts.quality_pose_reference import (
    evaluate_poses, fit_similarity, read_colmap_poses, read_reference_poses)
from scripts.quality_sfm_benchmark import run_order
from scripts.quality_sfm_inputs import (
    GROUPS, filter_inputs, pose_plan, prepare_experiment, verify_inputs)
from scripts.quality_sfm_results import aggregate, collect_result


class AcceptanceDataTests(unittest.TestCase):
    def test_uniform_sample_is_unique_in_order_and_includes_endpoints(self):
        items = list(range(617))
        selected = evenly_spaced(items, 60)
        self.assertEqual(len(set(selected)), 60)
        self.assertEqual(selected[0], 0)
        self.assertEqual(selected[-1], 616)
        self.assertEqual(selected, sorted(selected))
        with self.assertRaises(ValueError):
            evenly_spaced(items, 618)

    def test_variant_does_not_modify_original_and_keeps_dimensions(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source.jpg", Path(folder) / "variant.png"
            Image.new("RGB", (96, 64), (150, 120, 80)).save(source)
            original = source.read_bytes()
            row = make_variant(source, target, "brightness", 0.18)
            self.assertEqual(source.read_bytes(), original)
            with Image.open(target) as image:
                self.assertEqual(image.size, (96, 64))
                self.assertEqual(image.format, "PNG")
            self.assertEqual(row["source_name"], source.name)

    def test_filter_uses_flags_not_variant_identity_or_similarity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "source"
            images.mkdir()
            for name in ("original.png", "zz_variant.png"):
                Image.new("RGB", (8, 8), "gray").save(images / name)
            report = {"images": [
                {"name": "original.png", "decodable": True, "flags": ["blurry"]},
                {"name": "zz_variant.png", "decodable": True, "flags": ["near_duplicate"]}]}
            selected, excluded = filter_inputs(images, report, root / "filtered")
            self.assertEqual([row["name"] for row in selected], ["zz_variant.png"])
            self.assertEqual(excluded, [{"name": "original.png", "reasons": ["blurry"]}])
            self.assertEqual(excluded_by_quality({"decodable": False, "flags": []}), ["decode_error"])
            with self.assertRaises(FileExistsError):
                filter_inputs(images, report, root / "filtered")

    def test_unreviewed_template_has_no_invented_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "labels.csv"
            write_review_template(path, [{"name": "a.jpg", "source_name": "a.jpg",
                                         "split": "validation"}])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["blur_label"], "")
            self.assertEqual(row["review_status"], "unreviewed")

    def test_responses_are_split_and_level_specific_not_accuracy(self):
        rows = [{"name": "a", "split": "validation", "kind": "blur", "level": 6}]
        groups = response_groups(rows, [{"name": "a", "flags": ["blurry"]}])
        self.assertEqual(groups[0]["alert_rates"]["blurry"], 1)
        self.assertNotIn("precision", groups[0])

    def test_full_detection_preparation_preserves_source_split_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "scene" / "images"
            images.mkdir(parents=True)
            frames, originals = [], {}
            for index in range(24):
                name = f"frame_{index:03}.jpg"
                Image.new("RGB", (32, 24), (80 + index, 120, 160)).save(images / name)
                frames.append({"file_path": "./images/" + name})
                originals[name] = (images / name).read_bytes()
            (images.parent / "transforms.json").write_text(json.dumps({"frames": frames}))
            output = root / "result"
            response = run_experiment(images.parent, output)
            document = json.loads((output / "inputs.json").read_text())
            self.assertEqual(response["sample_count"], 240)
            self.assertIsNone(response["classification_accuracy"])
            for name, original in originals.items():
                related = [row for row in document["samples"] if row["source_name"] == name]
                self.assertEqual(len(related), 10)
                self.assertEqual(len({row["split"] for row in related}), 1)
                self.assertEqual((images / name).read_bytes(), original)
            with self.assertRaises(FileExistsError):
                run_experiment(images.parent, output)

    def test_sfm_preparation_freezes_actual_flags_and_detects_input_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "scene" / "images"
            images.mkdir(parents=True)
            frames = []
            for index in range(60):
                name = f"frame_{index:03}.jpg"
                Image.new("RGB", (32, 24), (128, 128, 128)).save(images / name)
                frames.append({"file_path": "./images/" + name})
            (images.parent / "transforms.json").write_text(json.dumps({"frames": frames}))
            output = root / "result"
            with patch("scripts.quality_sfm_inputs.provenance",
                       return_value={"files_sha256": {}}):
                manifest = prepare_experiment(images.parent, output)
            self.assertEqual(len(manifest["variants"]), 15)
            self.assertEqual(len({row["source_name"] for row in manifest["variants"]}), 15)
            self.assertEqual(len(manifest["groups"]["original"]["names"]), 60)
            self.assertEqual(len(manifest["groups"]["augmented"]["names"]), 75)
            self.assertEqual(len(manifest["groups"]["original_filtered"]["names"]), 0)
            self.assertFalse(manifest["pose_plan"]["available"])
            verify_inputs(output, manifest)
            target = Path(manifest["groups"]["original"]["images"]) / "frame_000.jpg"
            target.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                verify_inputs(output, manifest)


class PoseReferenceTests(unittest.TestCase):
    def setUp(self):
        self.centers = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0],
                                 [0, 0, 1], [2, 1, 1]], dtype=float)
        self.rotation = np.array([[0., -1, 0], [1, 0, 0], [0, 0, 1]])
        self.shift = np.array([3., -2, 7])

    def test_similarity_recovers_positive_scale_and_rotation(self):
        target = 2.5 * (self.centers @ self.rotation.T) + self.shift
        scale, rotation, shift = fit_similarity(self.centers, target)
        self.assertAlmostEqual(scale, 2.5)
        np.testing.assert_allclose(rotation, self.rotation, atol=1e-12)
        np.testing.assert_allclose(shift, self.shift, atol=1e-12)

    def test_similarity_never_fits_reflection_as_a_rotation(self):
        reflected = self.centers * [-1, 1, 1]
        scale, rotation, shift = fit_similarity(self.centers, reflected)
        self.assertAlmostEqual(np.linalg.det(rotation), 1)
        self.assertFalse(np.allclose(scale * self.centers @ rotation.T + shift, reflected))
        with self.assertRaisesRegex(ValueError, "Degenerate"):
            fit_similarity([[0, 0, 0], [1, 0, 0], [2, 0, 0]], self.centers[:3])

    def test_colmap_world_to_camera_and_blank_observation_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "images.txt"
            path.write_text("# images\n1 1 0 0 0 -1 -2 -3 1 a frame.jpg\n\n")
            poses = read_colmap_poses(path)
            np.testing.assert_allclose(poses["a frame.jpg"]["center"], [1, 2, 3])

    def test_reference_opengl_orientation_conversion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "transforms.json"
            path.write_text(json.dumps({"frames": [{"file_path": "./images/a.jpg",
                                                   "transform_matrix": np.eye(4).tolist()}]}))
            pose = read_reference_poses(path)["a.jpg"]
            np.testing.assert_array_equal(pose["rotation"], np.diag([1, -1, -1]))

    def test_fixed_anchors_separate_evaluation_and_missing_does_not_refit(self):
        estimate, reference = {}, {}
        for index, center in enumerate(self.centers):
            name = str(index)
            estimate[name] = {"center": center, "rotation": np.eye(3)}
            reference[name] = {"center": 2.5 * self.rotation @ center + self.shift,
                               "rotation": self.rotation}
        result = evaluate_poses(estimate, reference, ["0", "1", "2", "3"], ["4"])
        self.assertTrue(result["comparable"])
        self.assertLess(result["center_error_normalized"]["rmse"], 1e-12)
        self.assertLess(result["rotation_error_degrees"]["rmse"], 1e-5)
        del estimate["0"]
        result = evaluate_poses(estimate, reference, ["0", "1", "2", "3"], ["4"])
        self.assertFalse(result["comparable"])
        self.assertEqual(result["missing_anchors"], ["0"])
        with self.assertRaisesRegex(ValueError, "must not"):
            evaluate_poses(estimate, reference, ["1", "2", "3"], ["3"])

    def test_pose_plan_insufficient_views_and_held_out_denominator(self):
        self.assertFalse(pose_plan(list("abcd"), list("abc"))["available"])
        plan = pose_plan([str(i) for i in range(60)], [str(i) for i in range(40)])
        self.assertEqual(len(plan["anchors"]), 8)
        self.assertEqual(len(plan["evaluation"]), 32)
        self.assertFalse(set(plan["anchors"]) & set(plan["evaluation"]))


class SfMResultsTests(unittest.TestCase):
    def test_rotated_order_has_three_independent_attempts_per_group(self):
        order = run_order()
        self.assertEqual(len(set(order)), 12)
        self.assertEqual(order[0], (1, "original"))
        self.assertEqual(order[4], (2, "original_filtered"))
        self.assertEqual(order[8], (3, "augmented"))

    def test_original_denominator_retains_excluded_and_missing_views(self):
        manifest = {"originals": [{"name": name} for name in ("a", "b", "c")],
                    "groups": {"original_filtered": {"names": ["a", "b"]}},
                    "pose_plan": {"available": False}}
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "model_txt").mkdir()
            (output / "model_txt" / "images.txt").write_text("1 1 0 0 0 0 0 0 1 a\n\n")
            result = collect_result(output, manifest, "original_filtered",
                                    {"returncode": 0, "summary": {}, "elapsed_seconds": 1})
        self.assertEqual(result["original_denominator"], 3)
        self.assertEqual(result["original_coverage"], 1 / 3)
        self.assertEqual(result["excluded_original_names"], ["c"])
        self.assertEqual(result["unregistered_retained_original_names"], ["b"])

    def test_failed_run_retained_without_fake_zero_precision_metrics(self):
        manifest = {"original_denominator": 60, "groups": {
            name: {"names": ["a"], "excluded": []} for name in GROUPS}}
        runs = [{"group": "original", "returncode": 1, "elapsed_seconds": 4,
                 "summary": {}, "original_coverage": 0, "pose_reference_consistency": {}}]
        report = aggregate(runs, manifest)
        group = report["groups"]["original"]
        self.assertEqual(group["attempts"], 1)
        self.assertEqual(group["successful"], 0)
        self.assertIsNone(group["metrics_successful_only"]["mean_point_reprojection_error_px"])
        self.assertEqual(group["metrics_successful_only"]["elapsed_seconds_all_attempts"]["median"], 4)
        self.assertFalse(report["all_runs_successful"])


if __name__ == "__main__":
    unittest.main()
