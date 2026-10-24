#!/usr/bin/env python3
"""Offline production planning. Paid execution is isolated in studio.py."""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

from _common import atomic_write_json, digest_json, read_json, require_file, sha256_file
from ad_contract import AD_KINDS, validate_ad_contract, render_ad_prompt, validate_ad_references
from audio_timeline import plan_audio, slice_master
from capability_planner import plan_narration
from media import probe
from model_registry import load_registry, get_profile, resolve_voice
from request_preview import compile_preview

SPEC_FIELDS = {"schema_version", "name", "model_profile", "voice", "voice_design", "script", "assets",
               "reference_images", "character_bible", "resolution", "aspect_ratio", "duration_mode",
               "target_seconds", "speech_rate_cpm", "continuity_mode", "purpose", "ad_contract", "shots", "audio_cues", "merge_shots", "review_mode"}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text)


def build_plan(spec: dict, base: Path) -> dict:
    if spec.get("schema_version") != "1.0" or set(spec) - SPEC_FIELDS:
        raise ValueError("生产规划仅支持 schema_version=1.0；存在未知字段或版本")
    if not isinstance(spec.get("name"), str) or not spec["name"].strip():
        raise ValueError("请填写项目 name")
    profile = get_profile(spec.get("model_profile", ""))
    voice = resolve_voice(profile, spec.get("voice"))
    continuity = spec.get("continuity_mode", "cut")
    if continuity not in {"cut", "continue"} or (continuity == "continue" and not profile.get("continuation_implemented")):
        raise ValueError("所选组合没有实现参考视频续接，请选择 cut 或已接入续接的参考模式")
    resolution = spec.get("resolution", profile["resolutions"][0]).lower()
    ratio = spec.get("aspect_ratio", "9:16")
    if resolution not in profile["resolutions"] or ratio not in profile["aspect_ratios"]:
        raise ValueError("分辨率或画幅不在模型组合的已登记范围内")
    bible = spec.get("character_bible")
    if not isinstance(bible, dict) or set(bible) != {"identity", "wardrobe", "scene", "camera", "lighting"}:
        raise ValueError("character_bible 需要 identity、wardrobe、scene、camera、lighting")
    if any(not isinstance(x, str) or not 2 <= len(x.strip()) <= 300 for x in bible.values()):
        raise ValueError("人物资料每项需为 2–300 字实际描述")
    design = spec.get("voice_design", "")
    if not isinstance(design, str) or not 8 <= len(design.strip()) <= 600:
        raise ValueError("voice_design 需要 8–600 字角色声音设计")
    script = spec.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("需要完整口播 script")
    assets = spec.get("assets")
    refs = spec.get("reference_images")
    if not isinstance(assets, dict) or not isinstance(refs, list) or not 1 <= len(refs) <= profile["max_images"]:
        raise ValueError("请提供 assets 素材表和模型允许数量的 reference_images")
    if any(not isinstance(k, str) for k in refs) or len(set(refs)) != len(refs):
        raise ValueError("参考图 ID 不能重复，且必须是字符串")
    selected = refs + ([voice["selected_audio_asset"]] if voice["selected_audio_asset"] else [])
    bound = {}
    for key in selected:
        entry = assets.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError(f"所选素材 {key} 缺少本地 path")
        path = require_file(base / entry["path"], f"素材 {key}")
        expected_kind = "image" if key in refs else "audio"
        if entry.get("kind") != expected_kind:
            raise ValueError(f"所选素材 {key} 应为 {expected_kind}")
        meta = probe(path)
        streams = meta.get("streams", [])
        if expected_kind == "image":
            # An image decoder stream alone also accepts video; bound still-image formats.
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not any(s.get("codec_type") == "video" for s in streams):
                raise ValueError("参考图必须为可解码的 PNG/JPEG/WebP")
            role = entry.get("role")
            if role not in {"identity", "wardrobe", "scene", "product", "style", "composition"}:
                raise ValueError("参考图需声明身份/服装/场景/产品/风格/构图职责")
        else:
            if not any(s.get("codec_type") == "audio" for s in streams) or any(s.get("codec_type") == "video" for s in streams):
                raise ValueError("声音素材必须包含可解码音轨且不能是视频")
            if voice["mode"] == "reference_timbre":
                limits = profile["audio_limits"]
                duration = float(meta.get("format", {}).get("duration") or 0)
                if (not math.isfinite(duration) or not limits["min_seconds"] <= duration <= limits["max_seconds"]
                        or path.stat().st_size > limits["max_bytes"] or path.suffix.lower()[1:] not in limits["formats"]):
                    raise ValueError("参考音频格式、大小或时长超过当前模型的输入限制")
        bound[key] = {"path": str(path), "kind": expected_kind, "role": entry.get("role"),
                      "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
    roles = [bound[k]["role"] for k in refs]
    if roles[0] != "identity":
        raise ValueError("第一张参考图必须为 identity；广告单首帧应为已确认的完整实景人物图")
    purpose = spec.get("purpose", "narration")
    if purpose not in {"narration", *AD_KINDS}:
        raise ValueError("不支持的业务用途")
    ad = spec.get("ad_contract")
    ad_prompt = ""
    if purpose in AD_KINDS:
        ad = validate_ad_contract(ad)
        if ad["kind"] != purpose:
            raise ValueError("广告用途与 ad_contract.kind 不一致")
        mode = "reference-to-video" if profile["input_mode"] == "reference" else "image-to-video"
        validate_ad_references(ad, mode, roles)
        ad_prompt = render_ad_prompt(ad, mode, roles, 1)
    elif ad is not None:
        raise ValueError("广告合同必须声明对应 purpose")

    shots = spec.get("shots")
    audio_plan = None
    if voice["mode"] == "locked_audio":
        boundaries = []
        if shots is not None:
            if not isinstance(shots, list) or not 1 <= len(shots) <= 500:
                raise ValueError("严格音轨分镜需要 1–500 项")
            cursor_cue = 1
            ids = set()
            for shot in shots:
                if not isinstance(shot, dict) or set(shot) != {"id", "kind", "visual_brief", "cue_start", "cue_end"}:
                    raise ValueError("严格音轨分镜需要 id、kind、visual_brief、cue_start、cue_end")
                if not isinstance(shot['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', shot['id']) or shot['id'] in ids:
                    raise ValueError("分镜 ID 无效或重复")
                ids.add(shot['id'])
                if shot['kind'] not in {'talking_head', 'b_roll'} or not isinstance(shot['visual_brief'], str) or not 4 <= len(shot['visual_brief']) <= 500:
                    raise ValueError("分镜类型或画面描述无效")
                if type(shot['cue_start']) is not int or type(shot['cue_end']) is not int or shot['cue_start'] != cursor_cue or shot['cue_end'] < cursor_cue:
                    raise ValueError("分镜必须按顺序完整覆盖句子，不得重复或遗漏")
                cursor_cue = shot['cue_end'] + 1
                boundaries.append(shot['cue_end'])
            if cursor_cue != len(spec.get('audio_cues', {}).get('cues', [])) + 1:
                raise ValueError("分镜没有覆盖完整主音轨句子")
        audio_plan = plan_audio(Path(bound[voice["selected_audio_asset"]]["path"]), spec.get("audio_cues", {}), profile, script, boundaries[:-1])
        target = spec.get("target_seconds")
        duration_mode = spec.get("duration_mode", "reference")
        if duration_mode not in {"reference", "content-fit", "exact"}:
            raise ValueError("duration_mode 必须为 reference、content-fit 或 exact")
        if target is not None and (type(target) is not int or target < 1):
            raise ValueError("目标时长必须是正整数秒")
        if duration_mode == "exact" and target is None:
            raise ValueError("exact 模式需要目标时长")
        if target is not None and duration_mode != "reference" and ((duration_mode == "exact" and abs(target - audio_plan["delivery_audio_seconds"]) > 1 / audio_plan["master"]["sample_rate"])
                                   or audio_plan["delivery_audio_seconds"] > target):
            raise ValueError("主音轨实际时长与用户目标冲突；不可裁掉语音或静默拉伸")
        segments = []
        for item in audio_plan["segments"]:
            shot = next((x for x in (shots or []) if x['cue_start'] <= item['cue_start'] <= x['cue_end']), None)
            segments.append({**item, "shot_id": shot['id'] if shot else "narration",
                             "kind": shot['kind'] if shot else "talking_head",
                             "visual_brief": shot['visual_brief'] if shot else "自然正视镜头，克制动作"})
    elif shots is None:
        timing = plan_narration(script, profile, target=spec.get("target_seconds"), mode=spec.get("duration_mode", "reference"), speech_rate_cpm=spec.get("speech_rate_cpm", 260))
        segments = [{**s, "shot_id": "narration", "kind": "talking_head", "visual_brief": "自然正视镜头，克制动作"} for s in timing["segments"]]
    else:
        if not isinstance(shots, list) or not 1 <= len(shots) <= 500 or spec.get("duration_mode", "reference") not in {"reference", "content-fit"}:
            raise ValueError("分镜需为 1–500 项；分镜采用 reference 或 content-fit，逐镜头确定时长")
        segments = []
        spoken_parts = []
        ids = set()
        for shot in shots:
            if not isinstance(shot, dict) or set(shot) - {"id", "kind", "script", "visual_brief", "duration_seconds"}:
                raise ValueError("分镜字段无效")
            shot_id = shot.get("id")
            if not isinstance(shot_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", shot_id) or shot_id in ids:
                raise ValueError("分镜 ID 必须唯一且仅使用字母、数字、下划线、短横线")
            ids.add(shot_id)
            brief = shot.get("visual_brief")
            if not isinstance(brief, str) or not 4 <= len(brief.strip()) <= 500:
                raise ValueError("每个分镜需 4–500 字的具体动态画面说明")
            if shot.get("kind") == "talking_head":
                spoken = shot.get("script", "")
                if not isinstance(spoken, str):
                    raise ValueError("口播分镜 script 必须为文字")
                spoken_parts.append(spoken)
                parts = plan_narration(spoken, profile, target=shot.get("duration_seconds"), speech_rate_cpm=spec.get("speech_rate_cpm", 260))["segments"]
            elif shot.get("kind") == "b_roll" and not shot.get("script"):
                seconds = shot.get("duration_seconds")
                if type(seconds) is not int or seconds not in profile["duration_seconds"]:
                    raise ValueError("动态空镜必须声明模型支持的 duration_seconds；不含口播台词")
                parts = [{"script": "", "request_seconds": seconds}]
            else:
                raise ValueError("分镜仅支持 talking_head 或不含台词的动态 b_roll")
            segments.extend({**s, "shot_id": shot_id, "kind": shot["kind"], "visual_brief": brief} for s in parts)
        if _normalized("".join(spoken_parts)) != _normalized(script):
            raise ValueError("分镜台词遗漏、重复或修改了完整确认稿")
        maximum = spec.get("target_seconds")
        if maximum is not None and (type(maximum) is not int or maximum < 1 or (spec.get("duration_mode", "reference") != "reference" and sum(s["request_seconds"] for s in segments) > maximum)):
            raise ValueError("分镜总请求时长超过用户上限或上限无效")

    if type(spec.get('merge_shots', False)) is not bool:
        raise ValueError("merge_shots 必须为布尔值")
    # Optional conservative merge: same modality only, never merge the pilot or locked-audio cuts.
    # Different modalities keep independent review/lip-sync contracts.
    if spec.get('merge_shots'):
        if audio_plan or continuity == 'continue':
            raise ValueError("镜头合并仅用于原生声音 cut 路线；严格音轨按已审时间边界执行")
        merged = []
        for seg in segments:
            previous = merged[-1] if len(merged) > 1 else None
            total = previous['request_seconds'] + seg['request_seconds'] if previous else 0
            if previous and previous['kind'] == seg['kind'] and total in profile['duration_seconds']:
                old_seconds = previous['request_seconds']
                previous['visual_brief'] += f"；在本段 {old_seconds} 秒处切换：" + seg['visual_brief']
                previous['script'] += seg['script']
                previous['request_seconds'] = total
                previous.setdefault('merged_shot_ids', [previous['shot_id']]).append(seg['shot_id'])
            else:
                merged.append(dict(seg))
        segments = merged

    lock = {"character_bible": bible, "reference_images": [bound[k]["sha256"] for k in refs],
            "voice_design": design, "voice": voice,
            "selected_audio_sha256": bound.get(voice["selected_audio_asset"], {}).get("sha256")}
    jobs = []
    cursor = 0
    for index, segment in enumerate(segments, 1):
        is_talking = segment["kind"] == "talking_head"
        prompt = "；".join(f"{k}: {v}" for k, v in bible.items()) + "。\n" + segment["visual_brief"]
        prompt += "。生成真实连续动态画面，禁止照片拼贴或静态照片推拉。\n" + ad_prompt
        if not ad or not ad["source_text"]:
            from video_prompt import NO_TEXT_RULE
            prompt += "\n无文字画面合同：" + NO_TEXT_RULE
        audio_token = None
        if is_talking and voice["mode"] != "locked_audio":
            prompt += f"\n逐字口播：{segment['script']}\n角色声音设计：{design}"
            if voice["send_audio_to_video_model"]:
                audio_token = "asset://" + bound[voice["selected_audio_asset"]]["sha256"]
                prompt += "\n音色参考音频1；只参考音色，台词以本段口播稿为准。"
        else:
            prompt += "\n人物不说话，保持自然动作；不生成人声旁白。"
        preview = compile_preview(profile, prompt=prompt, seconds=segment["request_seconds"], resolution=resolution,
                                  ratio=ratio, images=["asset://" + bound[k]["sha256"] for k in refs], audio=audio_token)
        use_seconds = segment.get("use_seconds", segment["request_seconds"])
        jobs.append({**segment, "index": index, "job_id": f"segment_{index:04d}",
                     "planned_start_seconds": round(cursor, 6), "planned_end_seconds": round(cursor + use_seconds, 6),
                     "consistency_digest": digest_json(lock), "pilot_review_dependency": index > 1,
                     "native_audio_policy": "discard" if voice["mode"] == "locked_audio" or not is_talking else "retain_and_review",
                     "requires_lip_sync": voice["mode"] == "locked_audio" and is_talking,
                     "request_preview": preview})
        cursor += use_seconds
    blockers = ["此文件是本地生产计划；需经 studio.py 建立项目、记录已确认的制作范围后提交，无需报价",
                "需要账号级能力验收、素材传输和按既有流程确认图片与付费上限"]
    if voice["mode"] == "locked_audio":
        blockers.append("严格音轨需要单独配置并授权 fal 口型处理；最终保留完整主音轨")
    if any(j["request_preview"]["payload"] is None for j in jobs):
        blockers.append("当前模型仅登记能力，尚无请求编译器")
    result = {"schema_version": "production-plan-1.0", "status": "offline_plan_ready", "execution_allowed": False,
              "name": spec["name"], "purpose": purpose, "duration_mode": spec.get("duration_mode", "reference"), "target_seconds": spec.get("target_seconds"), "model_snapshot": profile, "voice": voice,
              "assets": bound, "ignored_asset_ids": sorted(set(assets) - set(selected)),
              "consistency_contract": lock, "continuity_mode": continuity, "jobs": jobs,
              "resolution": resolution, "aspect_ratio": ratio, "audio_cues": spec.get("audio_cues"),
              "audio_plan": audio_plan, "ad_contract": ad,
              "planned_delivery_seconds": round(cursor, 6), "request_count": len(jobs),
              "request_seconds": sum(j["request_seconds"] for j in jobs), "cost_estimate": None,
              "cost_note": "报价不是制作前置条件；未知金额不阻断已授权请求，也不代表免费",
              "actual_timeline_required": audio_plan is None,
              "required_reviews": ["逐段正常速度看听", "台词", "人物与产品身份", "口型", "跨段音色", "接缝", "画面文字", "广告事实和行动引导"],
              "execution_blockers": blockers}
    mode = spec.get('review_mode', 'auto')
    eligible = purpose == 'narration' and not audio_plan and all(j['kind'] == 'talking_head' and j['native_audio_policy'] == 'retain_and_review' for j in jobs)
    if mode == 'auto':mode = 'continuous_candidate' if eligible else 'full_per_segment'
    if mode not in {'full_per_segment', 'continuous_candidate'}:
        raise ValueError('review_mode 无效')
    if mode == 'continuous_candidate':
        if not eligible:
            raise ValueError('连续候选制作仅适用于普通原生声音口播；严格音轨、广告和 B-roll 保留原验收流程')
        from production_review import NOTICE
        result['review_mode'] = mode
        result['review_notice'] = NOTICE
    result["plan_digest"] = digest_json(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="多模型本地规划，不联网、不上传、不创建付费任务")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("models", help="列出能力与接入状态")
    plan = commands.add_parser("plan", help="生成可审查的生产计划与脱敏请求预览")
    plan.add_argument("--spec", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    audio = commands.add_parser("slice-audio", help="把严格音频计划按样本切成 WAV")
    audio.add_argument("--plan", type=Path, required=True)
    audio.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "models":
            print(json.dumps(load_registry(), ensure_ascii=False, indent=2))
        elif args.command == "plan":
            if args.output.exists():
                raise ValueError("输出文件已存在，请选新文件名保留旧计划")
            result = build_plan(read_json(args.spec), args.spec.resolve().parent)
            atomic_write_json(args.output, result)
            print(json.dumps({"status": result["status"], "plan_file": str(args.output.resolve()), "request_count": result["request_count"],
                              "request_seconds": result["request_seconds"], "execution_allowed": False}, ensure_ascii=False))
        else:
            value = read_json(args.plan)
            expected = value.pop("plan_digest", None)
            if expected != digest_json(value) or value.get("schema_version") != "production-plan-1.0":
                raise ValueError("计划内容发生变化或版本不符，请重新规划")
            if value.get("voice", {}).get("mode") != "locked_audio" or not value.get("audio_plan"):
                raise ValueError("计划不是严格音轨路线")
            print(json.dumps(slice_master(value["audio_plan"], args.output_dir), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
