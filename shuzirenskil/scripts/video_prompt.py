#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


PROMPT_SPEC_VERSION = "3.0"
CASTING_FIELDS = ("character_key", "role", "visual_basis", "rationale", "resonance", "texture", "articulation", "avoid")
CASTING_SOUND_FIELDS = ("resonance", "texture", "articulation", "avoid")
NO_TEXT_RULE = "画面从第一帧到最后一帧均不出现任何文字、字幕、标题、金句、片尾卡、Logo、水印或 UI；台词和表演触发词仅用于声音与动作，不得转为屏幕文字。末句说完继续保持同一人物和场景，自然收声，不生成总结字卡。"
MAX_PROMPT_CHARS = 4096
VOICE_PROFILE_FIELDS = (
    "gender",
    "age_impression",
    "pitch",
    "timbre",
    "pace",
    "emotion",
    "pronunciation",
    "accent",
)
REFERENCE_ROLE_VALUES = {
    "identity",
    "wardrobe",
    "product",
    "scene",
    "style",
    "composition",
}
RIGID_NARRATION_TIMING_RE = re.compile(r"(?:前|中间|最后|开头|结尾|第)[^。；，,]{0,12}(?:\d+(?:\.\d+)?|[一二三四五六七八九十]+)\s*秒|(?:\d+(?:\.\d+)?|[一二三四五六七八九十]+)\s*秒[^。；，,]{0,10}(?:提问|讲|说|总结|收尾|台词|念)")


def default_prompt_spec() -> dict[str, Any]:
    return {
        "schema_version": PROMPT_SPEC_VERSION,
        "visual_style": "真实摄影质感，自然皮肤纹理，稳定的人脸和身体比例",
        "shot": "平视中近景，人物居中，镜头稳定，轻微自然推近",
        "performance": "自然看向镜头，口型清楚，保持轻微呼吸、自然眨眼；只在语义需要时短暂做克制手势并放回",
        "speech_rate_cpm": 260,
        "performance_beats": [],
        "voice_casting": None,
        "voice_profile": {
            "gender": "中性",
            "age_impression": "成年",
            "pitch": "中等偏低的基础音高，允许随语义自然起伏",
            "timbre": "温和、清晰、可信，不模仿任何特定真人",
            "pace": "自然偏利落，目标约每分钟 260 个汉字；标点按句意短暂停顿",
            "emotion": "亲切可信，随句意有适度情绪变化",
            "pronunciation": "普通话吐字清晰，句首和句尾完整",
            "accent": "无明显地域口音",
        },
        "speech_delivery": "语速自然，发音清晰，句首和句尾完整，情绪亲切可信",
        "ambience": "安静、自然、不过度的室内环境声，不遮盖人声",
        "music": "无背景音乐",
        "negative_constraints": [
            "不得改变人物身份、脸型、发型、服装和身体比例",
            "不得出现字幕、文字、标题、Logo、水印、UI 或文字背景条",
            "不得出现额外人物、重复人物、换脸、肢体畸变、闪烁或跳切",
            "不得加入与台词冲突的口型、旁白或背景人声",
        ],
    }


def validate_prompt_spec(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("视频提示词方案必须是 JSON 对象")
    if value.get("schema_version") not in {"2.0", PROMPT_SPEC_VERSION}:
        raise ValueError(f"视频提示词方案 schema_version 必须是 2.0 或 {PROMPT_SPEC_VERSION}")
    result = default_prompt_spec()
    result["schema_version"] = value["schema_version"]
    if value["schema_version"] == "2.0":
        del result["voice_casting"]
    unknown_fields = sorted(set(value) - set(result))
    if unknown_fields:
        raise ValueError("视频提示词方案存在未知字段: " + ", ".join(unknown_fields))
    if value.get("voice_casting") is not None:
        casting = value["voice_casting"]
        if not isinstance(casting, dict) or set(casting) != set(CASTING_FIELDS):
            raise ValueError("voice_casting 必须完整包含: " + ", ".join(CASTING_FIELDS))
        for field in CASTING_FIELDS:
            item = casting[field]
            if not isinstance(item, str) or not 2 <= len(item.strip()) <= 200 or any(word in item for word in ("请填写", "请替换", "待填写")):
                raise ValueError(f"voice_casting.{field} 必须是实际设计的 2-200 字文本，不能保留模板占位符")
        result["voice_casting"] = {key: casting[key].strip() for key in CASTING_FIELDS}
    for field in ("visual_style", "shot", "performance", "speech_delivery", "ambience", "music"):
        if field in value:
            item = value[field]
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 500:
                raise ValueError(f"视频提示词字段 {field} 必须是 1-500 字的非空文本")
            result[field] = item.strip()
    if "speech_rate_cpm" in value:
        rate = value["speech_rate_cpm"]
        if isinstance(rate, bool) or not isinstance(rate, int) or not 180 <= rate <= 320:
            raise ValueError("speech_rate_cpm 必须是 180-320 的整数（每分钟汉字数）")
        result["speech_rate_cpm"] = rate
    if "performance_beats" in value:
        beats = value["performance_beats"]
        if not isinstance(beats, list) or len(beats) > 24:
            raise ValueError("performance_beats 必须是最多 24 项的数组")
        cleaned_beats = []
        for beat in beats:
            required = {"segment_index", "trigger_phrase", "expression"}
            if not isinstance(beat, dict) or not required.issubset(beat) or set(beat) - (required | {"gesture"}):
                raise ValueError("每个表演节拍必须包含 segment_index、trigger_phrase、expression；gesture 可选")
            if isinstance(beat["segment_index"], bool) or not isinstance(beat["segment_index"], int) or beat["segment_index"] < 1:
                raise ValueError("表演节拍 segment_index 必须是正整数")
            if any(not isinstance(beat[key], str) or not beat[key].strip() or len(beat[key]) > 100 for key in required - {"segment_index"}):
                raise ValueError("表演节拍的台词触发词和表情必须是 1-100 字文本")
            gesture = beat.get("gesture")
            if gesture is not None and (not isinstance(gesture, str) or len(gesture.strip()) > 100):
                raise ValueError("表演节拍的手势必须是 0-100 字文本")
            cleaned = {key: beat[key].strip() if isinstance(beat[key], str) else beat[key] for key in required}
            if isinstance(gesture, str) and gesture.strip():
                cleaned["gesture"] = gesture.strip()
            cleaned_beats.append(cleaned)
        result["performance_beats"] = cleaned_beats
    if "voice_profile" in value:
        profile = value["voice_profile"]
        if not isinstance(profile, dict):
            raise ValueError("voice_profile 必须是包含 8 项固定特征的 JSON 对象")
        unknown = sorted(set(profile) - set(VOICE_PROFILE_FIELDS))
        missing = [field for field in VOICE_PROFILE_FIELDS if not isinstance(profile.get(field), str) or not profile[field].strip()]
        if unknown or missing:
            detail = []
            if missing:
                detail.append("缺少 " + ", ".join(missing))
            if unknown:
                detail.append("未知 " + ", ".join(unknown))
            raise ValueError("voice_profile 字段不完整: " + "；".join(detail))
        cleaned_profile: dict[str, str] = {}
        for field in VOICE_PROFILE_FIELDS:
            item = profile[field].strip()
            if len(item) > 200:
                raise ValueError(f"voice_profile.{field} 不能超过 200 字")
            cleaned_profile[field] = item
        result["voice_profile"] = cleaned_profile
    if "negative_constraints" in value:
        items = value["negative_constraints"]
        if not isinstance(items, list) or not 1 <= len(items) <= 12:
            raise ValueError("negative_constraints 必须包含 1-12 条限制")
        cleaned: list[str] = []
        for item in items:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 300:
                raise ValueError("每条 negative_constraints 必须是 1-300 字的文本")
            cleaned.append(item.strip())
        result["negative_constraints"] = cleaned
    return result


def validate_new_narration_plan(spec: dict[str, Any], plan: dict[str, Any]) -> None:
    """新项目的付费前节奏关口；不改写已建立项目的旧合同。"""
    if any(RIGID_NARRATION_TIMING_RE.search(item) for item in (spec["speech_delivery"], spec["voice_profile"]["pace"])):
        raise ValueError("声音提示词含固定秒数分配；请改为自然语速和语义停顿，不要让模型按秒数慢读台词")
    stated_rates = [int(value) for value in re.findall(r"每分钟\s*(\d{3})\s*(?:个)?汉字", spec["voice_profile"]["pace"])]
    if stated_rates and any(abs(rate - spec["speech_rate_cpm"]) > 15 for rate in stated_rates):
        raise ValueError("voice_profile.pace 声明的语速与 speech_rate_cpm 不一致；请统一规划和生成提示词的每分钟汉字数")


def voice_signature(prompt_spec: dict[str, Any]) -> str:
    """锁定完整配音方案；旧版八字段的指纹保持兼容。并非声纹识别。"""
    spec = validate_prompt_spec(prompt_spec)
    profile = spec["voice_profile"]
    normalized = {field: profile[field] for field in VOICE_PROFILE_FIELDS}
    if spec.get("voice_casting"):
        normalized["voice_casting"] = spec["voice_casting"]
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def require_voice_casting(spec: dict[str, Any]) -> None:
    spec = validate_prompt_spec(spec)
    if spec["schema_version"] != "3.0" or not spec.get("voice_casting"):
        raise ValueError("新制作必须使用 3.0 方案并设计 voice_casting；不能直接沿用默认男女声音")
    default = default_prompt_spec()["voice_profile"]
    if spec["voice_profile"]["pitch"] == default["pitch"] and spec["voice_profile"]["timbre"] == default["timbre"]:
        raise ValueError("请针对角色设计 voice_profile 的音高和音色，不能原样使用默认配音模板")


def voice_identity_signature(prompt_spec: dict[str, Any]) -> str:
    spec = validate_prompt_spec(prompt_spec)
    identity = {key: spec["voice_profile"][key] for key in VOICE_PROFILE_FIELDS if key not in {"pace", "emotion"}}
    casting = spec.get("voice_casting") or {}
    identity.update({key: casting.get(key) for key in CASTING_SOUND_FIELDS})
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def render_voice_profile(profile: dict[str, str]) -> str:
    labels = {
        "gender": "性别听感",
        "age_impression": "年龄感",
        "pitch": "音高",
        "timbre": "音色",
        "pace": "语速",
        "emotion": "情绪",
        "pronunciation": "发音",
        "accent": "口音",
    }
    return "；".join(f"{labels[field]}={profile[field]}" for field in VOICE_PROFILE_FIELDS)


def validate_reference_roles(roles: list[str], reference_count: int) -> list[str]:
    if reference_count < 1 or reference_count > 7:
        raise ValueError("reference-to-video 必须使用 1-7 张参考图")
    if len(roles) != reference_count:
        raise ValueError("每张参考图都必须有且只有一个用途角色")
    cleaned = [item.strip().lower() for item in roles]
    unknown = [item for item in cleaned if item not in REFERENCE_ROLE_VALUES]
    if unknown:
        raise ValueError(f"未知参考图角色: {', '.join(unknown)}")
    if "identity" not in cleaned:
        raise ValueError("数字人口播的多参考图至少要有一张 identity 身份图")
    return cleaned


def build_video_prompt(
    *,
    segment: dict[str, Any],
    language: str,
    continuity: dict[str, Any] | None,
    prompt_spec: dict[str, Any],
    generation_mode: str,
    reference_roles: list[str] | None = None,
    preset_voices: list[str] | None = None,
    reference_image_index_base: int = 1,
) -> str:
    spec = validate_prompt_spec(prompt_spec)
    parts: list[str] = []
    roles = reference_roles or []
    voices = preset_voices or []

    if generation_mode == "reference-to-video":
        roles = validate_reference_roles(roles, len(roles))
        role_lines = []
        for offset, role in enumerate(roles):
            tag = f"<IMAGE_{reference_image_index_base + offset}>"
            meaning = {
                "identity": "只负责锁定人物身份、脸型、发型和身体特征",
                "wardrobe": "只负责锁定服装款式、颜色和材质",
                "product": "只负责锁定产品外观、结构、颜色和比例",
                "scene": "只负责锁定场景布局和背景环境",
                "style": "只负责锁定摄影风格、色彩和质感",
                "composition": "只负责锁定景别、构图和机位关系",
            }[role]
            role_lines.append(f"{tag}{meaning}")
        parts.append("参考素材分工：" + "；".join(role_lines) + "。不要交换不同参考图的职责。")
    elif generation_mode != "image-to-video":
        raise ValueError("未知视频生成模式")

    if voices:
        if len(voices) > 3:
            raise ValueError("最多只能使用 3 个预设声音")
        voice_tags = "、".join(f"<AUDIO_{index}>" for index in range(len(voices)))
        parts.append(f"声音参考：说话主体使用 {voice_tags} 中已指定的预设声音；本片只有一个说话人时只使用 <AUDIO_0>。")
    else:
        parts.append(
            "声音来源：不使用外部音频或声音克隆，由模型根据提示词生成口播人声。"
            "声音身份锁定：同一项目的所有分段都使用同一把声音；"
            "保持同一人的性别听感、年龄感、基础音高、音色、发音和口音；"
            "允许随句意自然变化语调、语速和情绪，避免整段机械平读。"
            f"固定声音特征：{render_voice_profile(spec['voice_profile'])}。"
        )
        casting = spec.get("voice_casting")
        if casting:
            parts.append("角色声音锚点（每段原样复用）：" + "；".join(
                f"{label}={casting[key]}" for key, label in (("resonance", "共鸣位置"), ("texture", "声音质地"), ("articulation", "咬字习惯"), ("avoid", "避免的声音"))
            ) + "。以上为配音选角，不是根据外貌推断真人声音；不要只生成通用播音腔。")

    if continuity:
        parts.append(
            "连续性锁定："
            f"人物={continuity['character_identity']}；服装={continuity['wardrobe']}；"
            f"场景={continuity['scene']}；机位={continuity['camera']}；"
            f"灯光={continuity['lighting']}；动作范围={continuity['motion_policy']}。"
        )

    beats = [beat for beat in spec["performance_beats"] if beat["segment_index"] == segment["index"]]
    for beat in beats:
        if beat["trigger_phrase"] not in segment["script"]:
            raise ValueError(f"第 {segment['index']} 段表演触发词不在本段台词中: {beat['trigger_phrase']}")
    parts.extend([
        f"视觉质感：{spec['visual_style'].rstrip('。')}。",
        f"镜头设计：{spec['shot'].rstrip('。')}。",
        f"人物表演：{spec['performance'].rstrip('。')}。",
        f"台词：同一位主体用{language}完整说出“{segment['script']}”。{spec['speech_delivery'].rstrip('。')}。",
        f"声音设计：{spec['ambience']}；{spec['music']}。人声与嘴部动作同步，环境声不能盖住台词。",
        "时间与动作：从自然呼吸和目光交流开始；手势只在对应语义出现时轻微发生，随后自然放回，避免全程固定微笑、持续点头或重复动作；句尾自然收声，不在一句话中途切镜。",
    ])
    if beats:
        parts.append("本段表演节拍：" + "；".join(
            f"说到‘{beat['trigger_phrase']}’时，表情{beat['expression']}" + (f"，手势{beat['gesture']}" if beat.get("gesture") else "")
            for beat in beats
        ) + "。")
    if "unspoken_budget_seconds" in segment:
        parts.append(f"口播节奏：计划语速约每分钟 {spec['speech_rate_cpm']} 个汉字，按完整语义连贯讲述；手势跟随说话，不为动作等候或为填满画面拖长停顿。句尾正常收声。")
    parts.append("强制限制：" + "；".join(spec["negative_constraints"]) + "。")
    if spec["schema_version"] == "3.0":
        parts.append("无文字画面合同：" + NO_TEXT_RULE)
    prompt = "\n".join(parts)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"视频提示词超过 {MAX_PROMPT_CHARS} 字符，请精简方案")
    return prompt
