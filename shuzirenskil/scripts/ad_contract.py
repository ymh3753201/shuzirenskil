"""广告创意与实景验收合同；检查记录完整性，不代替真实看图、看视频。"""
from typing import Any
from _common import digest_json, sha256_file

AD_KINDS = ("storefront_ad", "product_ad", "company_ad")
AD_SCENE_RULE = (
    "参考图仅用于重建真实摄影空间，不作为成片中的照片图层。人物与门店或产品处于同一透视、光照和遮挡关系中；"
    "生成自然连续的动态场景。禁止顶部照片条、上下或左右分屏、画中画、拼贴、幻灯片、静态照片推拉、"
    "将人物贴在静态照片上，以及后期插入或叠加用户静态图片。不得以这些方式修补招牌或产品失真。"
)
AD_TEXT_RULE = (
    "仅允许下列参考图原有的实体招牌或包装标识，文字逐字、标识形态保持一致，附着于原实体表面并随透视运动；"
    "禁止新增字幕、标题、价格、优惠、地址、Logo、水印、UI、文字背景条和片尾卡。台词不可转成屏幕文字。"
)
CREATIVE_FIELDS = ("audience", "proposition", "claim_evidence", "cta", "scene", "spatial_relationship", "action", "identity_locks")
IMAGE_CHECKS = ("same_space", "no_static_collage", "reference_fidelity", "composition", "source_text_fidelity")
VIDEO_CHECKS = IMAGE_CHECKS + ("scene_motion", "product_or_service_truth", "cta_consistency")


def validate_ad_contract(value: Any) -> dict:
    required = {"kind", "reference_use", "source_text", *CREATIVE_FIELDS}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("ad_contract 必须完整包含: " + ", ".join(sorted(required)))
    if value["kind"] not in AD_KINDS or value["reference_use"] != "scene_reconstruction_only":
        raise ValueError("广告类型无效或参考图被用作静态拼贴；reference_use 必须为 scene_reconstruction_only")
    for key in CREATIVE_FIELDS:
        item = value[key]
        if not isinstance(item, str) or not 8 <= len(item.strip()) <= 350 or any(x in item for x in ("请填写", "待填写", "请替换")):
            raise ValueError(f"ad_contract.{key} 必须是实际设计的 8-350 字内容")
    entries = value["source_text"]
    if not isinstance(entries, list) or len(entries) > 12:
        raise ValueError("source_text 必须是最多 12 项的原生文字白名单，无文字时用空数组")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"reference_index", "text", "surface"}:
            raise ValueError("每项原生文字必须包含 reference_index、text、surface")
        index = entry["reference_index"]
        if type(index) is not int or not 1 <= index <= 7:
            raise ValueError("原生文字 reference_index 必须是从 1 开始的参考图顺序")
        if any(not isinstance(entry[k], str) or not 1 <= len(entry[k].strip()) <= 100 for k in ("text", "surface")):
            raise ValueError("原生文字必须逐字记录内容与所在实体表面，不能使用笼统豁免")
    return value


def project_ad(project: dict) -> dict | None:
    value = (project.get("video_prompt_spec") or {}).get("ad_contract")
    purpose = project.get("settings", {}).get("purpose", "narration")
    if purpose in AD_KINDS and not value:
        raise ValueError("广告项目缺少 ad_contract，不能沿用普通口播验收")
    if value is not None:
        validate_ad_contract(value)
        if purpose in AD_KINDS and purpose != value["kind"]:
            raise ValueError("广告用途与创意合同不一致")
    return value


def validate_ad_references(ad: dict, mode: str, roles: list[str]) -> None:
    validate_ad_contract(ad)
    if mode == "reference-to-video":
        needed = "product" if ad["kind"] == "product_ad" else "scene"
        if needed not in roles:
            raise ValueError(f"广告参考图必须包含 {needed}，不能只发人物图后期贴照片")
    for entry in ad["source_text"]:
        index = entry["reference_index"] - 1
        if mode == "reference-to-video":
            if index >= len(roles) or roles[index] not in {"scene", "product", "composition"}:
                raise ValueError("原生文字必须对应实际绑定的场景、产品或构图参考图")
        elif entry["reference_index"] != 1:
            raise ValueError("单图模式的原生文字只能来自已确认的第一张完整场景图")


def render_ad_prompt(ad: dict, mode: str, roles: list[str], index_base: int) -> str:
    validate_ad_references(ad, mode, roles)
    parts = [AD_SCENE_RULE, f"场景：{ad['scene']}；空间关系：{ad['spatial_relationship']}；真实动作：{ad['action']}；保真锁定：{ad['identity_locks']}。"]
    if ad["source_text"]:
        parts.append(AD_TEXT_RULE + "；".join(
            f"参考图{item['reference_index'] + index_base - 1}的{item['surface']}：{item['text']}"
            for item in ad["source_text"]
        ))
    return "\n".join(parts)


def validate_ad_review(project: dict, evidence: dict, *, video=None) -> None:
    ad = project_ad(project)
    if not ad:
        return
    review = evidence.get("ad_review")
    if not isinstance(review, dict) or review.get("ad_contract_digest") != digest_json(ad):
        raise ValueError("缺少绑定当前广告创意合同的 ad_review")
    if review.get("image_set_digest") != project.get("image_set_digest") or not project.get("image_set_digest"):
        raise ValueError("广告检查没有绑定实际参考图组")
    if review.get("reviewer") not in {"human_user", "agent_visual"}:
        raise ValueError("广告检查必须记录真实检查者")
    if video is not None:
        if review.get("video_sha256") != sha256_file(video) or review.get("normal_speed_full_playback") is not True:
            raise ValueError("广告检查须绑定当前视频并完整正常速度播放")
    for key in VIDEO_CHECKS if video is not None else IMAGE_CHECKS:
        item = review.get(key)
        if not isinstance(item, dict) or item.get("status") != "pass" or not isinstance(item.get("observations"), str) or len(item["observations"].strip()) < 8:
            raise ValueError(f"广告验收 {key} 未通过或缺少实际观察；不能交付或继续付费")
