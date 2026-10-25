"""Offline tests for the eight-field journal and the related-reading recall."""
import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
_TMP = HERE / '_tmp_journal'
sys.path.insert(0, str(PLUGIN))


def load(name):
    spec = importlib.util.spec_from_file_location(f'feiyu_{name}', PLUGIN / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


journal = load('journal')
reading_store = load('reading_store')
FULL = {
    'encounter': '主人在私聊里丢来一个讲城市排水的老视频。',
    'material_facts': '视频用三段实拍说明雨水管网怎么分流。',
    'author_view': '作者认为老城区改造要先做管网普查。',
    'ai_inference': '我猜他手里有市政工程的一手资料。',
    'prior_judgment': '我以前觉得这种事只能靠大工程。',
    'revision': '现在觉得先查清管线是更省钱的一步。',
    'omissions': '画面是缩略图，看不见图纸细节。',
    'next_response': '下次可以问主人本地的排水情况。',
}


class ParseTests(unittest.TestCase):
    def test_all_eight_keys_round_trip(self):
        parsed = journal.parse_journal(json.dumps(FULL, ensure_ascii=False))
        self.assertTrue(parsed['parsed'])
        self.assertEqual(parsed['missing'], [])
        self.assertEqual(parsed['fields'], FULL)

    def test_missing_and_empty_keys_are_reported_not_invented(self):
        payload = dict(FULL)
        payload.pop('omissions')
        payload['revision'] = '   '
        parsed = journal.parse_journal(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(sorted(parsed['missing']), ['omissions', 'revision'])
        self.assertNotIn('omissions', parsed['fields'])
        self.assertNotIn('revision', parsed['fields'])

    def test_fenced_and_verbose_replies_still_parse(self):
        text = '好的，这是手账：\n```json\n' + json.dumps(FULL, ensure_ascii=False) + '\n```'
        self.assertEqual(journal.parse_journal(text)['fields'], FULL)

    def test_garbage_is_not_recorded(self):
        for text in ('', '我读完了，很有收获。', '```json\n{oops}\n```', '{"a":1}', None):
            with self.subTest(text=text):
                parsed = journal.parse_journal(text)
                self.assertEqual(parsed['fields'], {})
                self.assertEqual(len(parsed['missing']), 8)

    def test_values_are_flattened_and_stripped_of_links(self):
        payload = dict(FULL, material_facts=['第一点', '第二点'],
                       omissions='见 https://evil.example/x 这个链接')
        parsed = journal.parse_journal(json.dumps(payload, ensure_ascii=False))
        self.assertIn('第一点；第二点', parsed['fields']['material_facts'])
        self.assertNotIn('evil.example', parsed['fields']['omissions'])

    def test_full_text_uses_chinese_labels_in_order(self):
        rendered = journal.full_text(FULL)
        positions = [rendered.index(label) for label in journal.JOURNAL_LABELS]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(rendered.startswith('遇见与处境：'))
        self.assertEqual(journal.full_text({}), '')


class PromptTests(unittest.TestCase):
    def record(self):
        return {'title': '老城区排水', 'part_title': 'P1', 'author': '公开作者',
                'duration': 300, 'reading_id': 'rd_1', 'revision': 1, 'page': 1}

    def page(self):
        return {'items': [{'n': 1, 't': '00:15', 'text': '雨水和污水分开走'}],
                'coverage': {'ratio': 0.5, 'timed': True}}

    def test_without_frames_the_prompt_forbids_describing_pictures(self):
        prompt = journal.build_prompt(self.record(), self.page(), [], [])
        self.assertIn('不得描述画面内容', prompt)
        self.assertIn('00:15 雨水和污水分开走', prompt)
        self.assertIn('没有字幕', journal.build_prompt(self.record(), {'items': [], 'coverage': {}}, [], []))

    def test_observations_and_prior_views_are_included(self):
        observations = [{'actual_ms': 30000, 'interpretation': '画面是一片积水的路面',
                         'uncertainty': '看不清门牌'}]
        prior = [{'topic': '排水', 'new_view': '要先普查管网'}]
        prompt = journal.build_prompt(self.record(), self.page(), observations, prior, '主人说这段讲反了')
        self.assertIn('画面是一片积水的路面', prompt)
        self.assertIn('看不清门牌', prompt)
        self.assertIn('要先普查管网', prompt)
        self.assertIn('主人说这段讲反了', prompt)

    def test_private_material_never_enters_the_prompt(self):
        record = dict(self.record(), title='标题 PRIVATE_TITLE', author='作者')
        prompt = journal.build_prompt(record, self.page(), [], [])
        self.assertNotIn('SECRET_SENTINEL', prompt)


class RecallTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        self.store = reading_store.ReadingStore(_TMP / 'browse.db')
        self.scope = 'qq:owner:private'
        record = self.store.begin_reading(
            reading_id='rd_1', bvid='BV1xx411c7mD', page=1, cid=1, title='老城区排水与管网',
            author='作者', part_title='P1', duration=300,
            fetched_at='2026-10-25T10:00:00+08:00', source_type='bvid',
            subtitle_state='timed', subtitle_reason='', evidence='text_read', scope=self.scope)
        self.store.add_journal('jnl_1', 'rd_1', 1, dict(FULL), {'reading_id': 'rd_1', 'revision': 1},
                               state=reading_store.STATE_SELECTED)
        self.store.add_reflection(kind='recognition', topic='城市排水管网', old_view='只能靠大工程',
                                  new_view='先普查管网再改造省钱', journal_id='jnl_1',
                                  refs={'reading_id': record['reading_id'], 'revision': 1})

    def tearDown(self):
        self.store.close()
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_overlapping_question_finds_the_reading(self):
        picked = journal.relevant(self.store, self.scope, '你之前看过的排水管网那个视频', limit=2)
        self.assertTrue(picked)
        self.assertEqual(picked[0]['kind'], 'reflection')
        self.assertIn('普查', journal.recall_line(picked[0]))

    def test_unrelated_or_empty_question_recalls_nothing(self):
        self.assertEqual(journal.relevant(self.store, self.scope, ''), [])
        self.assertEqual(journal.relevant(self.store, self.scope, 'zzz qqq'), [])

    def test_other_owners_and_withdrawn_views_are_not_recalled(self):
        self.assertEqual(journal.relevant(self.store, 'qq:other:private', '排水管网'), [])
        for row in self.store.reflections(scope=self.scope):
            self.store.withdraw_reflection(row['id'])
        picked = journal.relevant(self.store, self.scope, '排水管网')
        self.assertTrue(all(item['kind'] == 'journal' for item in picked))


if __name__ == '__main__':
    unittest.main()
