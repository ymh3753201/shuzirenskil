"""绑定实际素材的视觉与角色配音观察；不自动替代人的看听判断。"""
from pathlib import Path
from typing import Any
from _common import read_json, require_file, sha256_file


def validate_character_review(project: dict[str, Any], evidence: dict[str, Any]) -> None:
    expected = {
        "canonical_sha256": project["canonical_reference"]["sha256"],
        "image_set_digest": project["image_set_digest"],
        "voice_signature": project["settings"]["voice_signature"],
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("角色看图与声音设计检查没有绑定当前图片组和声音方案")
    if evidence.get("casting_fit") != "pass" or evidence.get("reference_text_free") != "pass":
        raise ValueError("声音选角或参考图无文字检查未通过；先调整素材或方案")
    for key in ("visible_observations", "casting_observations", "reference_text_observations"):
        if not isinstance(evidence.get(key), str) or len(evidence[key].strip()) < 8:
            raise ValueError(f"角色检查 {key} 必须记录实际看图依据，至少 8 字")


def validate_text_review(evidence: dict[str, Any], video: Path) -> dict[str, Any]:
    review = evidence.get("text_review")
    if not isinstance(review, dict):
        raise ValueError("无文字通过必须提供 text_review，不能仅凭少量抽帧或一句说明")
    manifest_path = require_file(Path(review.get("manifest_file", "")), "视觉检查清单")
    if review.get("manifest_sha256") != sha256_file(manifest_path):
        raise ValueError("视觉检查清单已变更，请重新查看")
    manifest = read_json(manifest_path)
    if manifest.get("video_sha256") != sha256_file(video):
        raise ValueError("无文字证据与当前视频不一致")
    frames = manifest.get("frames") or []
    if not frames or manifest.get("sampling") != "2fps_plus_dense_tail_and_exact_first_last":
        raise ValueError("视觉检查清单缺少规定的全程取样与片尾加密取样")
    if not any("last" in frame.get("roles", []) and frame.get("frame_index") == manifest.get("total_video_frames", 0) - 1 for frame in frames):
        raise ValueError("视觉检查未包含真实最后一帧")
    for frame in frames:
        if sha256_file(require_file(Path(frame["path"]), "已查看画面")) != frame.get("sha256"):
            raise ValueError("已查看画面文件发生变化")
    if review.get("reviewer") not in {"human_user", "agent_visual"}:
        raise ValueError("必须记录真实视觉检查者")
    if any(review.get(key) is not True for key in ("full_video_viewed", "all_sampled_frames_viewed", "last_frame_viewed")) or review.get("text_present") is not False:
        raise ValueError("全片、取样、末帧未全部看完，或已发现文字；无文字项不能通过")
    if not isinstance(review.get("observations"), str) or len(review["observations"].strip()) < 8:
        raise ValueError("无文字检查必须保存具体观察")
    return manifest
