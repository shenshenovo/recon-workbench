import struct
import tempfile
import unittest
from pathlib import Path

from recon_workbench.prepare_brush import safe_image_path, validate_dataset


class BrushPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.model = self.root / "model"
        self.model.mkdir()
        (self.model / "cameras.txt").write_text("1 PINHOLE 10 8 9 9 5 4\n")
        (self.model / "images.txt").write_text(
            "# Image records\n1 1 0 0 0 0 0 0 1 sub/a space.png\n\n"
            "2 1 0 0 0 1 0 0 1 b.png\n\n")
        (self.model / "points3D.txt").write_text("1 0 0 3 255 0 0 0.1 1 0 2 0\n")
        for name in ("sub/a space.png", "b.png"):
            path = self.images / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 10, 8))

    def test_nested_names_spaces_and_empty_observations(self):
        result = validate_dataset(self.images, self.model, 10)
        self.assertEqual(result["registered_images"], 2)
        self.assertEqual(result["points3D"], 1)
        self.assertIn("sub/a space.png", result["image_names"])

    def test_dimension_mismatch_rejected(self):
        (self.model / "cameras.txt").write_text("1 PINHOLE 9 8 9 9 5 4\n")
        with self.assertRaisesRegex(ValueError, "尺寸与相机"):
            validate_dataset(self.images, self.model, 10)

    def test_camera_and_point_references_rejected(self):
        path = self.model / "images.txt"
        original = path.read_text()
        path.write_text(original.replace("1 b.png", "2 b.png"))
        with self.assertRaisesRegex(ValueError, "不存在的相机"):
            validate_dataset(self.images, self.model, 10)
        path.write_text(original)
        (self.model / "points3D.txt").write_text("1 0 0 3 255 0 0 0.1 1 0 99 0\n")
        with self.assertRaisesRegex(ValueError, "不存在的图片"):
            validate_dataset(self.images, self.model, 10)

    def test_paths_cannot_escape_image_root(self):
        for name in ("../outside.png", "/tmp/outside.png", "sub/../../outside.png"):
            with self.assertRaises(ValueError):
                safe_image_path(self.images, name)
        (self.images / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            safe_image_path(self.images, "link/outside.png")


if __name__ == "__main__":
    unittest.main()
