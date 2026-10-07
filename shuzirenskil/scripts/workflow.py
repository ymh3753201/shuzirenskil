#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from _common import (  # noqa: E402
    atomic_write_json,
    digest_json,
    ensure_no_secret_text,
    now_iso,
    project_lock,
    read_json,
    require_file,
    sha256_file,
)
from media import (  # noqa: E402
    analyze_speech_pacing,
    detect_tail_silence,
    extract_review_frames,
    normalize_reference_image,
    parse_srt_bounds,
    run,
    srt_timestamp,
    stitch,
    technical_summary,
    validate_reference_image,
)
from image_provider import ImageProviderError, generate_image  # noqa: E402
from image_transport import ImageTransportError, local_image_data_uri  # noqa: E402
from planner import plan_segments, require_ready_timing, split_sentences  # noqa: E402
from private_env import load_private_value  # noqa: E402
from quality_contract import validate_character_review, validate_text_review  # noqa: E402
from provider import ProviderError, create_task, download_content, download_video_url, extract_video_url, get_status, list_model_ids, probe_endpoint, upload_file  # noqa: E402
from video_prompt import (  # noqa: E402
    build_video_prompt,
    default_prompt_spec,
    validate_prompt_spec,
    validate_new_narration_plan,
    validate_reference_roles,
    voice_signature,
    voice_identity_signature,
    require_voice_casting,
)


PROJECT_FILE = "project.json"
ACTIVE_STATUSES = {"queued", "pending", "submitted", "processing", "in_progress", "running"}
SUCCESS_STATUSES = {"completed", "complete", "succeeded", "success", "done"}
FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "expired"}


def load_project(project_dir: Path) -> dict[str, Any]:
    path = project_dir / PROJECT_FILE
    if not path.is_file():
        raise ValueError(f"项目不存在: {path}")
    return read_json(path)


def save_project(project_dir: Path, project: dict[str, Any]) -> None:
    project["updated_at"] = now_iso()
    atomic_write_json(project_dir / PROJECT_FILE, project)


def default_config_path() -> Path:
    return SKILL_DIR / "assets" / "provider-chain.json"


def _load_video_config(path: Path | None, provider_name: str | None = None) -> dict[str, Any]:
    """Resolve the primary/fallback chain without putting credentials in config."""
    config_path = path or default_config_path()
    raw = read_json(config_path)
    if not isinstance(raw.get("providers"), dict):
        return raw
    selected = provider_name or os.environ.get("SHUZIRENSKIL_VIDEO_PROVIDER", "").strip() or raw.get("primary_provider")
    if not isinstance(selected, str) or selected not in raw["providers"]:
        raise ValueError(f"未知视频 Provider: {selected}")
    entry = raw["providers"][selected]
    if not isinstance(entry, dict):
        raise ValueError(f"Provider 配置不是对象: {selected}")
    if entry.get("config_path"):
        referenced = (config_path.parent / str(entry["config_path"])).resolve()
        config = read_json(referenced)
        config["provider_id"] = selected
        config["provider_chain_file"] = str(config_path.resolve())
        return config
    config = json.loads(json.dumps(entry, ensure_ascii=False))
    config["provider_id"] = selected
    config["provider_chain_file"] = str(config_path.resolve())
    return config


def default_image_config_path() -> Path:
    return SKILL_DIR / "assets" / "image-model-config.json"


def _copy_input(source: Path, target: Path) -> dict[str, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return {"path": str(target.resolve()), "sha256": sha256_file(target)}


def _project_core(project: dict[str, Any]) -> dict[str, Any]:
    plan = json.loads(json.dumps(project["plan"], ensure_ascii=False))
    for segment in plan.get("segments", []):
        for field in ("asset_sha256", "asset_path", "parent_canonical_sha256", "provider_image_url_sha256", "provider_input_sha256"):
            segment.pop(field, None)
    return {
        "route": project["route"],
        "settings": project["settings"],
        "script": project.get("script", ""),
        "plan": plan,
        "inputs": project["inputs"],
        "video_prompt_spec": project.get("video_prompt_spec"),
    }


def _verify_plan_digest(project: dict[str, Any]) -> None:
    if digest_json(_project_core(project)) != project.get("plan_digest"):
        raise ValueError("脚本、时长、分段、设置或输入记录已经变化，必须重新建立方案并确认")


def _verify_minimal_segment_plan(project: dict[str, Any]) -> None:
    """防止对话层把每个完整句子错误地变成一个付费片段。"""
    if project.get("route") != "video_generation":
        return
    plan = project.get("plan", {})
    expected = plan_segments(
        project.get("script", ""),
        plan.get("duration_constraint_seconds") if plan.get("planner_version") == "2.0" else int(plan.get("delivery_max_seconds", 0)),
        duration_mode=str(plan.get("duration_mode_requested") or plan.get("duration_mode") or "content-fit"),
        speech_rate_cpm=int(plan.get("speech_rate_cpm", 306)),
        planner_version=str(plan.get("planner_version", "1.0")),
    )
    if plan.get("planner_version") == "2.0" and not plan.get("duration_capability_test"):
        require_ready_timing(expected)
    if plan.get("duration_capability_test") is True:
        if len(expected["segments"]) != 1:
            raise ValueError("时长能力测试只允许单片段项目")
        test_seconds = int(plan.get("delivery_max_seconds", 0))
        if not 1 <= test_seconds <= 15:
            raise ValueError("时长能力测试必须在 1-15 秒范围内")
        expected["segments"][0]["request_seconds"] = test_seconds
    actual_segments = plan.get("segments", [])
    expected_segments = expected["segments"]
    if len(actual_segments) != len(expected_segments):
        raise ValueError(
            f"当前计划有 {len(actual_segments)} 个片段，但按完整句子合并后最少只需 {len(expected_segments)} 个；请重新生成方案"
        )
    for actual, expected_item in zip(actual_segments, expected_segments):
        for field in ("script", "request_seconds"):
            if actual.get(field) != expected_item.get(field):
                raise ValueError("当前片段没有按最少完整句子分段，必须重新生成方案")
    if plan.get("segment_count_is_minimal") is not True:
        raise ValueError("方案没有记录最少分段校验，必须重新生成方案")


def command_plan(args: argparse.Namespace) -> None:
    """Local draft planning only: no project, state change, image or paid call."""
    script = require_file(args.script_file, "口播稿").read_text(encoding="utf-8-sig").strip()
    plan = plan_segments(script, args.duration, duration_mode=args.duration_mode, speech_rate_cpm=args.speech_rate_cpm)
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def command_prepare(args: argparse.Namespace) -> None:
    project_dir = args.project_dir.resolve()
    if (project_dir / PROJECT_FILE).exists():
        raise ValueError("项目已经存在，不能覆盖；请使用新的项目目录")
    project_dir.mkdir(parents=True, exist_ok=True)
    inputs: dict[str, Any] = {}
    script = ""
    route = "postproduction_only" if args.existing_video else "video_generation"

    if args.script_file:
        script_source = require_file(args.script_file, "口播稿")
        script = script_source.read_text(encoding="utf-8-sig").strip()
        inputs["script"] = _copy_input(script_source, project_dir / "inputs" / "script.txt")
    if route == "video_generation" and not script:
        raise ValueError("新数字人视频必须提供供方案确认的最终口播稿 --script-file")

    if args.existing_video:
        video_source = require_file(args.existing_video, "已有视频")
        inputs["existing_video"] = {"path": str(video_source), "sha256": sha256_file(video_source)}
        if args.duration is None:
            args.duration = math.ceil(technical_summary(video_source)["duration_seconds"])

    if args.subtitle_source:
        subtitle_source = require_file(args.subtitle_source, "字幕文件")
        inputs["subtitle_source"] = _copy_input(subtitle_source, project_dir / "inputs" / "source-subtitles.srt")

    if args.video_prompt_spec:
        prompt_spec_source = require_file(args.video_prompt_spec, "视频提示词方案")
        prompt_spec = validate_prompt_spec(read_json(prompt_spec_source))
        inputs["video_prompt_spec"] = _copy_input(prompt_spec_source, project_dir / "inputs" / "video-prompt-spec.json")
    else:
        prompt_spec = default_prompt_spec()
    preset_voices = [item.strip().lower() for item in (args.preset_voice or [])]
    if len(preset_voices) > 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in preset_voices):
        raise ValueError("预设声音最多 3 个，voice_id 只能包含字母、数字、下划线或短横线")
    if preset_voices and args.generation_mode != "reference-to-video":
        raise ValueError("预设声音 reference_audios 只能用于 reference-to-video 模式")

    if route == "video_generation":
        if args.duration_capability_test and args.duration is None:
            raise ValueError("时长能力测试必须显式提供 --duration")
        plan = plan_segments(script, args.duration, duration_mode=args.duration_mode, speech_rate_cpm=prompt_spec["speech_rate_cpm"])
        if not args.duration_capability_test:
            require_ready_timing(plan)
        if not args.allow_legacy_performance_plan:
            if not args.video_prompt_spec:
                raise ValueError("新数字人口播必须提供逐段表演方案 --video-prompt-spec")
            missing = [segment["index"] for segment in plan["segments"] if not any(beat["segment_index"] == segment["index"] for beat in prompt_spec["performance_beats"])]
            if missing:
                raise ValueError(f"以下片段缺少与台词对应的表演节拍: {missing}")
            validate_new_narration_plan(prompt_spec, plan)
            require_voice_casting(prompt_spec)
        for beat in prompt_spec["performance_beats"]:
            index = beat["segment_index"]
            if index > len(plan["segments"]) or beat["trigger_phrase"] not in plan["segments"][index - 1]["script"]:
                raise ValueError(f"第 {index} 段表演触发词与分段台词不匹配: {beat['trigger_phrase']}")
        if args.duration_capability_test:
            if len(plan["segments"]) != 1:
                raise ValueError("时长能力测试只允许单片段项目")
            if not 1 <= args.duration <= 15:
                raise ValueError("时长能力测试必须在 1-15 秒范围内")
            plan["segments"][0]["request_seconds"] = args.duration
            plan["planned_request_total_seconds"] = args.duration
            plan["provider_request_total_seconds"] = args.duration
            plan["duration_capability_test"] = True
    else:
        plan = {
            "delivery_max_seconds": args.duration,
            "target_delivery_seconds": args.duration,
            "duration_mode_requested": args.duration_mode,
            "duration_mode": "not-applicable",
            "estimated_speech_seconds": None,
            "minimum_paid_segment_count": 0,
            "planned_request_total_seconds": 0,
            "provisional_duration_capability": False,
            "segments": [],
        }

    project = {
        "schema_version": "1.0",
        "project_id": str(uuid.uuid4()),
        "name": args.name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "route": route,
        "settings": {
            "language": args.language,
            "aspect_ratio": args.aspect_ratio,
            "resolution": args.resolution,
            "subtitle_choice": args.subtitles,
            "effects_choice": "disabled",
            "model_key": "grok_imagine_video_1_5_provider_chain",
            "generation_mode": args.generation_mode,
            "preset_voices": preset_voices,
            "voice_strategy": "preset_voice_reference" if preset_voices else "prompt_generated_voice",
            "voice_signature": voice_signature(prompt_spec),
            "voice_identity_signature": voice_identity_signature(prompt_spec),
            "character_review_required": route == "video_generation" and not args.allow_legacy_performance_plan,
            "pilot_first": not args.no_pilot_first,
            "performance_plan_required": not args.allow_legacy_performance_plan,
        },
        "inputs": inputs,
        "script": script,
        "video_prompt_spec": prompt_spec,
        "plan": plan,
        "assets": [],
        "state": {
            "stage": "awaiting_plan_confirmation",
            "plan_confirmed": False,
            "image_assets_confirmed": False,
            "paid_video_authorized": False,
            "pilot_review_passed": False,
        },
    }
    project["plan_digest"] = digest_json(_project_core(project))
    save_project(project_dir, project)
    print(json.dumps({"status": "prepared", "plan": plan, "plan_digest": project["plan_digest"]}, ensure_ascii=False, indent=2))


def command_confirm_plan(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    _verify_plan_digest(project)
    _verify_minimal_segment_plan(project)
    if project["state"]["stage"] != "awaiting_plan_confirmation":
        raise ValueError("当前阶段不能确认方案")
    if args.approved_by != "user":
        raise ValueError("方案必须由用户确认")
    project["state"]["plan_confirmed"] = True
    project["state"]["stage"] = "production_authorized" if project["route"] == "postproduction_only" else "awaiting_image_binding"
    project["plan_confirmation"] = {
        "approved_by": "user",
        "confirmed_at": now_iso(),
        "plan_digest": project["plan_digest"],
        "authorizes_paid_video": False,
    }
    save_project(args.project_dir, project)
    print("方案确认已记录；本次确认没有授权付费视频。")


def command_generate_image(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    _verify_plan_digest(project)
    if project["route"] != "video_generation" or project["state"]["stage"] != "awaiting_image_binding":
        raise ValueError("只有方案确认后、图片确认前才能生成生产参考图")
    if project["plan"]["minimum_paid_segment_count"] > 1 and not args.continuity_spec:
        raise ValueError("多段视频生图前必须准备一致性说明，避免付费生图后才发现材料不完整")
    prompt_path = require_file(args.prompt_file, "已确认方案中的图片提示词")
    prompt = prompt_path.read_text(encoding="utf-8-sig").strip()
    if len(prompt) < 20:
        raise ValueError("图片提示词过短，无法稳定约束人物和场景")
    config = read_json(args.config or default_image_config_path())
    if config.get("model") != "gpt-image-2" or config.get("generation_path") != "/v1/images/generations":
        raise ValueError("图片配置必须使用 gpt-image-2 和 /v1/images/generations")
    source_image = require_file(args.source_image, "本人形象参考图") if args.source_image else None
    if source_image and config.get("editing_path") != "/v1/images/edits":
        raise ValueError("本人形象参考图必须通过 gpt-image-2 的 /v1/images/edits 传入")
    base_url = load_private_value(config["base_url_env"], args.env_file, args.project_dir)
    api_key = load_private_value(config["api_key_env"], args.env_file, args.project_dir)
    size = config.get("sizes", {}).get(project["settings"]["aspect_ratio"])
    if not size:
        raise ValueError("图片配置缺少当前画幅对应的尺寸")
    generated_dir = args.project_dir / "assets" / "generated"
    raw_path = generated_dir / "canonical.raw"
    generation = generate_image(
        base_url,
        api_key,
        config["model"],
        config["generation_path"],
        prompt,
        size,
        raw_path,
        args.timeout,
        source_image=source_image,
        edit_path=config.get("editing_path"),
    )
    canonical_path = generated_dir / "canonical.png"
    technical = normalize_reference_image(raw_path, canonical_path, project["settings"]["aspect_ratio"])
    bind_args = argparse.Namespace(
        project_dir=args.project_dir,
        image=[],
        canonical_image=canonical_path,
        segment_image=[],
        reference_image=[],
        reference_role=[],
        storyboard_image=[],
        continuity_spec=args.continuity_spec,
        provider_image_url=[args.provider_image_url or generation.get("provider_image_url")]
        if (args.provider_image_url or generation.get("provider_image_url")) else [],
        provider_image_file_id=[],
        provider_reference_image_url=[],
        provider_reference_image_file_id=[],
        input_transport=args.input_transport,
        provider=getattr(args, "provider", None),
        allow_derived_segment_images_with_risk=False,
    )
    if args.input_transport == "public-https" and not bind_args.provider_image_url:
        raise ValueError(
            "当前已选择公共 HTTPS，但图片接口没有返回可用地址；请改用 --input-transport auto，"
            "让 91topgo 直接使用本地压缩 JPEG Data URI"
        )
    if project["settings"].get("generation_mode") == "reference-to-video":
        bind_args.reference_role = ["identity"]
    command_bind_image(bind_args)
    project = load_project(args.project_dir)
    project["image_generation"] = {
        "provider": config.get("provider"),
        "model": config["model"],
        "prompt_sha256": sha256_file(prompt_path),
        "source_image_sha256": sha256_file(source_image) if source_image else None,
        "request_fields": generation["request_fields"],
        "requested_size": size,
        "normalized_technical": technical,
        "generated_at": now_iso(),
        "user_confirmation_required_next": True,
    }
    save_project(args.project_dir, project)
    print(json.dumps({
        "status": "reference_image_ready_for_second_confirmation",
        "image": str((args.project_dir / "assets" / "production" / "canonical.png").resolve()),
        "model": config["model"],
        "additional_user_confirmation_before_image_generation": False,
    }, ensure_ascii=False, indent=2))


CONTINUITY_FIELDS = (
    "character_identity",
    "wardrobe",
    "scene",
    "camera",
    "lighting",
    "motion_policy",
)


def _load_continuity_spec(path: Path) -> dict[str, Any]:
    source = require_file(path, "长视频一致性说明")
    value = read_json(source)
    missing = [field for field in CONTINUITY_FIELDS if not isinstance(value.get(field), str) or not value[field].strip()]
    if missing:
        raise ValueError(f"长视频一致性说明缺少内容: {', '.join(missing)}")
    if value.get("voice_strategy") not in {
        "prompt_generated_voice",
        "gateway_generated_voice_unverified",
        "preset_voice_reference",
        "preset_voice_reference_gateway_unverified",
    }:
        raise ValueError("voice_strategy 必须标记为提示词生成声音或预设声音参考")
    return {field: value[field].strip() for field in (*CONTINUITY_FIELDS, "voice_strategy")}


def _validated_provider_image_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("视频模型参考图必须是无需登录的 HTTPS URL，且不能包含账号、密码或片段标记")
    return value


def _resolve_input_transport(requested: str | None, *, has_public_urls: bool, provider_config: dict[str, Any]) -> str:
    """Choose a safe image transport while keeping public URLs explicit."""
    value = (requested or "auto").strip().lower()
    if value == "auto":
        if has_public_urls:
            return "public-https"
        if provider_config.get("local_image_transport"):
            return "jpeg-data-uri"
        return "provider-file"
    if value not in {"public-https", "jpeg-data-uri", "provider-file"}:
        raise ValueError("input_transport 只能是 auto、public-https、jpeg-data-uri 或 provider-file")
    return value


def _provider_reference_payload(ref: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    """Compile one confirmed local or HTTPS reference for the selected Provider."""
    if "url" in ref:
        return {"url": _validated_provider_image_url(str(ref["url"]))}
    if "file_id" in ref:
        if str(config.get("provider_id") or config.get("provider")) == "91topgo":
            raise ValueError("91topgo 当前合同不使用 file_id；请使用 HTTPS 或本地 JPEG Data URI")
        return {"file_id": _validated_provider_file_id(str(ref["file_id"]))}
    if "local_path" not in ref:
        raise ValueError("Provider 参考图缺少 url、file_id 或 local_path")
    path = require_file(Path(str(ref["local_path"])), "已确认的本地 Provider 参考图")
    expected = str(ref.get("sha256") or "")
    if expected and sha256_file(path) != expected:
        raise ValueError(f"Provider 参考图指纹变化: {path}")
    try:
        value = local_image_data_uri(
            path,
            transport=str(config.get("local_image_transport") or "jpeg_data_uri"),
            max_bytes=int(config.get("max_inline_image_bytes") or 800_000),
            quality=int(config.get("jpeg_quality") or 6),
        )
    except ImageTransportError as exc:
        raise ValueError(str(exc)) from exc
    return {"url": value}


def _validated_provider_file_id(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"file[-_][A-Za-z0-9._:-]{1,200}", value):
        raise ValueError("Provider file_id 格式错误")
    return value


def _validated_provider_media_ref(value: dict[str, Any], *, allow_local: bool) -> dict[str, str]:
    keys = [key for key in ("url", "file_id", "local_path") if key in value]
    if len(keys) != 1:
        raise ValueError("每个 Provider 素材必须且只能使用 url、file_id 或 local_path 之一")
    key = keys[0]
    if key == "url":
        return {"url": _validated_provider_image_url(str(value[key]))}
    if key == "file_id":
        return {"file_id": _validated_provider_file_id(str(value[key]))}
    if not allow_local:
        raise ValueError("Provider 文件尚未上传，不能进入预检")
    path = require_file(Path(str(value[key])), "待上传 Provider 图片")
    result = {"local_path": str(path.resolve())}
    if isinstance(value.get("sha256"), str):
        if sha256_file(path) != value["sha256"]:
            raise ValueError("待上传 Provider 图片指纹变化")
        result["sha256"] = value["sha256"]
    return result


def _validate_private_provider_item(item: dict[str, Any], *, allow_local: bool) -> dict[str, Any]:
    shot_id = item.get("shot_id")
    mode = item.get("mode")
    if not isinstance(shot_id, str) or not re.fullmatch(r"shot_\d{3}", shot_id):
        raise ValueError("私密 Provider 输入缺少有效 shot_id")
    if mode == "image-to-video":
        if not isinstance(item.get("image"), dict):
            raise ValueError("image-to-video 缺少 image 输入")
        return {"shot_id": shot_id, "mode": mode, "image": _validated_provider_media_ref(item["image"], allow_local=allow_local)}
    if mode == "reference-to-video":
        refs = item.get("reference_images")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 7:
            raise ValueError("reference-to-video 必须包含 1-7 张参考图")
        if any(not isinstance(ref, dict) for ref in refs):
            raise ValueError("reference_images 中的每个素材都必须是对象")
        return {
            "shot_id": shot_id,
            "mode": mode,
            "reference_images": [
                _validated_provider_media_ref(ref, allow_local=allow_local)
                for ref in refs
            ],
        }
    raise ValueError("私密 Provider 输入的生成模式错误")


def _write_private_provider_inputs(project_dir: Path, items: list[dict[str, Any]]) -> str:
    path = project_dir / "provider-inputs.json"
    normalized = [_validate_private_provider_item(item, allow_local=True) for item in items]
    atomic_write_json(path, {"schema_version": "2.0", "items": normalized})
    path.chmod(0o600)
    return digest_json(normalized)


def _load_private_provider_inputs(project_dir: Path, expected_digest: str, *, allow_local: bool = False) -> dict[str, dict[str, Any]]:
    path = project_dir / "provider-inputs.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("缺少私密 Provider 图片地址文件")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("provider-inputs.json 权限必须是 600")
    items = read_json(path).get("items")
    if not isinstance(items, list) or digest_json(items) != expected_digest:
        raise ValueError("私密 Provider 图片地址与已确认图片不一致")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("私密 Provider 图片地址格式错误")
        normalized = _validate_private_provider_item(item, allow_local=allow_local)
        result[normalized["shot_id"]] = normalized
    return result


def command_bind_image(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    _verify_plan_digest(project)
    if project["route"] != "video_generation" or project["state"]["stage"] not in {"awaiting_image_binding", "awaiting_image_confirmation"}:
        raise ValueError("当前阶段不能绑定生产图片")
    generation_mode = project["settings"].get("generation_mode", "image-to-video")
    provider_config = _load_video_config(None, getattr(args, "provider", None))
    input_transport = _resolve_input_transport(
        getattr(args, "input_transport", "auto"),
        has_public_urls=bool(args.provider_image_url or getattr(args, "provider_reference_image_url", None)),
        provider_config=provider_config,
    )
    legacy_images = args.image or []
    if args.canonical_image and legacy_images:
        raise ValueError("--canonical-image 与旧参数 --image 只能使用一种")
    if len(legacy_images) > 1:
        raise ValueError("多段视频必须先指定一张主参考图；分段图请使用 --segment-image")
    canonical_source = require_file(args.canonical_image or legacy_images[0], "主参考图") if (args.canonical_image or legacy_images) else None
    if canonical_source is None:
        raise ValueError("必须提供一张确认后的主参考图")
    segment_count = project["plan"]["minimum_paid_segment_count"]
    segment_sources = [require_file(item, "分段首帧") for item in (args.segment_image or [])]
    allow_derived_risk = bool(getattr(args, "allow_derived_segment_images_with_risk", False))
    voice_strategy = project["settings"].get("voice_strategy", "prompt_generated_voice")
    prompt_generated_voice = voice_strategy in {"prompt_generated_voice", "gateway_generated_voice_unverified"}
    reference_sources = [require_file(item, "额外参考图") for item in (getattr(args, "reference_image", None) or [])]
    if generation_mode == "reference-to-video" and segment_sources:
        raise ValueError("reference-to-video 不锁定首帧，不能同时使用 --segment-image")
    if segment_sources and len(segment_sources) != segment_count:
        raise ValueError(f"分段首帧必须正好是 {segment_count} 张")
    if segment_count > 1 and prompt_generated_voice and segment_sources and not allow_derived_risk:
        raise ValueError(
            "多段提示词生成声音默认必须复用同一张主参考图；"
            "只有用户明确接受音色和人物漂移风险时，才可使用 --allow-derived-segment-images-with-risk"
        )
    if segment_count > 1 and not args.continuity_spec:
        raise ValueError("多段视频必须提供 --continuity-spec，固定人物、服装、场景、机位和灯光")
    continuity = _load_continuity_spec(args.continuity_spec) if args.continuity_spec else None
    image_reuse_policy = "same_canonical_image_for_all_segments" if not segment_sources else "derived_segment_images_risk_accepted"
    if continuity:
        continuity["voice_signature"] = project["settings"]["voice_signature"]
        continuity["image_reuse_policy"] = image_reuse_policy
    continuity_signature = digest_json(continuity) if continuity else None
    provider_urls = [_validated_provider_image_url(item) for item in (args.provider_image_url or [])]
    provider_reference_urls = [_validated_provider_image_url(item) for item in (getattr(args, "provider_reference_image_url", None) or [])]
    if input_transport == "provider-file" and (provider_urls or provider_reference_urls):
        raise ValueError("provider-file 传输只绑定本地素材，授权后由 upload-inputs 上传")
    if input_transport == "jpeg-data-uri" and (provider_urls or provider_reference_urls):
        raise ValueError("jpeg-data-uri 传输不应同时提供公共 HTTPS 地址")
    assets_dir = args.project_dir / "assets" / "production"
    assets: list[dict[str, Any]] = []
    canonical_target = assets_dir / f"canonical{canonical_source.suffix.lower() or '.png'}"
    canonical = _copy_input(canonical_source, canonical_target)
    canonical.update({
        "role": "canonical_video_source",
        "source": "third_party_gpt_image_2_or_user_confirmed",
        "technical": validate_reference_image(canonical_target, project["settings"]["aspect_ratio"]),
    })
    assets.append(canonical)

    reference_assets: list[dict[str, Any]] = [canonical]
    for index, source in enumerate(reference_sources, start=2):
        target = assets_dir / f"reference_{index:03d}{source.suffix.lower() or '.png'}"
        copied = _copy_input(source, target)
        copied.update({
            "role": "reference_video_source",
            "source": "user_confirmed_reference",
            "technical": validate_reference_image(target, project["settings"]["aspect_ratio"]),
        })
        assets.append(copied)
        reference_assets.append(copied)

    reference_roles: list[str] = []
    if generation_mode == "reference-to-video":
        if len(reference_assets) > 7:
            raise ValueError("reference-to-video 最多只能绑定 7 张参考图")
        reference_roles = validate_reference_roles(getattr(args, "reference_role", None) or [], len(reference_assets))
        for asset, role in zip(reference_assets, reference_roles):
            asset["reference_role"] = role
        if input_transport == "public-https":
            if len(provider_urls) != 1 or len(provider_reference_urls) != len(reference_sources):
                raise ValueError("reference-to-video 必须为主图提供 1 个地址，并按顺序为每张额外参考图各提供 1 个地址")
    elif reference_sources:
        raise ValueError("额外参考图只能用于 reference-to-video")
    elif provider_reference_urls:
        raise ValueError("额外参考图地址只能用于 reference-to-video")
    elif (
        input_transport == "public-https"
        and segment_count > 1
        and prompt_generated_voice
        and not segment_sources
        and len(provider_urls) != 1
    ):
        raise ValueError("多段提示词生成声音必须共用 1 个主参考图 HTTPS 地址")
    elif input_transport == "public-https" and len(provider_urls) not in {1, segment_count}:
        raise ValueError(f"--provider-image-url 必须提供 1 个公共 HTTPS 地址，或正好提供 {segment_count} 个分段地址")

    bound_segment_assets: list[dict[str, Any]] = []
    for index, source in enumerate(segment_sources, start=1):
        target = assets_dir / f"segment_{index:03d}{source.suffix.lower() or '.png'}"
        copied = _copy_input(source, target)
        copied.update({
            "role": "derived_segment_source",
            "source": "gpt_image_2_derived_from_canonical",
            "parent_canonical_sha256": canonical["sha256"],
            "technical": validate_reference_image(target, project["settings"]["aspect_ratio"]),
        })
        assets.append(copied)
        bound_segment_assets.append(copied)

    storyboards: list[dict[str, Any]] = []
    for index, source in enumerate(args.storyboard_image or [], start=1):
        source = require_file(source, "分镜预览图")
        target = args.project_dir / "assets" / "storyboards" / f"storyboard_{index:03d}{source.suffix.lower() or '.png'}"
        copied = _copy_input(source, target)
        copied.update({"role": "preview_only_storyboard", "sent_to_video_provider": False})
        assets.append(copied)
        storyboards.append(copied)

    project["assets"] = assets
    for segment in project["plan"]["segments"]:
        source_asset = canonical if not bound_segment_assets else bound_segment_assets[segment["index"] - 1]
        segment["asset_sha256"] = source_asset["sha256"]
        segment["asset_path"] = source_asset["path"]
        segment["parent_canonical_sha256"] = canonical["sha256"]
    private_items: list[dict[str, Any]] = []
    for segment in project["plan"]["segments"]:
        shot_id = f"shot_{segment['index']:03d}"
        if generation_mode == "image-to-video":
            source_asset = canonical if not bound_segment_assets else bound_segment_assets[segment["index"] - 1]
            if input_transport == "public-https":
                provider_ref = {"url": provider_urls[0] if len(provider_urls) == 1 else provider_urls[segment["index"] - 1]}
            else:
                provider_ref = {"local_path": source_asset["path"], "sha256": source_asset["sha256"]}
            item = {"shot_id": shot_id, "mode": generation_mode, "image": provider_ref}
        else:
            if input_transport == "public-https":
                all_urls = provider_urls + provider_reference_urls
                refs = [{"url": value} for value in all_urls]
            else:
                refs = [{"local_path": asset["path"], "sha256": asset["sha256"]} for asset in reference_assets]
            item = {"shot_id": shot_id, "mode": generation_mode, "reference_images": refs}
        normalized_item = _validate_private_provider_item(item, allow_local=True)
        segment["provider_input_sha256"] = digest_json(normalized_item)
        private_items.append(normalized_item)
    project["provider_input_digest"] = _write_private_provider_inputs(args.project_dir, private_items)
    project["canonical_reference"] = canonical
    project["reference_assets"] = reference_assets if generation_mode == "reference-to-video" else []
    project["reference_roles"] = reference_roles
    project["input_transport"] = input_transport
    project["storyboards"] = storyboards
    project["continuity"] = continuity
    project["continuity_signature"] = continuity_signature
    project["image_reuse_policy"] = image_reuse_policy
    project["image_set_digest"] = digest_json([
        {"sha256": item["sha256"], "role": item["role"], "parent": item.get("parent_canonical_sha256")}
        for item in assets
    ] + [{
        "generation_mode": generation_mode,
        "reference_roles": reference_roles,
        "input_transport": input_transport,
        "image_reuse_policy": image_reuse_policy,
    }])
    project["state"]["stage"] = "awaiting_image_confirmation"
    save_project(args.project_dir, project)
    print(json.dumps({
        "status": "images_bound",
        "canonical_reference": canonical,
        "generation_mode": generation_mode,
        "reference_sources": len(reference_assets) if generation_mode == "reference-to-video" else 0,
        "input_transport": input_transport,
        "segment_sources": len(bound_segment_assets),
        "storyboards_preview_only": len(storyboards),
        "image_set_digest": project["image_set_digest"],
        "continuity_signature": continuity_signature,
        "voice_signature": project["settings"]["voice_signature"],
        "image_reuse_policy": image_reuse_policy,
    }, ensure_ascii=False, indent=2))


def command_review_character(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    _verify_plan_digest(project)
    if project["state"]["stage"] != "awaiting_image_confirmation":
        raise ValueError("请先绑定生产图片，再核对角色与声音设计")
    evidence = read_json(require_file(args.evidence_file, "角色声音与图片观察"))
    validate_character_review(project, evidence)
    project["character_review"] = evidence
    save_project(args.project_dir, project)
    print("已记录实际看图与声音选角检查；请随图片展示声音说明，由原定第二次确认统一授权。")


def command_authorize(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    _verify_plan_digest(project)
    if project["state"]["stage"] != "awaiting_image_confirmation":
        raise ValueError("当前阶段不能授权制作")
    if args.approved_by != "user" or args.confirmation_intent != "confirm_images_and_start":
        raise ValueError("需要用户明确确认图片并开始制作")
    expected = project["plan"]["minimum_paid_segment_count"]
    if args.max_paid_submissions != expected:
        raise ValueError(f"最高付费次数必须等于基础片段数 {expected}，不设置重试额度")
    if expected > 1 and not args.acknowledge_long_video_risk:
        raise ValueError("多段视频的声音、口型和跨段动作仍未付费验证，必须由用户明确确认该风险")
    if project["settings"].get("character_review_required"):
        validate_character_review(project, project.get("character_review", {}))
    project["production_authorization"] = {
        "approved_by": "user",
        "confirmation_intent": args.confirmation_intent,
        "confirmed_at": now_iso(),
        "plan_digest": project["plan_digest"],
        "image_set_digest": project["image_set_digest"],
        "max_paid_submissions": expected,
        "automatic_retry_allowed": False,
        "long_video_risk_acknowledged": bool(args.acknowledge_long_video_risk),
        "character_review_digest": digest_json(project.get("character_review", {})),
    }
    project["state"].update({
        "stage": "production_authorized",
        "image_assets_confirmed": True,
        "paid_video_authorized": True,
    })
    save_project(args.project_dir, project)
    print(f"第二次确认已记录，最高允许 {expected} 次付费提交；没有自动重试额度。")


def _validate_config(config: dict[str, Any]) -> None:
    provider_id = str(config.get("provider_id") or config.get("provider") or "mikuapi")
    if provider_id == "91topgo":
        if config.get("provider") != "91topgo":
            raise ValueError("91topgo 配置的 provider 必须为 91topgo")
        if config.get("base_url") != "https://router.91topgo.com":
            raise ValueError("91topgo 根地址必须为 https://router.91topgo.com")
        if config.get("create_path") not in {"/v1/videos", "/v1/videos/generations"}:
            raise ValueError("91topgo 创建路径必须是 /v1/videos 或 /v1/videos/generations")
        if config.get("status_path_template") != "/v1/videos/{task_id}":
            raise ValueError("91topgo 状态路径必须是 /v1/videos/{task_id}")
        if config.get("model") != "grok-imagine-video-1.5":
            raise ValueError("91topgo 模型必须为 grok-imagine-video-1.5")
        if config.get("duration_field") != "seconds" or config.get("duration_payload_type") != "string":
            raise ValueError("91topgo 必须使用字符串 seconds 字段")
        fields = set(config.get("documented_payload_fields", []))
        required_fields = {"model", "prompt", "seconds"}
        if not required_fields.issubset(fields):
            raise ValueError("91topgo 基础字段必须包含 model、prompt、seconds")
        if config.get("supports_image_to_video") is not False:
            raise ValueError("91topgo 当前只允许 reference-to-video，不得打开 image-to-video")
        if config.get("supports_reference_images") is not True:
            raise ValueError("91topgo 必须打开 reference_images 输入")
        if config.get("reference_field") != "reference_images" or config.get("reference_payload_format") != "url_objects":
            raise ValueError("91topgo 必须使用 reference_images 的 url 对象格式")
        if config.get("supports_multiple_references") is not True or int(config.get("max_reference_images", 0)) != 7:
            raise ValueError("91topgo reference_images 必须支持最多 7 张")
        if config.get("requires_public_https_images") is not False:
            raise ValueError("91topgo 不应强制公共 HTTPS 图片")
        if config.get("local_image_transport") not in {"jpeg_data_uri", "jpg_data_uri"}:
            raise ValueError("91topgo 必须配置本地 JPEG Data URI 传输")
        if int(config.get("max_inline_image_bytes", 0)) != 800_000:
            raise ValueError("91topgo 本地 Data URI 大小上限必须为 800000 字节")
        if config.get("supports_audio") is not True or config.get("supports_native_speech_output") is not True:
            raise ValueError("91topgo 必须保留提示词生成人声能力")
        if config.get("reference_audio_field") is not None:
            raise ValueError("91topgo 不得配置 reference_audios 字段")
        if config.get("automatic_retry_allowed") is not False:
            raise ValueError("91topgo 必须关闭自动重试")
        if not 1 <= int(config.get("min_duration_seconds", 0)) <= int(config.get("max_duration_seconds", 0)) <= 15:
            raise ValueError("91topgo 时长范围必须在 1-15 秒")
        if not isinstance(config.get("health_probe_expected_statuses"), list):
            raise ValueError("91topgo 必须记录健康探测允许状态")
        return
    if config.get("model") != "grok-imagine-video-1.5":
        raise ValueError("配置模型不是 grok-imagine-video-1.5")
    if config.get("canonical_model") != config.get("model") or config.get("provider_request_model") != config.get("model"):
        raise ValueError("官方标准模型、MikuAPI 请求模型和实际 model 必须一致")
    aliases = config.get("official_model_aliases")
    if not isinstance(aliases, list) or "grok-imagine-video-1.5-preview" not in aliases:
        raise ValueError("配置必须记录官方 preview 别名，但不得自动改写 MikuAPI 请求模型")
    base_url = str(config.get("base_url", ""))
    loopback_test_base = base_url.startswith("http://127.0.0.1:") or base_url.startswith("http://localhost:")
    if config.get("provider") != "mikuapi.org" or (base_url != "https://mikuapi.org" and not loopback_test_base) or config.get("create_path") != "/v1/videos/generations":
        raise ValueError("视频配置必须使用 MikuAPI 的 xAI 兼容 /v1/videos/generations 合同")
    if config.get("automatic_retry_allowed") is not False:
        raise ValueError("配置必须关闭自动重试")
    if config.get("runtime_paid_verified") is not True:
        raise ValueError("MikuAPI 已完成一次真实短片验证，配置不得降级为未验证")
    if config.get("runtime_paid_verified_scope") != "mikuapi_4s_480p_9_16_image_to_video_success":
        raise ValueError("真实验证范围必须准确记录为 MikuAPI 4 秒 480p 9:16 图生视频")
    if config.get("long_video_runtime_verified") is not False:
        raise ValueError("长视频仍未验证，不能提前开放")
    if config.get("prompt_generated_voice_runtime_verified") is not True or config.get("prompt_generated_voice_exact_speech_verified") is not True:
        raise ValueError("提示词生成人声的真实短句验证记录不完整")
    expected_fields = {"model", "prompt", "duration", "aspect_ratio", "resolution", "image"}
    if set(config.get("gateway_documented_payload_fields", [])) != expected_fields:
        raise ValueError("MikuAPI 已实测的单图请求字段合同发生变化")
    for implemented in (
        "gateway_reference_to_video_implemented",
        "gateway_preset_voice_implemented",
        "gateway_files_api_implemented",
    ):
        if config.get(implemented) is not True:
            raise ValueError(f"新能力实现标记缺失: {implemented}")
    for flag in (
        "gateway_reference_to_video_enabled",
        "gateway_preset_voice_enabled",
        "gateway_files_api_enabled",
        "gateway_custom_audio_reference_enabled",
    ):
        if not isinstance(config.get(flag), bool):
            raise ValueError(f"网关能力开关必须是布尔值: {flag}")
    if config.get("gateway_custom_audio_reference_enabled") is True:
        if config.get("gateway_custom_audio_reference_implemented") is not True or config.get("trusted_partner_custom_audio_entitlement_confirmed") is not True:
            raise ValueError("自有音频上传必须同时具备实现和可信合作方授权")
    if config.get("gateway_video_extension_enabled") is not False or config.get("gateway_1080p_enabled") is not False:
        raise ValueError("视频续写和 MikuAPI 1080p 尚未验收，必须保持关闭")
    if config.get("files_path") != "/v1/files" or not 3600 <= int(config.get("file_upload_default_expires_after_seconds", 0)) <= 2_592_000:
        raise ValueError("Files API 路径或自动过期策略错误")


def _verify_project_hashes(project: dict[str, Any]) -> None:
    for item in project["inputs"].values():
        path = Path(item["path"])
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise ValueError(f"输入文件已变化: {path}")
    for asset in project.get("assets", []):
        path = Path(asset["path"])
        if not path.is_file() or sha256_file(path) != asset["sha256"]:
            raise ValueError(f"生产图片已变化: {path}")


def _segment_prompt(
    segment: dict[str, Any],
    project: dict[str, Any],
    config: dict[str, Any],
    *,
    generation_mode_override: str | None = None,
) -> str:
    return build_video_prompt(
        segment=segment,
        language=project["settings"]["language"],
        continuity=project.get("continuity"),
        prompt_spec=project.get("video_prompt_spec") or default_prompt_spec(),
        generation_mode=generation_mode_override or project["settings"].get("generation_mode", "image-to-video"),
        reference_roles=(project.get("reference_roles") or (["identity"] if generation_mode_override == "reference-to-video" else [])),
        preset_voices=project["settings"].get("preset_voices") or [],
        reference_image_index_base=int(config.get("reference_image_prompt_index_base", 1)),
    )


def command_preflight(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    if project["state"]["stage"] not in {"production_authorized", "preflight_passed"}:
        raise ValueError("必须先完成相应用户确认")
    _verify_plan_digest(project)
    _verify_minimal_segment_plan(project)
    _verify_project_hashes(project)
    config = _load_video_config(args.config, getattr(args, "provider", None))
    _validate_config(config)

    dry_requests: list[dict[str, Any]] = []
    if project["route"] == "video_generation":
        provider_id = str(config.get("provider_id") or config.get("provider") or "mikuapi")
        provider_inputs = _load_private_provider_inputs(
            args.project_dir,
            project.get("provider_input_digest", ""),
            allow_local=provider_id == "91topgo" and config.get("local_image_transport") in {"jpeg_data_uri", "jpg_data_uri"},
        )
        authorization = project.get("production_authorization", {})
        if project["settings"].get("character_review_required"):
            validate_character_review(project, project.get("character_review", {}))
            if authorization.get("character_review_digest") != digest_json(project.get("character_review", {})):
                raise ValueError("角色声音观察在图片确认后发生变化")
        segment_count = project["plan"]["minimum_paid_segment_count"]
        generation_mode = project["settings"].get("generation_mode", "image-to-video")
        preset_voices = project["settings"].get("preset_voices") or []
        if generation_mode == "reference-to-video" and provider_id == "91topgo" and config.get("supports_reference_images") is not True:
            raise ValueError("91topgo 的 reference_images 能力未打开")
        if generation_mode == "reference-to-video" and provider_id != "91topgo" and config.get("gateway_reference_to_video_enabled") is not True:
            raise ValueError("MikuAPI 的 reference_images 透传尚未通过单独验收，禁止付费预检")
        if preset_voices and provider_id == "91topgo":
            raise ValueError("91topgo 不接受 reference_audios；请使用提示词中的 AUDIO/Dialogue 生成声音")
        if preset_voices and config.get("gateway_preset_voice_enabled") is not True:
            raise ValueError("MikuAPI 的 reference_audios 预设声音透传尚未通过单独验收，禁止付费预检")
        if generation_mode == "reference-to-video" and project["settings"]["resolution"] not in {"480p", "720p"}:
            raise ValueError("reference-to-video 最高只允许 720p")
        if authorization.get("max_paid_submissions") != segment_count:
            raise ValueError("付费上限与分段数不一致")
        if provider_id == "91topgo" and config.get("runtime_paid_verified") is not True:
            raise ValueError("91topgo 尚未完成明确授权的最小付费验收，不能进入付费预检")
        canonical = project.get("canonical_reference", {})
        if not canonical.get("sha256") and config.get("supports_image_to_video") is not False:
            raise ValueError("缺少主参考图")
        if segment_count > 1:
            if not project.get("continuity"):
                raise ValueError("多段视频缺少一致性说明")
            if not authorization.get("long_video_risk_acknowledged"):
                raise ValueError("多段视频风险尚未明确确认")
        expected_voice_signature = voice_signature(project.get("video_prompt_spec") or default_prompt_spec())
        if project["settings"].get("voice_signature") != expected_voice_signature:
            raise ValueError("声音特征指纹与已确认提示词不一致，必须重新建立方案")
        if segment_count > 1 and project.get("continuity", {}).get("voice_signature") != expected_voice_signature:
            raise ValueError("多段一致性记录中的声音指纹不一致")
        for segment in project["plan"]["segments"]:
            seconds = segment["request_seconds"]
            if seconds not in config["allowed_duration_seconds"]:
                raise ValueError(f"片段 {segment['index']} 秒数不在临时允许范围")
            if not segment["script"].endswith(tuple("。！？!?")):
                raise ValueError(f"片段 {segment['index']} 没有在完整句子结束")
            if provider_id != "91topgo" and config.get("supports_image_to_video") is False and generation_mode == "image-to-video":
                raise ValueError(
                    f"当前 Provider {provider_id} 的公开合同没有确认图片输入，不能用于数字人口播 image-to-video；"
                    "请先完成该 Provider 的图片字段验收，或显式选择已兼容的降级 Provider"
                )
            if config.get("supports_image_to_video") is not False and segment.get("parent_canonical_sha256") != canonical["sha256"]:
                raise ValueError(f"片段 {segment['index']} 参考图没有关联主参考图")
            shot_id = f"shot_{segment['index']:03d}"
            provider_input = provider_inputs.get(shot_id)
            if not provider_input or digest_json(provider_input) != segment.get("provider_input_sha256"):
                raise ValueError(f"片段 {segment['index']} 的 Provider 素材发生变化")
            effective_mode = provider_input.get("mode")
            if provider_id == "91topgo" and effective_mode == "image-to-video":
                # Compatibility for projects prepared before the 91topgo
                # reference-to-video route was enabled.
                provider_input = {
                    "shot_id": shot_id,
                    "mode": "reference-to-video",
                    "reference_images": [provider_input["image"]],
                }
            prompt_mode = "reference-to-video" if provider_id == "91topgo" and generation_mode == "image-to-video" else generation_mode
            payload: dict[str, Any] = {
                "model": config["model"],
                "prompt": _segment_prompt(segment, project, config, generation_mode_override=prompt_mode),
            }
            duration_field = str(config.get("duration_field") or "duration")
            payload[duration_field] = str(seconds) if config.get("duration_payload_type") == "string" else seconds
            documented_fields = set(config.get("documented_payload_fields") or {
                "model", "prompt", "duration", "aspect_ratio", "resolution", "image"
            })
            if "aspect_ratio" in documented_fields or config.get("supports_aspect_ratio") is True:
                payload["aspect_ratio"] = project["settings"]["aspect_ratio"]
            if "resolution" in documented_fields or config.get("supports_resolution") is True:
                payload["resolution"] = project["settings"]["resolution"]
            if provider_input.get("mode") == "image-to-video":
                payload["image"] = {next(iter(provider_input["image"])): "<redacted:confirmed-image>"}
            else:
                payload["reference_images"] = [
                    {next(iter(ref)): "<redacted:confirmed-reference-image>"}
                    for ref in provider_input["reference_images"]
                ]
            if preset_voices and provider_id != "91topgo":
                payload["reference_audios"] = [{"voice_id": item} for item in preset_voices]
            dry_requests.append({
                "shot_id": shot_id,
                "generation_mode": provider_input.get("mode", generation_mode),
                "payload": payload,
                "asset_path": segment["asset_path"],
                "asset_sha256": segment["asset_sha256"],
                "provider_input_sha256": segment["provider_input_sha256"],
                "parent_canonical_sha256": segment["parent_canonical_sha256"],
                "continuity_signature": project.get("continuity_signature"),
                "voice_signature": expected_voice_signature,
                "image_reuse_policy": project.get("image_reuse_policy"),
            })

    contract_core = {
        "project_id": project["project_id"],
        "route": project["route"],
        "plan_digest": project["plan_digest"],
        "image_set_digest": project.get("image_set_digest"),
        "settings": project["settings"],
        "plan": project["plan"],
        "continuity": project.get("continuity"),
        "continuity_signature": project.get("continuity_signature"),
        "voice_signature": project.get("settings", {}).get("voice_signature"),
        "image_reuse_policy": project.get("image_reuse_policy"),
        "canonical_reference_sha256": project.get("canonical_reference", {}).get("sha256"),
        "dry_requests": dry_requests,
        "max_paid_submissions": project.get("production_authorization", {}).get("max_paid_submissions", 0),
        "runtime_paid_verified": config["runtime_paid_verified"],
        "verification_level": config["verification_level"],
        "automatic_retry_allowed": False,
        "gateway_capabilities_used": {
            "generation_mode": project.get("settings", {}).get("generation_mode") if project["route"] == "video_generation" else None,
            "preset_voice_count": len(project.get("settings", {}).get("preset_voices") or []),
            "voice_strategy": project.get("settings", {}).get("voice_strategy"),
            "voice_signature": project.get("settings", {}).get("voice_signature"),
            "image_reuse_policy": project.get("image_reuse_policy"),
            "custom_audio_file_reference": False,
        },
    }
    ensure_no_secret_text(contract_core)
    contract = {**contract_core, "contract_digest": digest_json(contract_core), "created_at": now_iso()}
    existing_path = args.project_dir / "production-contract.json"
    if existing_path.exists() and read_json(existing_path).get("contract_digest") != contract["contract_digest"]:
        raise ValueError("生产合同发生变化，不能覆盖；需要重新确认")
    atomic_write_json(existing_path, contract)
    snapshot = {**config, "base_url": config["base_url"].rstrip("/")}
    atomic_write_json(args.project_dir / "model-snapshot.json", snapshot)
    dry_dir = args.project_dir / "requests" / "dry-run"
    for request in dry_requests:
        atomic_write_json(dry_dir / f"{request['shot_id']}.json", request)
    jobs = {
        "schema_version": "1.0",
        "contract_digest": contract["contract_digest"],
        "jobs": [
            {
                "shot_id": request["shot_id"],
                "status": "planned",
                "submission_attempts": 0,
                "task_id": None,
                "clip_path": None,
            }
            for request in dry_requests
        ],
    }
    jobs_path = args.project_dir / "jobs.json"
    if not jobs_path.exists():
        atomic_write_json(jobs_path, jobs)
    elif read_json(jobs_path).get("contract_digest") != contract["contract_digest"]:
        raise ValueError("任务账本与当前合同不一致")
    project["contract_digest"] = contract["contract_digest"]
    project["state"]["stage"] = "preflight_passed"
    save_project(args.project_dir, project)
    print(json.dumps({"status": "preflight_passed", "paid_requests": len(dry_requests), "contract_digest": contract["contract_digest"], "network_used": False}, ensure_ascii=False, indent=2))


def _api_key(config: dict[str, Any], args: argparse.Namespace) -> str:
    env_name = config["api_key_env"]
    provider_id = str(config.get("provider_id") or config.get("provider") or "")
    aliases = tuple(config.get("api_key_env_aliases") or ())
    prefer_keychain = bool(config.get("prefer_keychain"))
    keychain_service = config.get("api_key_keychain_service")
    if provider_id in {"mikuapi", "mikuapi.org"} and env_name == "SHUZIRENSKIL_API_KEY":
        # Keep historical project snapshots usable without reading a generic
        # key that might belong to 91topgo or another provider.
        env_name = "SHUZIRENSKIL_MIKUAPI_API_KEY"
        prefer_keychain = True
    if provider_id in {"91topgo", "mikuapi", "mikuapi.org"}:
        # Older snapshots may still carry the generic alias. Never send a key
        # from that alias to a different video provider.
        aliases = tuple(name for name in aliases if name != "SHUZIRENSKIL_API_KEY")
    return load_private_value(
        env_name,
        args.env_file,
        args.project_dir,
        keychain_service=keychain_service,
        fallback_names=aliases,
        prefer_keychain=prefer_keychain,
    )


def _require_snapshot_provider(config: dict[str, Any], requested_provider: str | None) -> None:
    """A CLI flag must not silently differ from the already-approved contract."""
    if not requested_provider:
        return
    snapshot_provider = str(config.get("provider_id") or config.get("provider") or "")
    if requested_provider != snapshot_provider:
        raise ValueError(
            f"当前项目已锁定视频 Provider {snapshot_provider}；不能用 --provider {requested_provider} 在提交或轮询时静默切换"
        )


def _verify_pre_submit_auth(config: dict[str, Any], api_key: str, timeout: float) -> None:
    """Check provider-specific credentials and target model before a paid POST."""
    provider_id = str(config.get("provider_id") or config.get("provider") or "mikuapi")
    if provider_id == "91topgo":
        probe = probe_endpoint(config["base_url"], config["health_probe_path"], api_key, timeout)
        status = int(probe["status_code"])
        if status in {401, 403}:
            raise ValueError(
                "91topgo API Key 或访问权限检查失败（HTTP %s）。已阻止付费提交；"
                "请检查钥匙串服务 %s 或服务商访问限制。"
                % (status, config.get("api_key_keychain_service") or "shuzirenskil-91topgo-video")
            )
        expected = {int(item) for item in config.get("health_probe_expected_statuses", [])}
        if status not in expected:
            raise ValueError(f"91topgo 免费鉴权探测返回未预期状态 HTTP {status}，已阻止付费提交")
    try:
        model_ids = list_model_ids(config["base_url"], api_key, timeout)
    except ProviderError as exc:
        if "Provider HTTP 401" in str(exc) or "Provider HTTP 403" in str(exc):
            raise ValueError(f"{provider_id} API Key 或访问权限检查失败；已阻止付费提交") from exc
        raise
    if config["model"] not in model_ids:
        raise ValueError(f"{provider_id} 当前密钥不可使用目标模型 {config['model']}；已阻止付费提交")


def command_upload_inputs(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    if project["route"] != "video_generation" or project["state"]["stage"] != "production_authorized":
        raise ValueError("只有确认图片并授权制作后才能上传 Provider 私密素材")
    if project.get("input_transport") != "provider-file":
        raise ValueError("当前项目没有选择 provider-file 传输")
    _verify_plan_digest(project)
    _verify_project_hashes(project)
    config = _load_video_config(args.config, getattr(args, "provider", None))
    _validate_config(config)
    if str(config.get("provider_id") or config.get("provider")) == "91topgo":
        raise ValueError("91topgo 直接使用本地 JPEG Data URI，不调用图片上传接口")
    if config.get("gateway_files_api_enabled") is not True:
        raise ValueError("MikuAPI Files API 尚未通过验收，不能上传真实素材")
    api_key = _api_key(config, args)
    expires_after = args.expires_after or int(config["file_upload_default_expires_after_seconds"])
    private_items = _load_private_provider_inputs(
        args.project_dir, project.get("provider_input_digest", ""), allow_local=True
    )
    ledger_path = args.project_dir / "provider-upload-ledger.json"
    ledger: dict[str, Any] = {"schema_version": "1.0", "uploads": {}}
    if ledger_path.exists():
        if ledger_path.is_symlink() or (os.name != "nt" and stat.S_IMODE(ledger_path.stat().st_mode) & 0o077):
            raise ValueError("provider-upload-ledger.json 必须是权限 600 的普通文件")
        ledger = read_json(ledger_path)
        if not isinstance(ledger.get("uploads"), dict):
            raise ValueError("Provider 上传账本格式错误")

    def save_upload_ledger() -> None:
        atomic_write_json(ledger_path, ledger)
        ledger_path.chmod(0o600)

    def uploaded_ref(ref: dict[str, Any]) -> dict[str, str]:
        if "local_path" not in ref:
            return _validated_provider_media_ref(ref, allow_local=False)
        path = require_file(Path(ref["local_path"]), "待上传 Provider 图片")
        fingerprint = sha256_file(path)
        if ref.get("sha256") != fingerprint:
            raise ValueError("待上传 Provider 图片指纹变化")
        existing = ledger["uploads"].get(fingerprint)
        if isinstance(existing, dict) and isinstance(existing.get("file_id"), str):
            return {"file_id": _validated_provider_file_id(existing["file_id"])}
        if isinstance(existing, dict) and existing.get("status") in {"uploading", "upload_unknown", "failed"}:
            raise ValueError("该素材已经尝试上传但结果不完整，禁止自动重复上传；请人工核对 Provider 文件列表")
        ledger["uploads"][fingerprint] = {
            "status": "uploading",
            "submission_attempts": 1,
            "source_sha256": fingerprint,
            "attempted_at": now_iso(),
            "automatic_retry_allowed": False,
        }
        save_upload_ledger()
        try:
            uploaded = upload_file(
                config["base_url"], config["files_path"], api_key, path, expires_after, args.timeout
            )
        except ProviderError as exc:
            ledger["uploads"][fingerprint].update({
                "status": "upload_unknown",
                "error": str(exc),
            })
            save_upload_ledger()
            raise
        ledger["uploads"][fingerprint] = {
            "status": "uploaded",
            "submission_attempts": 1,
            "file_id": uploaded["file_id"],
            "filename": uploaded["filename"],
            "bytes": uploaded["bytes"],
            "expires_at": uploaded.get("expires_at"),
            "uploaded_at": now_iso(),
            "automatic_retry_allowed": False,
        }
        save_upload_ledger()
        return {"file_id": uploaded["file_id"]}

    normalized_items: list[dict[str, Any]] = []
    for shot_id in sorted(private_items):
        item = private_items[shot_id]
        if item["mode"] == "image-to-video":
            normalized = {**item, "image": uploaded_ref(item["image"])}
        else:
            normalized = {**item, "reference_images": [uploaded_ref(ref) for ref in item["reference_images"]]}
        normalized_items.append(normalized)
    project["provider_input_digest"] = _write_private_provider_inputs(args.project_dir, normalized_items)
    by_shot = {item["shot_id"]: item for item in normalized_items}
    for segment in project["plan"]["segments"]:
        segment["provider_input_sha256"] = digest_json(by_shot[f"shot_{segment['index']:03d}"])
    project["provider_upload"] = {
        "transport": "provider_file_id",
        "unique_file_count": len(ledger["uploads"]),
        "expires_after_seconds": expires_after,
        "completed_at": now_iso(),
        "custom_audio_uploaded": False,
    }
    save_project(args.project_dir, project)
    print(json.dumps({
        "status": "provider_inputs_uploaded",
        "unique_file_count": len(ledger["uploads"]),
        "expires_after_seconds": expires_after,
        "paid_video_request_created": False,
    }, ensure_ascii=False, indent=2))


def command_check_provider(args: argparse.Namespace) -> None:
    config = _load_video_config(args.config, getattr(args, "provider", None))
    _validate_config(config)
    api_key = _api_key(config, args)
    provider_id = str(config.get("provider_id") or config.get("provider") or "mikuapi")
    if provider_id == "91topgo":
        probe = probe_endpoint(config["base_url"], config["health_probe_path"], api_key, args.timeout)
        expected = {int(item) for item in config.get("health_probe_expected_statuses", [])}
        model_ids = list_model_ids(config["base_url"], api_key, args.timeout) if probe["status_code"] in expected else []
        available = probe["status_code"] in expected and config["model"] in model_ids
        result = {
            "network_check": f"GET {config['health_probe_path']} and GET /v1/models only",
            "paid_video_request_created": False,
            "provider": provider_id,
            "model": config["model"],
            "endpoint_reachable": available,
            "model_available": config["model"] in model_ids,
            "probe_status_code": probe["status_code"],
            "image_to_video_available": False,
            "reference_images_available": bool(config.get("supports_reference_images")),
            "local_image_transport": config.get("local_image_transport"),
            "note": "91topgo 免费探测只验证端点可达性；不会创建视频任务。参考图走 reference_images，本地图片使用 JPEG Data URI。",
        }
    else:
        model_ids = list_model_ids(config["base_url"], api_key, args.timeout)
        available = config["model"] in model_ids
        result = {
            "network_check": "GET /v1/models only",
            "paid_video_request_created": False,
            "provider": provider_id,
            "model": config["model"],
            "model_available": available,
            "returned_model_count": len(model_ids),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not available:
        raise ValueError(f"当前 Provider {provider_id} 的免费连通性检查未通过")


def command_submit(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    if project["route"] != "video_generation" or project["state"]["stage"] not in {"preflight_passed", "production_started"}:
        raise ValueError("当前项目不能提交视频任务")
    pilot_first = bool(project["settings"].get("pilot_first")) and project["plan"]["minimum_paid_segment_count"] > 1
    if pilot_first and project["state"]["stage"] == "production_started":
        pilot_path = args.project_dir / "pilot-review.json"
        if not project["state"].get("pilot_review_passed") or not pilot_path.is_file():
            raise ValueError("首段尚未通过自然度和口播检查，后续片段不能付费提交")
        pilot = read_json(pilot_path)
        first_job = read_json(args.project_dir / "jobs.json")["jobs"][0]
        if (pilot.get("status") != "pass" or pilot.get("project_id") != project["project_id"]
                or pilot.get("contract_digest") != project.get("contract_digest")
                or pilot.get("task_id") != first_job.get("task_id")
                or pilot.get("clip_sha256") != sha256_file(Path(first_job["clip_path"]))):
            raise ValueError("首段试片验收记录与当前项目或视频不匹配，后续付费提交已停止")
        if pilot.get("evidence", {}).get("text_review") or project["settings"].get("character_review_required"):
            validate_text_review(pilot.get("evidence", {}), Path(first_job["clip_path"]))
    config = read_json(args.project_dir / "model-snapshot.json")
    _require_snapshot_provider(config, getattr(args, "provider", None))
    _verify_plan_digest(project)
    _verify_project_hashes(project)
    api_key = _api_key(config, args)
    _verify_pre_submit_auth(config, api_key, args.timeout)
    contract = read_json(args.project_dir / "production-contract.json")
    jobs_path = args.project_dir / "jobs.json"
    jobs = read_json(jobs_path)
    provider_id = str(config.get("provider_id") or config.get("provider") or "mikuapi")
    provider_inputs = _load_private_provider_inputs(
        args.project_dir,
        project.get("provider_input_digest", ""),
        allow_local=provider_id == "91topgo" and config.get("local_image_transport") in {"jpeg_data_uri", "jpg_data_uri"},
    )
    if jobs["contract_digest"] != project["contract_digest"]:
        raise ValueError("任务账本与生产合同不一致")
    dry_dir = args.project_dir / "requests" / "dry-run"

    for job in jobs["jobs"]:
        if pilot_first and project["state"]["stage"] == "preflight_passed" and job is not jobs["jobs"][0]:
            break
        if job["status"] in {"submitted", "polling", "downloaded", "verified"}:
            continue
        if job["submission_attempts"] >= 1 or job["status"] in {"submitting", "submission_unknown", "failed"}:
            raise ValueError(f"{job['shot_id']} 已经尝试提交，禁止再次 POST")
        dry = read_json(dry_dir / f"{job['shot_id']}.json")
        image_path = Path(dry["asset_path"])
        if sha256_file(image_path) != dry["asset_sha256"]:
            raise ValueError(f"{job['shot_id']} 图片指纹变化")
        payload = dict(dry["payload"])
        provider_input = provider_inputs.get(job["shot_id"])
        if not provider_input or digest_json(provider_input) != dry.get("provider_input_sha256"):
            raise ValueError(f"{job['shot_id']} 的 Provider 素材发生变化")
        if provider_id == "91topgo":
            if provider_input["mode"] == "image-to-video":
                references = [provider_input["image"]]
            elif provider_input["mode"] == "reference-to-video":
                references = provider_input["reference_images"]
            else:
                raise ValueError(f"{job['shot_id']} 的生成模式错误")
            payload.pop("image", None)
            payload.pop("reference_audios", None)
            payload["reference_images"] = [
                _provider_reference_payload(ref, config) for ref in references
            ]
        elif provider_input["mode"] == "image-to-video":
            payload["image"] = provider_input["image"]
        elif provider_input["mode"] == "reference-to-video":
            payload["reference_images"] = provider_input["reference_images"]
        else:
            raise ValueError(f"{job['shot_id']} 的生成模式错误")
        job.update({"status": "submitting", "submission_attempts": 1, "attempted_at": now_iso()})
        atomic_write_json(jobs_path, jobs)
        try:
            task_id = create_task(config["base_url"], config["create_path"], api_key, payload, args.timeout)
        except ProviderError as exc:
            job.update({
                "status": "submission_unknown" if exc.ambiguous else "failed",
                "error": str(exc),
                "automatic_retry_allowed": False,
            })
            atomic_write_json(jobs_path, jobs)
            raise
        job.update({"status": "submitted", "task_id": task_id, "submitted_at": now_iso()})
        atomic_write_json(jobs_path, jobs)

    project["state"]["stage"] = "production_started"
    save_project(args.project_dir, project)
    print("首段已提交，待下载并检查后再提交后续片段。" if pilot_first and any(item["status"] == "planned" for item in jobs["jobs"]) else "所有计划片段均已各提交一次；不会自动追加或重试。")


def command_poll(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    if project["route"] != "video_generation":
        raise ValueError("现有视频后期项目不需要轮询")
    config = read_json(args.project_dir / "model-snapshot.json")
    _require_snapshot_provider(config, getattr(args, "provider", None))
    api_key = _api_key(config, args)
    jobs_path = args.project_dir / "jobs.json"
    jobs = read_json(jobs_path)
    clips_dir = args.project_dir / "clips"
    pilot_waiting = bool(project["settings"].get("pilot_first")) and len(jobs["jobs"]) > 1 and not project["state"].get("pilot_review_passed")
    if pilot_waiting and jobs["jobs"][0]["status"] == "planned":
        raise ValueError("请先提交首段试片")

    for poll_index in range(args.max_polls):
        all_downloaded = True
        for job in jobs["jobs"]:
            if job["status"] in {"downloaded", "verified"}:
                continue
            if pilot_waiting and job["status"] == "planned":
                continue
            if job["status"] == "planned":
                raise ValueError(f"{job['shot_id']} 尚未提交；请先运行 submit，不要用 poll 创建任务")
            if not job.get("task_id"):
                raise ValueError(f"{job['shot_id']} 没有 task_id，禁止通过重新提交恢复")
            all_downloaded = False
            status_path = config["status_path_template"].format(task_id=job["task_id"])
            status, response = get_status(config["base_url"], status_path, api_key, args.timeout)
            job["last_status"] = status
            job["last_polled_at"] = now_iso()
            if status in ACTIVE_STATUSES:
                job["status"] = "polling"
            elif status in SUCCESS_STATUSES:
                clip_path = clips_dir / f"{job['shot_id']}.mp4"
                video_url = extract_video_url(response)
                if video_url:
                    download_video_url(config["base_url"], video_url, clip_path, args.timeout)
                elif config.get("content_path_template"):
                    content_path = config["content_path_template"].format(task_id=job["task_id"])
                    download_content(config["base_url"], content_path, api_key, clip_path, args.timeout)
                else:
                    raise ProviderError(f"{job['shot_id']} 完成响应缺少 video.url")
                job.update({"status": "downloaded", "clip_path": str(clip_path.resolve()), "downloaded_at": now_iso()})
            elif status in FAILURE_STATUSES:
                job.update({"status": "failed", "provider_status": status, "provider_error": response.get("error"), "automatic_retry_allowed": False})
                atomic_write_json(jobs_path, jobs)
                raise ProviderError(f"{job['shot_id']} Provider 终止状态: {status}")
            else:
                atomic_write_json(jobs_path, jobs)
                raise ProviderError(f"未知 Provider 状态: {status}")
            atomic_write_json(jobs_path, jobs)
        if pilot_waiting and jobs["jobs"][0]["status"] in {"downloaded", "verified"}:
            project["state"]["stage"] = "pilot_review_pending"
            save_project(args.project_dir, project)
            print("首段试片已下载；请正常速度看完整段、听完整段并运行 review-pilot。")
            return
        if all(item["status"] in {"downloaded", "verified"} for item in jobs["jobs"]):
            project["state"]["stage"] = "clips_downloaded"
            save_project(args.project_dir, project)
            print("所有已有任务均已下载；轮询过程没有创建新任务。")
            return
        if poll_index + 1 < args.max_polls:
            time.sleep(args.interval)
    print("任务仍在处理中；可以稍后继续 poll，同一 task_id 不会重复付费。")


def command_review_pilot(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    if project["route"] != "video_generation" or project["state"]["stage"] != "pilot_review_pending":
        raise ValueError("当前项目没有待检查的首段试片")
    jobs = read_json(args.project_dir / "jobs.json")["jobs"]
    first = jobs[0]
    if first["status"] not in {"downloaded", "verified"} or any(job["status"] != "planned" for job in jobs[1:]):
        raise ValueError("首段试片状态与后续未提交片段不符")
    clip = require_file(Path(first["clip_path"]), "首段试片")
    summary = technical_summary(clip)
    if not summary["has_video"] or not summary["has_audio"]:
        raise ValueError("首段试片缺少画面或声音")
    evidence = read_json(require_file(args.evidence_file, "首段质检证据"))
    clip_sha256 = sha256_file(clip)
    if evidence.get("reviewed_clip_sha256") != clip_sha256:
        raise ValueError("首段质检证据未绑定当前试片指纹，请对实际视频重新检查")
    required = ("motion_naturalness", "gesture_phrase_fit", "lip_sync", "voice_naturalness", "speech_pacing", "script_complete", "identity_consistency", "normal_speed_playback")
    required += ("no_generated_text",)
    if project["settings"].get("character_review_required"):
        required += ("voice_casting_match",)
    for key in required:
        if not isinstance(evidence.get(key), str) or len(evidence[key].strip()) < 8:
            raise ValueError(f"首段质检 {key} 必须记录至少 8 字的真实观察依据")
    spoken = evidence.get("spoken_words")
    if not isinstance(spoken, str) or not spoken.strip():
        raise ValueError("首段质检必须提供实际听到的 spoken_words")
    expected = _normalized_speech_text(project["plan"]["segments"][0]["script"])
    similarity = difflib.SequenceMatcher(None, expected, _normalized_speech_text(spoken)).ratio()
    if _normalized_speech_text(spoken) != expected:
        raise ValueError(f"首段实际听写与确认稿不完全一致（相似度 {similarity:.0%}），禁止提交后续片段")
    validate_text_review(evidence, clip)
    report = {"status": "pass", "project_id": project["project_id"], "contract_digest": project["contract_digest"], "task_id": first["task_id"], "clip_sha256": clip_sha256, "voice_signature": project["settings"]["voice_signature"], "summary": summary, "speech_similarity": round(similarity, 4), "evidence": evidence, "reviewed_at": now_iso()}
    atomic_write_json(args.project_dir / "pilot-review.json", report)
    project["state"]["pilot_review_passed"] = True
    project["state"]["stage"] = "production_started"
    save_project(args.project_dir, project)
    print("首段试片已通过质检，可以在已授权的付费上限内提交剩余片段。")


def command_stitch(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    allowed_stages = {"preflight_passed"} if project["route"] == "postproduction_only" else {"clips_downloaded", "clean_candidate_ready"}
    if project["state"]["stage"] not in allowed_stages:
        raise ValueError("当前阶段不能生成待检查成片")
    output = args.project_dir / "outputs" / "final.clean.mp4"
    if project["route"] == "postproduction_only":
        source = Path(project["inputs"]["existing_video"]["path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        report = {"method": "postproduction_source_preserved", "input": str(source), "output": technical_summary(output)}
    else:
        jobs = read_json(args.project_dir / "jobs.json")
        if not jobs["jobs"] or any(job["status"] not in {"downloaded", "verified"} for job in jobs["jobs"]):
            raise ValueError("并非所有片段都已下载")
        clips = [Path(job["clip_path"]) for job in jobs["jobs"]]
        report = stitch(clips, output, args.project_dir / "work" / "stitch", project["settings"]["aspect_ratio"])
    atomic_write_json(args.project_dir / "stitch-report.json", report)
    project["state"]["stage"] = "clean_candidate_ready"
    save_project(args.project_dir, project)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _review_required(kind: str, args: argparse.Namespace, project: dict[str, Any]) -> dict[str, str]:
    checks = {
        "visual_consistency": args.visual_consistency,
        "speech_complete": args.speech_complete,
        "stitching_quality": args.stitching_quality,
        "critical_facts_exact": args.critical_facts,
    }
    if kind == "clean":
        checks["no_generated_text"] = args.no_generated_text
        if project.get("settings", {}).get("character_review_required"):
            checks["voice_casting_match"] = getattr(args, "voice_casting_match", "pending")
        if project.get("route") == "video_generation":
            checks.update({
                "motion_naturalness": getattr(args, "motion_naturalness", "pending"),
                "gesture_phrase_fit": getattr(args, "gesture_phrase_fit", "pending"),
                "lip_sync": getattr(args, "lip_sync", "pending"),
                "voice_naturalness": getattr(args, "voice_naturalness", "pending"),
                "speech_pacing": getattr(args, "speech_pacing", "pending"),
                "normal_speed_playback": getattr(args, "normal_speed_playback", "pending"),
            })
        if project.get("route") == "video_generation" and int(project.get("plan", {}).get("minimum_paid_segment_count", 0)) > 1:
            checks["cross_segment_voice_consistency"] = args.voice_consistency
    else:
        checks.update({
            "subtitle_safe_and_readable": args.subtitle_safe,
            "subtitle_matches_confirmed_script": args.subtitle_matches,
            "no_unapproved_text": args.no_unapproved_text,
        })
    return checks


def _normalized_speech_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()


def _load_review_evidence(kind: str, args: argparse.Namespace, checks: dict[str, str]) -> tuple[dict[str, Any], Path, float | None]:
    evidence_path = require_file(args.evidence_file, "质检证据")
    evidence = read_json(evidence_path)
    for name, status in checks.items():
        if status == "pass" and (not isinstance(evidence.get(name), str) or len(evidence[name].strip()) < 8):
            raise ValueError(f"检查项 {name} 标记通过时，必须写明至少 8 个字的真实检查依据")
    transcript = require_file(Path(evidence.get("speech_transcript_file", "")), "最终语音转写")
    transcript_text = _extract_srt_text(transcript) if transcript.suffix.lower() == ".srt" else transcript.read_text(encoding="utf-8-sig", errors="replace")
    if not _normalized_speech_text(transcript_text):
        raise ValueError("最终语音转写为空")
    project = load_project(args.project_dir)
    confirmed = _normalized_speech_text(project.get("script", ""))
    heard = _normalized_speech_text(transcript_text)
    similarity = round(difflib.SequenceMatcher(None, confirmed, heard).ratio(), 4) if confirmed else None
    if confirmed and similarity < 0.55:
        raise ValueError(f"最终语音与确认稿相似度仅 {similarity:.0%}，不能标记语音完整")
    return evidence, transcript, similarity


def _duration_review_checks(duration: float, plan: dict[str, Any], route: str) -> dict[str, bool]:
    exact = plan.get("duration_mode") == "exact"
    tolerance = float(plan.get("delivery_duration_tolerance_seconds") or 0)
    maximum = float(plan["target_delivery_seconds"]) + tolerance if exact else float(plan["delivery_max_seconds"]) + 0.25
    return {
        "within_confirmed_delivery_max": duration <= maximum,
        "matches_exact_duration_intent": not exact or abs(duration - float(plan["target_delivery_seconds"])) <= tolerance,
        "within_content_request_duration": route != "video_generation" or exact or duration <= float(plan["planned_request_total_seconds"]) + 0.25,
    }


def command_review(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    required_stage = "clean_candidate_ready" if args.kind == "clean" else "captioned_candidate_ready"
    if project["state"]["stage"] != required_stage:
        raise ValueError(f"当前阶段不能检查 {args.kind} 视频")
    candidate = args.project_dir / "outputs" / ("final.clean.mp4" if args.kind == "clean" else "final.subtitled.mp4")
    require_file(candidate, "待检查视频")
    summary = technical_summary(candidate)
    expected_ratio = 9 / 16 if project["settings"]["aspect_ratio"] == "9:16" else 16 / 9
    actual_ratio = summary["width"] / summary["height"] if summary["width"] and summary["height"] else 0
    resolution_matches = (
        abs(actual_ratio - expected_ratio) < 0.01
        if project["route"] == "postproduction_only"
        else (summary["width"], summary["height"]) == ((720, 1280) if project["settings"]["aspect_ratio"] == "9:16" else (1280, 720))
    )
    manual = _review_required(args.kind, args, project)
    evidence, transcript, speech_similarity = _load_review_evidence(args.kind, args, manual)
    inspected_visuals = None
    if args.kind == "clean" and manual.get("no_generated_text") == "pass":
        inspected_visuals = validate_text_review(evidence, candidate)
    tail_silence = detect_tail_silence(candidate)
    pacing_analysis = analyze_speech_pacing(candidate, project.get("script", ""), int(project["plan"].get("speech_rate_cpm") or 260)) if args.kind == "clean" and project["route"] == "video_generation" else None
    transcript_end_gap: float | None = None
    if transcript.suffix.lower() == ".srt":
        _, transcript_end = parse_srt_bounds(transcript)
        transcript_end_gap = max(0.0, summary["duration_seconds"] - transcript_end)
        transcript_overshoot = max(0.0, transcript_end - summary["duration_seconds"])
    else:
        transcript_overshoot = None
    automatic = {
        "has_video": summary["has_video"],
        "has_audio": summary["has_audio"],
        "duration_positive": summary["duration_seconds"] > 0,
        "resolution_matches": resolution_matches,
        **_duration_review_checks(summary["duration_seconds"], project["plan"], project["route"]),
        "speech_transcript_present": transcript.stat().st_size > 0,
        "speech_matches_confirmed_script": speech_similarity is None or speech_similarity >= 0.55,
        "has_effective_speech": float(tail_silence["tail_silence_seconds"]) < max(0.1, summary["duration_seconds"] - 0.2),
        "tail_silence_within_0_6_seconds": float(tail_silence["tail_silence_seconds"]) <= 0.6,
        "transcript_end_near_video_end": transcript_end_gap is None or transcript_end_gap <= 0.8,
        "transcript_end_within_video": transcript_overshoot is None or transcript_overshoot <= 0.25,
    }
    boundary_times: list[float] = []
    stitch_report_path = args.project_dir / "stitch-report.json"
    if stitch_report_path.exists():
        stitch_report = read_json(stitch_report_path)
        elapsed = 0.0
        effective_summaries = stitch_report.get("effective_input_summaries") or stitch_report.get("input_summaries", [])
        for item in effective_summaries[:-1]:
            elapsed += float(item.get("duration_seconds") or 0)
            boundary_times.append(elapsed)
    review_frames = [frame["path"] for frame in inspected_visuals["frames"]] if inspected_visuals else extract_review_frames(candidate, args.project_dir / "review-frames" / args.kind, boundary_times)
    passed = all(automatic.values()) and all(value == "pass" for value in manual.values())
    evidence_dir = args.project_dir / "review-evidence" / args.kind
    evidence_snapshot = _copy_input(args.evidence_file, evidence_dir / "observations.json")
    transcript_snapshot = _copy_input(transcript, evidence_dir / f"speech-transcript{transcript.suffix.lower() or '.txt'}")
    report = {
        "schema_version": "1.0",
        "kind": args.kind,
        "candidate": summary,
        "candidate_sha256": sha256_file(candidate),
        "automatic_checks": automatic,
        "manual_codex_checks": manual,
        "manual_evidence": evidence,
        "evidence_snapshot": evidence_snapshot,
        "speech_transcript_snapshot": transcript_snapshot,
        "speech_script_similarity": speech_similarity,
        "tail_silence_analysis": tail_silence,
        "speech_pacing_analysis": pacing_analysis,
        "transcript_end_gap_seconds": round(transcript_end_gap, 3) if transcript_end_gap is not None else None,
        "review_frames": [{"path": item, "sha256": sha256_file(Path(item))} for item in review_frames],
        "notes": args.notes,
        "status": "pass" if passed else "blocked",
        "reviewed_at": now_iso(),
        "paid_regeneration_authorized": False,
    }
    atomic_write_json(args.project_dir / f"{args.kind}-review.json", report)
    if passed:
        project["state"]["stage"] = "clean_review_passed" if args.kind == "clean" else "captioned_review_passed"
        save_project(args.project_dir, project)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def command_inspect_visuals(args: argparse.Namespace) -> None:
    video = require_file(args.video, "待检查视频")
    frames = extract_review_frames(video, args.output_dir, args.boundary or [])
    print(json.dumps({"manifest_file": str((args.output_dir / "manifest.json").resolve()),
        "manifest_sha256": sha256_file(args.output_dir / "manifest.json"), "frame_count": len(frames),
        "status": "awaiting_visual_review", "last_frame": frames[-1]}, ensure_ascii=False, indent=2))


def _extract_srt_text(path: Path) -> str:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    kept = [line.strip() for line in lines if line.strip() and not line.strip().isdigit() and "-->" not in line]
    return "".join(kept)


def _write_confirmed_srt(script: str, timing_srt: Path, output: Path) -> None:
    sentences = split_sentences(script)
    if not sentences:
        raise ValueError("没有可用于字幕的确认文字")
    start, end = parse_srt_bounds(timing_srt)
    total = max(0.1, end - start)
    weights = [max(1, len(re.sub(r"\s+", "", sentence))) for sentence in sentences]
    weight_total = sum(weights)
    cursor = start
    blocks: list[str] = []
    for index, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
        next_cursor = end if index == len(sentences) else cursor + total * weight / weight_total
        blocks.append(f"{index}\n{srt_timestamp(cursor)} --> {srt_timestamp(next_cursor)}\n{sentence}\n")
        cursor = next_cursor
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(blocks), encoding="utf-8")


def command_subtitles(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    if project["state"]["stage"] != "clean_review_passed":
        raise ValueError("必须先完成干净视频检查")
    if project["settings"]["subtitle_choice"] != "enabled":
        raise ValueError("方案没有确认启用字幕")
    clean_review = read_json(args.project_dir / "clean-review.json")
    if clean_review.get("status") != "pass":
        raise ValueError("干净视频尚未通过检查，不能制作字幕")
    clean_video = require_file(args.project_dir / "outputs" / "final.clean.mp4", "干净视频")
    timing_srt: Path
    if args.timing_srt:
        timing_srt = require_file(args.timing_srt, "测试/确认时间轴")
    else:
        whisper = shutil.which("whisper-cli")
        if not whisper:
            raise RuntimeError("缺少 whisper-cli")
        model = args.whisper_model or (Path(os.environ["SHUZIRENSKIL_WHISPER_MODEL"]) if os.environ.get("SHUZIRENSKIL_WHISPER_MODEL") else None)
        if not model:
            raise ValueError("需要 --whisper-model 或 SHUZIRENSKIL_WHISPER_MODEL")
        model = require_file(model, "Whisper 模型")
        audio = args.project_dir / "subtitles" / "final-audio.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("缺少 ffmpeg")
        run([ffmpeg, "-y", "-i", str(clean_video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
        prefix = args.project_dir / "subtitles" / "whisper-timing"
        subprocess.run([whisper, "-m", str(model), "-f", str(audio), "-osrt", "-of", str(prefix)], check=True)
        timing_srt = prefix.with_suffix(".srt")
    script = project.get("script", "")
    if not script and project.get("inputs", {}).get("subtitle_source"):
        script = _extract_srt_text(Path(project["inputs"]["subtitle_source"]["path"]))
    confirmed_srt = args.project_dir / "subtitles" / "confirmed-script.srt"
    _write_confirmed_srt(script, timing_srt, confirmed_srt)
    output = args.project_dir / "outputs" / "final.subtitled.mp4"
    escaped = str(confirmed_srt).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg")
    run([
        ffmpeg, "-y", "-i", str(clean_video),
        "-vf", f"subtitles='{escaped}':force_style='FontSize=18,MarginV=72,Outline=2,Shadow=0,Alignment=2'",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "copy", "-movflags", "+faststart",
        str(output),
    ])
    evidence = {
        "timing_source": str(timing_srt.resolve()),
        "lexical_source": "confirmed_script",
        "confirmed_srt": str(confirmed_srt.resolve()),
        "output": technical_summary(output),
        "provider_payload_used_subtitles": False,
    }
    atomic_write_json(args.project_dir / "subtitle-report.json", evidence)
    project["state"]["stage"] = "captioned_candidate_ready"
    save_project(args.project_dir, project)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


def command_finalize(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    review_kind = "captioned" if project["settings"]["subtitle_choice"] == "enabled" else "clean"
    required_stage = "captioned_review_passed" if review_kind == "captioned" else "clean_review_passed"
    if project["state"]["stage"] != required_stage:
        raise ValueError("当前阶段不能生成交付清单")
    review = read_json(args.project_dir / f"{review_kind}-review.json")
    if review.get("status") != "pass":
        raise ValueError("最终候选视频尚未通过全部检查")
    if project["route"] == "video_generation":
        jobs = read_json(args.project_dir / "jobs.json")
        if any(job.get("submission_attempts", 0) > 1 for job in jobs["jobs"]):
            raise ValueError("检测到片段重复提交，禁止交付")
        paid_count = sum(job.get("submission_attempts", 0) for job in jobs["jobs"])
    else:
        paid_count = 0
    candidate = args.project_dir / "outputs" / ("final.subtitled.mp4" if review_kind == "captioned" else "final.clean.mp4")
    if sha256_file(candidate) != review.get("candidate_sha256"):
        raise ValueError("最终视频在质检后发生变化，必须重新检查")
    for key in ("evidence_snapshot", "speech_transcript_snapshot"):
        snapshot = review.get(key, {})
        snapshot_path = Path(snapshot.get("path", ""))
        if not snapshot_path.is_file() or sha256_file(snapshot_path) != snapshot.get("sha256"):
            raise ValueError("质检证据在审核后发生变化，必须重新检查")
    if project["settings"]["subtitle_choice"] == "enabled":
        clean_review = read_json(args.project_dir / "clean-review.json")
        clean_candidate = args.project_dir / "outputs" / "final.clean.mp4"
        if clean_review.get("status") != "pass" or sha256_file(clean_candidate) != clean_review.get("candidate_sha256"):
            raise ValueError("字幕项目的干净视频检查记录已失效")
    else:
        clean_review = review
        clean_candidate = candidate
    clean_snapshot = clean_review.get("evidence_snapshot", {})
    clean_observations = require_file(Path(clean_snapshot.get("path", "")), "干净版检查证据")
    if sha256_file(clean_observations) != clean_snapshot.get("sha256"):
        raise ValueError("干净版检查证据在审核后发生变化")
    clean_evidence = read_json(clean_observations)
    if clean_evidence.get("text_review") or project["settings"].get("character_review_required"):
        validate_text_review(clean_evidence, clean_candidate)
    delivery = {
        "schema_version": "1.0",
        "status": "pass",
        "project_id": project["project_id"],
        "final_video": {"path": str(candidate.resolve()), "sha256": sha256_file(candidate), **technical_summary(candidate)},
        "subtitle_enabled": project["settings"]["subtitle_choice"] == "enabled",
        "paid_submission_count": paid_count,
        "automatic_retry_count": 0,
        "clean_review": str((args.project_dir / "clean-review.json").resolve()),
        "final_review": str((args.project_dir / f"{review_kind}-review.json").resolve()),
        "provider_runtime_paid_verified": bool(read_json(args.project_dir / "model-snapshot.json").get("runtime_paid_verified")),
        "provider_runtime_paid_verification_scope": read_json(args.project_dir / "model-snapshot.json").get("runtime_paid_verified_scope"),
        "remaining_unverified": [
            "MikuAPI 已验证 4 秒常规短片和 1 秒提示词人声短片；长句、长视频连续性和 720p/1080p 仍未验证",
            "用户原音色复刻、长句口播准确率和跨段声音一致性",
        ],
        "finalized_at": now_iso(),
    }
    ensure_no_secret_text(delivery)
    atomic_write_json(args.project_dir / "delivery-manifest.json", delivery)
    project["state"]["stage"] = "delivered"
    save_project(args.project_dir, project)
    print(json.dumps(delivery, ensure_ascii=False, indent=2))


def command_status(args: argparse.Namespace) -> None:
    project = load_project(args.project_dir)
    value: dict[str, Any] = {"project_id": project["project_id"], "route": project["route"], "state": project["state"], "plan": project["plan"]}
    if (args.project_dir / "jobs.json").exists():
        value["jobs"] = read_json(args.project_dir / "jobs.json")["jobs"]
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="shuzirenskil guarded workflow")
    parser.add_argument("--project-dir", type=Path, help="除只读 plan 外，所有命令必须提供项目目录")
    parser.add_argument("--env-file", type=Path, help="可选的私密 .env；必须是普通文件且权限为 600")
    parser.add_argument(
        "--provider",
        choices=["91topgo", "mikuapi"],
        help="视频 Provider；默认使用配置链的首选 91topgo，mikuapi 仅作为显式降级",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft_plan = subparsers.add_parser("plan", help="方案阶段的免费本地时长规划，不创建项目")
    draft_plan.add_argument("--script-file", type=Path, required=True)
    draft_plan.add_argument("--duration", type=int, help="exact 的用户目标；content-fit 的用户上限；无要求时省略")
    draft_plan.add_argument("--duration-mode", choices=["auto", "exact", "content-fit"], default="auto")
    draft_plan.add_argument("--speech-rate-cpm", type=int, default=260)
    draft_plan.set_defaults(handler=command_plan)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--name", required=True)
    prepare.add_argument("--script-file", type=Path)
    prepare.add_argument("--existing-video", type=Path)
    prepare.add_argument("--subtitle-source", type=Path)
    prepare.add_argument("--duration", type=int, help="用户目标或上限秒数；未指定时按内容规划")
    prepare.add_argument(
        "--duration-mode",
        choices=["auto", "exact", "content-fit"],
        default="auto",
        help="exact 对应用户指定时长；content-fit/auto 按内容规划，--duration 仅为上限，不按 15 的倍数猜测意图",
    )
    prepare.add_argument("--language", default="中文")
    prepare.add_argument("--aspect-ratio", choices=["9:16", "16:9"], default="9:16")
    prepare.add_argument("--resolution", choices=["480p", "720p"], default="720p")
    prepare.add_argument("--subtitles", choices=["enabled", "disabled"], default="disabled")
    prepare.add_argument("--generation-mode", choices=["image-to-video", "reference-to-video"], default="image-to-video")
    prepare.add_argument("--video-prompt-spec", type=Path, help="结构化视频提示词方案 JSON")
    prepare.add_argument("--allow-legacy-performance-plan", action="store_true", help="仅用于迁移旧流程；新数字人口播默认要求每段真实台词表演节拍")
    prepare.add_argument("--no-pilot-first", action="store_true", help="仅供兼容旧流程；多段新项目默认先检查首段试片")
    prepare.add_argument("--preset-voice", action="append", help="最多 3 个 xAI 预设 voice_id；MikuAPI 透传验收前不会进入付费预检")
    prepare.add_argument(
        "--duration-capability-test",
        action="store_true",
        help="明确测试单片段时长上限；请求秒数锁定为 --duration，范围 1-15 秒",
    )
    prepare.set_defaults(handler=command_prepare)

    confirm_plan = subparsers.add_parser("confirm-plan")
    confirm_plan.add_argument("--approved-by", required=True)
    confirm_plan.set_defaults(handler=command_confirm_plan)

    generate_image_parser = subparsers.add_parser("generate-image")
    generate_image_parser.add_argument("--prompt-file", type=Path, required=True)
    generate_image_parser.add_argument("--source-image", type=Path, help="可选：本人形象参考图，通过 gpt-image-2 图片编辑接口传入")
    generate_image_parser.add_argument("--continuity-spec", type=Path, help="多段视频必需的一致性 JSON")
    generate_image_parser.add_argument("--provider-image-url", help="可选：生成图片对应的公共 HTTPS 地址")
    generate_image_parser.add_argument("--input-transport", choices=["auto", "public-https", "jpeg-data-uri", "provider-file"], default="auto", help="auto 优先使用 HTTPS，91topgo 无 HTTPS 地址时使用本地 JPEG Data URI")
    generate_image_parser.add_argument("--config", type=Path)
    generate_image_parser.add_argument("--timeout", type=float, default=180.0)
    generate_image_parser.set_defaults(handler=command_generate_image)

    bind = subparsers.add_parser("bind-image")
    bind.add_argument("--image", type=Path, action="append", help="兼容旧用法：只接受一张主参考图")
    bind.add_argument("--canonical-image", type=Path, help="确认后的主参考图")
    bind.add_argument("--segment-image", type=Path, action="append", help="从主参考图派生的分段首帧")
    bind.add_argument("--reference-image", type=Path, action="append", help="reference-to-video 的额外参考图，连同主图最多 7 张")
    bind.add_argument("--reference-role", action="append", choices=["identity", "wardrobe", "product", "scene", "style", "composition"], help="按图片顺序声明每张参考图的唯一职责")
    bind.add_argument("--storyboard-image", type=Path, action="append", help="仅预览、不发送给视频接口的分镜图")
    bind.add_argument("--continuity-spec", type=Path, help="多段视频必需的一致性 JSON")
    bind.add_argument("--provider-image-url", action="append", help="视频模型读取的公共 HTTPS 图片地址；可传 1 个共用或每段 1 个")
    bind.add_argument("--provider-reference-image-url", action="append", help="额外参考图的公共 HTTPS 地址；顺序必须与 --reference-image 一致")
    bind.add_argument("--input-transport", choices=["auto", "public-https", "jpeg-data-uri", "provider-file"], default="auto", help="auto 优先使用 HTTPS，91topgo 无 HTTPS 地址时使用本地 JPEG Data URI")
    bind.add_argument(
        "--allow-derived-segment-images-with-risk",
        action="store_true",
        help="仅当用户明确接受多段人物和音色漂移风险时，允许每段使用派生图",
    )
    bind.set_defaults(handler=command_bind_image)

    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--approved-by", required=True)
    authorize.add_argument("--confirmation-intent", required=True)
    authorize.add_argument("--max-paid-submissions", type=int, required=True)
    authorize.add_argument("--acknowledge-long-video-risk", action="store_true")
    authorize.set_defaults(handler=command_authorize)

    character_review = subparsers.add_parser("review-character")
    character_review.add_argument("--evidence-file", type=Path, required=True)
    character_review.set_defaults(handler=command_review_character)

    inspect_visuals = subparsers.add_parser("inspect-visuals")
    inspect_visuals.add_argument("--video", type=Path, required=True)
    inspect_visuals.add_argument("--output-dir", type=Path, required=True)
    inspect_visuals.add_argument("--boundary", type=float, action="append")
    inspect_visuals.set_defaults(handler=command_inspect_visuals)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--config", type=Path)
    preflight.set_defaults(handler=command_preflight)

    check_provider = subparsers.add_parser("check-provider")
    check_provider.add_argument("--config", type=Path)
    check_provider.add_argument("--timeout", type=float, default=30.0)
    check_provider.set_defaults(handler=command_check_provider)

    upload_inputs = subparsers.add_parser("upload-inputs")
    upload_inputs.add_argument("--config", type=Path)
    upload_inputs.add_argument("--expires-after", type=int, help="Provider 文件自动删除秒数，范围 3600-2592000")
    upload_inputs.add_argument("--timeout", type=float, default=120.0)
    upload_inputs.set_defaults(handler=command_upload_inputs)

    submit = subparsers.add_parser("submit")
    submit.add_argument("--timeout", type=float, default=60.0)
    submit.set_defaults(handler=command_submit)

    poll = subparsers.add_parser("poll")
    poll.add_argument("--timeout", type=float, default=60.0)
    poll.add_argument("--interval", type=float, default=5.0)
    poll.add_argument("--max-polls", type=int, default=120)
    poll.set_defaults(handler=command_poll)

    pilot_review = subparsers.add_parser("review-pilot")
    pilot_review.add_argument("--evidence-file", type=Path, required=True, help="首段正常速度完整看听后的逐项观察 JSON")
    pilot_review.set_defaults(handler=command_review_pilot)

    stitch_parser = subparsers.add_parser("stitch")
    stitch_parser.set_defaults(handler=command_stitch)

    review = subparsers.add_parser("review")
    review.add_argument("--kind", choices=["clean", "captioned"], required=True)
    for name in ["visual-consistency", "speech-complete", "stitching-quality", "critical-facts", "no-generated-text", "voice-consistency", "voice-casting-match", "motion-naturalness", "gesture-phrase-fit", "lip-sync", "voice-naturalness", "speech-pacing", "normal-speed-playback", "subtitle-safe", "subtitle-matches", "no-unapproved-text"]:
        review.add_argument(f"--{name}", choices=["pass", "fail", "pending"], default="pending", dest=name.replace("-", "_"))
    review.add_argument("--notes", default="")
    review.add_argument("--evidence-file", type=Path, required=True, help="Codex 的逐项观察和最终语音转写路径")
    review.set_defaults(handler=command_review)

    subtitles = subparsers.add_parser("subtitles")
    subtitles.add_argument("--whisper-model", type=Path)
    subtitles.add_argument("--timing-srt", type=Path, help="仅用于确认过的现成时间轴或离线测试")
    subtitles.set_defaults(handler=command_subtitles)

    finalize = subparsers.add_parser("finalize")
    finalize.set_defaults(handler=command_finalize)

    status = subparsers.add_parser("status")
    status.set_defaults(handler=command_status)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command not in {"plan", "inspect-visuals"} and args.project_dir is None:
        parser.error("此命令需要 --project-dir")
    try:
        if args.command in {"plan", "inspect-visuals"}:
            args.handler(args)
        else:
            with project_lock(args.project_dir):
                args.handler(args)
    except (ValueError, RuntimeError, ProviderError, ImageProviderError, subprocess.CalledProcessError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
