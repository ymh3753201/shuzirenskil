"""Conservative, provider/mode-scoped capabilities; never a generation entitlement."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from _common import read_json, digest_json

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "assets/model-registry.json"


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    registry = read_json(path)
    if registry.get("schema_version") != "1.0" or not isinstance(registry.get("profiles"), dict):
        raise ValueError("模型能力表格式错误")
    for key, profile in registry["profiles"].items():
        if profile.get("id") != key:
            raise ValueError("模型能力表 ID 不一致")
        allowed_durations(profile)
        if not profile.get("sources") or not profile.get("reviewed_on"):
            raise ValueError(f"{key} 缺少能力证据")
        date.fromisoformat(profile["reviewed_on"])
    return registry


def allowed_durations(profile: dict) -> list[int]:
    values = profile.get("duration_seconds")
    if not isinstance(values, list) or not values or any(type(x) is not int or not 1 <= x <= 300 for x in values):
        raise ValueError("模型时长必须是 1–300 秒之间的整数选项")
    if values != sorted(set(values)):
        raise ValueError("模型时长选项必须递增且无重复")
    return values


def get_profile(key: str) -> dict:
    profiles = load_registry()["profiles"]
    if key not in profiles:
        raise ValueError(f"未登记的模型组合: {key}；不能仅替换模型名继续生成")
    result = deepcopy(profiles[key])
    result["snapshot_digest"] = digest_json(result)
    result["capability_warning"] = "文档与本地适配状态不证明当前账号权限或成片质量"
    if (date.today() - date.fromisoformat(result["reviewed_on"])).days > 14:
        result["capability_warning"] += "；能力资料已超过 14 天，付费前需重新核对"
    return result


def resolve_voice(profile: dict, choice: dict) -> dict:
    """A provided audio file is inert unless the user explicitly selects it."""
    if not isinstance(choice, dict) or set(choice) - {"mode", "consistency", "audio_asset"}:
        raise ValueError("voice 仅接受 mode、consistency、audio_asset")
    mode = choice.get("mode", "model_generated")
    consistency = choice.get("consistency", "best_effort")
    if mode == "locked_audio" or consistency == "strict":
        raise ValueError("新口播由视频模型生成台词，不能原样保留整条录音。参考音色只用于生成新的声音。")
    if mode not in {"model_generated", "reference_timbre"} or consistency != "best_effort":
        raise ValueError("声音模式支持 model_generated 或 reference_timbre，consistency 仅支持 best_effort")
    if profile.get("native_speech") is not True:
        raise ValueError("当前组合未确认能生成口播人声，请选择支持人声输出的视频模型")
    asset = choice.get("audio_asset")
    if mode == "model_generated":
        if asset is not None:
            raise ValueError("自由配音不能同时选定 audio_asset；可把文件保留在素材库，但不选入 voice")
    elif not isinstance(asset, str) or not asset.strip():
        raise ValueError(f"{mode} 必须选定 audio_asset")
    if mode == "reference_timbre" and profile.get("reference_timbre") is not True:
        raise ValueError("当前供应商/输入模式不支持参考音色；请更换组合或明确改选声音方式，不会自动降级")
    return {
        "mode": mode, "consistency": consistency,
        "selected_audio_asset": asset,
        "send_audio_to_video_model": mode == "reference_timbre",
        "audio_processing": "native_audio",
        "requires_master_audio_preservation": False,
        "voice_identity_guaranteed_by_model": False,
    }
