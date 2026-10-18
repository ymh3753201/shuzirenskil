"""Iterative, capability-aware planning. Existing v1/v2 contracts stay unchanged."""
from __future__ import annotations

import math
from planner import split_sentences, estimate_speech_seconds, ends_complete_sentence
from model_registry import allowed_durations


def plan_narration(script: str, profile: dict, *, target: int | None = None,
                   mode: str = "content-fit", speech_rate_cpm: int = 260) -> dict:
    choices = allowed_durations(profile)
    if mode not in {"content-fit", "exact"}:
        raise ValueError("新版规划 mode 为 content-fit 或 exact")
    if target is not None and (type(target) is not int or not 1 <= target <= 3600):
        raise ValueError("目标时长须为 1–3600 秒整数；更长内容请按章节规划")
    if mode == "exact" and target is None:
        raise ValueError("exact 需要 target")
    sentences = split_sentences(script)
    if not sentences or any(not ends_complete_sentence(s) for s in sentences):
        raise ValueError("口播每句需要完整结束标点，不在半句话处切分")
    if len(sentences) > 2000:
        raise ValueError("单章最多 2000 句，请按章节规划")
    estimates = [estimate_speech_seconds(s, speech_rate_cpm) for s in sentences]
    n = len(sentences)
    groups: list[list[tuple[int, int, float]]] = [[] for _ in sentences]
    for start in range(n):
        speech = 0.0
        for end in range(start + 1, n + 1):
            speech = round(speech + estimates[end - 1], 6)
            minimum = math.ceil(round(speech + 0.6, 6))
            if minimum > choices[-1]:
                break
            valid = [s for s in choices if s >= minimum]
            if mode == "content-fit":
                valid = valid[:1]
            else:
                valid = [s for s in valid if s - speech <= max(1.5, s * .15) + 1e-8]
            groups[start].extend((end, s, speech) for s in valid)
        if not groups[start] and estimates[start] + .6 > choices[-1]:
            raise ValueError(f"第 {start + 1} 句超过模型 {choices[-1]} 秒上限，请改写完整句子")

    # Each state retains only its best predecessor; no recursion-depth ceiling.
    # content-fit minimizes calls, then billed seconds. exact minimizes calls, then idle-time imbalance.
    states: list[dict[int, tuple[int, float, tuple[int, int, int, float] | None]]] = [{} for _ in range(n + 1)]
    states[0][0] = (0, 0.0, None)
    count_states = 1
    for start in range(n):
        for used, (calls, cost, _) in list(states[start].items()):
            for end, seconds, speech in groups[start]:
                total = used + seconds if mode == "exact" else 0
                if mode == "exact" and total > target:
                    continue
                next_cost = cost + ((seconds - speech - .6) ** 2 if mode == "exact" else seconds)
                candidate = (calls + 1, next_cost, (start, used, seconds, speech))
                previous = states[end].get(total)
                if previous is None or candidate[:2] < previous[:2]:
                    if previous is None:
                        count_states += 1
                        if count_states > 150000:
                            raise ValueError("精确时长规划过于复杂，请拆成章节或使用按内容时长")
                    states[end][total] = candidate
    final_key = target if mode == "exact" else 0
    if final_key not in states[n]:
        raise ValueError("模型时长选项、完整句子和自然语速不能同时满足目标；请调整稿件或目标，不能补长静音")
    parts = []
    end, key = n, final_key
    while end:
        start, old_key, seconds, speech = states[end][key][2]
        parts.append({"script": " ".join(sentences[start:end]), "request_seconds": seconds,
                      "estimated_speech_seconds": round(speech, 3),
                      "sentence_start": start + 1, "sentence_end": end})
        end, key = start, old_key
    parts.reverse()
    total = sum(s["request_seconds"] for s in parts)
    if target is not None and total > target:
        raise ValueError(f"按完整句子至少需要 {total} 秒请求，超过 {target} 秒上限")
    cursor = 0
    for index, item in enumerate(parts, 1):
        item.update(index=index, planned_start_seconds=cursor, planned_end_seconds=cursor + item["request_seconds"])
        cursor += item["request_seconds"]
    return {"planner_version": "3.0", "mode": mode, "target_seconds": target,
            "allowed_duration_seconds": choices, "segments": parts,
            "minimum_paid_segment_count": len(parts), "planned_request_total_seconds": total,
            "timeline_basis": "request_estimates_not_actual_media", "actual_timeline_required_after_generation": True}
