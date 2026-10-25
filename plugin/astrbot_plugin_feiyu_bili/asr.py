"""Local CPU+INT8 speech recognition: eligibility, budget and evidence gates.

No ASR engine is imported here on purpose. The worker that actually loads a model
lives in `asr_worker.py` and runs as a separate process, so this module stays
importable (and testable) on a machine where faster-whisper is not installed.
`engine_probe()` is the only place that knows how to ask the worker a question.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fixed offset: Asia/Shanghai has had no DST since 1991, so a fixed zone is exact
# and keeps day boundaries stable across hosts.
TZ = timezone(timedelta(hours=8))
DAY_LABEL = "Asia/Shanghai"

ENGINE = "faster-whisper"
DEFAULT_MODEL = "small"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_DEVICE = "cpu"
DEFAULT_LANGUAGE = "zh"
DEFAULT_THREADS = 2

# One clip must be strictly under half an hour; 1799 s is the documented ceiling.
MAX_SECONDS = 1799
DAILY_LIMIT = 5

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"
STATE_TIMEOUT = "timeout"
STATE_CANCELLED = "cancelled"

_TERMINAL = (STATE_COMPLETED, STATE_FAILED, STATE_SKIPPED, STATE_TIMEOUT, STATE_CANCELLED)

# Evidence gates for "this transcript is substantial enough to judge a like".
# They are deliberately conservative: a failed gate means "do not like", never
# "like anyway because the task asked for it".
MIN_CHARS = 200
MIN_SEGMENTS = 5
MIN_COVERAGE = 0.35
MAX_REPEAT = 0.55
MIN_UNIQUE_CHARS = 40
MAX_SILENCE = 0.6

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CQ = re.compile(r"\[CQ:[^\]]*\]", re.I)


def day_key(moment=None) -> str:
    """Calendar day in Asia/Shanghai; the ASR budget resets on this boundary."""
    stamp = moment or datetime.now(TZ)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    return stamp.astimezone(TZ).strftime("%Y-%m-%d")


def job_id_for(reading_id: str, revision: int, model: str = DEFAULT_MODEL) -> str:
    """Deterministic id: the same part+revision+model is the same job, so a retry
    cannot take a second budget slot."""
    raw = f"{reading_id}|{int(revision)}|{model}"
    return "asr-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def clean_text(value, limit: int = 300) -> str:
    text = _URL.sub(" ", str(value or ""))
    text = _CQ.sub(" ", text)
    text = _CONTROL.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def qualify(*, asr_on: bool, explicit: bool, automatic: bool, engine_ready: bool,
            subtitle_text: str, seconds: float,
            max_seconds: float = MAX_SECONDS) -> tuple[bool, str]:
    """Decide whether this task may start ASR, and say why not.

    Explicit owner intent is required and is *not* inherited by scheduled rounds:
    a background round that follows the same video gets `not_requested`.
    """
    if not asr_on:
        return False, "disabled"
    if automatic:
        return False, "automatic_task"
    if not explicit:
        return False, "not_requested"
    if str(subtitle_text or "").strip():
        return False, "subtitle_available"
    if not engine_ready:
        return False, "engine_missing"
    if not seconds or float(seconds) <= 0:
        return False, "unknown_duration"
    if float(seconds) > float(max_seconds):
        return False, "too_long"
    return True, ""


def check_duration(declared_ms, decoded_ms, *, max_seconds: float = MAX_SECONDS) -> tuple[bool, str]:
    """Two independent checks: what the API claims and what actually decoded."""
    declared = max(0.0, float(declared_ms or 0) / 1000.0)
    decoded = max(0.0, float(decoded_ms or 0) / 1000.0)
    if decoded > float(max_seconds):
        return False, "decoded_too_long"
    if declared and declared > float(max_seconds):
        return False, "declared_too_long"
    if not decoded:
        return False, "decoded_empty"
    return True, ""


def brief_reason(reason: str) -> str:
    return {
        "disabled": "本地转写没有打开（asr_enabled=false）",
        "automatic_task": "定时后台活动不能启动转写",
        "not_requested": "这次不是主人的明确阅读任务，不启动转写",
        "subtitle_available": "这个分P本来就有字幕，不花转写次数",
        "engine_missing": "本地转写引擎未安装（faster-whisper 不可用）",
        "remote_missing": "本机转写执行端连不上（隧道或服务没开）",
        "remote_unavailable": "本机转写执行端没响应（不自动改用别的模型）",
        "remote_bad_response": "本机转写执行端回的格式看不懂",
        "remote_too_large": "本机转写执行端回的内容过大，已拒绝",
        "remote_busy": "本机转写执行端正在处理另一条，稍后再试",
        "unknown_duration": "不知道这条视频多长，先不下音频",
        "too_long": "这条音频超过 29 分 59 秒上限，整条跳过（不截取前段冒充）",
        "declared_too_long": "接口报的时长超过上限，整条跳过",
        "decoded_too_long": "实际解出超过 1799 秒，立即终止",
        "decoded_empty": "音频解出来是空的，没有转写",
        "budget_exhausted": "今天的 5 次转写额度用完了",
        "download_failed": "音轨没下载下来",
        "decode_failed": "音轨解不开",
        "worker_failed": "转写进程出错",
        "worker_timeout": "转写超时被终止",
        "total_timeout": "整个转写任务超时被终止（下载+解码+推理合计超限）",
        "cancelled": "任务被取消",
        "empty": "没识别出任何文字",
    }.get(reason, reason)


def _mean(values) -> float | None:
    items = [float(v) for v in values if isinstance(v, (int, float))]
    return sum(items) / len(items) if items else None


def assess(segments, *, processed_ms: int = 0, total_ms: int = 0) -> dict:
    """Judge whether the transcript carries enough substance for a like decision.

    Engine scores (avg_logprob / no_speech) are recorded for the report but are
    never turned into a single made-up "confidence": they are a veto on obvious
    silence, not a truth meter.
    """
    texts = [clean_text(s.get("text")) for s in (segments or [])]
    texts = [t for t in texts if t]
    chars = sum(len(t) for t in texts)
    unique_texts = len(set(texts))
    repeat = 1.0 - (unique_texts / len(texts)) if texts else 1.0
    unique_chars = len(set("".join(texts)))
    coverage = min(1.0, processed_ms / total_ms) if total_ms else 0.0
    avg_logprob = _mean(s.get("avg_logprob") for s in (segments or []))
    no_speech = _mean(s.get("no_speech") for s in (segments or []))

    reasons = []
    if not texts:
        reasons.append("no_speech")
    else:
        if chars < MIN_CHARS:
            reasons.append("too_little_text")
        if len(texts) < MIN_SEGMENTS:
            reasons.append("too_few_segments")
        if total_ms and coverage < MIN_COVERAGE:
            reasons.append("low_coverage")
        if len(texts) >= 4 and repeat > MAX_REPEAT:
            reasons.append("repetitive")
        if unique_chars < MIN_UNIQUE_CHARS:
            reasons.append("degenerate_text")
        if no_speech is not None and no_speech >= MAX_SILENCE:
            reasons.append("mostly_silence")
    like_ok = not reasons
    return {
        "like_ok": like_ok,
        "reasons": reasons,
        "chars": chars,
        "segments": len(texts),
        "unique_chars": unique_chars,
        "repeat": round(repeat, 3),
        "coverage": round(coverage, 4),
        "avg_logprob": None if avg_logprob is None else round(avg_logprob, 3),
        "no_speech": None if no_speech is None else round(no_speech, 3),
        "quality": "ok" if like_ok else "insufficient",
    }


def gate_note(quality: dict) -> str:
    labels = {
        "no_speech": "几乎没有识别出人声（静音或纯音乐）",
        "too_little_text": f"有效文字不足 {MIN_CHARS} 字",
        "too_few_segments": f"片段少于 {MIN_SEGMENTS} 段",
        "low_coverage": "只覆盖了音轨的一小部分（疑似严重截断）",
        "repetitive": "大量重复（疑似幻觉循环）",
        "degenerate_text": "文字重复度过高（疑似乱码）",
        "mostly_silence": "引擎判为静音的比例过高",
    }
    found = [labels.get(r, r) for r in quality.get("reasons") or []]
    return "；".join(found)


def _stamp(ms) -> str:
    try:
        total = max(0, int(ms) // 1000)
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60:02d}:{total % 60:02d}"


def format_segments(segments, limit: int = 40, width: int = 240) -> str:
    lines = []
    for idx, seg in enumerate((segments or [])[:limit], 1):
        text = clean_text(seg.get("text"), width)
        if not text:
            continue
        lines.append(f"[{idx}] {_stamp(seg.get('from_ms'))}–{_stamp(seg.get('to_ms'))} {text}")
    return "\n".join(lines)


def format_job(job: dict, segments=None) -> str:
    """Human-readable report for the owner: what was processed, what is limited."""
    state = job.get("state", "")
    head = (f"[转写 {job.get('job_id', '')}] {clean_text(job.get('bvid'), 20)}"
            f" P{int(job.get('revision') or 0)}｜{_stamp(job.get('decoded_ms'))}"
            f"｜{job.get('engine', '')} {job.get('model', '')}｜{job.get('language', '')}")
    if state != STATE_COMPLETED:
        return head + "\n结果：" + (brief_reason(job.get("reason") or state))
    quality = {}
    try:
        quality = json.loads(job.get("coverage") or "{}")
    except (TypeError, ValueError):
        quality = {}
    if not isinstance(quality, dict):
        quality = {}
    body = format_segments(segments or [])
    note = (f"这是本地 {job.get('engine', '')} {job.get('model', '')} 的机器转写，可能有错字；"
            "它只证明听到了这些字，不证明理解了画面、音乐或语气。")
    tail = ""
    if quality and not quality.get("like_ok", True):
        tail = "\n（证据不足，不据此点赞：" + (gate_note(quality) or "内容太薄") + "）"
    return f"{head}\n{note}\n{body or '（没有可用片段）'}{tail}"


def prompt_block(job: dict, segments, quality: dict | None = None) -> str:
    """Text handed to the journal/decision model, clearly separated from anything
    the model might paraphrase: the raw segments are quoted, not "summarised"."""
    body = format_segments(segments or [], limit=30)
    quality = quality or {}
    head = (f"[本地机器转写 {job.get('engine', '')} {job.get('model', '')}"
            f" {job.get('language', '')}] 覆盖 {_stamp(job.get('decoded_ms'))}，"
            f"{int(job.get('segments') or 0)} 段、约 {int(job.get('chars') or 0)} 字。")
    warn = ("这些是机器听的，可能有错字、漏字、把专名听错；只能当作『他说了什么』的参考，"
            "不能当成官方字幕，也不能当成画面描述。")
    if quality and not quality.get("like_ok", True):
        warn += " 本次识别质量不足（" + (gate_note(quality) or "内容太薄") + "），不要仅凭它下强判断。"
    return f"{head}\n{warn}\n{body}"


def worker_args(*, audio, out, model: str, compute_type: str, device: str,
                threads: int, language: str, model_dir: str = "") -> list[str]:
    """Fixed argv for the worker: paths and scalar knobs only, never a shell string."""
    args = [sys.executable, "-B", str(Path(__file__).with_name("asr_worker.py")),
            "--audio", str(audio), "--out", str(out), "--model", model,
            "--compute-type", compute_type, "--device", device,
            "--threads", str(int(threads)), "--language", language]
    if model_dir:
        args += ["--model-dir", model_dir]
    return args


def worker_env(threads: int, lib_dir: str = "") -> dict:
    """Minimal env that pins the worker to CPU and to a fixed thread budget.

    `lib_dir` is where the ASR runtime is installed when it lives outside Python's
    own site-packages (the usual case for a plugin: a self-contained directory that
    survives restarts). It is prepended to PYTHONPATH, never written to disk here.
    """
    import os

    keep = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP",
            "LD_LIBRARY_PATH", "PYTHONPATH", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k in keep}
    if lib_dir:
        env["PYTHONPATH"] = str(lib_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update({
        "CUDA_VISIBLE_DEVICES": "",
        "OMP_NUM_THREADS": str(int(threads)),
        "MKL_NUM_THREADS": str(int(threads)),
        "OPENBLAS_NUM_THREADS": str(int(threads)),
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def parse_worker_output(text: str) -> dict:
    """Parse the worker's JSON; a crash or a chatty log must not look like success."""
    payload = None
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            break
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "worker_failed", "segments": []}
    if not payload.get("ok"):
        return {"ok": False, "reason": str(payload.get("reason") or "worker_failed"),
                "segments": [], "detail": str(payload.get("detail") or "")[:200]}
    segments = []
    for item in payload.get("segments") or []:
        if not isinstance(item, dict):
            continue
        text = clean_text(item.get("text"))
        if not text:
            continue
        segments.append({
            "from_ms": max(0, int(item.get("from_ms", 0) or 0)),
            "to_ms": max(0, int(item.get("to_ms", 0) or 0)),
            "text": text,
            "avg_logprob": item.get("avg_logprob"),
            "no_speech": item.get("no_speech"),
        })
    segments.sort(key=lambda row: row["from_ms"])
    processed = int(segments[-1]["to_ms"]) if segments else 0
    return {"ok": True, "reason": "", "segments": segments,
            "processed_ms": processed,
            "duration_ms": int(payload.get("duration_ms", 0) or 0),
            "language": str(payload.get("language") or "")[:16],
            "engine": str(payload.get("engine") or ENGINE)[:40],
            "model": str(payload.get("model") or "")[:80]}


def probe(timeout: float = 30.0, out=None, lib_dir: str = "") -> dict:
    """Ask the worker whether the engine is importable (never downloads weights).

    `out` is a file path the worker writes its JSON to. Piped stdio is unreliable
    under the sandboxed host, so callers that have a writable directory should pass
    one and let the answer arrive as a file.
    """
    path = str(out) if out else ""
    argv = [sys.executable, "-B", str(Path(__file__).with_name("asr_worker.py")), "--probe"]
    if path:
        argv += ["--out", path]
    try:
        done = subprocess.run(
            argv, capture_output=not path, text=not path, timeout=timeout, check=False,
            stdout=subprocess.DEVNULL if path else None,
            stderr=subprocess.DEVNULL if path else None,
            env=worker_env(DEFAULT_THREADS, lib_dir=lib_dir))
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "probe_failed", "engine": ENGINE}
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    else:
        text = done.stdout or ""
    if not text.strip():
        return {"available": False, "reason": "probe_failed", "engine": ENGINE}
    payload = parse_worker_output(text)
    payload.setdefault("engine", ENGINE)
    return payload


def empty_result(reason: str, *, job: dict | None = None) -> dict:
    return {"ok": False, "reason": reason, "text": "", "segments": [],
            "like_ok": False, "quality": {}, "job": job or {},
            "note": brief_reason(reason)}


# --- remote executor -----------------------------------------------------------------
#
# The authoritative transcriber may live on another machine (the owner's desktop) while
# the plugin runs in the cloud. In that case only bvid/cid cross the wire: the remote
# side downloads the audio, decodes it and transcribes it, so no audio and no cookie ever
# leaves the machine that owns them. A failure over the wire is reported as unavailable;
# it never quietly falls back to a local model, because that would be a second runner.

REMOTE_MAX_BYTES = 4 * 1024 * 1024
REMOTE_PATHS = ("/transcribe",)


def remote_payload(*, bvid: str, cid: int, model: str, language: str, max_seconds: float,
                   reading_id: str = "", revision: int = 0) -> dict:
    """The whole request body: identifiers and knobs, never audio or credentials."""
    return {
        "bvid": str(bvid), "cid": int(cid or 0), "model": str(model),
        "language": str(language), "max_seconds": float(max_seconds),
        "reading_id": str(reading_id)[:40], "revision": int(revision or 0),
        "engine": ENGINE, "compute_type": DEFAULT_COMPUTE_TYPE, "device": DEFAULT_DEVICE,
    }


def parse_remote(text: str) -> dict:
    """Normalise the remote answer into the same shape `parse_worker_output` returns."""
    try:
        payload = json.loads(text or "")
    except ValueError:
        return {"ok": False, "reason": "remote_bad_response", "segments": []}
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "remote_bad_response", "segments": []}
    if str(payload.get("reason") or "") in ("too_long", "declared_too_long", "decoded_too_long"):
        return {"ok": False, "reason": "too_long", "segments": []}
    return parse_worker_output(json.dumps(payload, ensure_ascii=False))

