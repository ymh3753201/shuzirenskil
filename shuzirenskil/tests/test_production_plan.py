"""Offline engineering tests; synthetic media is not model/voice quality evidence."""
from __future__ import annotations
import copy
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _common import sha256_file
from model_registry import get_profile, resolve_voice, load_registry
from capability_planner import plan_narration
from audio_timeline import plan_audio, slice_master
from production_plan import build_plan
from request_preview import compile_preview
from quality_contract import PILOT_CHECKS, validate_pilot_checks
from video_prompt import default_prompt_spec, validate_prompt_spec


class GateTests(unittest.TestCase):
    def test_explicit_pilot_status_required_for_every_item(self):
        project = {"settings": {"character_review_required": True}}
        good = {k: {"status": "pass", "observations": "测试用模拟观察，已按要求核对具体内容。"} for k in (*PILOT_CHECKS, "voice_casting_match")}
        validate_pilot_checks(good, project)
        for key in good:
            for bad in ("FAIL：此项明确未通过，不能继续付费制作。", {"status": "fail", "observations": "检查发现问题，必须停止继续生成。"}, {"status": "pending", "observations": "尚未完整检查，不能通过验收。"}, {"observations": "没有结论，也不得视为通过验收。"}, None):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    validate_pilot_checks({**good, key: bad}, project)

    def test_beat_limit_is_per_segment_not_entire_long_film(self):
        spec = default_prompt_spec()
        spec["performance_beats"] = [{"segment_index": i, "trigger_phrase": "你好", "expression": "自然微笑"} for i in range(1, 101)]
        self.assertEqual(len(validate_prompt_spec(spec)["performance_beats"]), 100)
        spec["performance_beats"] = [{"segment_index": 1, "trigger_phrase": "你好", "expression": "自然微笑"}] * 25
        with self.assertRaises(ValueError):
            validate_prompt_spec(spec)


class RoutingTests(unittest.TestCase):
    def test_reference_capability_does_not_force_audio(self):
        for key in load_registry()["profiles"]:
            voice = resolve_voice(get_profile(key), {"mode": "model_generated"})
            self.assertFalse(voice["send_audio_to_video_model"])
            self.assertIsNone(voice["selected_audio_asset"])

    def test_no_automatic_voice_fallback_and_no_wrong_audio_semantics(self):
        with self.assertRaises(ValueError):
            resolve_voice(get_profile("91topgo-grok-reference"), {"mode": "reference_timbre", "audio_asset": "voice"})
        with self.assertRaises(ValueError):
            resolve_voice(get_profile("wan3-first-frame"), {"mode": "reference_timbre", "audio_asset": "voice"})
        for mode in ("model_generated", "reference_timbre"):
            with self.assertRaises(ValueError):
                resolve_voice(get_profile("wan3-reference"), {"mode": mode, "consistency": "strict", "audio_asset": "voice"})
        locked = resolve_voice(get_profile("veo31-reference"), {"mode": "locked_audio", "audio_asset": "voice", "consistency": "strict"})
        self.assertFalse(locked["send_audio_to_video_model"])
        with self.assertRaises(ValueError):
            resolve_voice(get_profile("wan3-reference"), {"mode": "model_generated", "audio_asset": "voice"})

    def test_native_payload_shapes_and_audio_omission(self):
        for key in ("wan3-reference", "minimax-h3-reference", "91topgo-grok-reference"):
            p = get_profile(key)
            args = dict(prompt="人物自然讲话。", seconds=8, resolution=p["resolutions"][0], ratio="9:16", images=["asset://image"])
            request = compile_preview(p, **args)
            self.assertFalse(request["execution_allowed"])
            self.assertNotIn("reference_audio", json.dumps(request["payload"]))
            if p["reference_timbre"]:
                request = compile_preview(p, **args, audio="asset://voice")
                self.assertIn("reference_audio", json.dumps(request["payload"]))
            elif key == "91topgo-grok-reference":
                self.assertEqual(request["payload"]["reference_images"], [{"url": "asset://image"}])
        with self.assertRaises(ValueError):
            compile_preview(get_profile("wan3-first-frame"), prompt="你好", seconds=5, resolution="720p", ratio="9:16", images=["asset://image"], audio="asset://voice")


class TimingTests(unittest.TestCase):
    def test_model_specific_durations_and_unavailable_exact_target(self):
        p = get_profile("veo31-reference")
        plan = plan_narration("你好。", p)
        self.assertEqual([x["request_seconds"] for x in plan["segments"]], [8])
        with self.assertRaises(ValueError):
            plan_narration("你好。", p, target=7, mode="exact")
        with self.assertRaises(ValueError):
            plan_narration("你好。", p, target=8, mode="exact") # disallow padding with silence

    def test_30_second_model_does_not_split_at_15(self):
        script = "今天我们详细介绍这款工具怎样帮助门店整理客户信息，并且自动提醒后续跟进事项，让工作人员能够在清楚了解客户具体需求之后提供合适的服务方案。"
        self.assertEqual(len(plan_narration(script, get_profile("wan3-reference"))["segments"]), 1)
        with self.assertRaises(ValueError):
            plan_narration(script, get_profile("91topgo-grok-reference"))

    def test_long_plan_exceeds_24_segments_without_recursive_limit(self):
        script = "这段内容介绍门店服务的具体流程与实际体验。" * 100
        plan = plan_narration(script, get_profile("91topgo-grok-reference"))
        self.assertGreater(len(plan["segments"]), 24)
        self.assertTrue(all(1 <= x["request_seconds"] <= 15 for x in plan["segments"]))
        self.assertEqual("".join(x["script"].replace(" ", "") for x in plan["segments"]), script)

    def test_exact_feasible_plan_and_legacy_planner_untouched(self):
        script = "这段内容介绍门店服务的具体流程与实际体验。" * 3
        plan = plan_narration(script, get_profile("wan3-reference"), target=17, mode="exact")
        self.assertEqual(sum(s["request_seconds"] for s in plan["segments"]), 17)
        from planner import plan_segments
        self.assertEqual(plan_segments("你好。", planner_version="2.0")["planner_version"], "2.0")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.image = self.root / "person.png"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=256x256", "-frames:v", "1", str(self.image)], check=True, capture_output=True)
        self.audio = self.root / "master.wav"
        self.samples = b"".join(struct.pack("<h", (i % 2000) - 1000) for i in range(16000 * 10))
        with wave.open(str(self.audio), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(self.samples)
        self.script = "你好。欢迎来到我们的门店。"
        self.cues = {"audio_sha256": sha256_file(self.audio), "alignment_reviewed": True,
                     "cues": [{"start_seconds": .1, "end_seconds": 3, "text": "你好。"},
                              {"start_seconds": 5, "end_seconds": 9.8, "text": "欢迎来到我们的门店。"}]}
        self.spec = {"schema_version": "1.0", "name": "离线合成测试", "model_profile": "wan3-reference",
                     "voice": {"mode": "model_generated"}, "voice_design": "成年中性音色，清晰可信，语速自然。",
                     "script": self.script, "assets": {"person": {"path": "person.png", "kind": "image", "role": "identity"},
                                                        "voice": {"path": "master.wav", "kind": "audio"}},
                     "reference_images": ["person"], "character_bible": {"identity": "固定同一人物", "wardrobe": "白色衬衫", "scene": "浅色背景", "camera": "平视中景", "lighting": "柔和侧光"},
                     "resolution": "720p", "aspect_ratio": "9:16"}

    def tearDown(self):
        self.temp.cleanup()

    def test_unused_audio_is_not_read_bound_or_compiled(self):
        self.spec["assets"]["voice"]["path"] = "absent-private-audio.wav"
        result = build_plan(self.spec, self.root)
        self.assertEqual(result["ignored_asset_ids"], ["voice"])
        self.assertNotIn("voice", result["assets"])
        self.assertNotIn("reference_audio", json.dumps(result["jobs"]))

    def test_explicit_reference_audio_uses_same_asset_all_segments(self):
        self.spec["voice"] = {"mode": "reference_timbre", "audio_asset": "voice"}
        self.spec["script"] = "这段内容介绍门店服务的具体流程与实际体验。" * 12
        result = build_plan(self.spec, self.root)
        expected = "asset://" + sha256_file(self.audio)
        for job in result["jobs"]:
            self.assertEqual(job["request_preview"]["payload"]["input"]["media"][-1], {"type": "reference_audio", "url": expected})
        self.assertEqual(len({j["consistency_digest"] for j in result["jobs"]}), 1)

    def test_master_audio_samples_are_preserved_across_cuts(self):
        # A 6s cap forces a cut at 4s, between the two spoken cues.
        profile = {"duration_seconds": [4, 5, 6]}
        plan = plan_audio(self.audio, self.cues, profile, self.script)
        self.assertEqual(len(plan["segments"]), 2)
        output = slice_master(plan, self.root / "slices")
        actual = b""
        for item in output["slices"]:
            with wave.open(item["path"], "rb") as r:
                actual += r.readframes(r.getnframes())
        self.assertEqual(actual, self.samples)
        self.assertFalse(plan["automatic_tail_silence_trim"])
        self.assertFalse(output["lip_sync_verified"])

    def test_audio_drift_tamper_and_unreviewed_alignment_blocked(self):
        for field, value in (("alignment_reviewed", False), ("audio_sha256", "wrong")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan_audio(self.audio, {**self.cues, field: value}, get_profile("wan3-reference"), self.script)
        with self.assertRaises(ValueError):
            plan_audio(self.audio, self.cues, get_profile("wan3-reference"), self.script + "多说一句。")
        bad = copy.deepcopy(self.cues); bad["cues"][1]["start_seconds"] = 2
        with self.assertRaises(ValueError):
            plan_audio(self.audio, bad, get_profile("wan3-reference"), self.script)
        plan = plan_audio(self.audio, self.cues, get_profile("wan3-reference"), self.script)
        plan["segments"][0]["start_sample"] = 1
        with self.assertRaises(ValueError):
            slice_master(plan, self.root / "bad-slices")

    def test_locked_audio_never_sent_as_timbre_and_is_not_marked_delivered(self):
        self.spec["voice"] = {"mode": "locked_audio", "consistency": "strict", "audio_asset": "voice"}
        self.spec["audio_cues"] = self.cues
        result = build_plan(self.spec, self.root)
        self.assertEqual(result["planned_delivery_seconds"], 10)
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(result["jobs"][0]["requires_lip_sync"])
        self.assertNotIn("reference_audio", json.dumps(result["jobs"]))
        self.assertEqual(result["jobs"][0]["native_audio_policy"], "discard")

    def test_explicit_shots_keep_broll_separate_and_complete_script(self):
        self.spec["shots"] = [{"id": "talk", "kind": "talking_head", "script": self.script, "visual_brief": "人物自然介绍服务"},
                              {"id": "detail", "kind": "b_roll", "duration_seconds": 4, "visual_brief": "真实展示产品运作过程"}]
        result = build_plan(self.spec, self.root)
        self.assertEqual(result["jobs"][-1]["kind"], "b_roll")
        self.assertEqual(result["jobs"][-1]["native_audio_policy"], "discard")
        self.spec["shots"][0]["script"] = "你好。"
        with self.assertRaises(ValueError):
            build_plan(self.spec, self.root)

    def test_reject_fake_continuation_and_unknown_model(self):
        self.spec["continuity_mode"] = "continue"
        self.spec["model_profile"] = "veo31-reference"
        with self.assertRaises(ValueError):
            build_plan(self.spec, self.root)
        self.spec.pop("continuity_mode")
        self.spec["model_profile"] = "made-up-model"
        with self.assertRaises(ValueError):
            build_plan(self.spec, self.root)

    def test_ad_requires_facts_and_scene_reference_and_preserves_text_policy(self):
        from test_ad_contract import ad_spec
        self.spec["purpose"] = "storefront_ad"
        self.spec["ad_contract"] = ad_spec()["ad_contract"]
        with self.assertRaises(ValueError):
            build_plan(self.spec, self.root)
        self.spec["assets"]["scene"] = {"path": "person.png", "kind": "image", "role": "scene"}
        self.spec["reference_images"].append("scene")
        result = build_plan(self.spec, self.root)
        prompt = result["jobs"][0]["request_preview"]["payload"]["input"]["prompt"]
        self.assertIn("智慧城市自助洗车", prompt)
        self.assertIn("禁止顶部照片条", prompt)
        self.assertNotIn("不出现字幕、文字、Logo", prompt)
        self.spec["ad_contract"].pop("claim_evidence")
        with self.assertRaises(ValueError):
            build_plan(self.spec, self.root)

    def test_reference_audio_duration_and_locked_target_are_checked(self):
        self.spec["voice"] = {"mode": "reference_timbre", "audio_asset": "voice"}
        self.spec["model_profile"] = "minimax-h3-reference"
        self.spec["resolution"] = "768p"
        with wave.open(str(self.audio), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 16000)
        with self.assertRaises(ValueError):
            build_plan(self.spec, self.root) # H3 reference must be at least 2s
        self.spec["voice"] = {"mode": "locked_audio", "audio_asset": "voice"}
        self.spec["audio_cues"] = {"audio_sha256": sha256_file(self.audio), "alignment_reviewed": True,
                                   "cues": [{"start_seconds": 0, "end_seconds": 1, "text": self.script}]}
        self.spec["duration_mode"] = "exact"
        self.spec["target_seconds"] = 8
        with self.assertRaisesRegex(ValueError, "实际时长"):
            build_plan(self.spec, self.root)


if __name__ == "__main__":
    unittest.main()
