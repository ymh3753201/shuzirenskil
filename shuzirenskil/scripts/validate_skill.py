#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
REQUIRED = [
    "SKILL.md",
    "README.md",
    ".env.example",
    "assets/model-config.json",
    "assets/provider-chain.json",
    "assets/image-model-config.json",
    "assets/continuity-template.json",
    "assets/video-prompt-spec-template.json",
    "references/api-contract.md",
    "references/workflow.md",
    "references/quality-checklist.md",
    "references/long-video-consistency.md",
    "references/video-prompt-guide.md",
    "references/duration-planning.md",
    "scripts/workflow.py",
    "scripts/private_env.py",
    "scripts/image_transport.py",
    "scripts/image_provider.py",
    "scripts/provider.py",
    "scripts/video_prompt.py",
    "scripts/quality_contract.py",
    "references/voice-casting-and-text-review.md",
    "assets/character-review-template.json",
    "assets/text-review-template.json",
    "scripts/planner.py",
    "evals/evals.json",
]
FORBIDDEN_NAMES = {".env", "__pycache__", ".git"}
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{20,}")


def main() -> int:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (SKILL_DIR / relative).is_file():
            errors.append(f"缺少文件: {relative}")
    for path in SKILL_DIR.rglob("*"):
        if any(part in FORBIDDEN_NAMES for part in path.parts):
            errors.append(f"禁止打包的路径: {path.relative_to(SKILL_DIR)}")
        if path.is_file() and path.stat().st_size < 2_000_000:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if SECRET_RE.search(text):
                errors.append(f"疑似真实 API Key: {path.relative_to(SKILL_DIR)}")
            auth_header_pattern = "Authorization" + ": Bearer"
            if auth_header_pattern in text:
                errors.append(f"疑似鉴权内容: {path.relative_to(SKILL_DIR)}")
    try:
        chain = json.loads((SKILL_DIR / "assets/provider-chain.json").read_text(encoding="utf-8"))
        if chain.get("primary_provider") != "91topgo":
            errors.append("首选视频 Provider 必须是 91topgo")
        if "mikuapi" not in chain.get("fallback_providers", []):
            errors.append("Provider 链必须保留 mikuapi 降级")
        primary = chain.get("providers", {}).get("91topgo", {})
        if primary.get("base_url") != "https://router.91topgo.com" or primary.get("create_path") != "/v1/videos":
            errors.append("91topgo 地址或创建路径不符合文档")
        if not {"model", "prompt", "seconds"}.issubset(set(primary.get("documented_payload_fields", []))):
            errors.append("91topgo 文档字段合同缺少基础字段")
        if primary.get("default_generation_mode") != "reference-to-video":
            errors.append("91topgo 默认能力模式必须保持为 reference-to-video")
        if primary.get("supports_image_to_video") is not False or primary.get("supports_reference_images") is not True:
            errors.append("91topgo 必须关闭 image-to-video 并打开 reference_images")
        if primary.get("requires_public_https_images") is not False or primary.get("local_image_transport") != "jpeg_data_uri":
            errors.append("91topgo 必须允许本地 JPEG Data URI 参考图")
        if primary.get("reference_audio_field") is not None or primary.get("supports_audio_file_upload") is not False:
            errors.append("91topgo 不得打开音频文件上传")
        if primary.get("api_key_env") != "SHUZIRENSKIL_91TOPGO_API_KEY" or primary.get("prefer_keychain") is not True:
            errors.append("91topgo 必须使用 Provider 专用 Key 变量并优先读取钥匙串")
        if primary.get("api_key_env_aliases"):
            errors.append("91topgo 不得复用通用视频 Key 变量")
        if primary.get("runtime_paid_verified") is True and "91topgo_1s_text_to_video_success" not in str(primary.get("runtime_paid_verified_scope")):
            errors.append("91topgo 真实验证范围记录不准确")
    except Exception as exc:
        errors.append(f"Provider 链配置不可读取: {exc}")
    try:
        config = json.loads((SKILL_DIR / "assets/model-config.json").read_text(encoding="utf-8"))
        if config.get("provider") != "mikuapi.org" or config.get("base_url") != "https://mikuapi.org":
            errors.append("视频配置没有固定为 MikuAPI")
        if config.get("api_key_env") != "SHUZIRENSKIL_MIKUAPI_API_KEY" or config.get("prefer_keychain") is not True:
            errors.append("MikuAPI 必须使用 Provider 专用 Key 变量并优先读取钥匙串")
        if config.get("model") != "grok-imagine-video-1.5":
            errors.append("视频模型 ID 不正确")
        if config.get("canonical_model") != config.get("model") or config.get("provider_request_model") != config.get("model"):
            errors.append("官方标准模型和 MikuAPI 请求模型必须一致")
        if "grok-imagine-video-1.5-preview" not in config.get("official_model_aliases", []):
            errors.append("没有记录官方 preview 别名")
        if config.get("runtime_paid_verified") is not True:
            errors.append("没有记录 MikuAPI 已完成的真实短片验证")
        if config.get("runtime_paid_verified_scope") != "mikuapi_4s_480p_9_16_image_to_video_success":
            errors.append("没有准确记录 MikuAPI 4 秒短片的验证边界")
        if config.get("long_video_runtime_verified") is not False:
            errors.append("长视频被错误标记为已验证")
        if config.get("automatic_retry_allowed") is not False:
            errors.append("模型配置没有关闭自动重试")
        if config.get("default_voice_strategy") != "prompt_generated_voice" or config.get("prompt_generated_voice_enabled") is not True:
            errors.append("默认声音策略必须是提示词生成口播人声")
        if config.get("prompt_generated_voice_runtime_verified") is not True:
            errors.append("提示词生成人声没有记录真实运行验证")
        if config.get("prompt_generated_voice_runtime_verified_scope") != "mikuapi_1s_480p_16_9_base64_image_prompt_voice_with_aac":
            errors.append("提示词生成人声的真实验证边界记录不准确")
        if config.get("prompt_generated_voice_exact_speech_verified") is not True or config.get("prompt_generated_voice_exact_speech_text") != "你好":
            errors.append("提示词生成人声的精确台词听写证据记录不准确")
        if config.get("prompt_generated_voice_paid_attempt_count") != 1:
            errors.append("提示词生成人声付费提交次数记录不准确")
        if config.get("custom_audio_fallback") != "prompt_generated_voice":
            errors.append("自有音频不可用时没有回退到提示词生成人声")
        official = config.get("official_native_capabilities", {})
        if official.get("reference_image_max_count") != 7 or official.get("preset_voice_max_count") != 3:
            errors.append("官方多参考图或预设声音能力上限记录不正确")
        if official.get("custom_audio_file_reference") != "trusted_partners_on_request":
            errors.append("自有音频文件的可信合作方限制记录不正确")
        if config.get("files_path") != "/v1/files" or config.get("file_upload_max_bytes") != 48 * 1024 * 1024:
            errors.append("Files API 路径或 48MB 安全上限不正确")
        for field in (
            "gateway_reference_to_video_implemented",
            "gateway_preset_voice_implemented",
            "gateway_files_api_implemented",
        ):
            if config.get(field) is not True:
                errors.append(f"新能力实现标记缺失: {field}")
        for field in (
            "gateway_reference_to_video_enabled",
            "gateway_preset_voice_enabled",
            "gateway_files_api_enabled",
            "gateway_custom_audio_reference_enabled",
        ):
            if config.get(field) is not False:
                errors.append(f"MikuAPI 未验收能力必须默认关闭: {field}")
        if config.get("gateway_custom_audio_reference_implemented") is not False:
            errors.append("自有音频上传尚未实现，不能标记为已实现")
        if config.get("trusted_partner_custom_audio_entitlement_confirmed") is not False:
            errors.append("没有证据证明自有音频可信合作方权限")
        if config.get("gateway_files_api_runtime_result") != "http_404_before_any_video_submission":
            errors.append("没有准确记录 MikuAPI Files API 的 HTTP 404 实测结果")
    except Exception as exc:
        errors.append(f"模型配置不可读取: {exc}")
    try:
        prompt_spec = json.loads((SKILL_DIR / "assets/video-prompt-spec-template.json").read_text(encoding="utf-8"))
        expected_voice_fields = {"gender", "age_impression", "pitch", "timbre", "pace", "emotion", "pronunciation", "accent"}
        if prompt_spec.get("schema_version") != "3.0" or set(prompt_spec.get("voice_profile", {})) != expected_voice_fields or not prompt_spec.get("voice_casting"):
            errors.append("视频提示词模板没有使用 3.0 角色声音设计")
    except Exception as exc:
        errors.append(f"视频提示词模板不可读取: {exc}")
    if errors:
        print("\n".join(f"ERROR: {item}" for item in errors))
        return 1
    print(f"PASS: {len(REQUIRED)} 个必需文件存在，未发现密钥、缓存或危险打包项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
