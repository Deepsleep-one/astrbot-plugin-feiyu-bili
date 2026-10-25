"""Offline tests for paged reading, frame sampling and the reading store."""
import importlib.util
import json
import shutil
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
_TMP = HERE / '_tmp_reading'


def load(name):
    spec = importlib.util.spec_from_file_location(f'feiyu_{name}', PLUGIN / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reading = load('reading')
reading_store = load('reading_store')
BV1, BV2 = 'BV1xx411c7mD', 'BV1Q541167Qg'


class TargetTests(unittest.TestCase):
    def test_bv_and_page_parsing(self):
        cases = [
            (BV1, BV1, 1),
            (f'看看 {BV1}', BV1, 1),
            (f'https://www.bilibili.com/video/{BV1}', BV1, 1),
            (f'https://www.bilibili.com/video/{BV1}?p=3', BV1, 3),
            (f'{BV1} P2', BV1, 2),
            (f'{BV1} 第4P', BV1, 4),
            (f'BV1xx411c7mD{"/" * 3}', BV1, 1),
        ]
        for text, bvid, page in cases:
            with self.subTest(text=text):
                self.assertEqual(reading.parse_target(text), (bvid, page))

    def test_rejects_missing_or_malformed_bv(self):
        for text in ('', '看这个', 'BV123', 'bv1xx411c7mD', 'BV1xx411c7mDx'):
            with self.subTest(text=text):
                with self.assertRaises(reading.ReadingError):
                    reading.parse_target(text)


class FramePlanTests(unittest.TestCase):
    def test_spacing_and_limits(self):
        for duration, want in ((600, 4), (600, 40), (30, 10), (15, 5), (3600, 120)):
            times = reading.plan_frames(duration, want)
            with self.subTest(duration=duration, want=want, times=times):
                self.assertTrue(1 <= len(times) <= 40)
                self.assertEqual(times, sorted(times))
                self.assertTrue(all(t >= 0 for t in times))
                if len(times) > 1:
                    gaps = [b - a for a, b in zip(times, times[1:])]
                    self.assertGreaterEqual(min(gaps), reading.MIN_FRAME_GAP_S * 1000 - 1)
                self.assertLess(max(times), int(duration * 1000) + 1)

    def test_zero_duration_and_bad_budget(self):
        self.assertEqual(reading.plan_frames(0, 4), [0])
        self.assertEqual(reading.plan_frames(None, 4), [0])
        for want in (0, -5, None, 'x'):
            self.assertEqual(len(reading.plan_frames(600, want)), 1)


class TierTests(unittest.TestCase):
    def tier(self, height, bandwidth, urls=('u',)):
        return {'height': height, 'width': 854, 'codec': 'avc1', 'bandwidth': bandwidth,
                'urls': list(urls), 'kind': 'dash'}

    def test_picks_best_tier_that_fits_the_cap(self):
        info = {'tiers': [self.tier(480, 900_000, ['hi']), self.tier(360, 500_000, ['mid']),
                          self.tier(240, 200_000, ['lo'])], 'urls': ['hi']}
        # 600s at 900kbps is ~64MiB: too big for a 32MiB cap, 500kbps (~36MiB) also too big.
        self.assertEqual(reading.pick_tier(info, 600, 32 * 1024 * 1024), ['lo'])
        # A 5-minute part fits the middle tier but not the top one.
        self.assertEqual(reading.pick_tier(info, 300, 32 * 1024 * 1024), ['mid'])
        self.assertEqual(reading.pick_tier(info, 60, 32 * 1024 * 1024), ['hi'])

    def test_falls_back_to_smallest_when_nothing_fits(self):
        info = {'tiers': [self.tier(480, 9_000_000, ['hi']), self.tier(240, 1_000_000, ['lo'])]}
        self.assertEqual(reading.pick_tier(info, 3600, 32 * 1024 * 1024), ['lo'])

    def test_unknown_bandwidth_or_duration_uses_best_tier(self):
        info = {'tiers': [self.tier(480, 0, ['hi']), self.tier(240, 0, ['lo'])]}
        self.assertEqual(reading.pick_tier(info, 600, 32 * 1024 * 1024), ['hi'])
        info2 = {'tiers': [self.tier(480, 900_000, ['hi'])]}
        self.assertEqual(reading.pick_tier(info2, 0, 32 * 1024 * 1024), ['hi'])

    def test_legacy_payload_without_tiers_returns_urls(self):
        self.assertEqual(reading.pick_tier({'urls': ['only']}, 600, 1024), ['only'])
        self.assertEqual(reading.pick_tier({'tiers': []}, 600, 1024), [])


class CoverageTests(unittest.TestCase):
    def test_merges_adjacent_spans_and_reports_ratio(self):
        segments = [{'from_ms': 0, 'to_ms': 5000}, {'from_ms': 5000, 'to_ms': 10000},
                    {'from_ms': 60000, 'to_ms': 65000}]
        result = reading.coverage(segments, 120)
        self.assertEqual(result['covered_ms'], 15000)
        self.assertAlmostEqual(result['ratio'], 0.125)
        self.assertTrue(result['timed'])
        self.assertEqual([(h['from_ms'], h['to_ms']) for h in result['holes']],
                         [(10000, 60000), (65000, 120000)])

    def test_short_gaps_are_not_reported_as_omissions(self):
        result = reading.coverage([{'from_ms': 0, 'to_ms': 5000},
                                   {'from_ms': 6000, 'to_ms': 12000}], 12)
        self.assertEqual(result['holes'], [])
        self.assertEqual(result['covered_ms'], 11000)

    def test_no_timeline_means_no_invented_coverage(self):
        result = reading.coverage([{'from_ms': 0, 'to_ms': 0, 'text': '只有文字'}], 600)
        self.assertFalse(result['timed'])
        self.assertEqual(result['covered_ms'], 0)
        self.assertEqual(result['holes'], [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        self.store = reading_store.ReadingStore(_TMP / 'browse.db')
        self.scope = 'qq:owner:private'

    def tearDown(self):
        self.store.close()
        shutil.rmtree(_TMP, ignore_errors=True)

    def begin(self, bvid=BV1, page=1, scope=None, title='公开标题'):
        return self.store.begin_reading(
            reading_id=reading.new_id('rd'), bvid=bvid, page=page, cid=100 + page,
            title=title, author='公开作者', part_title='P', duration=600,
            fetched_at='2026-10-25T10:00:00+08:00', source_type='bvid',
            subtitle_state='timed', subtitle_reason='', evidence='text_read',
            scope=scope or self.scope)

    def test_reading_the_same_part_again_becomes_a_new_revision(self):
        first = self.begin()
        self.assertEqual(first['revision'], 1)
        self.store.add_segments(first['reading_id'], 1, [{'from_ms': 0, 'to_ms': 1, 'text': '旧'}])
        second = self.begin()
        self.assertEqual(second['reading_id'], first['reading_id'])
        self.assertEqual(second['revision'], 2)
        self.assertEqual(self.store.segments(first['reading_id'], 2), [])
        self.assertEqual(len(self.store.segments(first['reading_id'], 1)), 1)

    def test_other_parts_and_other_owners_stay_separate(self):
        first = self.begin(page=1)
        second = self.begin(page=2)
        self.assertNotEqual(first['reading_id'], second['reading_id'])
        self.assertIsNone(self.store.reading(first['reading_id'], 'qq:other:private'))
        self.assertEqual(self.store.reading(first['reading_id'], self.scope)['page'], 1)

    def test_cursor_is_bound_to_reading_revision_and_owner(self):
        record = self.begin()
        token = self.store.cursor(record['reading_id'], 1, 24, self.scope)
        self.assertEqual(self.store.read_cursor(token, self.scope), (record['reading_id'], 1, 24))
        for scope in ('qq:other:private', '', 'qq:owner:group'):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    self.store.read_cursor(token, scope)
        tampered = token.rsplit('.', 1)[0] + '.000000000000000000000000'
        with self.assertRaises(ValueError):
            self.store.read_cursor(tampered, self.scope)
        for bad in ('', 'x', 'a.b.c', f'{record["reading_id"]}.1.0', '../etc/passwd'):
            with self.assertRaises(ValueError):
                self.store.read_cursor(bad, self.scope)

    def test_reflections_supersede_and_withdraw(self):
        record = self.begin()
        self.store.add_journal('jnl_1', record['reading_id'], 1, {'revision': '先这样'},
                               {'reading_id': record['reading_id'], 'revision': 1})
        old = self.store.add_reflection(kind='recognition', topic='公开标题', old_view='',
                                        new_view='先这样', journal_id='jnl_1',
                                        refs={'reading_id': record['reading_id'], 'revision': 1})
        new = self.store.add_reflection(kind='correction', topic='公开标题', old_view='先这样',
                                        new_view='改成那样', journal_id='jnl_1',
                                        refs={'reading_id': record['reading_id'], 'revision': 1},
                                        supersedes=old)
        active = self.store.reflections(scope=self.scope)
        self.assertEqual([row['id'] for row in active], [new])
        self.assertEqual(self.store.reflection(old)['state'], reading_store.STATE_SUPERSEDED)
        self.store.withdraw_reflection(new)
        self.assertEqual(self.store.reflections(scope=self.scope), [])
        self.assertEqual(self.store.reflections(scope=self.scope,
                                                state=reading_store.STATE_WITHDRAWN)[0]['id'], new)

    def test_invalidation_cascades_to_journals_and_reflections(self):
        record = self.begin()
        other = self.begin(bvid=BV2)
        for journal_id, source in (('jnl_a', record), ('jnl_b', record), ('jnl_c', other)):
            self.store.add_journal(journal_id, source['reading_id'], source['revision'],
                                   {'revision': 'x'}, {'reading_id': source['reading_id'],
                                                       'revision': source['revision']})
            self.store.add_reflection(kind='recognition', topic='t', old_view='', new_view='v',
                                      journal_id=journal_id,
                                      refs={'reading_id': source['reading_id'],
                                            'revision': source['revision']})
        changed = self.store.invalidate_reading(record['reading_id'])
        self.assertEqual(sorted(changed['journals']), ['jnl_a', 'jnl_b'])
        self.assertEqual(len(changed['reflections']), 2)
        self.assertEqual(self.store.journal('jnl_c')['state'], reading_store.STATE_PENDING)
        self.assertEqual(len(self.store.reflections(scope=self.scope)), 1)

    def test_journals_are_scoped_and_counted(self):
        record = self.begin()
        for index in range(3):
            self.store.add_journal(f'jnl_{index}', record['reading_id'], 1, {'revision': 'x'},
                                   {'reading_id': record['reading_id'], 'revision': 1})
        self.assertEqual(len(self.store.journals(scope=self.scope, limit=5)), 3)
        self.assertEqual(len(self.store.journals(scope='qq:other:private')), 0)
        self.assertEqual(self.store.journal('jnl_0')['version'], 1)

    def test_scope_hash_never_leaks_the_session_id(self):
        digest = reading_store.scope_hash(self.scope)
        self.assertEqual(len(digest), 32)
        self.assertNotIn('owner', digest)
        self.assertEqual(digest, reading_store.scope_hash(self.scope))
        self.assertNotEqual(digest, reading_store.scope_hash('qq:owner:group'))


class PagingTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(_TMP, ignore_errors=True)
        self.store = reading_store.ReadingStore(_TMP / 'browse.db')
        self.scope = 'qq:owner:private'
        self.record = self.store.begin_reading(
            reading_id='rd_test', bvid=BV1, page=1, cid=100, title='公开标题', author='作者',
            part_title='P1', duration=600, fetched_at='2026-10-25T10:00:00+08:00',
            source_type='bvid', subtitle_state='timed', subtitle_reason='',
            evidence='text_read', scope=self.scope)
        segments = [{'from_ms': i * 1000, 'to_ms': i * 1000 + 900, 'text': f'第{i}条内容'}
                    for i in range(60)]
        self.store.add_segments('rd_test', 1, segments)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_every_line_is_served_exactly_once(self):
        seen, offset, guard = [], 0, 0
        while True:
            guard += 1
            self.assertLess(guard, 20)
            page = reading.render_page(self.store, 'rd_test', 1, offset, scope=self.scope)
            seen.extend(item['n'] for item in page['items'])
            if page['done']:
                self.assertIsNone(page['cursor'])
                break
            self.assertEqual(page['total'], 60)
            offset = self.store.read_cursor(page['cursor'], self.scope)[2]
        self.assertEqual(seen, list(range(1, 61)))

    def test_page_bounds_are_clamped_and_foreign_scope_refused(self):
        page = reading.render_page(self.store, 'rd_test', 1, 10 ** 6, scope=self.scope)
        self.assertTrue(page['done'])
        self.assertEqual(page['offset'], 59)
        with self.assertRaises(reading.ReadingError):
            reading.render_page(self.store, 'rd_test', 1, 0, scope='qq:other:private')
        with self.assertRaises(reading.ReadingError):
            reading.render_page(self.store, 'rd_test', 2, 0, scope=self.scope)
        with self.assertRaises(reading.ReadingError):
            reading.render_page(self.store, 'rd_missing', 1, 0, scope=self.scope)

    def test_timestamps_render_as_clock_markers(self):
        page = reading.render_page(self.store, 'rd_test', 1, 0, scope=self.scope)
        self.assertEqual(page['items'][0]['t'], '00:00')
        self.assertEqual(page['items'][2]['t'], '00:02')


if __name__ == '__main__':
    unittest.main()
