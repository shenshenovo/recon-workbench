import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageFilter

from recon_workbench import server as app
from recon_workbench import image_quality as quality
from recon_workbench.workspace import Workspace


def texture(size=(320,240)):
    rng = random.Random(42)
    return Image.frombytes('L', size, bytes(rng.randrange(40,216) for _ in range(size[0]*size[1]))).convert('RGB')


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.photos = self.root / 'photos'
        self.photos.mkdir()
        self.store = Workspace(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self):
        image_set = self.store.register_set('default', self.photos)
        return image_set, self.store.analyze_images(image_set['id'])

    def two_photos(self):
        image = texture()
        image.save(self.photos / 'a.png')
        image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).save(self.photos / 'b.png')

    def test_blur_exposure_exact_duplicate_and_decode_error(self):
        image = texture()
        image.save(self.photos / 'a-sharp.png')
        image.filter(ImageFilter.GaussianBlur(8)).save(self.photos / 'blur.png')
        Image.new('RGB',(320,240),'black').save(self.photos / 'dark.png')
        Image.new('RGB',(320,240),'white').save(self.photos / 'bright.png')
        shutil.copyfile(self.photos / 'a-sharp.png', self.photos / 'z-copy.png')
        (self.photos / 'bad.png').write_bytes(b'\x89PNG\r\n\x1a\ntruncated')
        _, state = self.analyze()
        rows = {item['name']:item for item in state['report']['images']}
        self.assertNotIn('blurry', rows['a-sharp.png']['flags'])
        self.assertIn('blurry', rows['blur.png']['flags'])
        self.assertIn('underexposed', rows['dark.png']['flags'])
        self.assertIn('overexposed', rows['bright.png']['flags'])
        self.assertEqual(rows['z-copy.png']['duplicate_of'], 'a-sharp.png')
        self.assertFalse(rows['bad.png']['decodable'])
        self.assertEqual(state['report']['counts']['exact_duplicate'], 1)

    def test_similar_compressed_photo_is_advisory_and_can_be_kept(self):
        # A smooth but textured scene survives JPEG compression with nearly identical dHash.
        image = Image.new('L',(320,240))
        image.putdata([int(40 + x/3 + 25*((y//24)%2)) for y in range(240) for x in range(320)])
        image.convert('RGB').save(self.photos / 'a.png')
        image.convert('RGB').save(self.photos / 'b.jpg', quality=85)
        image_set, state = self.analyze()
        self.assertIn('near_duplicate', state['report']['images'][1]['flags'])
        self.assertNotIn('exact_duplicate', state['report']['images'][1]['flags'])
        saved = self.store.save_selection(image_set['id'], ['a.png','b.jpg'], state['report']['fingerprint'])
        self.assertEqual(saved['selection']['names'], ['a.png','b.jpg'])

    def test_exif_orientation_and_invalid_thresholds(self):
        image = texture((120,80))
        exif = Image.Exif(); exif[274] = 6
        image.save(self.photos / 'rotated.jpg', exif=exif)
        _, state = self.analyze()
        row = state['report']['images'][0]
        self.assertEqual((row['width'],row['height']), (80,120))
        for values in ({'blur_threshold':float('nan')},{'dark_mean':220,'bright_mean':100},
                       {'near_distance':2.5},{'unknown':1},{'max_side':10000}):
            with self.assertRaises(ValueError): quality.policy(values)

    def test_selection_persists_and_rejects_unknown_invalid_and_stale_names(self):
        self.two_photos()
        (self.photos / 'bad.png').write_bytes(b'not an image')
        image_set, state = self.analyze()
        fingerprint = state['report']['fingerprint']
        self.assertEqual(self.store.image_set(image_set['id'])['selection_status'], 'pending')
        for names in ([], ['a.png'], ['a.png','a.png'], ['a.png','missing.png'], ['a.png','bad.png']):
            with self.assertRaises(ValueError): self.store.save_selection(image_set['id'], names, fingerprint)
        with self.assertRaises(ValueError): self.store.save_selection(image_set['id'], ['a.png','b.png'], 'stale')
        self.store.save_selection(image_set['id'], ['a.png','b.png'], fingerprint)
        self.assertEqual(Workspace(self.root).quality_state(image_set['id'])['selection']['names'], ['a.png','b.png'])
        summary = Workspace(self.root).image_set(image_set['id'])
        self.assertEqual((summary['count'], summary['selected_count'], summary['selection_status']), (3, 2, 'saved'))
        self.store.analyze_images(image_set['id'])
        self.assertIsNotNone(self.store.quality_state(image_set['id'])['selection'])
        self.store.analyze_images(image_set['id'], {'blur_threshold':100})
        self.assertIsNone(self.store.quality_state(image_set['id'])['selection'])
        self.assertIsNone(self.store.image_set(image_set['id'])['selected_count'])

    def test_new_upload_and_added_files_invalidate_review(self):
        self.two_photos()
        image_set, state = self.analyze()
        self.store.save_selection(image_set['id'], ['a.png','b.png'], state['report']['fingerprint'])
        Image.new('RGB',(20,20),'red').save(self.photos / 'new.png')
        state = self.store.analyze_images(image_set['id'])
        self.assertEqual(state['report']['counts']['total'], 3)
        self.assertIsNone(state['selection'])
        self.store.add_image(image_set['id'], {'name':'another.png','path':'photos/another.png','bytes':0})
        state = self.store.quality_state(image_set['id'])
        self.assertIsNone(state['report'])

    def test_existing_camera_datasets_cannot_be_filtered(self):
        image_set = self.store.register_set('default', self.photos, 'dataset')
        with self.assertRaisesRegex(ValueError,'仅用于原始照片'):
            self.store.analyze_images(image_set['id'])

    def test_snapshot_copies_exact_selection_and_rejects_changed_source(self):
        self.two_photos()
        (self.photos / 'nested').mkdir()
        shutil.copyfile(self.photos / 'a.png', self.photos / 'nested/copy.png')
        _, state = self.analyze()
        destination = self.root / 'snapshots/images'
        files = quality.snapshot(self.root, state['report'], ['b.png','nested/copy.png'], destination)
        self.assertEqual({str(path.relative_to(destination)) for path in destination.rglob('*.png')}, {'b.png','nested/copy.png'})
        self.assertEqual(len(files), 2)
        original = (self.photos / 'b.png').read_bytes()
        (self.photos / 'b.png').write_bytes(b'changed')
        self.assertEqual((destination / 'b.png').read_bytes(), original)
        failed = self.root / 'failed/images'
        with self.assertRaisesRegex(ValueError,'已变化'):
            quality.snapshot(self.root, state['report'], ['a.png','b.png'], failed)
        self.assertFalse(failed.exists())

    def test_job_uses_reviewed_snapshot_and_saves_manifest(self):
        self.two_photos()
        shutil.copyfile(self.photos / 'a.png', self.photos / 'excluded.png')
        image_set, state = self.analyze()
        payload = {'method':'sfm','input_type':'images','input':'photos','image_set_id':image_set['id']}
        with patch.object(app,'ROOT',self.root):
            jobs = app.Jobs(self.store)
            with self.assertRaisesRegex(ValueError,'先检测'):
                jobs.start(payload)
            self.store.save_selection(image_set['id'], ['a.png','b.png'], state['report']['fingerprint'])
            (self.root / 'main.py').write_text('''import json, pathlib, sys
source=pathlib.Path(sys.argv[sys.argv.index('--images')+1])
assert sorted(p.name for p in source.iterdir())==['a.png','b.png']
output=pathlib.Path(sys.argv[sys.argv.index('--output')+1]); output.mkdir(parents=True)
(output/'status.json').write_text(json.dumps({'status':'completed'}))
(output/'summary.json').write_text(json.dumps({'method':'sfm','input_images':2}))
''')
            result = jobs.start(payload)
            jobs.worker.join(timeout=5)
            task = self.store.task(result['task_id'])
            self.assertEqual(task['status'],'completed')
            self.assertEqual(task['parameters']['selected_images'],['a.png','b.png'])
            manifest = json.loads((self.root / task['parameters']['quality_manifest']).read_text())
            self.assertEqual([item['name'] for item in manifest['files']],['a.png','b.png'])
            self.assertEqual(manifest['files'][0]['sha256'], hashlib.sha256((self.photos/'a.png').read_bytes()).hexdigest())
            self.assertTrue((self.photos/'excluded.png').is_file())

    def test_changed_source_blocks_launch_and_records_failure(self):
        self.two_photos()
        image_set, state = self.analyze()
        self.store.save_selection(image_set['id'], ['a.png','b.png'], state['report']['fingerprint'])
        (self.photos/'a.png').write_bytes(b'changed')
        with patch.object(app,'ROOT',self.root), patch.object(app.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(ValueError,'已变化'):
                app.Jobs(self.store).start({'method':'sfm','image_set_id':image_set['id']})
            launch.assert_not_called()
        self.assertEqual(self.store.tasks()[0]['status'],'failed')


if __name__ == '__main__':
    unittest.main()
