from __future__ import annotations
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _common import digest_json, sha256_file
from ad_contract import IMAGE_CHECKS, VIDEO_CHECKS, validate_ad_contract, validate_ad_review
from video_prompt import default_prompt_spec, validate_prompt_spec, build_video_prompt, NO_TEXT_RULE
from quality_contract import validate_character_review, validate_text_review
from workflow import build_parser, command_prepare


def ad_spec(kind="storefront_ad"):
    spec = default_prompt_spec()
    spec.update(schema_version="3.1", ad_contract={
        "kind": kind, "reference_use": "scene_reconstruction_only",
        "audience": "附近有自主洗车需求的成年车主",
        "proposition": "介绍参考图中的自助洗车服务",
        "claim_evidence": "测试素材只确认店名与自助洗车，不编造价格和营业时间",
        "cta": "邀请观众了解门店或到店体验",
        "scene": "保留参考图门头和洗车位的空间结构",
        "spatial_relationship": "人物站在门口地面，门头在身后，同一透视光向",
        "action": "提及门店时短暂侧身示意再看向镜头",
        "identity_locks": "店名逐字不变，绿色门头与立柱不改变",
        "source_text": [{"reference_index": 2, "text": "智慧城市自助洗车", "surface": "门头主招牌"}],
    })
    return spec


def prompt(spec, roles=None):
    return build_video_prompt(segment={"index": 1, "script": "欢迎来到门店。"}, language="中文",
                              continuity=None, prompt_spec=spec, generation_mode="reference-to-video",
                              reference_roles=roles or ["identity", "scene"])


def evidence_for(project):
    return {"ad_review": {
        "ad_contract_digest": digest_json(project["video_prompt_spec"]["ad_contract"]),
        "image_set_digest": project["image_set_digest"], "reviewer": "agent_visual",
        **{k: {"status": "pass", "observations": "仅限单元测试的模拟证据，不是真实视频验收。"} for k in VIDEO_CHECKS},
    }}


class AdContractTests(unittest.TestCase):
    def setUp(self):
        self.spec = ad_spec()
        self.project = {"video_prompt_spec": self.spec, "image_set_digest": "set1",
                        "canonical_reference": {"sha256": "image1"}, "reference_roles": ["identity", "scene"],
                        "settings": {"purpose": "storefront_ad", "generation_mode": "reference-to-video", "voice_signature": "voice1"}}

    def test_signage_is_preserved_without_contradictory_global_ban(self):
        rendered = prompt(self.spec)
        self.assertIn("智慧城市自助洗车", rendered)
        self.assertIn("禁止顶部照片条", rendered)
        self.assertNotIn(NO_TEXT_RULE, rendered)
        self.assertNotIn(default_prompt_spec()["negative_constraints"][1], rendered)
        self.spec["negative_constraints"] = ["禁止任何文字"]
        with self.assertRaises(ValueError):
            prompt(self.spec)

    def test_custom_negatives_cannot_remove_space_rule(self):
        self.spec["negative_constraints"] = ["避免闪烁"]
        self.assertIn("后期插入或叠加用户静态图片", prompt(self.spec))
        self.spec["ad_contract"]["source_text"] = []
        self.assertIn(NO_TEXT_RULE, prompt(self.spec))

    def test_photo_strip_and_missing_scene_or_product_are_rejected(self):
        ad = copy.deepcopy(self.spec["ad_contract"])
        ad["reference_use"] = "top_photo_strip"
        with self.assertRaises(ValueError):
            validate_ad_contract(ad)
        with self.assertRaises(ValueError):
            prompt(self.spec, ["identity"])
        product = ad_spec("product_ad")
        with self.assertRaises(ValueError):
            prompt(product)
        self.assertIn("同一透视", prompt(product, ["identity", "product"]))
        company = ad_spec("company_ad")
        self.assertIn("同一透视", prompt(company))

    def test_wrong_reference_index_and_unversioned_ad_are_rejected(self):
        self.spec["ad_contract"]["source_text"][0]["reference_index"] = 1
        with self.assertRaises(ValueError):
            prompt(self.spec)
        self.spec["schema_version"] = "3.0"
        with self.assertRaises(ValueError):
            validate_prompt_spec(self.spec)

    def test_every_failed_or_missing_visual_gate_blocks(self):
        good = evidence_for(self.project)
        validate_ad_review(self.project, good)
        for key in IMAGE_CHECKS:
            bad = copy.deepcopy(good)
            bad["ad_review"][key]["status"] = "fail"
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_ad_review(self.project, bad)
        for key in ("ad_contract_digest", "image_set_digest", "reviewer"):
            bad = copy.deepcopy(good)
            bad["ad_review"][key] = "stale"
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_ad_review(self.project, bad)

    def test_video_review_cannot_reuse_image_or_other_video_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "fixture.bin"
            video.write_bytes(b"not media: only testing evidence binding")
            evidence = evidence_for(self.project)
            with self.assertRaises(ValueError):
                validate_ad_review(self.project, evidence, video=video)
            evidence["ad_review"].update(video_sha256=sha256_file(video), normal_speed_full_playback=True)
            validate_ad_review(self.project, evidence, video=video)
            for key in VIDEO_CHECKS:
                bad = copy.deepcopy(evidence)
                bad["ad_review"][key]["status"] = "pending"
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate_ad_review(self.project, bad, video=video)
            video.write_bytes(b"changed video")
            with self.assertRaises(ValueError):
                validate_ad_review(self.project, evidence, video=video)

    def test_signage_character_review_requires_policy_evidence(self):
        evidence = {**evidence_for(self.project), "canonical_sha256": "image1", "image_set_digest": "set1",
                    "voice_signature": "voice1", "casting_fit": "pass", "reference_text_free": "fail",
                    "reference_text_policy_pass": "pass", "visible_observations": "测试图片人物与门头位于同一空间。",
                    "casting_observations": "测试声音与方案中角色选角保持一致。",
                    "reference_text_observations": "测试招牌与白名单中所有文字保持一致。"}
        validate_character_review(self.project, evidence)
        del evidence["ad_review"]
        with self.assertRaises(ValueError):
            validate_character_review(self.project, evidence)

    def test_ad_cli_cannot_fall_back_to_legacy_or_omit_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "script.txt"
            script.write_text("欢迎来到门店。", encoding="utf-8")
            args = build_parser().parse_args(["--project-dir", str(Path(tmp)/"project"), "prepare", "--name", "test",
                                             "--purpose", "storefront_ad", "--script-file", str(script), "--allow-legacy-performance-plan"])
            with self.assertRaisesRegex(ValueError, "广告必须"):
                command_prepare(args)
            specfile = Path(tmp) / "spec.json"
            specfile.write_text(json.dumps(self.spec), encoding="utf-8")
            args.video_prompt_spec = specfile
            with self.assertRaisesRegex(ValueError, "绕过检查"):
                command_prepare(args)

    def test_text_whitelist_does_not_accept_new_caption_or_wrong_sign(self):
        # Synthetic files test the evidence protocol only, never claim visual inspection.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video, frame, manifest = (root / x for x in ("video.bin", "frame.bin", "manifest.json"))
            video.write_bytes(b"video fixture")
            frame.write_bytes(b"frame fixture")
            manifest.write_text(json.dumps({"video_sha256": sha256_file(video), "sampling": "2fps_plus_dense_tail_and_exact_first_last",
                "total_video_frames": 1, "frames": [{"path": str(frame), "sha256": sha256_file(frame), "roles": ["last"], "frame_index": 0}]}), encoding="utf-8")
            review = {"manifest_file": str(manifest), "manifest_sha256": sha256_file(manifest), "reviewer": "agent_visual",
                "full_video_viewed": True, "all_sampled_frames_viewed": True, "last_frame_viewed": True, "text_present": True,
                "source_text_digest": digest_json(self.spec["ad_contract"]["source_text"]), "source_text_matches": "pass",
                "unapproved_text_present": False, "observations": "仅为合成测试数据，不声明查看过真实视频。"}
            validate_text_review({"text_review": review}, video, self.project)
            for key, value in (("text_present", False), ("unapproved_text_present", True), ("source_text_matches", "fail"), ("source_text_digest", "old"), ("last_frame_viewed", False)):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    validate_text_review({"text_review": {**review, key: value}}, video, self.project)
            with self.assertRaises(ValueError):
                validate_text_review({"text_review": review}, video)


if __name__ == "__main__":
    unittest.main()
