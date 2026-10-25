"""Offline tests for the visual pass: which frames go out, what comes back."""
import importlib.util
import json
import shutil
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
_TMP = HERE / '_tmp_vision'


def load(name):
    spec = importlib.util.spec_from_file_location(f'feiyu_{name}', PLUGIN / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vision = load('vision')


def fake_frames(count=6):
    frames = []
    for index in range(count):
        path = _TMP / f'f{index}.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\xff\xd8' + b'0' * 200)
        frames.append({'frame_id': f'frame_{index}', 'idx': index, 'path': str(path),
                       'bytes': 202, 'actual_ms': index * 15000, 'width': 854, 'height': 480})
    return frames


class BatchTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_only_real_frames_and_at_most_four(self):
        frames = fake_frames(6)
        frames.append({'frame_id': 'empty', 'path': '', 'bytes': 100})
        frames.append({'frame_id': 'tiny', 'path': str(_TMP / 'f0.jpg'), 'bytes': 10})
        picked = vision.batch_frames(frames)
        self.assertEqual(len(picked), 4)
        self.assertEqual([row['frame_id'] for row in picked],
                         ['frame_0', 'frame_1', 'frame_2', 'frame_3'])
        self.assertEqual(vision.batch_frames([]), [])
        self.assertEqual(vision.batch_frames(None), [])


class ParseTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_fenced_json_with_preamble(self):
        frames = fake_frames(2)
        text = ('我看了这几张。\n```json\n' + json.dumps({
            'observations': [{'n': 1, 'visible': True, 'reading': '室内构图',
                              'uncertainty': ''},
                             {'n': 2, 'visible': False, 'reading': '全黑',
                              'uncertainty': '可能是转场'}],
            'summary': '两帧差异明显'}, ensure_ascii=False) + '\n```\n完毕')
        parsed = vision.parse_observations(text, frames)
        self.assertTrue(parsed['parsed'])
        self.assertEqual([row['frame_id'] for row in parsed['observations']],
                         ['frame_0', 'frame_1'])
        self.assertEqual(parsed['observations'][1]['visible'], False)
        self.assertEqual(parsed['summary'], '两帧差异明显')

    def test_out_of_range_and_broken_items_are_dropped(self):
        frames = fake_frames(2)
        payload = {'observations': [{'n': 0}, {'n': 3, 'reading': 'x'},
                                    {'n': 'two'}, 'garbage',
                                    {'n': 2, 'reading': '合法的第二帧'}], 'summary': ''}
        parsed = vision.parse_observations(json.dumps(payload, ensure_ascii=False), frames)
        self.assertEqual(len(parsed['observations']), 1)
        self.assertEqual(parsed['observations'][0]['actual_ms'], 15000)

    def test_unparseable_text_keeps_raw_summary_only(self):
        frames = fake_frames(1)
        for text in ('', '看不出什么', '```json\n{mangled}\n```', '{"a":1}'):
            with self.subTest(text=text):
                parsed = vision.parse_observations(text, frames)
                self.assertFalse(parsed['parsed'])
                self.assertEqual(parsed['observations'], [])

    def test_links_are_stripped_from_observations(self):
        frames = fake_frames(1)
        payload = {'observations': [{'n': 1, 'reading': '看 https://evil.example/x 这里',
                                     'uncertainty': ''}], 'summary': ''}
        parsed = vision.parse_observations(json.dumps(payload, ensure_ascii=False), frames)
        self.assertNotIn('evil.example', parsed['observations'][0]['interpretation'])


class DescribeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP.mkdir(parents=True, exist_ok=True)
        self.context = {'title': '公开标题', 'part_title': 'P1', 'author': '作者', 'duration': 120}

    def tearDown(self):
        shutil.rmtree(_TMP, ignore_errors=True)

    async def test_real_paths_are_sent_and_observations_stored(self):
        frames = fake_frames(4)
        seen = {}

        async def llm(system, prompt, image_urls=None):
            seen['system'] = system
            seen['prompt'] = prompt
            seen['images'] = list(image_urls or [])
            return json.dumps({'observations': [
                {'n': index, 'visible': True, 'reading': f'第{index}张',
                 'uncertainty': ''} for index in range(1, 5)],
                'summary': '四张都看到了'}, ensure_ascii=False)

        result = await vision.describe(llm, frames, self.context, provider_id='pid_1')
        self.assertEqual(result['state'], 'visual_observed')
        self.assertEqual(result['images_submitted'], 4)
        self.assertEqual(seen['images'], [str(_TMP / f'f{i}.jpg') for i in range(4)])
        self.assertIn('不要用推测代替观察', seen['system'])
        self.assertIn('第 15 秒', seen['prompt'])
        self.assertEqual([row['frame_id'] for row in result['observations']],
                         ['frame_0', 'frame_1', 'frame_2', 'frame_3'])

    async def test_no_frames_means_no_request_and_no_claim(self):
        called = []

        async def llm(system, prompt, image_urls=None):
            called.append(image_urls)
            return '{}'

        result = await vision.describe(llm, [], self.context)
        self.assertEqual(called, [])
        self.assertEqual(result['state'], 'visual_unavailable')
        self.assertEqual(result['reason'], 'no_frames')
        self.assertEqual(result['images_submitted'], 0)

    async def test_provider_failure_is_reported_not_swallowed(self):
        frames = fake_frames(2)

        async def llm(system, prompt, image_urls=None):
            raise RuntimeError('PROVIDER_SENTINEL')

        result = await vision.describe(llm, frames, self.context)
        self.assertEqual(result['state'], 'visual_unavailable')
        self.assertEqual(result['reason'], 'provider_error:RuntimeError')
        self.assertNotIn('PROVIDER_SENTINEL', json.dumps(result, ensure_ascii=False))

    async def test_unparsed_reply_is_not_treated_as_seen(self):
        frames = fake_frames(2)

        async def llm(system, prompt, image_urls=None):
            return '我看完了，挺好的。'

        result = await vision.describe(llm, frames, self.context)
        self.assertEqual(result['state'], 'visual_unavailable')
        self.assertEqual(result['reason'], 'unparsed')
        self.assertEqual(result['observations'], [])


if __name__ == '__main__':
    unittest.main()
