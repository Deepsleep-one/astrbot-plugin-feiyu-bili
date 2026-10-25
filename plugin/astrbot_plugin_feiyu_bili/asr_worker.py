"""Isolated CPU+INT8 speech recognition worker.

Run as a separate process by `asr.py`; never imported by the plugin itself. It
loads one model for one clip, writes a JSON result, and exits so the memory is
released. Nothing here is allowed to switch device, change model, or fetch a
different (larger) weight set on its own: `--allow-download` defaults to off, so a
missing model is reported as `model_missing` instead of being downloaded silently.

    python asr_worker.py --probe
    python asr_worker.py --audio job.wav --out job.json \
        --model small --compute-type int8 --device cpu --threads 2 --language zh
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ENGINE = "faster-whisper"
MAX_SECONDS = 1799
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ENGINE_MISSING = 3
EXIT_TOO_LONG = 4
EXIT_MODEL_MISSING = 5


def _emit(payload: dict, out: str = "") -> None:
    text = json.dumps(payload, ensure_ascii=False)
    print(text, flush=True)
    if out:
        try:
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


def probe(out: str = "") -> int:
    try:
        import faster_whisper  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any import failure means "not usable"
        _emit({"ok": True, "available": False, "engine": ENGINE,
               "reason": "engine_missing", "detail": type(exc).__name__}, out)
        return EXIT_OK
    version = ""
    try:
        from faster_whisper import __version__ as version  # type: ignore
    except Exception:  # noqa: BLE001
        version = ""
    _emit({"ok": True, "available": True, "engine": ENGINE,
           "version": str(version or "")[:40], "device": "cpu",
           "compute_type": "int8", "max_seconds": MAX_SECONDS,
           "python": f"{sys.version_info.major}.{sys.version_info.minor}"}, out)
    return EXIT_OK


def wav_seconds(path: str) -> float:
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0.0
    return max(0.0, (size - 44) / 32000.0)  # 16 kHz mono PCM16


def resolve_model(model: str, model_dir: str) -> str:
    """Turn the configured size name into the directory that actually holds the weights.

    Two layouts are accepted, and nothing else: either `model` is already a directory
    of weights, or `model_dir/faster-whisper-<size>/` is. A bare size name falls back
    to the library's own cache lookup, which is only reachable when downloads are
    explicitly allowed.
    """
    if model and os.path.isdir(model):
        return model
    if model_dir:
        candidate = os.path.join(model_dir, "faster-whisper-%s" % model)
        if os.path.isdir(candidate):
            return candidate
        if os.path.exists(os.path.join(model_dir, "model.bin")):
            return model_dir
    return model


def run(args) -> int:
    seconds = wav_seconds(args.audio)
    if seconds <= 0:
        _emit({"ok": False, "reason": "decoded_empty", "engine": ENGINE}, args.out)
        return EXIT_FAILED
    if seconds > float(args.max_seconds):
        _emit({"ok": False, "reason": "too_long", "engine": ENGINE,
               "duration_ms": int(seconds * 1000)}, args.out)
        return EXIT_TOO_LONG
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "reason": "engine_missing", "engine": ENGINE,
               "detail": type(exc).__name__}, args.out)
        return EXIT_ENGINE_MISSING

    source = resolve_model(args.model, args.model_dir)
    if not args.allow_download and not os.path.isdir(source) and not os.path.exists(source):
        _emit({"ok": False, "reason": "model_missing", "engine": ENGINE,
               "model": args.model, "detail": "weights not found and download not allowed"},
              args.out)
        return EXIT_MODEL_MISSING

    load_start = time.time()
    try:
        model = WhisperModel(
            source,
            device=args.device,
            compute_type=args.compute_type,
            cpu_threads=max(1, int(args.threads)),
            download_root=args.model_dir or None,
            local_files_only=not args.allow_download,
        )
    except Exception as exc:  # noqa: BLE001
        reason = "model_missing" if "local" in str(exc).lower() or "download" in str(exc).lower() else "load_failed"
        _emit({"ok": False, "reason": reason, "engine": ENGINE,
               "detail": type(exc).__name__, "message": str(exc)[:300]}, args.out)
        return EXIT_MODEL_MISSING if reason == "model_missing" else EXIT_FAILED
    load_seconds = round(time.time() - load_start, 2)

    started = time.time()
    try:
        segments, info = model.transcribe(
            args.audio,
            language=args.language or None,
            task="transcribe",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        rows = []
        for seg in segments:
            text = (getattr(seg, "text", "") or "").strip()
            if not text:
                continue
            rows.append({
                "from_ms": int(round(float(getattr(seg, "start", 0.0)) * 1000)),
                "to_ms": int(round(float(getattr(seg, "end", 0.0)) * 1000)),
                "text": text[:300],
                "avg_logprob": getattr(seg, "avg_logprob", None),
                "no_speech": getattr(seg, "no_speech_prob", None),
            })
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "reason": "transcribe_failed", "engine": ENGINE,
               "detail": type(exc).__name__, "message": str(exc)[:300]}, args.out)
        return EXIT_FAILED
    run_seconds = round(time.time() - started, 2)

    _emit({
        "ok": True,
        "engine": ENGINE,
        "model": args.model,
        "compute_type": args.compute_type,
        "device": args.device,
        "threads": int(args.threads),
        "language": str(getattr(info, "language", args.language or "") or "")[:16],
        "duration_ms": int(round(float(getattr(info, "duration", seconds) or seconds) * 1000)),
        "audio_seconds": round(seconds, 2),
        "load_seconds": load_seconds,
        "run_seconds": run_seconds,
        "segments": rows,
    }, args.out)
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CPU+INT8 ASR worker (one clip, then exit)")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--audio", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--model", default="small")
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--max-seconds", type=float, default=float(MAX_SECONDS))
    parser.add_argument("--allow-download", action="store_true")
    args = parser.parse_args(argv)
    if args.probe:
        return probe(args.out)
    if args.device != "cpu":
        _emit({"ok": False, "reason": "device_not_allowed"}, args.out)
        return EXIT_FAILED
    if not args.audio:
        _emit({"ok": False, "reason": "no_audio"}, args.out)
        return EXIT_FAILED
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
