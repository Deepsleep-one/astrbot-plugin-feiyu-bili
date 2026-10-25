"""Offline tests for the ASR eligibility, budget and evidence logic.

Nothing here imports faster-whisper or touches the network: `asr.py` is pure
bookkeeping, and the worker that loads a model runs in its own process.
"""
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("feiyu_asr", PLUGIN / "asr.py")
asr = importlib.util.module_from_spec(spec)
sys.modules["feiyu_asr"] = asr
spec.loader.exec_module(asr)


def segs(count=8, text="泵站的进出水主管道要按设计标高排布并留出检修空间，这一段还讲了检修阀门的位置", **extra):
    rows = []
    for idx in range(count):
        rows.append({"from_ms": idx * 5000, "to_ms": idx * 5000 + 4500,
                     "text": f"{text}，第{idx}段。", **extra})
    return rows


class DayKeyTests(unittest.TestCase):
    def test_uses_shanghai_calendar_day(self):
        moment = datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(asr.day_key(moment), "2026-10-26")

    def test_naive_time_is_read_as_local(self):
        self.assertEqual(asr.day_key(datetime(2026, 10, 25, 1, 0)), "2026-10-25")

    def test_midnight_splits_days(self):
        before = datetime(2026, 10, 25, 23, 59, tzinfo=asr.TZ)
        after = datetime(2026, 10, 26, 0, 1, tzinfo=asr.TZ)
        self.assertEqual(asr.day_key(before), "2026-10-25")
        self.assertEqual(asr.day_key(after), "2026-10-26")


class JobIdTests(unittest.TestCase):
    def test_same_part_and_revision_is_the_same_job(self):
        one = asr.job_id_for("rd-1", 2)
        self.assertEqual(one, asr.job_id_for("rd-1", 2))
        self.assertNotEqual(one, asr.job_id_for("rd-1", 3))
        self.assertNotEqual(one, asr.job_id_for("rd-1", 2, "base"))
        self.assertTrue(one.startswith("asr-"))


class QualifyTests(unittest.TestCase):
    def call(self, **kw):
        base = dict(asr_on=True, explicit=True, automatic=False, engine_ready=True,
                    subtitle_text="", seconds=600)
        base.update(kw)
        return asr.qualify(**base)

    def test_plain_explicit_task_may_run(self):
        self.assertEqual(self.call(), (True, ""))

    def test_switch_off_wins(self):
        self.assertEqual(self.call(asr_on=False)[1], "disabled")

    def test_scheduled_rounds_never_run_asr(self):
        # Even with an owner token in hand, a scheduled round is not a request.
        self.assertEqual(self.call(automatic=True)[1], "automatic_task")
        self.assertEqual(self.call(automatic=True, explicit=False)[1], "automatic_task")

    def test_unrequested_reads_never_run_asr(self):
        self.assertEqual(self.call(explicit=False)[1], "not_requested")

    def test_subtitle_means_no_asr(self):
        self.assertEqual(self.call(subtitle_text="有字幕")[1], "subtitle_available")

    def test_missing_engine(self):
        self.assertEqual(self.call(engine_ready=False)[1], "engine_missing")

    def test_unknown_length_is_not_downloaded(self):
        self.assertEqual(self.call(seconds=0)[1], "unknown_duration")
        self.assertEqual(self.call(seconds=None)[1], "unknown_duration")

    def test_thirty_minutes_is_the_ceiling(self):
        self.assertEqual(self.call(seconds=1799), (True, ""))
        self.assertEqual(self.call(seconds=1800)[1], "too_long")

    def test_custom_ceiling(self):
        self.assertEqual(self.call(seconds=60, max_seconds=30)[1], "too_long")


class DurationTests(unittest.TestCase):
    def test_ok_within_limits(self):
        self.assertEqual(asr.check_duration(600_000, 598_000), (True, ""))

    def test_decoded_over_limit_is_stopped(self):
        self.assertEqual(asr.check_duration(600_000, 1_800_000)[1], "decoded_too_long")

    def test_declared_over_limit_is_stopped(self):
        self.assertEqual(asr.check_duration(1_800_000, 600_000)[1], "declared_too_long")

    def test_empty_decode(self):
        self.assertEqual(asr.check_duration(600_000, 0)[1], "decoded_empty")


class AssessTests(unittest.TestCase):
    def test_clean_speech_passes_and_keeps_engine_scores(self):
        report = asr.assess(segs(8, avg_logprob=-0.31, no_speech=0.02),
                            processed_ms=598_000, total_ms=600_000)
        self.assertTrue(report["like_ok"])
        self.assertEqual(report["reasons"], [])
        self.assertEqual(report["quality"], "ok")
        self.assertAlmostEqual(report["avg_logprob"], -0.31, places=3)
        # The score is recorded, not converted into a claimed confidence.
        self.assertNotIn("confidence", report)

    def test_silence_has_no_text(self):
        report = asr.assess([], processed_ms=0, total_ms=600_000)
        self.assertFalse(report["like_ok"])
        self.assertIn("no_speech", report["reasons"])

    def test_too_little_text(self):
        report = asr.assess([{"from_ms": 0, "to_ms": 1000, "text": "嗯"}])
        self.assertIn("too_little_text", report["reasons"])

    def test_too_few_segments(self):
        rows = [dict(row, text=row["text"] + "补" * 20) for row in segs(3)]
        report = asr.assess(rows)
        self.assertIn("too_few_segments", report["reasons"])

    def test_low_coverage(self):
        report = asr.assess(segs(8), processed_ms=30_000, total_ms=600_000)
        self.assertIn("low_coverage", report["reasons"])

    def test_repetition_loop(self):
        rows = [{"from_ms": i * 1000, "to_ms": i * 1000 + 900,
                 "text": "谢谢老板谢谢老板谢谢老板"} for i in range(6)]
        report = asr.assess(rows, processed_ms=600_000, total_ms=600_000)
        self.assertIn("repetitive", report["reasons"])

    def test_degenerate_characters(self):
        rows = [{"from_ms": i * 1000, "to_ms": i * 1000 + 900,
                 "text": "啊啊啊啊啊" + str(i)} for i in range(6)]
        report = asr.assess(rows, processed_ms=600_000, total_ms=600_000)
        self.assertIn("degenerate_text", report["reasons"])

    def test_mostly_silence(self):
        rows = segs(8, no_speech=0.9)
        report = asr.assess(rows, processed_ms=600_000, total_ms=600_000)
        self.assertIn("mostly_silence", report["reasons"])

    def test_gate_note_is_human_readable(self):
        note = asr.gate_note({"reasons": ["no_speech", "low_coverage"]})
        self.assertIn("没有识别出人声", note)
        self.assertIn("只覆盖", note)


class WorkerContractTests(unittest.TestCase):
    def test_args_are_paths_and_scalars_only(self):
        args = asr.worker_args(audio="a.wav", out="o.json", model="small",
                               compute_type="int8", device="cpu", threads=2, language="zh")
        self.assertIn("asr_worker.py", args[2])
        self.assertLess(args.index("--model") + 1, len(args))
        self.assertEqual(args[args.index("--device") + 1], "cpu")
        self.assertNotIn(";", " ".join(args))
        self.assertNotIn("&&", " ".join(args))

    def test_model_dir_is_optional(self):
        plain = asr.worker_args(audio="a", out="b", model="small", compute_type="int8",
                                device="cpu", threads=2, language="zh")
        self.assertNotIn("--model-dir", plain)
        pinned = asr.worker_args(audio="a", out="b", model="small", compute_type="int8",
                                 device="cpu", threads=2, language="zh", model_dir="D:/m")
        self.assertEqual(pinned[pinned.index("--model-dir") + 1], "D:/m")

    def test_env_pins_cpu_and_threads_and_drops_proxies(self):
        env = asr.worker_env(3)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")
        self.assertEqual(env["OMP_NUM_THREADS"], "3")
        self.assertEqual(env["MKL_NUM_THREADS"], "3")
        self.assertEqual(env["TOKENIZERS_PARALLELISM"], "false")
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("ALL_PROXY", env)


class ParseTests(unittest.TestCase):
    def test_reads_json_after_chatty_logs(self):
        text = 'loading\n{"ok": true, "segments": [{"from_ms": 5000, "to_ms": 9000, "text": "第二句"}, {"from_ms": 0, "to_ms": 3000, "text": "第一句"}], "duration_ms": 9000, "language": "zh", "model": "small"}\n'
        parsed = asr.parse_worker_output(text)
        self.assertTrue(parsed["ok"])
        self.assertEqual([row["text"] for row in parsed["segments"]], ["第一句", "第二句"])
        self.assertEqual(parsed["processed_ms"], 9000)
        self.assertEqual(parsed["model"], "small")

    def test_crash_is_not_success(self):
        parsed = asr.parse_worker_output("Traceback (most recent call last):\n boom")
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "worker_failed")

    def test_failure_payload_keeps_reason(self):
        parsed = asr.parse_worker_output('{"ok": false, "reason": "model_missing"}')
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "model_missing")

    def test_blank_segments_are_dropped(self):
        parsed = asr.parse_worker_output('{"ok": true, "segments": [{"from_ms": 0, "to_ms": 1, "text": "  "}]}')
        self.assertEqual(parsed["segments"], [])


class FormatTests(unittest.TestCase):
    def job(self, **extra):
        row = {"job_id": "asr-abc", "bvid": "BV0000000001", "revision": 1,
               "state": asr.STATE_COMPLETED, "reason": "ok", "engine": "faster-whisper",
               "model": "small", "language": "zh", "decoded_ms": 600_000,
               "segments": 8, "chars": 900, "coverage": '{"like_ok": true}'}
        row.update(extra)
        return row

    def test_segment_timestamps(self):
        text = asr.format_segments([{"from_ms": 65000, "to_ms": 71000, "text": "泵站"}])
        self.assertIn("01:05", text)
        self.assertIn("泵站", text)

    def test_report_says_it_is_machine_made(self):
        text = asr.format_job(self.job(), segs(3))
        self.assertIn("机器转写", text)
        self.assertIn("不证明理解了画面", text)

    def test_failed_job_explains_reason(self):
        text = asr.format_job(self.job(state=asr.STATE_TIMEOUT, reason="worker_timeout"))
        self.assertIn("转写超时", text)

    def test_insufficient_evidence_is_flagged(self):
        text = asr.format_job(self.job(coverage='{"like_ok": false, "reasons": ["no_speech"]}'), segs(2))
        self.assertIn("证据不足", text)

    def test_prompt_block_warns_against_treating_it_as_subtitles(self):
        block = asr.prompt_block(self.job(), segs(2))
        self.assertIn("机器转写", block)
        self.assertIn("不能当成官方字幕", block)

    def test_empty_result_carries_reason_text(self):
        empty = asr.empty_result("engine_missing")
        self.assertFalse(empty["ok"])
        self.assertIn("未安装", empty["note"])


class RemoteContractTests(unittest.TestCase):
    """The remote executor gets identifiers only, and its answer is normalised same as local."""

    def test_payload_carries_no_audio_and_no_credentials(self):
        body = asr.remote_payload(bvid="BV1xx411c7mD", cid=43019799288, model="small",
                                  language="zh", max_seconds=1799, reading_id="rd_1", revision=2)
        self.assertEqual(body["bvid"], "BV1xx411c7mD")
        self.assertEqual(body["cid"], 43019799288)
        self.assertEqual(body["revision"], 2)
        flat = json.dumps(body)
        self.assertNotIn("http", flat)          # no signed CDN URL
        self.assertNotIn("cookie", flat.lower())
        self.assertNotIn("path", flat.lower())

    def test_answer_is_parsed_like_a_local_worker(self):
        payload = {"ok": True, "duration_ms": 60000, "language": "zh",
                   "segments": [{"from_ms": 0, "to_ms": 1500, "text": "泵站按标高排"}]}
        parsed = asr.parse_remote(json.dumps(payload))
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["segments"][0]["text"], "泵站按标高排")
        self.assertEqual(parsed["duration_ms"], 60000)

    def test_garbage_is_not_success(self):
        for text in ("", "not json", "[]", "<html>oops</html>"):
            parsed = asr.parse_remote(text)
            self.assertFalse(parsed["ok"], text)
            self.assertEqual(parsed["reason"], "remote_bad_response")

    def test_remote_refusal_keeps_its_reason(self):
        parsed = asr.parse_remote(json.dumps({"ok": False, "reason": "too_long"}))
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "too_long")

    def test_remote_failures_have_readable_reasons(self):
        for reason in ("remote_missing", "remote_unavailable", "remote_busy", "remote_bad_response"):
            self.assertNotEqual(asr.brief_reason(reason), reason, reason)


if __name__ == "__main__":
    unittest.main()
