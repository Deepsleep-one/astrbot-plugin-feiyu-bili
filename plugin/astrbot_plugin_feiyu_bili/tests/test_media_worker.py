"""Offline tests for frame cutting: tool discovery, size limits and honest reasons."""
import asyncio
import importlib.util
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
_TMP = HERE / '_tmp_media'
sys.path.insert(0, str(PLUGIN))


def load(name):
    spec = importlib.util.spec_from_file_location(f'feiyu_{name}', PLUGIN / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


media = load('media_worker')


def jpeg_bytes(width=854, height=480):
    """A header-only JPEG: SOI + APP0 + SOF0 + EOI, enough for marker parsing."""
    app0 = b'\xff\xe0' + (16).to_bytes(2, 'big') + b'JFIF\x00' + b'\x00' * 9
    sof = (b'\xff\xc0' + (11).to_bytes(2, 'big') + bytes([8])
           + height.to_bytes(2, 'big') + width.to_bytes(2, 'big')
           + bytes([1, 1, 0x11, 0]))
    return b'\xff\xd8' + app0 + sof + b'\xff\xd9'


class FindToolTests(unittest.TestCase):
    def test_unlikely_name_is_not_found(self):
        self.assertIsNone(media.find_tool('feiyu-ffmpeg-absent-xyz'))

    def test_configured_path_wins(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, _TMP, True)
        exe = _TMP / 'ffmpeg.exe'
        exe.write_bytes(b'stub')
        self.assertEqual(media.find_tool('ffmpeg', str(exe)), str(exe))
        self.assertEqual(media.find_tool('ffmpeg', str(_TMP)), str(exe))
        self.assertIsNone(media.find_tool('ffprobe', str(_TMP)))

    def test_worker_reports_status_without_a_decoder(self):
        worker = media.MediaWorker(_TMP)
        status = worker.status()
        self.assertEqual(status['decoder'], worker.available)
        self.assertEqual(sorted(status), ['decoder', 'ffmpeg', 'ffprobe'])


class JpegSizeTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_reads_dimensions_from_the_sof_marker(self):
        path = _TMP / 'ok.jpg'
        path.write_bytes(jpeg_bytes(854, 480))
        self.assertEqual(media.jpeg_size(path), (854, 480))
        path.write_bytes(jpeg_bytes(640, 360))
        self.assertEqual(media.jpeg_size(path), (640, 360))

    def test_garbage_and_missing_files_are_not_guessed(self):
        for payload in (b'', b'not a jpeg at all', b'\xff\xd8', b'\xff\xd8\xff\xc0'):
            path = _TMP / 'bad.jpg'
            path.write_bytes(payload)
            with self.subTest(payload=payload[:4]):
                self.assertEqual(media.jpeg_size(path), (0, 0))
        self.assertEqual(media.jpeg_size(_TMP / 'absent.jpg'), (0, 0))


class ExtractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)
        self.source = _TMP / 'stream.m4s'
        self.source.write_bytes(b'fake container')
        self.worker = media.MediaWorker(_TMP / 'media')
        self.worker.ffmpeg = 'ffmpeg'  # pretend a decoder exists; _one is replaced below

    def tearDown(self):
        shutil.rmtree(_TMP, ignore_errors=True)

    async def test_missing_decoder_is_reported_not_faked(self):
        worker = media.MediaWorker(_TMP / 'media2')
        worker.ffmpeg = None
        result = await worker.extract(self.source, [0, 15000], job_id='job_a')
        self.assertEqual(result, {'frames': [], 'reason': 'decoder_missing'})
        self.assertFalse((_TMP / 'media2' / 'job_a').exists())

    async def test_missing_source_is_reported(self):
        result = await self.worker.extract(_TMP / 'nothing.m4s', [0], job_id='job_b')
        self.assertEqual(result['reason'], 'source_missing')

    async def test_good_frames_come_back_with_metadata(self):
        async def fake_one(source, target_ms, dest, budget):
            dest.write_bytes(jpeg_bytes(854, 480))
            return target_ms + 200

        self.worker._one = fake_one
        result = await self.worker.extract(self.source, [0, 15000, 30000], job_id='job_c')
        self.assertEqual(result['reason'], '')
        self.assertEqual([row['target_ms'] for row in result['frames']], [0, 15000, 30000])
        self.assertEqual([row['idx'] for row in result['frames']], [0, 1, 2])
        self.assertEqual(result['frames'][0]['actual_ms'], 200)
        self.assertEqual(result['frames'][0]['width'], 854)
        self.assertEqual(result['frames'][0]['source'], 'ffmpeg')
        self.assertEqual(len(result['frames'][0]['digest']), 32)
        self.assertTrue(Path(result['frames'][0]['path']).is_file())

    async def test_oversized_and_overwide_frames_are_dropped(self):
        async def fake_one(source, target_ms, dest, budget):
            if target_ms == 0:
                dest.write_bytes(jpeg_bytes(1920, 1080))   # too wide
            elif target_ms == 15000:
                dest.write_bytes(b'\xff\xd8' + b'0' * (3 * 1024 * 1024))  # too big
            else:
                dest.write_bytes(jpeg_bytes(640, 360))
            return target_ms

        self.worker._one = fake_one
        result = await self.worker.extract(self.source, [0, 15000, 30000], job_id='job_d',
                                          frame_max_bytes=2 * 1024 * 1024)
        self.assertEqual([row['target_ms'] for row in result['frames']], [30000])
        self.assertEqual(result['frames'][0]['idx'], 0)
        self.assertFalse((self.worker.job_dir('job_d') / 'f00.jpg').exists())

    async def test_all_frames_failing_is_decode_failed(self):
        async def fake_one(source, target_ms, dest, budget):
            raise asyncio.TimeoutError

        self.worker._one = fake_one
        result = await self.worker.extract(self.source, [0, 15000], job_id='job_e')
        self.assertEqual(result['frames'], [])
        self.assertEqual(result['reason'], 'decode_failed')

    async def test_frame_budget_is_capped(self):
        async def fake_one(source, target_ms, dest, budget):
            dest.write_bytes(jpeg_bytes(640, 360))
            return target_ms

        self.worker._one = fake_one
        result = await self.worker.extract(self.source, list(range(0, 600000, 15000)),
                                          job_id='job_f', max_frames=3)
        self.assertEqual(len(result['frames']), 3)

    async def test_probe_duration_without_ffprobe_is_zero(self):
        self.worker.ffprobe = None
        self.assertEqual(await self.worker.probe_duration(self.source), 0.0)


if __name__ == '__main__':
    unittest.main()
