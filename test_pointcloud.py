import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import app
from pointcloud import cloud_packet


class ViewerTests(unittest.TestCase):
    def test_binary_cloud_sampling_and_rgb(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cloud.ply'
            header = b'ply\nformat binary_little_endian 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n'
            path.write_bytes(header + b''.join(struct.pack('<fffBBB', i, 0, 0, 255, 0, 0) for i in range(3)))
            packet = cloud_packet(path, limit=2)
            length = struct.unpack('<I', packet[:4])[0]
            info = json.loads(packet[4:4+length])
            self.assertEqual(info['original_points'], 3)
            self.assertEqual(info['display_points'], 2)
            self.assertEqual(len(packet)-4-length, 2*24)
            self.assertEqual(struct.unpack('<ffffff', packet[4+length:4+length+24]), (0, 0, 0, 1, 0, 0))
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, '截断'):
                cloud_packet(path)

    def test_ascii_cloud_and_mesh_faces(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mesh.ply'
            path.write_text('ply\nformat ascii 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n1 2 3\n3 0 0 0\n')
            packet = cloud_packet(path)
            length = struct.unpack('<I',packet[:4])[0]
            self.assertEqual(json.loads(packet[4:4+length])['display_points'],1)

    def test_upload_validates_paths_and_content(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app,'ROOT',Path(directory)):
            uploads = app.Uploads()
            batch = uploads.create()
            png = b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',10,8)
            result = uploads.receive(batch['id'], 'a.png', io.BytesIO(png), len(png))
            self.assertEqual(result['files'],1)
            for name in ('../outside.png', 'a.png'):
                with self.assertRaises(ValueError):
                    uploads.receive(batch['id'], name, io.BytesIO(png), len(png))
            with self.assertRaises(ValueError):
                uploads.receive(batch['id'], 'bad.png', io.BytesIO(b'bad data'), 8)
            self.assertFalse((Path(directory)/batch['path']/'bad.png').exists())


if __name__ == '__main__':
    unittest.main()
