from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from video_prompt import build_video_prompt, default_prompt_spec, validate_new_narration_plan, validate_prompt_spec, validate_reference_roles, voice_signature, voice_identity_signature, require_voice_casting, NO_TEXT_RULE  # noqa: E402


class VideoPromptTests(unittest.TestCase):
    def test_new_production_rejects_generic_or_placeholder_casting(self) -> None:
        spec = default_prompt_spec()
        with self.assertRaisesRegex(ValueError, "voice_casting"):
            require_voice_casting(spec)
        import json
        spec = json.loads((SCRIPTS.parent / "assets/video-prompt-spec-template.json").read_text())
        with self.assertRaisesRegex(ValueError, "占位符"):
            require_voice_casting(spec)

    def test_delivery_changes_do_not_redefine_voice_identity(self) -> None:
        spec = default_prompt_spec()
        original_identity, original_plan = voice_identity_signature(spec), voice_signature(spec)
        spec["voice_profile"]["emotion"] = "轻快地分享，有疑问时短暂扬调"
        self.assertEqual(original_identity, voice_identity_signature(spec))
        self.assertNotEqual(original_plan, voice_signature(spec))
        spec["voice_profile"]["timbre"] = "清亮偏薄，带少量柔和气息"
        self.assertNotEqual(original_identity, voice_identity_signature(spec))

    def test_custom_negatives_cannot_remove_clean_video_contract(self) -> None:
        spec = default_prompt_spec()
        spec["negative_constraints"] = ["不抖动"]
        prompt = build_video_prompt(segment={"index": 1, "script": "喜欢自己。"}, language="普通话", continuity=None, prompt_spec=spec, generation_mode="image-to-video")
        self.assertIn(NO_TEXT_RULE, prompt)

    def test_old_schema_keeps_its_prompt_and_signature_contract(self) -> None:
        import hashlib, json
        spec = default_prompt_spec()
        spec["schema_version"] = "2.0"
        del spec["voice_casting"]
        expected = hashlib.sha256(json.dumps(spec["voice_profile"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(voice_signature(spec), expected)
        self.assertNotIn("voice_casting", validate_prompt_spec(spec))
        prompt = build_video_prompt(segment={"index": 1, "script": "你好。"}, language="普通话", continuity=None, prompt_spec=spec, generation_mode="image-to-video")
        self.assertNotIn(NO_TEXT_RULE, prompt)

    def test_prompt_has_professional_visual_speech_audio_and_safety_sections(self) -> None:
        prompt = build_video_prompt(
            segment={"script": "今天分享一个实用的自动化方法。"},
            language="中文",
            continuity=None,
            prompt_spec=default_prompt_spec(),
            generation_mode="image-to-video",
        )
        for text in ("视觉质感", "镜头设计", "人物表演", "台词", "声音设计", "时间与动作", "强制限制"):
            self.assertIn(text, prompt)
        self.assertIn("不使用外部音频或声音克隆", prompt)
        self.assertIn("声音特征", prompt)
        self.assertIn("声音身份锁定", prompt)
        self.assertIn("性别听感=", prompt)
        self.assertIn("不得出现字幕", prompt)

    def test_voice_signature_is_stable_and_changes_with_voice_profile(self) -> None:
        first = default_prompt_spec()
        second = default_prompt_spec()
        second["shot"] = "固定近景"
        self.assertEqual(voice_signature(first), voice_signature(second))
        second["voice_profile"]["pitch"] = "高音，明亮"
        self.assertNotEqual(voice_signature(first), voice_signature(second))

    def test_phrase_triggered_performance_is_in_the_correct_segment(self) -> None:
        spec = default_prompt_spec()
        spec["performance_beats"] = [{
            "segment_index": 1,
            "trigger_phrase": "省下时间",
            "expression": "轻微露出鼓励的笑意",
            "gesture": "单手轻轻向前提示一次",
        }]
        prompt = build_video_prompt(
            segment={"index": 1, "script": "这样就能省下时间。"}, language="中文", continuity=None,
            prompt_spec=spec, generation_mode="image-to-video",
        )
        self.assertIn("说到‘省下时间’时", prompt)
        self.assertIn("允许随句意自然变化语调、语速和情绪", prompt)
        with self.assertRaisesRegex(ValueError, "触发词不在本段台词"):
            build_video_prompt(
                segment={"index": 1, "script": "这是其他台词。"}, language="中文", continuity=None,
                prompt_spec=spec, generation_mode="image-to-video",
            )

    def test_expression_beat_does_not_force_a_hand_gesture(self) -> None:
        spec = default_prompt_spec()
        spec["performance_beats"] = [{"segment_index": 1, "trigger_phrase": "省下时间", "expression": "短暂微笑"}]
        prompt = build_video_prompt(segment={"index": 1, "script": "这样能省下时间。"}, language="中文", continuity=None, prompt_spec=spec, generation_mode="image-to-video")
        self.assertIn("表情短暂微笑", prompt)
        self.assertNotIn("手势None", prompt)

    def test_declared_voice_rate_must_match_planner_rate(self) -> None:
        spec = default_prompt_spec()
        spec["voice_profile"]["pace"] = "每分钟190汉字"
        plan = {"duration_mode": "content-fit", "segments": [{"index": 1, "request_seconds": 5, "estimated_speech_seconds": 4.0}]}
        with self.assertRaisesRegex(ValueError, "语速与 speech_rate_cpm 不一致"):
            validate_new_narration_plan(spec, plan)

    def test_unknown_prompt_fields_are_rejected(self) -> None:
        spec = default_prompt_spec()
        spec["unreviewed_provider_field"] = "ignored before"
        with self.assertRaisesRegex(ValueError, "未知字段"):
            validate_prompt_spec(spec)

    def test_reference_roles_and_preset_voice_are_explicitly_tagged(self) -> None:
        prompt = build_video_prompt(
            segment={"script": "你好。"},
            language="中文",
            continuity=None,
            prompt_spec=default_prompt_spec(),
            generation_mode="reference-to-video",
            reference_roles=["identity", "wardrobe", "scene"],
            preset_voices=["eve"],
            reference_image_index_base=1,
        )
        self.assertIn("<IMAGE_1>", prompt)
        self.assertIn("<IMAGE_2>", prompt)
        self.assertIn("<IMAGE_3>", prompt)
        self.assertIn("<AUDIO_0>", prompt)
        self.assertNotIn("不使用外部音频或声音克隆", prompt)
        self.assertIn("不要交换不同参考图的职责", prompt)

    def test_reference_mode_requires_identity_role(self) -> None:
        with self.assertRaises(ValueError):
            validate_reference_roles(["wardrobe", "scene"], 2)

    def test_reference_mode_rejects_more_than_seven_images(self) -> None:
        with self.assertRaises(ValueError):
            validate_reference_roles(["identity"] * 8, 8)


if __name__ == "__main__":
    unittest.main()
