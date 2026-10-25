"""Media decoding: fetch a 480p stream, then cut frames in an isolated subprocess.

The plugin process never imports a decoder. Frames are produced by an external
`ffmpeg` in a child process with a hard timeout, one still image per requested
timestamp, scaled down and re-encoded as JPEG so a model receives small pictures.
When no decoder is installed the caller is told `decoder_missing` — a `videoshot`
contact sheet is only used when the owner explicitly enabled that source.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from pathlib import Path

MAX_WIDTH = 854
FRAME_MAX_BYTES = 2 * 1024 * 1024
_JPEG_QUALITY = "4"
_PTS = re.compile(r"pts_time:\s*([0-9.]+)")

# Directories that commonly hold a portable build on Windows; never searched broadly.
_HINTS = (
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
)


def _names(name: str) -> tuple[str, ...]:
    """Executable names to try, native spelling first.

    Both spellings are tried on every platform: a Windows build dropped next to a
    POSIX one (or a config copied between them) should still be found.
    """
    if os.name == "nt":
        return (f"{name}.exe", name)
    return (name, f"{name}.exe")


def find_tool(name: str, configured: str | None = None) -> str | None:
    """Locate ffmpeg/ffprobe: configured path, then PATH, then two fixed hint dirs."""
    if configured:
        candidate = Path(configured)
        if candidate.is_dir():
            for exe in _names(name):
                probe = candidate / exe
                if probe.is_file():
                    return str(probe)
        elif candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    for hint in _HINTS:
        for exe in _names(name):
            candidate = Path(hint) / exe
            if candidate.is_file():
                return str(candidate)
    return None


def jpeg_size(path: Path) -> tuple[int, int]:
    """Read width/height straight from the JPEG SOF marker (no Pillow needed)."""
    try:
        data = path.read_bytes()[:65536]
    except OSError:
        return 0, 0
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return 0, 0
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            return 0, 0
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + length
    return 0, 0


class MediaWorker:
    """Frame extraction with a hard cap on frames, bytes, dimensions and time."""

    def __init__(self, root, *, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.root = Path(root)
        self.ffmpeg = find_tool("ffmpeg", ffmpeg)
        self.ffprobe = find_tool("ffprobe", ffprobe)

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    def status(self) -> dict:
        return {"ffmpeg": self.ffmpeg or "", "ffprobe": self.ffprobe or "",
                "decoder": bool(self.ffmpeg)}

    def job_dir(self, job_id: str) -> Path:
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def extract(self, source, times_ms, *, job_id: str, max_frames: int = 40,
                      frame_max_bytes: int = FRAME_MAX_BYTES, timeout: float = 180) -> dict:
        """Cut one JPEG per timestamp. Returns frames plus a reason when nothing came out."""
        source = Path(source)
        if not self.available:
            return {"frames": [], "reason": "decoder_missing"}
        if not source.is_file():
            return {"frames": [], "reason": "source_missing"}
        wanted = [max(0, int(t)) for t in (times_ms or [])][:max(1, int(max_frames))]
        out_dir = self.job_dir(job_id)
        frames, loop = [], asyncio.get_running_loop()
        deadline = loop.time() + float(timeout)
        for idx, target in enumerate(wanted):
            if loop.time() >= deadline:
                return {"frames": frames, "reason": "timeout" if not frames else ""}
            dest = out_dir / f"f{idx:02d}.jpg"
            remaining = max(5.0, min(60.0, deadline - loop.time()))
            try:
                actual = await asyncio.wait_for(
                    self._one(source, target, dest, remaining), timeout=remaining + 5)
            except asyncio.TimeoutError:
                dest.unlink(missing_ok=True)
                continue
            if not dest.is_file():
                continue
            size = dest.stat().st_size
            if size <= 0 or size > int(frame_max_bytes):
                dest.unlink(missing_ok=True)
                continue
            width, height = jpeg_size(dest)
            if width <= 0 or height <= 0 or width > MAX_WIDTH + 2:
                dest.unlink(missing_ok=True)
                continue
            frames.append({
                "idx": len(frames), "target_ms": target, "actual_ms": actual,
                "path": str(dest), "bytes": size, "width": width, "height": height,
                "digest": hashlib.sha256(dest.read_bytes()).hexdigest()[:32],
                "source": "ffmpeg",
            })
        return {"frames": frames, "reason": "" if frames else "decode_failed"}

    async def _one(self, source: Path, target_ms: int, dest: Path, budget: float) -> int:
        """One isolated ffmpeg run: seek, take a single frame, scale, write JPEG.

        stdio goes to a file, never to a pipe: file handles survive confined hosts
        (a sandboxed process cannot create the anonymous pipes asyncio would need),
        and a log on disk cannot deadlock on a chatty decoder.
        """
        need = max(0.0, target_ms / 1000.0)
        args = [
            self.ffmpeg, "-nostdin", "-hide_banner", "-v", "info",
            "-ss", f"{need:.3f}", "-i", str(source),
            "-frames:v", "1", "-an", "-sn", "-dn",
            "-vf", f"scale='min({MAX_WIDTH},iw)':-2,showinfo",
            "-q:v", _JPEG_QUALITY, "-y", str(dest),
        ]
        log = dest.with_suffix(".log")
        try:
            await self._run(args, budget, log_path=log)
        finally:
            text = ""
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            log.unlink(missing_ok=True)
        hit = _PTS.search(text)
        if hit:
            try:
                # Input seek resets the timeline, so showinfo reports an offset from
                # the cut point: the real moment is the target plus that offset.
                offset = float(hit.group(1))
                if -1.0 <= offset <= 5.0:
                    return int(round(target_ms + offset * 1000))
            except ValueError:
                pass
        return int(target_ms)

    async def extract_audio(self, source, dest, *, timeout: float = 180,
                            sample_rate: int = 16000) -> dict:
        """Decode one media file to 16 kHz mono PCM WAV for the ASR worker.

        A muxed video container is accepted on purpose (`-vn` drops the picture), so a
        part whose video stream was already fetched can be reused. The returned
        `seconds` comes from the written PCM size, not from metadata: callers use it
        to cross-check what the API claimed.
        """
        source, dest = Path(source), Path(dest)
        if not self.available:
            return {"ok": False, "reason": "decoder_missing", "seconds": 0.0}
        if not source.is_file():
            return {"ok": False, "reason": "source_missing", "seconds": 0.0}
        dest.parent.mkdir(parents=True, exist_ok=True)
        rate = max(4000, int(sample_rate))
        args = [self.ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
                "-i", str(source), "-vn", "-sn", "-dn",
                "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le",
                "-f", "wav", "-y", str(dest)]
        log = dest.with_suffix(".ffmpeg.log")
        try:
            code = await self._run(args, timeout, log_path=log)
        except asyncio.TimeoutError:
            dest.unlink(missing_ok=True)
            return {"ok": False, "reason": "timeout", "seconds": 0.0}
        except OSError:
            dest.unlink(missing_ok=True)
            return {"ok": False, "reason": "exec_failed", "seconds": 0.0}
        finally:
            log.unlink(missing_ok=True)
        try:
            size = dest.stat().st_size
        except OSError:
            size = 0
        if code != 0 or size <= 44:
            dest.unlink(missing_ok=True)
            return {"ok": False, "reason": "decode_failed", "seconds": 0.0}
        # PCM16 mono: 2 bytes per sample, minus the 44-byte WAV header.
        seconds = round(max(0.0, (size - 44) / (2.0 * rate)), 2)
        return {"ok": True, "reason": "", "seconds": seconds, "path": str(dest)}

    async def _run(self, args, timeout: float, *, log_path: Path) -> int:
        """Run one tool with file-backed stdio; raises TimeoutError on overrun."""
        sink_path = Path(log_path)
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sink_path, "wb") as sink:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=sink, stderr=sink)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise
        return int(process.returncode or 0)

    async def probe_duration(self, source, timeout: float = 15) -> float:
        """Container duration in seconds; 0 when ffprobe is missing or unhappy."""
        if not self.ffprobe or not Path(source).is_file():
            return 0.0
        args = [self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(source)]
        log = self.job_dir("_probe") / "duration.txt"
        try:
            await self._run(args, timeout, log_path=log)
            return round(float(log.read_text(encoding="utf-8", errors="replace").strip() or 0), 2)
        except (asyncio.TimeoutError, ValueError, OSError):
            return 0.0
        finally:
            log.unlink(missing_ok=True)
