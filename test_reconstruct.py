"""Small regression checks for filesystem safety and COLMAP process handling."""

import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

import reconstruct as app
import densify
import ply_compatible


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="design3-tests-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def make_images(self):
        images = self.root / "照片 带空格"
        images.mkdir()
        for name in ["0001.JPG", "0002.png", "0003.jpeg"]:
            (images / name).touch()
        return images

    def test_input_filter_ignores_hidden_files(self):
        images = self.make_images()
        (images / ".preview.jpg").touch()
        (images / "phone.heic").touch()
        (images / ".cache").mkdir()
        (images / ".cache" / "test.jpg").touch()
        self.assertEqual(app.collect_images(images), ["0001.JPG", "0002.png", "0003.jpeg"])

    def test_existing_output_and_nested_output_are_rejected(self):
        images = self.make_images()
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("do not overwrite")
        for output in [existing, images / "result"]:
            args = app.get_parser().parse_args(["--images", str(images), "--output", str(output)])
            with self.assertRaises(ValueError):
                app.reconstruct(args, sys.executable, {})
        self.assertEqual(sentinel.read_text(), "do not overwrite")
        self.assertFalse((images / "result").exists())

    def test_new_and_legacy_option_names(self):
        for prefix in ["FeatureExtraction", "SiftExtraction"]:
            selected = app.select_option("--{}.use_gpu arg (=1)".format(prefix),
                                         "FeatureExtraction.use_gpu", "SiftExtraction.use_gpu")
            self.assertEqual(selected, "--" + prefix + ".use_gpu")
        with self.assertRaises(RuntimeError):
            app.select_option("--FeatureExtraction.use_gpu_extra", "FeatureExtraction.use_gpu")

    def test_select_largest_valid_component_not_first_directory(self):
        for name, images, points in [("0", 3, 100), ("1", 6, 70), ("2", 8, 0), ("3", 6, 90)]:
            folder = self.root / name
            folder.mkdir()
            for filename, count in [("images.bin", images), ("points3D.bin", points), ("cameras.bin", 1)]:
                (folder / filename).write_bytes(struct.pack("<Q", count))
        best, models = app.select_model(self.root)
        self.assertEqual(Path(best["path"]).name, "3")
        self.assertEqual(len(models), 4)

    def test_empty_model_is_failure(self):
        with self.assertRaises(RuntimeError):
            app.select_model(self.root)

    def test_process_failure_records_exit_code_and_log(self):
        (self.root / "logs").mkdir()
        pipeline = app.Pipeline(self.root, sys.executable, {})
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "退出码 7"):
            pipeline.run("failure", ["-c", "import sys; print('test failure'); sys.exit(7)"])
        state = json.loads((self.root / "status.json").read_text())
        self.assertEqual(state["stages"][0]["status"], "failed")
        self.assertEqual(state["stages"][0]["returncode"], 7)
        self.assertIn("test failure", (self.root / "logs" / "failure.log").read_text())

    def test_process_arguments_with_spaces_are_not_shell_commands(self):
        (self.root / "logs").mkdir()
        value = "照片 带空格 $(touch SHOULD_NOT_EXIST)"
        pipeline = app.Pipeline(self.root, sys.executable, {})
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run("arguments", ["-c", "import sys; print(repr(sys.argv[1]))", value])
        log = (self.root / "logs" / "arguments.log").read_text()
        self.assertIn(repr(value), log)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_statistics_use_point_errors_and_track_lengths(self):
        points = self.root / "points3D.txt"
        points.write_text("# header\n1 0 0 0 255 0 0 0.5 1 0 2 3\n2 1 0 0 0 255 0 1.5 1 4 2 5 3 6\n")
        stats = app.point_statistics(points)
        self.assertEqual(stats["points3D"], 2)
        self.assertEqual(stats["mean_point_reprojection_error_px"], 1.0)
        self.assertEqual(stats["mean_track_length"], 2.5)

    def test_ply_counts_read_binary_without_loading_points(self):
        path = self.root / "binary.ply"
        path.write_bytes(b"ply\nformat binary_little_endian 1.0\nelement vertex 1000000\n"
                         b"property float x\nelement face 7\nend_header\n" + bytes(12))
        self.assertEqual(densify.ply_counts(path), {"vertex": 1000000, "face": 7})

    def test_empty_or_truncated_ply_is_rejected(self):
        path = self.root / "broken.ply"
        for content in [b"ply\nelement vertex 0\nend_header\n", b"ply\nelement vertex 5\n",
                        b"ply\nelement vertex 5\nend_header\n", b"not a ply"]:
            path.write_bytes(content)
            with self.assertRaises(RuntimeError):
                densify.ply_counts(path)

    def test_external_tool_override_is_used(self):
        (self.root / "logs").mkdir()
        pipeline = app.Pipeline(self.root, "missing-colmap-test-only", {})
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run("external", ["-c", "print('external tool works')"], executable=sys.executable)
        state = json.loads((self.root / "status.json").read_text())
        self.assertEqual(state["stages"][0]["command"][0], sys.executable)
        self.assertEqual(state["stages"][0]["status"], "completed")

    def test_missing_texture_is_not_reported_as_success(self):
        material = self.root / "textured.mtl"
        material.write_text("newmtl material_0\nmap_Kd texture.jpg\n")
        with self.assertRaises(RuntimeError):
            densify.material_textures(material)
        (self.root / "texture.jpg").write_bytes(b"test image contents")
        self.assertEqual(densify.material_textures(material), [str(self.root / "texture.jpg")])

    def test_colmap_ply_preserves_xyz_rgb_and_skips_visibility_lists(self):
        source = self.root / "openmvs.ply"
        header = ("ply\nformat binary_little_endian 1.0\nelement vertex 2\n"
                  "property float32 x\nproperty float32 y\nproperty float32 z\n"
                  "property uint8 red\nproperty uint8 green\nproperty uint8 blue\n"
                  "property list uint8 uint32 view_indices\nend_header\n").encode()
        values = [(1.5, 2.0, -3.0, 12, 34, 255), (-1.0, 8.0, 0.5, 255, 0, 60)]
        source.write_bytes(header + struct.pack('<fffBBBBII', *values[0], 2, 5, 7)
                           + struct.pack('<fffBBBBI', *values[1], 1, 9))
        destination = self.root / 'compatible.ply'
        self.assertEqual(ply_compatible.convert(source, destination), 2)
        data = destination.read_bytes()
        exported_header, payload = data.split(b'end_header\n', 1)
        self.assertIn(b'property uchar red', exported_header)
        self.assertNotIn(b'property list', exported_header)
        self.assertEqual(list(struct.iter_unpack('<fffBBB', payload)), values)
        self.assertEqual(source.read_bytes()[:len(header)], header)

    def test_truncated_conversion_removes_partial_output(self):
        source = self.root / 'broken.ply'
        source.write_bytes(b'ply\nformat binary_little_endian 1.0\nelement vertex 1\n'
                           b'property float x\nproperty float y\nproperty float z\n'
                           b'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
        destination = self.root / 'compatible.ply'
        with self.assertRaises(ValueError):
            ply_compatible.convert(source, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_suffix('.ply.tmp').exists())


if __name__ == "__main__":
    unittest.main()
