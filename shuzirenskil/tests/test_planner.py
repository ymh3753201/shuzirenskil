from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
WORKFLOW = SCRIPT_DIR / "workflow.py"
sys.path.insert(0, str(SCRIPT_DIR))

from planner import estimate_speech_seconds, plan_segments
from video_prompt import default_prompt_spec
from voice_fixture import designed_voice_spec


class PlannerTests(unittest.TestCase):
    def test_required_performance_plan_needs_a_real_phrase_in_each_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "script.txt"
            script.write_text("今天教你一个省时间的方法。", encoding="utf-8")
            spec = designed_voice_spec()
            spec["performance_beats"] = [{
                "segment_index": 1, "trigger_phrase": "省时间",
                "expression": "轻微露出鼓励的笑意", "gesture": "单手轻轻提示一次后放回",
            }]
            spec_path = root / "prompt.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(WORKFLOW), "--project-dir", str(root / "missing"),
                "prepare", "--name", "missing", "--script-file", str(script), "--duration", "15",
            ], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("必须提供逐段表演方案", result.stderr)
            result = subprocess.run([
                sys.executable, str(WORKFLOW), "--project-dir", str(root / "valid"),
                "prepare", "--name", "valid", "--script-file", str(script), "--duration", "15",
                "--duration-mode", "content-fit",
                "--video-prompt-spec", str(spec_path),
            ], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_underfilled_exact_copy_is_blocked_before_paid_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "script.txt"
            script.write_text("普通人为什么要学 Codex？因为你可以用人话，让它帮你做网页、整理数据、自动处理重复工作。把想法变成工具，让自己少忙一点。", encoding="utf-8")
            spec = default_prompt_spec()
            spec["performance_beats"] = [{"segment_index": 1, "trigger_phrase": "做网页", "expression": "短暂强调"}]
            spec_path = root / "prompt.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([sys.executable, str(WORKFLOW), "--project-dir", str(root / "project"), "prepare", "--name", "pacing", "--script-file", str(script), "--duration", "15", "--duration-mode", "exact", "--video-prompt-spec", str(spec_path)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不用慢读凑时长", result.stderr)

    def test_rigid_narration_seconds_are_blocked_before_paid_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "script.txt"
            script.write_text("普通人为什么要学 Codex？因为你可以用人话，让它帮你做网页、整理数据、自动处理重复工作。把想法变成工具，让自己少忙一点。", encoding="utf-8")
            spec = default_prompt_spec()
            spec["speech_delivery"] = "前3秒提问，中间9秒讲用途，最后3秒总结收尾"
            spec["performance_beats"] = [{"segment_index": 1, "trigger_phrase": "做网页", "expression": "短暂强调"}]
            spec_path = root / "prompt.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([sys.executable, str(WORKFLOW), "--project-dir", str(root / "project"), "prepare", "--name", "pacing", "--script-file", str(script), "--duration", "15", "--video-prompt-spec", str(spec_path)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("固定秒数分配", result.stderr)

    def test_speech_rate_controls_estimate(self) -> None:
        phrase = "自然口播需要留出呼吸和强调的空间。"
        self.assertGreater(estimate_speech_seconds(phrase, 220), estimate_speech_seconds(phrase, 306))
        plan = plan_segments(phrase, 15, speech_rate_cpm=260)
        self.assertEqual(plan["speech_rate_cpm"], 260)

    def test_duration_capability_flag_locks_single_request_to_delivery_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script_path = root / "script.txt"
            script_path.write_text(
                "为什么要学Codex？因为它能帮助普通人把想法做成实际成果。",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable, str(WORKFLOW), "--project-dir", str(root / "project"),
                    "prepare", "--name", "duration-test", "--script-file", str(script_path),
                    "--duration", "15", "--duration-capability-test", "--allow-legacy-performance-plan",
                ],
                text=True,
                capture_output=True,
                check=True,
            )
            plan = json.loads(result.stdout)["plan"]
            self.assertTrue(plan["duration_capability_test"])
            self.assertEqual(plan["segments"][0]["request_seconds"], 15)
            self.assertEqual(plan["planned_request_total_seconds"], 15)

    def test_long_script_uses_complete_sentence_segments(self) -> None:
        script = (
            "先把每天收到的文件统一放进一个工作目录，再让Codex按项目和日期分类。"
            "接着让它提取每份材料的重点，并把无法确认的内容单独标出来。"
            "最后由你检查摘要和原文件是否一致，再决定哪些内容可以继续使用。"
        )
        result = plan_segments(script, 35)
        self.assertGreaterEqual(result["minimum_paid_segment_count"], 1)
        self.assertTrue(all(item["ends_on_complete_sentence"] for item in result["segments"]))
        self.assertTrue(all(1 <= item["request_seconds"] <= 15 for item in result["segments"]))

    def test_overlong_single_sentence_is_blocked(self) -> None:
        script = "这是一个" + "非常长而且没有合适停顿的句子" * 20 + "。"
        with self.assertRaisesRegex(ValueError, "不能在半句话处分段"):
            plan_segments(script, 60)

    def test_script_over_delivery_limit_is_blocked(self) -> None:
        script = "这是一句需要完整表达的测试内容。这里还有一句同样需要完整表达的测试内容。"
        with self.assertRaisesRegex(ValueError, "超过目标成片"):
            plan_segments(script, 5)

    def test_explicit_15_second_video_uses_one_15_second_request(self) -> None:
        script = "欢迎收看今日 AI 资讯。大模型视频能力持续升级，多模态工具加速迭代，AI 应用不断落地。关注前沿动态，我们下期再见。"
        result = plan_segments(script, 15, duration_mode="exact")
        self.assertEqual(result["minimum_paid_segment_count"], 1)
        self.assertEqual(result["duration_mode"], "exact")
        self.assertEqual(result["segments"][0]["request_seconds"], 15)
        self.assertEqual(result["planned_request_total_seconds"], 15)

    def test_explicit_30_second_video_uses_two_15_second_requests(self) -> None:
        script = (
            "今天分享一个实用方法，先把资料放进统一目录。"
            "再让Codex按项目自动分类，并标记所有无法确认的内容。"
            "接着逐项核对摘要和原文件，确保结果准确。"
            "最后输出一份清晰报告，方便团队继续使用。"
        )
        result = plan_segments(script, 30, duration_mode="exact")
        self.assertEqual(result["duration_mode"], "exact")
        self.assertEqual([item["request_seconds"] for item in result["segments"]], [15, 15])
        self.assertEqual(result["planned_request_total_seconds"], 30)

    def test_content_fit_mode_still_uses_script_length(self) -> None:
        script = "欢迎收看今日 AI 资讯。大模型视频能力持续升级，多模态工具加速迭代，AI 应用不断落地。关注前沿动态，我们下期再见。"
        result = plan_segments(script, 15, duration_mode="content-fit")
        self.assertEqual(result["duration_mode"], "content-fit")
        self.assertGreaterEqual(result["segments"][0]["request_seconds"], 10)
        self.assertLessEqual(result["segments"][0]["request_seconds"], 14)

    def test_real_6_second_sample_requests_about_5_seconds(self) -> None:
        result = plan_segments("多款视频生成模型本周集中发布功能更新。", 6)
        self.assertEqual(result["segments"][0]["request_seconds"], 5)

    def test_sentence_count_is_not_segment_count_and_rounding_respects_cap(self) -> None:
        script = (
            "各位好，今天全球 AI 热点速报：SpaceX 官宣，将独家采用英伟达 GPU，启动 10 吉瓦级算力建设。"
            "近地轨道还将部署专用太空 AI 模块。"
            "国际 AI 安全榜单出炉，中国开源方案拿下全球第二，创下开源赛道最好成绩。"
        )
        result = plan_segments(script, speech_rate_cpm=306)
        self.assertEqual(result["sentence_count"], 3)
        self.assertEqual(result["minimum_paid_segment_count"], 2)
        self.assertTrue(result["segment_count_is_minimal"])
        self.assertEqual(result["packing_strategy"], "complete_sentences_min_calls_then_timing")
        self.assertEqual(result["segments"][0]["request_seconds"], 12)
        self.assertEqual(result["segments"][1]["request_seconds"], 8)
        with self.assertRaisesRegex(ValueError, "整数秒取整"):
            plan_segments(script, 19, speech_rate_cpm=306)

    def test_content_alone_selects_13_seconds_for_real_sample(self) -> None:
        script = "普通人为什么要学 Codex？因为你可以用人话，让它帮你做网页、整理数据、自动处理重复工作。把想法变成工具，让自己少忙一点。"
        for cap in (None, 15, 30, 45, 60):
            plan = plan_segments(script, cap)
            self.assertEqual([segment["request_seconds"] for segment in plan["segments"]], [13])
            self.assertEqual(plan["timing_assessment"]["status"], "ready")
            self.assertEqual(plan["duration_mode"], "content-fit")

    def test_exact_20_seconds_uses_sentence_lengths_not_15_plus_5(self) -> None:
        plan = plan_segments("中" * 37 + "。" + "文" * 37 + "。", 20, duration_mode="exact")
        self.assertEqual([segment["request_seconds"] for segment in plan["segments"]], [10, 10])
        self.assertEqual(plan["timing_assessment"]["status"], "ready")

    def test_complete_sentences_can_require_three_calls_for_30_seconds(self) -> None:
        script = ("中" * 37 + "。") * 3
        plan = plan_segments(script, 30, duration_mode="exact")
        self.assertEqual([segment["request_seconds"] for segment in plan["segments"]], [10, 10, 10])
        self.assertEqual("".join(segment["script"] for segment in plan["segments"]), script)
        self.assertEqual(plan["timing_assessment"]["status"], "ready")

    def test_target_durations_are_ready_when_copy_has_enough_content(self) -> None:
        for target in (15, 30, 60):
            script = ("中" * 58 + "。") * (target // 15)
            plan = plan_segments(script, target, duration_mode="exact")
            self.assertEqual(plan["planned_request_total_seconds"], target)
            self.assertEqual(plan["minimum_paid_segment_count"], target // 15)
            self.assertEqual(plan["timing_assessment"]["status"], "ready")

    def test_plan_reports_short_draft_without_overriding_user_target(self) -> None:
        plan = plan_segments("今天一起学习。", 15, duration_mode="exact")
        self.assertEqual(plan["target_delivery_seconds"], 15)
        self.assertEqual(plan["timing_assessment"]["status"], "revise_script")
        self.assertLess(plan["content_fit_alternative"]["total_seconds"], 15)

    def test_short_fixed_target_allows_normal_start_and_end_margin(self) -> None:
        plan = plan_segments("你好。", 2, duration_mode="exact")
        self.assertEqual(plan["timing_assessment"]["status"], "ready")

    def test_exact_requires_explicit_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "用户指定"):
            plan_segments("今天一起学习。", duration_mode="exact")

    def test_short_locked_copy_cannot_be_stretched_to_one_minute(self) -> None:
        with self.assertRaisesRegex(ValueError, "补充有用内容.*逐字锁定稿"):
            plan_segments("今天一起学习。", 60, duration_mode="exact")

    def test_legacy_plan_recomputation_keeps_old_request_seconds(self) -> None:
        from workflow import _verify_minimal_segment_plan
        script = "普通人为什么要学 Codex？因为你可以用人话，让它帮你做网页、整理数据、自动处理重复工作。把想法变成工具，让自己少忙一点。"
        plan = plan_segments(script, 15, speech_rate_cpm=260, planner_version="1.0")
        self.assertNotIn("planner_version", plan)
        self.assertEqual(plan["segments"][0]["request_seconds"], 15)
        _verify_minimal_segment_plan({"route": "video_generation", "script": script, "plan": plan})

    def test_free_plan_and_prepare_work_without_an_invented_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "script.txt"
            script.write_text("今天教你一个省时间的方法。", encoding="utf-8")
            result = subprocess.run([sys.executable, str(WORKFLOW), "plan", "--script-file", str(script)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["duration_source"], "content_estimate")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["script.txt"])
            spec = designed_voice_spec()
            spec["performance_beats"] = [{"segment_index": 1, "trigger_phrase": "省时间", "expression": "轻轻微笑"}]
            spec_path = root / "prompt.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            project = root / "project"
            result = subprocess.run([sys.executable, str(WORKFLOW), "--project-dir", str(project), "prepare", "--name", "content", "--script-file", str(script), "--video-prompt-spec", str(spec_path)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([sys.executable, str(WORKFLOW), "--project-dir", str(project), "confirm-plan", "--approved-by", "user"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
