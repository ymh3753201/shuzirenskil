#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import bisect
from fractions import Fraction
from pathlib import Path
from typing import Any
from _common import atomic_write_json, sha256_file


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"缺少本地命令: {name}")
    return path


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def probe(path: Path) -> dict[str, Any]:
    ffprobe = require_command("ffprobe")
    result = run([
        ffprobe,
        "-v", "error",
        "-show_streams",
        "-show_format",
        "-of", "json",
        str(path),
    ])
    return json.loads(result.stdout)


def technical_summary(path: Path) -> dict[str, Any]:
    data = probe(path)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    return {
        "path": str(path.resolve()),
        "duration_seconds": round(duration, 3),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "frame_rate": video.get("r_frame_rate") if video else None,
        "average_frame_rate": video.get("avg_frame_rate") if video else None,
    }


def detect_tail_silence(path: Path, noise_db: str = "-35dB", min_duration: float = 0.25) -> dict[str, float | bool]:
    """用 FFmpeg 的 silencedetect 找最后一段连续静音，不改动源文件。"""
    ffmpeg = require_command("ffmpeg")
    duration = technical_summary(path)["duration_seconds"]
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path), "-af", f"silencedetect=noise={noise_db}:d={min_duration}", "-f", "null", "-"],
        text=True,
        capture_output=True,
        check=False,
    )
    log = f"{result.stdout}\n{result.stderr}"
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    last_start = starts[-1] if starts else None
    last_end = ends[-1] if ends else None
    tail_start = None
    if last_start is not None and (last_end is None or last_end >= duration - 0.05 or last_start > last_end):
        tail_start = last_start
    tail_seconds = max(0.0, duration - tail_start) if tail_start is not None else 0.0
    return {
        "duration_seconds": round(duration, 3),
        "tail_silence_seconds": round(tail_seconds, 3),
        "has_excess_tail_silence": tail_seconds > 0.6,
        "last_silence_start_seconds": round(last_start, 3) if last_start is not None else None,
    }


def analyze_speech_pacing(path: Path, script: str, planned_cpm: int) -> dict[str, Any]:
    """量化静音和整体交付节奏；只提供复核线索，不替代正常速度实听。"""
    duration = technical_summary(path)["duration_seconds"]
    result = subprocess.run(
        [require_command("ffmpeg"), "-hide_banner", "-i", str(path), "-af", "silencedetect=noise=-35dB:d=0.15", "-f", "null", "-"],
        text=True, capture_output=True, check=False,
    )
    log = result.stdout + "\n" + result.stderr
    starts = [float(item) for item in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(item) for item in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    intervals = [(start, ends[index] if index < len(ends) else duration) for index, start in enumerate(starts)]
    opening = intervals[0][1] if intervals and intervals[0][0] <= 0.05 else 0.0
    tail = duration - intervals[-1][0] if intervals and intervals[-1][1] >= duration - 0.05 else 0.0
    speech_window = max(0.0, duration - opening - tail)
    internal = [
        {"start_seconds": round(start, 3), "duration_seconds": round(end - start, 3)}
        for start, end in intervals if start >= opening - 0.01 and end <= duration - tail + 0.01
        and start > opening + 0.01 and end < duration - tail - 0.01
    ]
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", script))
    return {
        "method": "ffmpeg silencedetect -35dB, minimum 0.15s; Chinese character count excludes Latin words",
        "planned_cpm": planned_cpm,
        "cjk_count": cjk_count,
        "speech_window_seconds": round(speech_window, 3),
        "opening_silence_seconds": round(opening, 3),
        "tail_silence_seconds": round(tail, 3),
        "internal_silence_seconds": round(sum(item["duration_seconds"] for item in internal), 3),
        "long_internal_pauses": [item for item in internal if item["duration_seconds"] >= 0.75],
        "delivery_cjk_cpm": round(cjk_count * 60 / speech_window, 1) if speech_window else None,
        "needs_listening_review": len([item for item in internal if item["duration_seconds"] >= 0.75]) >= 2,
    }


def trim_tail_silence(path: Path, output: Path, work_dir: Path, keep_seconds: float = 0.3) -> dict[str, Any]:
    """只移除片段末尾连续静音，保留短暂自然收尾；中间停顿不会被处理。"""
    summary = technical_summary(path)
    silence = detect_tail_silence(path)
    tail = float(silence["tail_silence_seconds"])
    if tail <= 0.6:
        return {"source": str(path.resolve()), "output": str(path.resolve()), "trimmed": False, **silence}
    start = float(silence["last_silence_start_seconds"] or summary["duration_seconds"])
    trimmed_duration = max(0.1, min(summary["duration_seconds"], start + keep_seconds))
    ffmpeg = require_command("ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg, "-y", "-i", str(path), "-t", f"{trimmed_duration:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output),
    ])
    result = technical_summary(output)
    return {
        "source": str(path.resolve()),
        "output": str(output.resolve()),
        "trimmed": True,
        "original_duration_seconds": summary["duration_seconds"],
        "trimmed_duration_seconds": result["duration_seconds"],
        "removed_tail_silence_seconds": round(max(0.0, summary["duration_seconds"] - result["duration_seconds"]), 3),
        **silence,
    }


def image_summary(path: Path) -> dict[str, Any]:
    data = probe(path)
    stream = next((item for item in data.get("streams", []) if item.get("codec_type") == "video"), None)
    if not stream or not stream.get("width") or not stream.get("height"):
        raise ValueError(f"图片无法解码或没有有效尺寸: {path}")
    return {
        "path": str(path.resolve()),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "codec": stream.get("codec_name"),
        "format": data.get("format", {}).get("format_name"),
    }


def validate_reference_image(path: Path, aspect_ratio: str) -> dict[str, Any]:
    summary = image_summary(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError(f"生产参考图不能超过 20MB: {path}")
    suffix = path.suffix.lower()
    codec_by_suffix = {".png": {"png"}, ".jpg": {"mjpeg"}, ".jpeg": {"mjpeg"}, ".webp": {"webp"}}
    if suffix not in codec_by_suffix or summary["codec"] not in codec_by_suffix[suffix]:
        raise ValueError(f"图片内容与扩展名不匹配，支持 PNG、JPEG、WebP: {path}")
    expected = 9 / 16 if aspect_ratio == "9:16" else 16 / 9
    actual = summary["width"] / summary["height"]
    if min(summary["width"], summary["height"]) < 512:
        raise ValueError(f"生产参考图短边至少需要 512 像素: {path}")
    if abs(actual - expected) > 0.015:
        raise ValueError(f"生产参考图比例必须与视频 {aspect_ratio} 一致，避免接口拉伸人物: {path}")
    return summary


def normalize_reference_image(source: Path, output: Path, aspect_ratio: str) -> dict[str, Any]:
    """把图片居中裁成视频画幅，不拉伸人物。"""
    summary = image_summary(source)
    expected = 9 / 16 if aspect_ratio == "9:16" else 16 / 9
    width, height = summary["width"], summary["height"]
    if width / height > expected:
        crop_width = max(2, int((height * expected) // 2) * 2)
        crop_height = height
    else:
        crop_width = width
        crop_height = max(2, int((width / expected) // 2) * 2)
    output.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = require_command("ffmpeg")
    run([
        ffmpeg, "-y", "-i", str(source),
        "-vf", f"crop={crop_width}:{crop_height}:(iw-{crop_width})/2:(ih-{crop_height})/2",
        "-frames:v", "1", str(output),
    ])
    return validate_reference_image(output, aspect_ratio)


def _target_size(aspect_ratio: str) -> tuple[int, int]:
    if aspect_ratio == "9:16":
        return 720, 1280
    if aspect_ratio == "16:9":
        return 1280, 720
    raise ValueError(f"不支持的画幅: {aspect_ratio}")


def stitch(clips: list[Path], output: Path, work_dir: Path, aspect_ratio: str) -> dict[str, Any]:
    if not clips:
        raise ValueError("没有可拼接片段")
    ffmpeg = require_command("ffmpeg")
    width, height = _target_size(aspect_ratio)
    normalized_dir = work_dir / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    trimmed_dir = work_dir / "trimmed"
    trimmed_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    input_summaries: list[dict[str, Any]] = []
    tail_trim_reports: list[dict[str, Any]] = []
    source_frame_rate: Fraction | None = None
    for index, clip in enumerate(clips, start=1):
        summary = technical_summary(clip)
        input_summaries.append(summary)
        if not summary["has_video"] or not summary["has_audio"]:
            raise ValueError(f"片段缺少视频流或音频流: {clip}")
        rate = Fraction(summary["frame_rate"] or "0/1")
        if rate <= 0:
            raise ValueError(f"无法识别片段帧率: {clip}")
        if source_frame_rate is not None and rate != source_frame_rate:
            raise ValueError("多段源视频帧率不同；需检查 Provider 原片，不能静默插帧拼接")
        source_frame_rate = rate
        trimmed_path = trimmed_dir / f"clip_{index:03d}.mp4"
        trim_report = trim_tail_silence(clip, trimmed_path, trimmed_dir)
        tail_trim_reports.append(trim_report)
        source_for_normalize = Path(trim_report["output"])
        target = normalized_dir / f"clip_{index:03d}.mkv"
        filter_value = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p"
        )
        run([
            ffmpeg, "-y", "-i", str(source_for_normalize),
            "-vf", filter_value,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
            str(target),
        ])
        normalized.append(target)

    concat_file = work_dir / "concat.txt"
    concat_lines: list[str] = []
    for item in normalized:
        absolute = item.resolve().as_posix().replace("'", "'\\''")
        concat_lines.append(f"file '{absolute}'\n")
    concat_file.write_text("".join(concat_lines), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(output),
    ])
    return {
        "method": "normalized_concat_no_crossfade",
        "input_clips": [str(item.resolve()) for item in clips],
        "input_summaries": input_summaries,
        "source_frame_rate": str(source_frame_rate),
        "tail_trim_reports": tail_trim_reports,
        "normalized_clips": [str(item.resolve()) for item in normalized],
        "effective_input_summaries": [technical_summary(item) for item in normalized],
        "output": technical_summary(output),
    }


def retime_short_exact_candidate(
    video: Path,
    work_dir: Path,
    target_seconds: float,
    tolerance_seconds: float,
    max_slowdown: float = 1.25,
) -> dict[str, Any]:
    """Slow one clean clip within a small bound instead of keeping long silence."""
    summary = technical_summary(video)
    before = float(summary["duration_seconds"])
    if before >= target_seconds - tolerance_seconds:
        return {"applied": False, "reason": "already_within_exact_tolerance", "before_seconds": before}
    desired = target_seconds - min(0.5, tolerance_seconds / 2)
    factor = desired / before if before > 0 else float("inf")
    if factor > max_slowdown:
        return {
            "applied": False,
            "reason": "required_slowdown_exceeds_limit",
            "before_seconds": before,
            "required_factor": round(factor, 4),
            "max_factor": max_slowdown,
        }
    work_dir.mkdir(parents=True, exist_ok=True)
    candidate = work_dir / "exact-retimed.mp4"
    ffmpeg = require_command("ffmpeg")
    run([
        ffmpeg, "-y", "-i", str(video),
        "-filter_complex", f"[0:v]setpts={factor:.7f}*(PTS-STARTPTS)[v];[0:a]atempo={1 / factor:.7f}[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(candidate),
    ])
    after = technical_summary(candidate)
    tail = detect_tail_silence(candidate)
    if abs(after["duration_seconds"] - target_seconds) > tolerance_seconds or tail["tail_silence_seconds"] > 0.6:
        raise ValueError("本地限幅调速后仍不满足时长或尾部静音要求；保留原始干净视频等待人工处理")
    source_copy = work_dir / "before-exact-retime.mp4"
    shutil.copy2(video, source_copy)
    os.replace(candidate, video)
    return {
        "applied": True,
        "method": "bounded_video_slowdown_with_pitch_preserving_audio",
        "before_seconds": before,
        "after_seconds": after["duration_seconds"],
        "slowdown_factor": round(factor, 4),
        "tail_silence_seconds": tail["tail_silence_seconds"],
        "original_clean_copy": str(source_copy.resolve()),
    }


def extract_review_frames(video: Path, output_dir: Path, extra_times: list[float] | None = None) -> list[str]:
    """真实帧索引取样：首末帧、全程每半秒、末两秒每四分之一秒和接缝。"""
    ffmpeg = require_command("ffmpeg")
    data = json.loads(run([require_command("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_frames", "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(video)]).stdout)
    stamps = [float(frame["best_effort_timestamp_time"]) for frame in data.get("frames", [])]
    if not stamps:
        raise ValueError("视频中没有可提取的真实画面帧")
    times = [stamp - stamps[0] for stamp in stamps]
    last_time = times[-1]
    selected: dict[int, set[str]] = {0: {"first"}, len(times) - 1: {"last"}}
    selected[0].add("first")
    def add_at(value: float, role: str) -> None:
        index = min(len(times) - 1, bisect.bisect_left(times, max(0.0, value)))
        selected.setdefault(index, set()).add(role)
    for index in range(int(last_time * 2) + 1):
        add_at(index / 2, "sample")
    for index in range(9):
        add_at(max(0.0, last_time - 2.0) + index / 4, "tail")
    for value in extra_times or []:
        add_at(value - 0.12, "boundary")
        add_at(value + 0.12, "boundary")
    output_dir.mkdir(parents=True, exist_ok=True)
    indices = sorted(selected)
    expression = "+".join(f"eq(n\\,{index})" for index in indices)
    run([ffmpeg, "-y", "-i", str(video), "-vf", "select=" + expression, "-vsync", "0", "-q:v", "2", str(output_dir / "review_%05d.jpg")])
    records = []
    for number, index in enumerate(indices, 1):
        target = output_dir / f"review_{number:05d}.jpg"
        if not target.is_file() or not target.stat().st_size:
            raise ValueError("实际抽帧数量不足，不能记录为完成视觉取样")
        records.append({"path": str(target.resolve()), "sha256": sha256_file(target), "frame_index": index,
            "time_seconds": round(times[index], 6), "roles": sorted(selected[index])})
    atomic_write_json(output_dir / "manifest.json", {"schema_version": "1.0", "video": str(video.resolve()),
        "video_sha256": sha256_file(video), "total_video_frames": len(times), "sampling": "2fps_plus_dense_tail_and_exact_first_last",
        "frames": records, "note": "取样不代表全片无文字；须连续查看全片，补看每张取样和真实末帧。"})
    return [record["path"] for record in records]


def parse_srt_bounds(path: Path) -> tuple[float, float]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    matches = re.findall(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", text)
    if not matches:
        raise ValueError("SRT 中没有可识别时间轴")

    def seconds(values: tuple[str, ...], offset: int) -> float:
        return int(values[offset]) * 3600 + int(values[offset + 1]) * 60 + int(values[offset + 2]) + int(values[offset + 3]) / 1000

    return seconds(matches[0], 0), seconds(matches[-1], 4)


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
