#!/usr/bin/env python3
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache


STRONG_SENTENCE_RE = re.compile(r"[^。！？!?]+[。！？!?]+|[^。！？!?]+$")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
MIN_EXACT_SPEECH_FILL = 0.86  # Legacy v1 warning only; v2 evaluates unused seconds.


@dataclass(frozen=True)
class PlannedSegment:
    index: int
    script: str
    estimated_speech_seconds: float
    request_seconds: int

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "script": self.script,
            "estimated_speech_seconds": self.estimated_speech_seconds,
            "request_seconds": self.request_seconds,
            "ends_on_complete_sentence": bool(re.search(r"[。！？!?]$", self.script)),
        }


def normalize_script(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    normalized = normalize_script(text)
    return [match.group(0).strip() for match in STRONG_SENTENCE_RE.finditer(normalized) if match.group(0).strip()]


def estimate_speech_seconds(text: str, speech_rate_cpm: int = 260) -> float:
    cjk_count = len(CJK_RE.findall(text))
    latin_words = len(LATIN_WORD_RE.findall(text))
    punctuation_pause = len(re.findall(r"[，,；;：:。！？!?]", text)) * 0.14
    if not 180 <= speech_rate_cpm <= 320:
        raise ValueError("口播语速必须在每分钟 180-320 个汉字之间")
    return round(cjk_count * 60 / speech_rate_cpm + latin_words / 2.8 + punctuation_pause, 2)


def pack_sentences_minimally(sentences: list[str], durations: list[float], max_speech_seconds: float) -> tuple[list[list[str]], list[float]]:
    """按原顺序把完整句子装入尽可能少的片段；句子数不等于片段数。"""
    groups: list[list[str]] = []
    group_durations: list[float] = []
    current: list[str] = []
    current_duration = 0.0
    for sentence, duration in zip(sentences, durations):
        if current and current_duration + duration > max_speech_seconds:
            groups.append(current)
            group_durations.append(round(current_duration, 2))
            current = []
            current_duration = 0.0
        current.append(sentence)
        current_duration += duration
    if current:
        groups.append(current)
        group_durations.append(round(current_duration, 2))
    return groups, group_durations


def _exact_request_durations(delivery_seconds: int, max_request_seconds: int) -> list[int]:
    full, remainder = divmod(delivery_seconds, max_request_seconds)
    return [max_request_seconds] * full + ([remainder] if remainder else [])


def _pack_sentences_for_exact_requests(
    sentences: list[str],
    durations: list[float],
    request_durations: list[int],
) -> tuple[list[list[str]], list[float]]:
    """把完整句子按顺序分配到固定数量的请求。

    每段至少一句，且给起音和收声保留 0.6 秒。选择时尽量让各段的
    台词填充比接近，避免某一段过挤、另一段过空。
    """
    segment_count = len(request_durations)
    if len(sentences) < segment_count:
        raise ValueError(
            f"精确时长需要 {segment_count} 个完整句子分段，当前只有 {len(sentences)} 句；"
            "请补充句子或改用 content-fit 按内容时长模式"
        )
    prefix = [0.0]
    for duration in durations:
        prefix.append(prefix[-1] + duration)
    overall_fill = min(1.0, prefix[-1] / sum(max(0.1, item - 0.6) for item in request_durations))

    @lru_cache(maxsize=None)
    def solve(sentence_index: int, request_index: int) -> tuple[float, tuple[int, ...]] | None:
        if request_index == segment_count:
            return (0.0, ()) if sentence_index == len(sentences) else None
        remaining_requests = segment_count - request_index
        max_end = len(sentences) - (remaining_requests - 1)
        capacity = request_durations[request_index] - 0.6
        best: tuple[float, tuple[int, ...]] | None = None
        for end in range(sentence_index + 1, max_end + 1):
            speech_seconds = prefix[end] - prefix[sentence_index]
            if speech_seconds > capacity + 1e-9:
                break
            tail = solve(end, request_index + 1)
            if tail is None:
                continue
            fill = speech_seconds / capacity
            candidate = ((fill - overall_fill) ** 2 + tail[0], (end, *tail[1]))
            if best is None or candidate[0] < best[0]:
                best = candidate
        return best

    solution = solve(0, 0)
    if solution is None:
        raise ValueError(
            "口播稿无法在不拆半句的前提下装入精确时长分段；"
            "请调整句子长度，或改用 content-fit 模式"
        )
    groups: list[list[str]] = []
    group_durations: list[float] = []
    start = 0
    for end in solution[1]:
        groups.append(sentences[start:end])
        group_durations.append(round(prefix[end] - prefix[start], 2))
        start = end
    return groups, group_durations


def _plan_segments_v1(
    script: str,
    delivery_max_seconds: int,
    max_request_seconds: int = 15,
    duration_mode: str = "auto",
    speech_rate_cpm: int = 306,
) -> dict[str, object]:
    if delivery_max_seconds < 1:
        raise ValueError("目标时长必须大于 0 秒")
    sentences = split_sentences(script)
    if not sentences:
        raise ValueError("口播稿为空")
    if not re.search(r"[。！？!?]$", sentences[-1]):
        raise ValueError("口播稿最后一句缺少完整句子结束标点")

    if duration_mode not in {"auto", "exact", "content-fit"}:
        raise ValueError("duration_mode 只能是 auto、exact 或 content-fit")
    resolved_mode = "exact" if duration_mode == "auto" and delivery_max_seconds % max_request_seconds == 0 else duration_mode
    if resolved_mode == "auto":
        resolved_mode = "content-fit"

    # 给句首起音和句尾收声留出小余量。
    safe_speech_seconds = max_request_seconds - 0.6
    sentence_durations = [estimate_speech_seconds(sentence, speech_rate_cpm) for sentence in sentences]
    too_long = [sentences[i] for i, duration in enumerate(sentence_durations) if duration > safe_speech_seconds]
    if too_long:
        preview = too_long[0][:60]
        raise ValueError(f"存在单句预计超过 {safe_speech_seconds:.0f} 秒，不能在半句话处分段，请先改写: {preview}")

    total_estimated = round(sum(sentence_durations), 2)
    if total_estimated > delivery_max_seconds * 0.95:
        raise ValueError(
            f"口播预计 {total_estimated:.1f} 秒，超过目标成片 {delivery_max_seconds} 秒的安全范围，请先压缩稿件或延长时长"
        )

    if resolved_mode == "exact":
        request_durations = _exact_request_durations(delivery_max_seconds, max_request_seconds)
        groups, group_durations = _pack_sentences_for_exact_requests(sentences, sentence_durations, request_durations)
    else:
        groups, group_durations = pack_sentences_minimally(sentences, sentence_durations, safe_speech_seconds)
        request_durations = [min(max_request_seconds, max(1, math.ceil(item + 0.6))) for item in group_durations]

    segments: list[PlannedSegment] = []
    for index, (group, duration, request_seconds) in enumerate(zip(groups, group_durations, request_durations), start=1):
        segments.append(PlannedSegment(index, "".join(group), round(duration, 2), request_seconds))

    planned_request_total = sum(item.request_seconds for item in segments)
    speech_fill_ratio = round(total_estimated / max(1, planned_request_total), 4)
    return {
        "duration_mode_requested": duration_mode,
        "duration_mode": resolved_mode,
        "target_delivery_seconds": delivery_max_seconds,
        "delivery_max_seconds": delivery_max_seconds,
        "estimated_speech_seconds": total_estimated,
        "speech_rate_cpm": speech_rate_cpm,
        "sentence_count": len(sentences),
        "minimum_paid_segment_count": len(segments),
        "segment_count_is_minimal": True,
        "packing_strategy": (
            "ordered_complete_sentence_exact_duration_15_first"
            if resolved_mode == "exact"
            else "ordered_complete_sentence_minimum"
        ),
        "max_speech_seconds_per_segment": round(safe_speech_seconds, 2),
        "planned_request_total_seconds": planned_request_total,
        "provider_request_total_seconds": planned_request_total,
        "duration_request_policy": "prefer_15_second_requests" if resolved_mode == "exact" else "speech_duration_plus_0_6",
        "delivery_duration_tolerance_seconds": len(segments) if resolved_mode == "exact" else None,
        "speech_fill_ratio": speech_fill_ratio,
        "duration_timing_warning": (
            f"台词预计只占请求时长 {speech_fill_ratio:.0%}，低于自然口播参考下限 {MIN_EXACT_SPEECH_FILL:.0%}；请补充有用内容或改用 content-fit，不能要求模型用慢读和长停顿凑时长"
            if resolved_mode == "exact" and speech_fill_ratio < MIN_EXACT_SPEECH_FILL
            else None
        ),
        "provisional_duration_capability": True,
        "segments": [item.as_dict() for item in segments],
    }


PLANNER_VERSION = "2.0"
SPEECH_MARGIN_SECONDS = 0.6


def _content_groups(sentences: list[str], durations: list[float], cap: int) -> list[tuple[int, int, int]]:
    """Minimize paid calls, then requested seconds; preserve sentence order."""
    @lru_cache(maxsize=None)
    def solve(start: int) -> tuple[int, int, tuple[tuple[int, int, int], ...]]:
        if start == len(sentences):
            return 0, 0, ()
        best = None
        speech = 0.0
        for end in range(start + 1, len(sentences) + 1):
            speech += durations[end - 1]
            seconds = math.ceil(round(speech + SPEECH_MARGIN_SECONDS, 6))
            if seconds > cap:
                break
            count, total, tail = solve(end)
            candidate = (count + 1, total + seconds, ((start, end, seconds), *tail))
            if best is None or candidate[:2] < best[:2] or (candidate[:2] == best[:2] and end > best[2][0][1]):
                best = candidate
        if best is None:
            raise ValueError("单句超出单段能力，请在完整语义处改写后重新规划")
        return best

    return list(solve(0)[2])


def _target_groups(sentences: list[str], durations: list[float], target: int, cap: int) -> list[tuple[int, int, int]]:
    """Allocate integer seconds jointly with sentence cuts, never preset 15+remainder."""
    minimum_slots = math.ceil(target / cap)
    if len(sentences) < minimum_slots:
        raise ValueError(f"目标 {target} 秒至少需要 {minimum_slots} 个完整句子分段，当前只有 {len(sentences)} 句、口播约 {sum(durations):.2f} 秒；AI 草稿应补充有用内容，逐字锁定稿则需说明时长冲突")
    prefix = [0.0]
    for duration in durations:
        prefix.append(prefix[-1] + duration)

    @lru_cache(maxsize=None)
    def solve(start: int, count: int, seconds_left: int) -> tuple[float, tuple[tuple[int, int, int], ...]] | None:
        if count == 0:
            return (0.0, ()) if start == len(sentences) and seconds_left == 0 else None
        if len(sentences) - start < count or not count <= seconds_left <= count * cap:
            return None
        best = None
        for end in range(start + 1, len(sentences) - count + 2):
            speech = prefix[end] - prefix[start]
            minimum = math.ceil(round(speech + SPEECH_MARGIN_SECONDS, 6))
            if minimum > cap:
                break
            for seconds in range(max(minimum, seconds_left - (count - 1) * cap), min(cap, seconds_left - count + 1) + 1):
                tail = solve(end, count - 1, seconds_left - seconds)
                if tail is None:
                    continue
                # Prefer balanced unused time; do not force a 15-second first shot.
                cost = (seconds - speech - SPEECH_MARGIN_SECONDS) ** 2 + tail[0]
                candidate = (cost, ((start, end, seconds), *tail[1]))
                if best is None or cost < best[0] - 1e-9:
                    best = candidate
        return best

    for count in range(minimum_slots, min(len(sentences), target) + 1):
        result = solve(0, count, target)
        if result is not None:
            return list(result[1])
    raise ValueError("完整句子及起音收声余量无法装入目标时长；请调整句长或内容后重新规划，不能拆半句或强行加速")


def plan_segments(
    script: str,
    delivery_max_seconds: int | None = None,
    max_request_seconds: int = 15,
    duration_mode: str = "auto",
    speech_rate_cpm: int = 260,
    planner_version: str = PLANNER_VERSION,
) -> dict[str, object]:
    """Versioned planning: user target or content-led length, with 15s only a cap."""
    if planner_version == "1.0":
        if delivery_max_seconds is None:
            raise ValueError("旧方案缺少已确认时长")
        return _plan_segments_v1(script, delivery_max_seconds, max_request_seconds, duration_mode, speech_rate_cpm)
    if planner_version != PLANNER_VERSION:
        raise ValueError(f"未知分段规划版本: {planner_version}")
    if delivery_max_seconds is not None and (isinstance(delivery_max_seconds, bool) or not isinstance(delivery_max_seconds, int) or delivery_max_seconds < 1):
        raise ValueError("目标时长必须是正整数秒")
    if not 1 <= max_request_seconds <= 15:
        raise ValueError("当前单段时长上限必须在 1-15 秒之间")
    if duration_mode not in {"auto", "exact", "content-fit"}:
        raise ValueError("duration_mode 只能是 auto、exact 或 content-fit")
    # auto never infers intent from divisibility by 15. Explicit user targets use exact.
    mode = "content-fit" if duration_mode == "auto" else duration_mode
    if mode == "exact" and delivery_max_seconds is None:
        raise ValueError("exact 模式需要用户指定的 --duration；未指定时长请按内容规划")
    sentences = split_sentences(script)
    if not sentences:
        raise ValueError("口播稿为空")
    if not re.search(r"[。！？!?]$", sentences[-1]):
        raise ValueError("口播稿最后一句缺少完整句子结束标点")
    durations = [estimate_speech_seconds(sentence, speech_rate_cpm) for sentence in sentences]
    if any(duration > max_request_seconds - SPEECH_MARGIN_SECONDS for duration in durations):
        raise ValueError("存在单句超出单段安全时长，不能在半句话处分段，请在完整语义处改写")
    total_speech = round(sum(durations), 2)
    content_groups = _content_groups(sentences, durations, max_request_seconds)
    content_requests = [item[2] for item in content_groups]
    if delivery_max_seconds is not None and total_speech > delivery_max_seconds - SPEECH_MARGIN_SECONDS:
        raise ValueError(f"口播预计 {total_speech:.2f} 秒，超过目标成片 {delivery_max_seconds} 秒的安全范围；请压缩稿件或延长时长")
    groups = _target_groups(sentences, durations, delivery_max_seconds, max_request_seconds) if mode == "exact" else content_groups
    requested = sum(item[2] for item in groups)
    if delivery_max_seconds is not None and requested > delivery_max_seconds:
        raise ValueError(f"完整句子、起音收声和整数秒取整共需 {requested} 秒，超过目标成片上限 {delivery_max_seconds} 秒；请压缩稿件或调整上限")
    segments = []
    issues = []
    for index, (start, end, seconds) in enumerate(groups, 1):
        speech = round(sum(durations[start:end]), 2)
        segment = PlannedSegment(index, "".join(sentences[start:end]), speech, seconds).as_dict()
        unused = round(seconds - speech, 2)
        # A planning heuristic, allowing short utterances' fixed start/end margin.
        limit = round(max(1.5, seconds * 0.15), 2)
        segment.update({"unspoken_budget_seconds": unused, "unspoken_budget_limit_seconds": limit})
        segments.append(segment)
        if mode == "exact" and unused > limit:
            issues.append({"segment_index": index, "code": "copy_too_short_for_target", "message": f"第 {index} 段口播约 {speech:.2f} 秒，请求 {seconds} 秒，空余 {unused:.2f} 秒；先补充有用内容，不用慢读凑时长"})
    return {
        "planner_version": PLANNER_VERSION,
        "duration_mode_requested": duration_mode,
        "duration_mode": mode,
        "duration_constraint_seconds": delivery_max_seconds,
        "duration_source": "user_target" if mode == "exact" else ("user_maximum" if delivery_max_seconds is not None else "content_estimate"),
        "target_delivery_seconds": delivery_max_seconds if mode == "exact" else requested,
        "delivery_max_seconds": delivery_max_seconds if delivery_max_seconds is not None else requested,
        "estimated_speech_seconds": total_speech,
        "speech_rate_cpm": speech_rate_cpm,
        "script_counts": {"cjk_characters": len(CJK_RE.findall(script)), "latin_words_or_numbers": len(LATIN_WORD_RE.findall(script))},
        "estimation_note": "文字粗估，包含标点停顿；英文、数字和模型实际发声可能偏离，最终以原速实听为准",
        "sentence_estimates": [{"index": i, "script": sentence, "estimated_speech_seconds": duration} for i, (sentence, duration) in enumerate(zip(sentences, durations), 1)],
        "sentence_count": len(sentences),
        "minimum_paid_segment_count": len(segments),
        "segment_count_is_minimal": True,
        "packing_strategy": "complete_sentences_min_calls_then_timing",
        "max_speech_seconds_per_segment": round(max_request_seconds - SPEECH_MARGIN_SECONDS, 2),
        "planned_request_total_seconds": requested,
        "provider_request_total_seconds": requested,
        "duration_request_policy": "user_target_semantic_allocation" if mode == "exact" else "content_plus_margin_rounded_up",
        "delivery_duration_tolerance_seconds": min(2.0, max(1.0, requested * 0.03)) if mode == "exact" else None,
        "speech_fill_ratio": round(total_speech / requested, 4),
        "duration_timing_warning": "；".join(item["message"] for item in issues) or None,
        "timing_assessment": {"status": "revise_script" if issues else "ready", "issues": issues},
        "content_fit_alternative": {"request_seconds": content_requests, "total_seconds": sum(content_requests), "paid_calls": len(content_requests)},
        "provisional_duration_capability": True,
        "segments": segments,
    }


def require_ready_timing(plan: dict[str, object]) -> None:
    if plan.get("timing_assessment", {}).get("status") == "revise_script":
        raise ValueError(str(plan["duration_timing_warning"]) + "；AI 自写稿应先改稿再运行 plan，用户锁定稿与时长冲突则在方案中说明可选调整")
