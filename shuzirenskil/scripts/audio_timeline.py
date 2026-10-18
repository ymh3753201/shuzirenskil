"""Sample-exact local master-audio planning/slicing; no TTS, upload or lip-sync claim."""
from __future__ import annotations

import math
import re
import wave
from pathlib import Path
from _common import sha256_file, atomic_write_json
from model_registry import allowed_durations
from planner import ends_complete_sentence


def _text(value: str) -> str:
    # Ignore whitespace only: numbers, names and punctuation stay locked.
    return re.sub(r"\s+", "", value)


def master_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as source:
            if source.getcomptype() != "NONE" or source.getsampwidth() != 2 or source.getnchannels() != 1:
                raise ValueError("主音轨需为未压缩 16-bit 单声道 WAV；先显式转换和试听确认，不能静默重采样")
            frames, rate = source.getnframes(), source.getframerate()
            if rate not in {16000, 24000, 44100, 48000} or not 0 < frames <= rate * 3600:
                raise ValueError("主音轨采样率须为 16/24/44.1/48kHz，单章不超过 1 小时")
            # Read through to detect truncated WAVs; streaming bounds memory use.
            actual = 0
            while chunk := source.readframes(rate * 10):
                actual += len(chunk) // 2
            if actual != frames:
                raise ValueError("WAV 数据被截断")
    except (wave.Error, EOFError) as exc:
        raise ValueError("主音轨不是可读取的 PCM WAV") from exc
    return {"path": str(path.resolve()), "sha256": sha256_file(path), "sample_rate": rate,
            "sample_count": frames, "duration_seconds": frames / rate, "channels": 1, "sample_width": 2}


def plan_audio(master: Path, cue_sheet: dict, profile: dict, script: str, break_after_cues: list[int] | None = None) -> dict:
    info = master_info(master)
    if cue_sheet.get("audio_sha256") != info["sha256"]:
        raise ValueError("句子时间码没有绑定当前音频 SHA-256")
    if cue_sheet.get("alignment_reviewed") is not True:
        raise ValueError("时间码必须在实际音频上核对后声明 alignment_reviewed=true；文字估时不能冒充真实对齐")
    cues = cue_sheet.get("cues")
    if not isinstance(cues, list) or not 1 <= len(cues) <= 2000:
        raise ValueError("时间码需要 1–2000 个完整句子")
    normalized = []
    previous_end = 0
    for cue in cues:
        if not isinstance(cue, dict) or set(cue) != {"start_seconds", "end_seconds", "text"}:
            raise ValueError("每个时间码需要 start_seconds、end_seconds、text")
        if any(type(cue[k]) not in (int, float) or not math.isfinite(cue[k]) for k in ("start_seconds", "end_seconds")):
            raise ValueError("音频时间码必须为有限数字")
        start, end = [round(cue[k] * info["sample_rate"]) for k in ("start_seconds", "end_seconds")]
        if not previous_end <= start < end <= info["sample_count"]:
            raise ValueError("句子时间码重叠、倒序或超过主音轨")
        if not isinstance(cue["text"], str) or not ends_complete_sentence(cue["text"]):
            raise ValueError("音频按完整句子切分；时间码中的台词需要完整标点")
        normalized.append({"start_sample": start, "end_sample": end, "text": cue["text"]})
        previous_end = end
    if _text("".join(c["text"] for c in normalized)) != _text(script):
        raise ValueError("时间码台词与确认稿不一致，不能遗漏或替换台词")
    # Split only in reviewed inter-sentence gaps; retain all leading/trailing samples.
    cuts = [0] + [(a["end_sample"] + b["start_sample"]) // 2 for a, b in zip(normalized, normalized[1:])] + [info["sample_count"]]
    boundaries = set(break_after_cues or [])
    if any(type(x) is not int or not 1 <= x < len(normalized) for x in boundaries):
        raise ValueError("分镜句子边界无效")
    choices = allowed_durations(profile)
    n = len(normalized)
    best = [None] * (n + 1)
    best[0] = (0, 0, None)
    for end in range(1, n + 1):
        for start in range(end - 1, -1, -1):
            if any(start < point < end for point in boundaries):
                break
            duration = (cuts[end] - cuts[start]) / info["sample_rate"]
            request = next((v for v in choices if v + 1e-9 >= duration), None)
            if request is None:
                break
            if best[start] is None:
                continue
            candidate = (best[start][0] + 1, best[start][1] + request, (start, request))
            if best[end] is None or candidate[:2] < best[end][:2]:
                best[end] = candidate
    if best[n] is None:
        raise ValueError("完整句子与保留的音频间隔超过单次时长；请修改音轨或改选更长时长模型")
    segments = []
    end = n
    while end:
        start, request = best[end][2]
        segments.append({"cue_start": start + 1, "cue_end": end, "start_sample": cuts[start], "end_sample": cuts[end], "request_seconds": request,
                         "script": " ".join(c["text"] for c in normalized[start:end]),
                         "use_seconds": (cuts[end] - cuts[start]) / info["sample_rate"],
                         "planned_start_seconds": cuts[start] / info["sample_rate"],
                         "planned_end_seconds": cuts[end] / info["sample_rate"]})
        end = start
    segments.reverse()
    for index, segment in enumerate(segments, 1):
        segment["index"] = index
    return {"planner_version": "audio-1.0", "master": info, "segments": segments,
            "timeline_basis": "reviewed_master_audio_samples", "automatic_tail_silence_trim": False,
            "minimum_paid_segment_count": len(segments),
            "planned_request_total_seconds": sum(s["request_seconds"] for s in segments),
            "delivery_audio_seconds": info["duration_seconds"],
            "requires_audio_driver_or_lipsync": True, "lip_sync_verified": False}


def slice_master(plan: dict, output_dir: Path) -> dict:
    """Materialize a validated plan without dropping/duplicating a single PCM sample."""
    info = plan["master"]
    source_path = Path(info["path"])
    current = master_info(source_path)
    if current != info:
        raise ValueError("主音轨已发生变化，请重新规划")
    cursor = 0
    for seg in plan["segments"]:
        if type(seg.get("start_sample")) is not int or type(seg.get("end_sample")) is not int:
            raise ValueError("音频区间必须是整数样本位置")
        if seg["start_sample"] != cursor or not cursor < seg["end_sample"] <= info["sample_count"]:
            raise ValueError("音频区间有缺口、重叠或越界")
        cursor = seg["end_sample"]
    if cursor != info["sample_count"]:
        raise ValueError("音频区间没有覆盖完整主音轨")
    output_dir.mkdir(parents=True, exist_ok=False)
    outputs = []
    with wave.open(str(source_path), "rb") as source:
        for index, segment in enumerate(plan["segments"], 1):
            dest = output_dir / f"audio_{index:04d}.wav"
            remaining = segment["end_sample"] - segment["start_sample"]
            with wave.open(str(dest), "wb") as writer:
                writer.setparams(source.getparams())
                while remaining:
                    count = min(remaining, info["sample_rate"] * 10)
                    raw = source.readframes(count)
                    if len(raw) != count * 2:
                        raise ValueError("切分时主音轨读取不完整")
                    writer.writeframesraw(raw)
                    remaining -= count
            outputs.append({"index": index, "path": str(dest.resolve()), "sha256": sha256_file(dest),
                            "start_sample": segment["start_sample"], "end_sample": segment["end_sample"]})
    result = {"status": "local_audio_slices_ready", "master_sha256": info["sha256"],
              "sample_count": cursor, "sample_rate": info["sample_rate"], "slices": outputs,
              "video_generated": False, "lip_sync_verified": False}
    atomic_write_json(output_dir / "audio-manifest.json", result)
    return result
