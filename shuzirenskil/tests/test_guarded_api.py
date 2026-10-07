from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKFLOW = SKILL_DIR / "scripts" / "workflow.py"
BASE_CONFIG = json.loads((SKILL_DIR / "assets" / "model-config.json").read_text(encoding="utf-8"))
SCRIPT_TEXT = (
    "先把每天收到的资料放进统一目录，再让Codex按照项目名称进行整理。"
    "接着检查自动生成的摘要，把没有来源的说法全部标记为待确认。"
    "最后回到原文件逐项核对，确认内容正确以后再交给其他人使用。"
)


class FakeHandler(BaseHTTPRequestHandler):
    post_count = 0
    upload_count = 0
    upload_bodies: list[bytes] = []
    payloads: list[dict] = []
    request_headers: list[dict[str, str]] = []
    request_paths: list[str] = []
    download_headers: list[dict[str, str]] = []
    missing_task_id = False
    upload_fail = False
    video_url = ""
    video_bytes = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, value: dict, status: int = 200) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path == "/v1/files":
            type(self).upload_count += 1
            type(self).upload_bodies.append(raw)
            if type(self).upload_fail:
                self._json({"error": "simulated upload failure"}, status=500)
                return
            self._json({
                "id": f"file_test_{type(self).upload_count}",
                "filename": "source.png",
                "bytes": len(raw),
                "expires_at": 9999999999,
            })
            return
        payload = json.loads(raw.decode("utf-8"))
        type(self).post_count += 1
        type(self).payloads.append(payload)
        type(self).request_headers.append(dict(self.headers.items()))
        type(self).request_paths.append(self.path)
        if type(self).missing_task_id:
            self._json({"status": "accepted"})
        else:
            self._json({"request_id": f"task_{type(self).post_count}"})

    def do_GET(self) -> None:
        type(self).request_headers.append(dict(self.headers.items()))
        if self.path == "/v1/models":
            self._json({"data": [{"id": "grok-imagine-video-1.5"}, {"id": "other-model"}]})
        elif self.path == "/video.mp4":
            type(self).download_headers.append(dict(self.headers.items()))
            data = type(self).video_bytes
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"status": "completed", "download_url": type(self).video_url})


class GuardedApiTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeHandler.post_count = 0
        FakeHandler.upload_count = 0
        FakeHandler.upload_bodies = []
        FakeHandler.payloads = []
        FakeHandler.request_headers = []
        FakeHandler.request_paths = []
        FakeHandler.download_headers = []
        FakeHandler.missing_task_id = False
        FakeHandler.upload_fail = False
        FakeHandler.video_bytes = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        FakeHandler.video_url = f"http://127.0.0.1:{self.server.server_port}/video.mp4"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = dict(BASE_CONFIG)
        self.config["base_url"] = f"http://127.0.0.1:{self.server.server_port}"
        self.config["prefer_keychain"] = False
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.script = self.root / "script.txt"
        self.script.write_text(SCRIPT_TEXT, encoding="utf-8")
        self.image = self.root / "source.png"
        self.provider_image_url = "https://example.com/source.png"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=0.1",
            "-frames:v", "1", str(self.image),
        ], check=True, capture_output=True)
        self.continuity = self.root / "continuity.json"
        self.continuity.write_text(json.dumps({
            "character_identity": "同一位成年测试人物，面部和发型固定",
            "wardrobe": "蓝色衬衫，款式和颜色固定",
            "scene": "固定的室内白墙背景",
            "camera": "平视中近景，人物居中",
            "lighting": "左前方柔和白色主光",
            "motion_policy": "只允许眨眼、点头和轻微手势",
            "voice_strategy": "gateway_generated_voice_unverified",
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def run_cli(self, project: Path, *args: str, expect: int = 0, api_key: bool = False, env_file: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("SHUZIRENSKIL_MIKUAPI_API_KEY", None)
        if api_key:
            env["SHUZIRENSKIL_MIKUAPI_API_KEY"] = "test-only-not-a-real-key"
        command = ["python3", str(WORKFLOW), "--project-dir", str(project)]
        if env_file:
            command += ["--env-file", str(env_file)]
        cli_args = list(args)
        if cli_args and cli_args[0] == "prepare" and "--video-prompt-spec" not in cli_args:
            cli_args.append("--allow-legacy-performance-plan")
        result = subprocess.run(
            [*command, *cli_args],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def prepare_and_authorize(self, project: Path) -> int:
        self.run_cli(project, "prepare", "--name", "test", "--script-file", str(self.script), "--duration", "35", "--no-pilot-first")
        plan = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]
        count = plan["minimum_paid_segment_count"]
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--image", str(self.image),
            "--storyboard-image", str(self.image), "--continuity-spec", str(self.continuity),
            "--provider-image-url", self.provider_image_url,
        )
        self.run_cli(
            project,
            "authorize",
            "--approved-by", "user",
            "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", str(count),
            "--acknowledge-long-video-risk",
        )
        self.run_cli(project, "preflight", "--config", str(self.config_path))
        return count

    def test_preflight_is_offline_and_redacted(self) -> None:
        project = self.root / "project"
        count = self.prepare_and_authorize(project)
        self.assertEqual(FakeHandler.post_count, 0)
        contract_text = (project / "production-contract.json").read_text(encoding="utf-8")
        self.assertNotIn("data:image", contract_text)
        self.assertNotIn("Authorization", contract_text)
        self.assertNotIn(self.provider_image_url, contract_text)
        contract = json.loads(contract_text)
        self.assertEqual(contract["max_paid_submissions"], count)
        self.assertTrue(contract["runtime_paid_verified"])
        self.assertEqual(contract["verification_level"], "provider_paid_short_clip_verified")
        self.assertRegex(contract["voice_signature"], r"^[0-9a-f]{64}$")
        self.assertEqual(contract["image_reuse_policy"], "same_canonical_image_for_all_segments")
        self.assertTrue(all(item["voice_signature"] == contract["voice_signature"] for item in contract["dry_requests"]))

    def test_designed_voice_requires_image_review_and_repeats_anchors_in_every_segment(self) -> None:
        import sys
        sys.path.insert(0, str(SKILL_DIR / "scripts"))
        from voice_fixture import designed_voice_spec
        project = self.root / "voice-designed"
        plan = json.loads(self.run_cli(project, "plan", "--script-file", str(self.script)).stdout)
        spec = designed_voice_spec()
        spec["performance_beats"] = [{"segment_index": segment["index"], "trigger_phrase": segment["script"][:4], "expression": "短暂轻轻微笑"} for segment in plan["segments"]]
        spec_path = self.root / "voice-spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False))
        self.run_cli(project, "prepare", "--name", "voice", "--script-file", str(self.script), "--video-prompt-spec", str(spec_path))
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "bind-image", "--image", str(self.image), "--continuity-spec", str(self.continuity), "--provider-image-url", self.provider_image_url)
        auth = ("authorize", "--approved-by", "user", "--confirmation-intent", "confirm_images_and_start", "--max-paid-submissions", str(len(plan["segments"])), "--acknowledge-long-video-risk")
        result = self.run_cli(project, *auth, expect=1)
        self.assertIn("没有绑定当前图片", result.stderr)
        saved = json.loads((project / "project.json").read_text())
        evidence = {"canonical_sha256": saved["canonical_reference"]["sha256"], "image_set_digest": saved["image_set_digest"], "voice_signature": saved["settings"]["voice_signature"], "casting_fit": "pass", "reference_text_free": "pass", "visible_observations": "测试夹具为合成图片，模拟读取实际形象。", "casting_observations": "模拟根据讲解角色选取清亮偏薄的声线。", "reference_text_observations": "测试夹具模拟检查生产图片没有文字。"}
        evidence_path = self.root / "character.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False))
        self.run_cli(project, "review-character", "--evidence-file", str(evidence_path))
        self.run_cli(project, *auth)
        self.run_cli(project, "preflight", "--config", str(self.config_path))
        contract = json.loads((project / "production-contract.json").read_text())
        self.assertGreater(len(contract["dry_requests"]), 1)
        for request in contract["dry_requests"]:
            self.assertIn(spec["voice_casting"]["resonance"], request["payload"]["prompt"])
            self.assertIn("最后一帧", request["payload"]["prompt"])
        self.assertEqual(FakeHandler.post_count, 0)

    def test_91topgo_primary_accepts_confirmed_local_image_without_https(self) -> None:
        project = self.root / "91topgo-local-reference"
        short_script = self.root / "short-script.txt"
        short_script.write_text("Codex能帮你整理资料、检查信息，并把重复工作变成清晰流程。", encoding="utf-8")
        self.run_cli(project, "prepare", "--name", "local-reference", "--script-file", str(short_script), "--duration", "15")
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project,
            "bind-image",
            "--image", str(self.image),
            "--continuity-spec", str(self.continuity),
        )
        saved = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["input_transport"], "jpeg-data-uri")
        provider_inputs = json.loads((project / "provider-inputs.json").read_text(encoding="utf-8"))
        self.assertTrue(provider_inputs["items"][0]["image"]["local_path"])
        self.run_cli(
            project,
            "authorize",
            "--approved-by", "user",
            "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", "1",
        )
        self.run_cli(project, "preflight")
        contract = json.loads((project / "production-contract.json").read_text(encoding="utf-8"))
        payload = contract["dry_requests"][0]["payload"]
        self.assertEqual(payload["seconds"], str(saved["plan"]["segments"][0]["request_seconds"]))
        self.assertLess(int(payload["seconds"]), 15)
        self.assertEqual(contract["max_paid_submissions"], 1)
        self.assertIn("reference_images", payload)
        self.assertNotIn("image", payload)
        self.assertNotIn("data:image", (project / "production-contract.json").read_text(encoding="utf-8"))

    def test_prompt_voice_multisegment_defaults_to_same_canonical_image(self) -> None:
        project = self.root / "same-image-project"
        self.run_cli(project, "prepare", "--name", "same-image", "--script-file", str(self.script), "--duration", "35")
        plan = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]
        count = plan["minimum_paid_segment_count"]
        self.assertGreater(count, 1)
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        segment_args: list[str] = []
        for index in range(count):
            segment_args += ["--segment-image", str(self.image), "--provider-image-url", f"https://example.com/segment-{index}.png"]
        blocked = self.run_cli(
            project,
            "bind-image",
            "--canonical-image", str(self.image),
            "--continuity-spec", str(self.continuity),
            *segment_args,
            expect=1,
        )
        self.assertIn("默认必须复用同一张主参考图", blocked.stderr)
        self.run_cli(
            project,
            "bind-image",
            "--canonical-image", str(self.image),
            "--continuity-spec", str(self.continuity),
            "--allow-derived-segment-images-with-risk",
            *segment_args,
        )
        saved = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["image_reuse_policy"], "derived_segment_images_risk_accepted")

    def test_submit_uses_exact_documented_fields_and_only_once(self) -> None:
        project = self.root / "project"
        count = self.prepare_and_authorize(project)
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        self.assertEqual(FakeHandler.post_count, count)
        self.assertTrue(FakeHandler.payloads)
        self.assertEqual(
            set(FakeHandler.payloads[0]),
            {"model", "prompt", "duration", "aspect_ratio", "resolution", "image"},
        )
        self.assertEqual(FakeHandler.request_paths, ["/v1/videos/generations"] * count)
        self.assertEqual(FakeHandler.payloads[0]["model"], "grok-imagine-video-1.5")
        self.assertIsInstance(FakeHandler.payloads[0]["duration"], int)
        self.assertEqual(FakeHandler.payloads[0]["image"], {"url": self.provider_image_url})
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        self.assertEqual(FakeHandler.post_count, count)
        self.run_cli(project, "poll", "--max-polls", "1", "--interval", "0", api_key=True)
        jobs = json.loads((project / "jobs.json").read_text(encoding="utf-8"))["jobs"]
        self.assertTrue(all(job["submission_attempts"] == 1 for job in jobs))
        self.assertTrue(all(job["status"] == "downloaded" for job in jobs))
        self.assertTrue(FakeHandler.download_headers)
        self.assertNotIn("Authorization", FakeHandler.download_headers[0])

    def test_multisegment_pilot_blocks_later_paid_posts_until_review(self) -> None:
        project = self.root / "pilot-project"
        self.run_cli(project, "prepare", "--name", "pilot", "--script-file", str(self.script), "--duration", "35")
        saved = json.loads((project / "project.json").read_text(encoding="utf-8"))
        count = saved["plan"]["minimum_paid_segment_count"]
        self.assertGreater(count, 1)
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "bind-image", "--image", str(self.image), "--continuity-spec", str(self.continuity), "--provider-image-url", self.provider_image_url)
        self.run_cli(project, "authorize", "--approved-by", "user", "--confirmation-intent", "confirm_images_and_start", "--max-paid-submissions", str(count), "--acknowledge-long-video-risk")
        self.run_cli(project, "preflight", "--config", str(self.config_path))
        valid_video = self.root / "pilot-video.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:r=24:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
            "-c:v", "libx264", "-c:a", "aac", str(valid_video),
        ], check=True, capture_output=True)
        FakeHandler.video_bytes = valid_video.read_bytes()
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        self.assertEqual(FakeHandler.post_count, 1)
        self.run_cli(project, "submit", "--timeout", "3", api_key=True, expect=1)
        self.assertEqual(FakeHandler.post_count, 1)
        self.run_cli(project, "poll", "--max-polls", "1", "--interval", "0", api_key=True)
        self.assertEqual(json.loads((project / "project.json").read_text(encoding="utf-8"))["state"]["stage"], "pilot_review_pending")
        evidence = self.root / "pilot-evidence.json"
        pilot_clip = project / "clips" / "shot_001.mp4"
        # This synthetic clip tests the payment gate only; the notes below are simulated evidence.
        evidence_data = {
            "no_generated_text": "测试夹具模拟完整查看，包含最后一帧没有文字。",
            "reviewed_clip_sha256": hashlib.sha256(pilot_clip.read_bytes()).hexdigest(),
            "motion_naturalness": "完整观看后，动作轻微且没有僵住或重复手势。",
            "gesture_phrase_fit": "已对照重点台词检查手势出现的时机和回位。",
            "lip_sync": "逐句检查嘴部开合与人声同步，未见明显错位。",
            "voice_naturalness": "完整听过，语调有自然起伏，停顿符合句意。",
            "speech_pacing": "完整听过，语速和句间停顿符合本段内容。",
            "script_complete": "逐句听写核对，首段句首句尾都完整。",
            "identity_consistency": "完整观看，人物脸型服装与确认图片保持一致。",
            "normal_speed_playback": "按正常速度完整播放视频并听完音轨。",
            "spoken_words": "这不是原来的口播。",
        }
        evidence.write_text(json.dumps(evidence_data, ensure_ascii=False), encoding="utf-8")
        blocked = self.run_cli(project, "review-pilot", "--evidence-file", str(evidence), expect=1)
        self.assertIn("不完全一致", blocked.stderr)
        self.assertFalse((project / "pilot-review.json").exists())
        evidence_data["spoken_words"] = saved["plan"]["segments"][0]["script"]
        evidence.write_text(json.dumps(evidence_data, ensure_ascii=False), encoding="utf-8")
        missing_text = self.run_cli(project, "review-pilot", "--evidence-file", str(evidence), expect=1)
        self.assertIn("text_review", missing_text.stderr)
        self.assertEqual(FakeHandler.post_count, 1)
        inspection = json.loads(self.run_cli(project, "inspect-visuals", "--video", str(pilot_clip), "--output-dir", str(project / "pilot-visuals")).stdout)
        evidence_data["text_review"] = {"manifest_file": inspection["manifest_file"], "manifest_sha256": inspection["manifest_sha256"], "reviewer": "agent_visual", "full_video_viewed": True, "all_sampled_frames_viewed": True, "last_frame_viewed": True, "text_present": False, "observations": "纯色合成测试视频，模拟检查首尾及全程画面。"}
        evidence.write_text(json.dumps(evidence_data, ensure_ascii=False), encoding="utf-8")
        self.run_cli(project, "review-pilot", "--evidence-file", str(evidence))
        pilot_report_path = project / "pilot-review.json"
        pilot_report = json.loads(pilot_report_path.read_text(encoding="utf-8"))
        forged_report = {**pilot_report, "task_id": "another-task"}
        pilot_report_path.write_text(json.dumps(forged_report), encoding="utf-8")
        self.run_cli(project, "submit", "--timeout", "3", api_key=True, expect=1)
        self.assertEqual(FakeHandler.post_count, 1)
        pilot_report_path.write_text(json.dumps(pilot_report), encoding="utf-8")
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        self.assertEqual(FakeHandler.post_count, count)

    def test_reference_mode_builds_multi_image_and_preset_voice_payload(self) -> None:
        project = self.root / "reference-project"
        self.config["gateway_reference_to_video_enabled"] = True
        self.config["gateway_preset_voice_enabled"] = True
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.run_cli(
            project, "prepare", "--name", "reference", "--script-file", str(self.script),
            "--duration", "35", "--generation-mode", "reference-to-video", "--preset-voice", "eve",
        )
        plan = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]
        count = plan["minimum_paid_segment_count"]
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--canonical-image", str(self.image),
            "--reference-image", str(self.image), "--reference-role", "identity", "--reference-role", "wardrobe",
            "--provider-image-url", self.provider_image_url,
            "--provider-reference-image-url", "https://example.com/wardrobe.png",
            "--continuity-spec", str(self.continuity),
        )
        self.run_cli(
            project, "authorize", "--approved-by", "user", "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", str(count), "--acknowledge-long-video-risk",
        )
        self.run_cli(project, "preflight", "--config", str(self.config_path))
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        payload = FakeHandler.payloads[0]
        self.assertNotIn("image", payload)
        self.assertEqual(payload["reference_images"], [
            {"url": self.provider_image_url},
            {"url": "https://example.com/wardrobe.png"},
        ])
        self.assertEqual(payload["reference_audios"], [{"voice_id": "eve"}])
        self.assertIn("<IMAGE_1>", payload["prompt"])
        self.assertIn("<IMAGE_2>", payload["prompt"])
        self.assertIn("<AUDIO_0>", payload["prompt"])

    def test_preset_voice_cannot_be_attached_to_image_to_video(self) -> None:
        project = self.root / "invalid-voice-mode"
        self.run_cli(
            project, "prepare", "--name", "invalid", "--script-file", str(self.script),
            "--duration", "35", "--preset-voice", "eve", expect=1,
        )
        self.assertFalse((project / "project.json").exists())
        self.assertEqual(FakeHandler.post_count, 0)

    def test_reference_urls_must_match_local_reference_order(self) -> None:
        project = self.root / "reference-url-mismatch"
        self.run_cli(
            project, "prepare", "--name", "reference", "--script-file", str(self.script),
            "--duration", "35", "--generation-mode", "reference-to-video",
        )
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--canonical-image", str(self.image),
            "--reference-image", str(self.image), "--reference-role", "identity", "--reference-role", "wardrobe",
            "--provider-image-url", self.provider_image_url,
            "--continuity-spec", str(self.continuity), expect=1,
        )
        self.assertEqual(FakeHandler.post_count, 0)

    def test_reference_mode_is_blocked_when_miku_gateway_is_unverified(self) -> None:
        project = self.root / "blocked-reference-project"
        self.run_cli(
            project, "prepare", "--name", "reference", "--script-file", str(self.script),
            "--duration", "35", "--generation-mode", "reference-to-video",
        )
        plan = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]
        count = plan["minimum_paid_segment_count"]
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--canonical-image", str(self.image), "--reference-role", "identity",
            "--provider-image-url", self.provider_image_url, "--continuity-spec", str(self.continuity),
        )
        self.run_cli(
            project, "authorize", "--approved-by", "user", "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", str(count), "--acknowledge-long-video-risk",
        )
        self.run_cli(project, "preflight", "--config", str(self.config_path), expect=1)
        self.assertEqual(FakeHandler.post_count, 0)

    def test_confirmed_local_image_can_be_uploaded_with_ttl_then_used_by_file_id(self) -> None:
        project = self.root / "file-upload-project"
        self.config["gateway_files_api_enabled"] = True
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.run_cli(project, "prepare", "--name", "upload", "--script-file", str(self.script), "--duration", "35")
        plan = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]
        count = plan["minimum_paid_segment_count"]
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--canonical-image", str(self.image), "--input-transport", "provider-file",
            "--continuity-spec", str(self.continuity),
        )
        self.run_cli(
            project, "authorize", "--approved-by", "user", "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", str(count), "--acknowledge-long-video-risk",
        )
        self.run_cli(
            project, "upload-inputs", "--config", str(self.config_path), "--expires-after", "3600",
            "--timeout", "3", api_key=True,
        )
        self.assertEqual(FakeHandler.upload_count, 1)
        body = FakeHandler.upload_bodies[0]
        self.assertLess(body.index(b'name="expires_after"'), body.index(b'name="file"'))
        self.run_cli(project, "preflight", "--config", str(self.config_path))
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        self.assertEqual(FakeHandler.payloads[0]["image"], {"file_id": "file_test_1"})
        ledger_mode = (project / "provider-upload-ledger.json").stat().st_mode & 0o777
        self.assertEqual(ledger_mode, 0o600)

    def test_unknown_file_upload_result_is_not_retried(self) -> None:
        project = self.root / "unknown-file-upload-project"
        self.config["gateway_files_api_enabled"] = True
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.run_cli(project, "prepare", "--name", "upload", "--script-file", str(self.script), "--duration", "35")
        plan = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]
        count = plan["minimum_paid_segment_count"]
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--canonical-image", str(self.image), "--input-transport", "provider-file",
            "--continuity-spec", str(self.continuity),
        )
        self.run_cli(
            project, "authorize", "--approved-by", "user", "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", str(count), "--acknowledge-long-video-risk",
        )
        FakeHandler.upload_fail = True
        self.run_cli(
            project, "upload-inputs", "--config", str(self.config_path), "--timeout", "3",
            api_key=True, expect=1,
        )
        self.assertEqual(FakeHandler.upload_count, 1)
        self.run_cli(
            project, "upload-inputs", "--config", str(self.config_path), "--timeout", "3",
            api_key=True, expect=1,
        )
        self.assertEqual(FakeHandler.upload_count, 1)
        ledger = json.loads((project / "provider-upload-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(next(iter(ledger["uploads"].values()))["status"], "upload_unknown")

    def test_missing_task_id_blocks_retry(self) -> None:
        project = self.root / "project"
        count = self.prepare_and_authorize(project)
        FakeHandler.missing_task_id = True
        self.run_cli(project, "submit", "--timeout", "3", expect=1, api_key=True)
        self.assertEqual(FakeHandler.post_count, 1)
        jobs = json.loads((project / "jobs.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual(jobs[0]["status"], "submission_unknown")
        self.run_cli(project, "submit", "--timeout", "3", expect=1, api_key=True)
        self.assertEqual(FakeHandler.post_count, 1)
        self.assertGreaterEqual(count, 1)

    def test_authorization_cap_must_equal_segment_count(self) -> None:
        project = self.root / "project"
        self.run_cli(project, "prepare", "--name", "test", "--script-file", str(self.script), "--duration", "35")
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "bind-image", "--image", str(self.image), "--continuity-spec", str(self.continuity), "--provider-image-url", self.provider_image_url)
        self.run_cli(
            project,
            "authorize",
            "--approved-by", "user",
            "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", "99",
            expect=1,
        )

    def test_multisegment_requires_continuity_and_explicit_risk_acknowledgement(self) -> None:
        project = self.root / "project"
        self.run_cli(project, "prepare", "--name", "test", "--script-file", str(self.script), "--duration", "35")
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(project, "bind-image", "--image", str(self.image), "--provider-image-url", self.provider_image_url, expect=1)
        self.run_cli(project, "bind-image", "--image", str(self.image), "--continuity-spec", str(self.continuity), "--provider-image-url", self.provider_image_url)
        count = json.loads((project / "project.json").read_text(encoding="utf-8"))["plan"]["minimum_paid_segment_count"]
        self.run_cli(
            project, "authorize", "--approved-by", "user",
            "--confirmation-intent", "confirm_images_and_start",
            "--max-paid-submissions", str(count), expect=1,
        )

    def test_secure_dotenv_and_free_model_check(self) -> None:
        project = self.root / "provider-check"
        env_file = self.root / ".env"
        env_file.write_text("SHUZIRENSKIL_MIKUAPI_API_KEY=test-only-not-a-real-key\n", encoding="utf-8")
        env_file.chmod(0o600)
        result = self.run_cli(
            project, "check-provider", "--config", str(self.config_path), "--timeout", "3", env_file=env_file,
        )
        self.assertIn('"model_available": true', result.stdout)
        self.assertEqual(FakeHandler.post_count, 0)
        self.assertNotIn("test-only-not-a-real-key", result.stdout + result.stderr)
        self.assertTrue(FakeHandler.request_headers)
        self.assertEqual(FakeHandler.request_headers[-1].get("Accept"), "application/json")
        self.assertTrue(FakeHandler.request_headers[-1].get("User-Agent", "").startswith("shuzirenskil/"))

    def test_insecure_dotenv_is_rejected_without_network(self) -> None:
        project = self.root / "provider-check"
        env_file = self.root / ".env"
        env_file.write_text("SHUZIRENSKIL_MIKUAPI_API_KEY=test-only-not-a-real-key\n", encoding="utf-8")
        env_file.chmod(0o644)
        self.run_cli(
            project, "check-provider", "--config", str(self.config_path), "--timeout", "3",
            env_file=env_file, expect=1,
        )
        self.assertEqual(FakeHandler.post_count, 0)

    def test_storyboard_is_preview_only_and_never_sent(self) -> None:
        project = self.root / "project"
        count = self.prepare_and_authorize(project)
        stored = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["canonical_reference"]["role"], "canonical_video_source")
        self.assertEqual(len(stored["storyboards"]), 1)
        self.assertFalse(stored["storyboards"][0]["sent_to_video_provider"])
        self.assertTrue(all(item["parent_canonical_sha256"] == stored["canonical_reference"]["sha256"] for item in stored["plan"]["segments"]))
        self.run_cli(project, "submit", "--timeout", "3", api_key=True)
        self.assertEqual(FakeHandler.post_count, count)
        self.assertTrue(all(set(payload) == {"model", "prompt", "duration", "aspect_ratio", "resolution", "image"} for payload in FakeHandler.payloads))

    def test_plan_tampering_is_blocked_before_network(self) -> None:
        project = self.root / "project"
        self.prepare_and_authorize(project)
        stored = json.loads((project / "project.json").read_text(encoding="utf-8"))
        stored["plan"]["segments"][0]["script"] = "被修改的台词。"
        (project / "project.json").write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        self.run_cli(project, "preflight", "--config", str(self.config_path), expect=1)
        self.assertEqual(FakeHandler.post_count, 0)

    def test_image_content_must_match_extension(self) -> None:
        project = self.root / "project"
        wrong_extension = self.root / "pretends-to-be-jpeg.jpg"
        wrong_extension.write_bytes(self.image.read_bytes())
        self.run_cli(project, "prepare", "--name", "test", "--script-file", str(self.script), "--duration", "35")
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        self.run_cli(
            project, "bind-image", "--image", str(wrong_extension),
            "--continuity-spec", str(self.continuity), "--provider-image-url", self.provider_image_url, expect=1,
        )


if __name__ == "__main__":
    unittest.main()
