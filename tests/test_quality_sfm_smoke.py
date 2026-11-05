import json
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from scripts.quality_sfm_smoke import (
    analyze_directory,
    choose_frames,
    copy_selected,
    make_selection,
    main,
    read_source,
    registered_image_names,
)
from recon_workbench import image_quality as quality


class QualitySfMSmokeTests(unittest.TestCase):
    def test_choose_frames_follows_transforms_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            images = root / "images"
            images.mkdir()
            for name in ("frame_00001.jpg", "frame_00002.jpg", "frame_00003.jpg"):
                Image.new("RGB", (32, 24), (80, 100, 120)).save(images / name)
            (root / "transforms.json").write_text(json.dumps({
                "frames": [{"file_path": "./images/frame_00003.jpg"},
                           {"file_path": "./images/frame_00001.jpg"},
                           {"file_path": "./images/frame_00002.jpg"}]}))
            image_root, transforms, selected = choose_frames(root, 3)
            self.assertEqual(image_root, images.resolve())
            self.assertTrue(transforms.is_file())
            self.assertEqual([path.name for path in selected],
                             ["frame_00003.jpg", "frame_00001.jpg", "frame_00002.jpg"])

    def test_selection_keeps_all_normal_views_even_when_a_normal_view_is_flagged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            injected = root / "injected"
            injected.mkdir()
            for index, name in enumerate(("frame_00001.jpg", "frame_00002.jpg", "zz_blur.jpg")):
                Image.new("RGB", (64, 48), (80 + index * 20,) * 3).save(injected / name)
            report = quality.analyze(injected, [
                {"name": path.name, "path": path.name} for path in sorted(injected.iterdir())
            ])
            selection_root = root / "filtered"
            document = make_selection(
                injected, report, {"frame_00001.jpg", "frame_00002.jpg"},
                {"zz_blur.jpg"}, selection_root)
            self.assertEqual([item["name"] for item in document["selected"]],
                             ["frame_00001.jpg", "frame_00002.jpg"])
            self.assertTrue((selection_root / "frame_00001.jpg").is_file())
            self.assertFalse((selection_root / "zz_blur.jpg").exists())

    def test_source_directory_falls_back_to_sorted_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "b.png").write_bytes(b"not-an-image")
            (root / "a.png").write_bytes(b"not-an-image")
            (root / "c.png").write_bytes(b"not-an-image")
            image_root, transforms, paths = read_source(root)
            self.assertEqual(image_root, root.resolve())
            self.assertIsNone(transforms)
            self.assertEqual([path.name for path in paths], ["a.png", "b.png", "c.png"])

    def test_quality_report_has_stable_path_next_to_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "clean" / "images"
            root.mkdir(parents=True)
            for index in range(3):
                Image.new("RGB", (64, 48), (80 + index * 20,) * 3).save(root / f"{index}.jpg")
            report = analyze_directory(root)
            report_path = root.parent / "quality.json"
            self.assertTrue(report_path.is_file())
            self.assertEqual(json.loads(report_path.read_text())["fingerprint"], report["fingerprint"])

    def test_registered_image_names_preserve_spaces_and_blank_observations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "images.txt"
            path.write_text(
                "# Image list\n"
                "1 1 0 0 0 0 0 0 1 frame one.jpg\n"
                "\n"
                "2 1 0 0 0 0 0 0 1 frame two.jpg\n"
                "0 0 -1\n",
                encoding="utf-8",
            )
            self.assertEqual(registered_image_names(path), ["frame one.jpg", "frame two.jpg"])

    def test_copy_rejects_duplicate_basenames(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "frame.jpg").write_bytes(b"first")
            (second / "frame.jpg").write_bytes(b"second")
            destination = root / "copy"
            with self.assertRaisesRegex(ValueError, "重名"):
                copy_selected([first / "frame.jpg", second / "frame.jpg"], destination)
            self.assertFalse(destination.exists())

    def test_sfm_numeric_options_must_be_positive(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "must-not-be-created"
            for option in ("--max-image-size", "--max-features", "--threads", "--overlap"):
                stderr = io.StringIO()
                with self.subTest(option=option), redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as error:
                        main(["--output", str(output), option, "0"])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("必须是正整数", stderr.getvalue())
                self.assertFalse(output.exists())

    def test_main_returns_nonzero_when_any_sfm_run_fails(self):
        report = {"success": False, "sfm": {"runs": [{
            "name": "injected", "returncode": 1, "summary": {},
            "registered_original_images": 0, "elapsed_seconds": 0.1,
        }]}}
        with tempfile.TemporaryDirectory() as folder, \
                patch("scripts.quality_sfm_smoke.run_experiment", return_value=report):
            stderr = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = main(["--output", str(Path(folder) / "result")])
        self.assertEqual(code, 1)
        self.assertIn("未全部成功", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
