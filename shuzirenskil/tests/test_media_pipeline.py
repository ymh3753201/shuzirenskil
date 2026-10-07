from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKFLOW = SKILL_DIR / "scripts" / "workflow.py"
import sys

sys.path.insert(0, str(SKILL_DIR / "scripts"))
from media import analyze_speech_pacing, detect_tail_silence, retime_short_exact_candidate, stitch, technical_summary
from workflow import _duration_review_checks, _review_required


class ReviewRuleTests(unittest.TestCase):
    def test_user_target_tolerance_and_content_maximum_are_distinct(self) -> None:
        target = {"duration_mode": "exact", "target_delivery_seconds": 15, "delivery_max_seconds": 15, "delivery_duration_tolerance_seconds": 1}
        self.assertTrue(all(_duration_review_checks(15.5, target, "video_generation").values()))
        self.assertFalse(all(_duration_review_checks(16.1, target, "video_generation").values()))
        content = {"duration_mode": "content-fit", "target_delivery_seconds": 13, "delivery_max_seconds": 30, "planned_request_total_seconds": 13}
        self.assertTrue(all(_duration_review_checks(13.042, content, "video_generation").values()))
        self.assertFalse(all(_duration_review_checks(20, content, "video_generation").values()))

    def test_multisegment_clean_review_requires_real_voice_consistency_gate(self) -> None:
        args = argparse.Namespace(
            visual_consistency="pass",
            speech_complete="pass",
            stitching_quality="pass",
            critical_facts="pass",
            no_generated_text="pass",
            voice_consistency="pending",
        )
        project = {"route": "video_generation", "plan": {"minimum_paid_segment_count": 2}}
        checks = _review_required("clean", args, project)
        self.assertEqual(checks["cross_segment_voice_consistency"], "pending")
        self.assertEqual(checks["motion_naturalness"], "pending")
        self.assertEqual(checks["gesture_phrase_fit"], "pending")
        self.assertEqual(checks["voice_naturalness"], "pending")
        self.assertEqual(checks["speech_pacing"], "pending")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_pacing_analysis_flags_two_long_internal_pauses(self) -> None:
        import math
        import struct
        import wave
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "voice.wav"
            sample_rate = 16000
            with wave.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(sample_rate)
                for index in range(3 * sample_rate):
                    second = index / sample_rate
                    active = 0.2 <= second < 0.6 or 1.5 <= second < 1.9 or 2.75 <= second < 2.9
                    value = int(5000 * math.sin(2 * math.pi * 440 * second)) if active else 0
                    out.writeframesraw(struct.pack("<h", value))
            analysis = analyze_speech_pacing(path, "今天一起学习数字工具。", 260)
            self.assertEqual(len(analysis["long_internal_pauses"]), 2)
            self.assertTrue(analysis["needs_listening_review"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class MediaPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "source.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=2:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(self.video),
        ], check=True, capture_output=True)
        self.script = self.root / "script.txt"
        self.script.write_text("这是第一句测试口播。这是第二句测试口播。", encoding="utf-8")
        self.timing = self.root / "timing.srt"
        self.timing.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试时间轴\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, project: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            ["python3", str(WORKFLOW), "--project-dir", str(project), *args],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def review(self, project: Path, kind: str) -> None:
        transcript = self.root / f"{kind}-transcript.txt"
        transcript.write_text(self.script.read_text(encoding="utf-8"), encoding="utf-8")
        evidence = {
            "visual_consistency": "已查看开头、中间、结尾抽帧，画面主体保持一致",
            "speech_complete": "已用最终语音转写逐句对照确认稿，首尾内容完整",
            "stitching_quality": "已检查全部拼接点两侧，没有断音或重复",
            "critical_facts_exact": "已核对确认稿中的测试内容，没有数字或名称变化",
            "speech_transcript_file": str(transcript),
        }
        args = [
            "review", "--kind", kind,
            "--visual-consistency", "pass",
            "--speech-complete", "pass",
            "--stitching-quality", "pass",
            "--critical-facts", "pass",
        ]
        if kind == "clean":
            inspection = json.loads(self.run_cli(project, "inspect-visuals", "--video", str(project / "outputs" / "final.clean.mp4"), "--output-dir", str(project / "review-frames" / kind)).stdout)
            # Synthetic media and simulated observations verify the review contract, not actual human perception.
            evidence["text_review"] = {"manifest_file": inspection["manifest_file"], "manifest_sha256": inspection["manifest_sha256"], "reviewer": "agent_visual", "full_video_viewed": True, "all_sampled_frames_viewed": True, "last_frame_viewed": True, "text_present": False, "observations": "测试夹具为纯色合成视频，模拟已完整查看的证据。"}
            evidence["no_generated_text"] = "已检查抽帧，画面中没有字幕、标题、Logo 或水印"
            args += ["--no-generated-text", "pass"]
        else:
            evidence.update({
                "subtitle_safe_and_readable": "字幕位于底部安全区，没有遮挡主要主体",
                "subtitle_matches_confirmed_script": "字幕文字来自确认稿并已逐句核对",
                "no_unapproved_text": "除已确认字幕外没有新增文字内容",
            })
            args += [
                "--subtitle-safe", "pass",
                "--subtitle-matches", "pass",
                "--no-unapproved-text", "pass",
            ]
        evidence_path = self.root / f"{kind}-evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        args += ["--evidence-file", str(evidence_path)]
        self.run_cli(project, *args)

    def test_postproduction_clean_subtitles_and_finalize(self) -> None:
        project = self.root / "project"
        self.run_cli(
            project,
            "prepare", "--name", "post", "--script-file", str(self.script),
            "--existing-video", str(self.video), "--duration", "3", "--subtitles", "enabled",
        )
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "preflight")
        self.run_cli(project, "stitch")
        self.review(project, "clean")
        self.run_cli(project, "subtitles", "--timing-srt", str(self.timing))
        self.review(project, "captioned")
        self.run_cli(project, "finalize")
        delivery = json.loads((project / "delivery-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(delivery["status"], "pass")
        self.assertEqual(delivery["paid_submission_count"], 0)
        self.assertTrue(delivery["subtitle_enabled"])
        self.assertTrue((project / "outputs" / "final.subtitled.mp4").is_file())
        self.assertGreaterEqual(len(list((project / "review-frames" / "clean").glob("*.jpg"))), 3)

    def test_finalize_rejects_video_changed_after_review(self) -> None:
        project = self.root / "tampered-project"
        self.run_cli(
            project,
            "prepare", "--name", "post", "--script-file", str(self.script),
            "--existing-video", str(self.video), "--duration", "3",
        )
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "preflight")
        self.run_cli(project, "stitch")
        self.review(project, "clean")
        candidate = project / "outputs" / "final.clean.mp4"
        candidate.write_bytes(candidate.read_bytes() + b"changed-after-review")
        self.run_cli(project, "finalize", expect=1)
        self.assertFalse((project / "delivery-manifest.json").exists())

    def test_finalize_rejects_text_inspection_changed_after_review(self) -> None:
        project = self.root / "changed-visual-evidence"
        self.run_cli(project, "prepare", "--name", "post", "--script-file", str(self.script), "--existing-video", str(self.video), "--duration", "3")
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "preflight")
        self.run_cli(project, "stitch")
        self.review(project, "clean")
        manifest = project / "review-frames/clean/manifest.json"
        frames = json.loads(manifest.read_text())["frames"]
        Path(frames[-1]["path"]).write_bytes(b"changed-after-review")
        result = self.run_cli(project, "finalize", expect=1)
        self.assertIn("画面文件发生变化", result.stderr)
        self.assertFalse((project / "delivery-manifest.json").exists())

    def test_two_real_clips_are_normalized_and_stitched_with_audio(self) -> None:
        clips: list[Path] = []
        for index, color in enumerate(("red", "green"), start=1):
            clip = self.root / f"clip-{index}.mp4"
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=360x640:d=1:r=24",
                "-f", "lavfi", "-i", f"sine=frequency={400 + index * 100}:duration=1:sample_rate=44100",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(clip),
            ], check=True, capture_output=True)
            clips.append(clip)
        output = self.root / "stitched.mp4"
        report = stitch(clips, output, self.root / "stitch-work", "9:16")
        summary = technical_summary(output)
        self.assertEqual(report["method"], "normalized_concat_no_crossfade")
        self.assertTrue(summary["has_video"])
        self.assertTrue(summary["has_audio"])
        self.assertEqual((summary["width"], summary["height"]), (720, 1280))
        self.assertEqual(summary["frame_rate"], "24/1")
        self.assertGreater(summary["duration_seconds"], 1.8)
        self.assertLess(summary["duration_seconds"], 2.3)

    def test_stitch_accepts_relative_project_paths(self) -> None:
        cwd = Path.cwd()
        clip = Path(os.path.relpath(self.video, cwd))
        output = Path(os.path.relpath(self.root / "relative-output.mp4", cwd))
        work = Path(os.path.relpath(self.root / "relative-work", cwd))
        report = stitch([clip], output, work, "9:16")
        self.assertTrue(output.is_file())
        self.assertTrue(report["output"]["has_audio"])
        concat_text = (work / "concat.txt").read_text(encoding="utf-8")
        self.assertIn(str((work / "normalized" / "clip_001.mkv").resolve()), concat_text)

    def test_long_silent_tails_are_trimmed_before_stitching(self) -> None:
        clips: list[Path] = []
        for index, (video_seconds, speech_seconds) in enumerate(((15, 10), (6, 4)), start=1):
            clip = self.root / f"silent-tail-{index}.mp4"
            subprocess.run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=blue:s=360x640:d={video_seconds}:r=30",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={speech_seconds}:sample_rate=48000",
                "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono:d={video_seconds - speech_seconds}",
                "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(clip),
            ], check=True, capture_output=True)
            clips.append(clip)
        output = self.root / "trimmed-stitch.mp4"
        report = stitch(clips, output, self.root / "trim-work", "9:16")
        self.assertTrue(all(item["trimmed"] for item in report["tail_trim_reports"]))
        self.assertGreater(report["tail_trim_reports"][0]["removed_tail_silence_seconds"], 4.0)
        self.assertGreater(report["tail_trim_reports"][1]["removed_tail_silence_seconds"], 1.0)
        final = technical_summary(output)
        self.assertGreater(final["duration_seconds"], 14.0)
        self.assertLess(final["duration_seconds"], 15.5)
        self.assertLessEqual(detect_tail_silence(output)["tail_silence_seconds"], 0.6)

    def test_bounded_exact_retime_preserves_audio_and_reaches_target(self) -> None:
        output = self.root / "retimed.mp4"
        shutil.copy2(self.video, output)
        report = retime_short_exact_candidate(output, self.root / "retime-work", 2.5, 0.2)
        self.assertTrue(report["applied"])
        self.assertLessEqual(report["slowdown_factor"], 1.25)
        summary = technical_summary(output)
        self.assertTrue(summary["has_audio"])
        self.assertAlmostEqual(summary["duration_seconds"], 2.4, delta=0.1)
        self.assertTrue((self.root / "retime-work" / "before-exact-retime.mp4").is_file())

    def test_review_blocks_candidate_with_long_tail_silence(self) -> None:
        source = self.root / "review-long-tail.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=4:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=2",
            "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(source),
        ], check=True, capture_output=True)
        project = self.root / "review-tail-project"
        self.run_cli(
            project, "prepare", "--name", "tail-review", "--script-file", str(self.script),
            "--existing-video", str(source), "--duration", "5",
        )
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "preflight")
        self.run_cli(project, "stitch")
        self.review(project, "clean")
        report = json.loads((project / "clean-review.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["automatic_checks"]["tail_silence_within_0_6_seconds"])
        self.run_cli(project, "finalize", expect=1)
        self.assertFalse((project / "delivery-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
