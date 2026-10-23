"""Offline request compilers. Asset tokens are intentionally not uploadable URLs."""
from __future__ import annotations
from model_registry import allowed_durations


def compile_preview(profile: dict, *, prompt: str, seconds: int, resolution: str,
                    ratio: str, images: list[str], audio: str | None = None) -> dict:
    if type(seconds) is not int or seconds not in allowed_durations(profile):
        raise ValueError("请求时长不在此模型组合的允许选项内")
    if resolution not in profile["resolutions"] or ratio not in profile["aspect_ratios"]:
        raise ValueError("分辨率或画幅未登记为此组合支持")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
        raise ValueError("请求提示词须为 1–4000 字")
    if not 1 <= len(images) <= profile["max_images"]:
        raise ValueError("参考图数量不符合此模型组合")
    if audio is not None and profile.get("reference_timbre") is not True:
        raise ValueError("此输入模式不能发送参考音频")
    if any(not isinstance(x, str) or not x.startswith("asset://") for x in images + ([audio] if audio else [])):
        raise ValueError("离线预览只接受 asset:// 素材指纹，不保存或提交真实上传地址")
    compiler = profile.get("compiler")
    if compiler is None:
        return {"status": "adapter_not_implemented", "execution_allowed": False, "payload": None}
    if compiler == "wan3":
        role = "reference_image" if profile["input_mode"] == "reference" else "first_frame"
        media = [{"type": role, "url": image} for image in images]
        if audio:
            media.append({"type": "reference_audio", "url": audio})
        payload = {"model": profile["model"], "input": {"prompt": prompt, "media": media},
                   "parameters": {"duration": seconds, "resolution": resolution.upper(), "ratio": ratio,
                                  "prompt_extend": False}}
        endpoint = "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
    elif compiler == "minimax_h3":
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": image}, "role": "reference_image"} for image in images]
        if audio:
            content.append({"type": "audio_url", "audio_url": {"url": audio}, "role": "reference_audio"})
        payload = {"model": profile["model"], "content": content, "duration": seconds,
                   "resolution": resolution.upper(), "ratio": ratio}
        endpoint = "https://api.minimax.io/v2/video_generation"
    elif compiler in {"seedance25", "seedance20"}:
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": image}, "role": "reference_image"} for image in images]
        if audio:
            content.append({"type": "audio_url", "audio_url": {"url": audio}, "role": "reference_audio"})
        payload = {"model": profile["model"], "content": content, "duration": seconds,
                   "resolution": resolution, "ratio": ratio, "generate_audio": True}
        endpoint = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    elif compiler == "veo31":
        payload = {"model": profile["model"], "instances": [{"prompt": prompt,
                   "referenceImages": [{"image": {"asset_token": image}, "referenceType": "asset"} for image in images]}],
                   "parameters": {"durationSeconds": seconds, "resolution": resolution, "aspectRatio": ratio}}
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models/{model}:predictLongRunning"
    elif compiler == "aihge_grok":
        payload = {"model": profile["model"], "prompt": prompt, "duration": seconds, "resolution": resolution, "aspect_ratio": ratio, "reference_images": [{"url": x} for x in images]}
        endpoint = "https://api-hk.aihge.com/v1/videos"
    elif compiler == "legacy_preview":
        payload = {"model": profile["model"], "prompt": prompt, "seconds": str(seconds), "reference_images": [{"url": x} for x in images]}
        endpoint = "https://router.91topgo.com/v1/videos"
    else:
        raise ValueError("未实现的请求编译器")
    return {"status": "offline_preview", "execution_allowed": False, "method": "POST",
            "endpoint_template": endpoint, "contains_asset_placeholders": True, "payload": payload}
