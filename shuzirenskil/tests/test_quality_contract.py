from __future__ import annotations
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from _common import sha256_file
from media import extract_review_frames
from quality_contract import validate_character_review, validate_text_review


class CharacterReviewTests(unittest.TestCase):
    def test_casting_review_is_bound_to_actual_images_and_voice(self):
        project = {"canonical_reference": {"sha256": "image1"}, "image_set_digest": "set1", "settings": {"voice_signature": "voice1"}}
        evidence = {"canonical_sha256": "image1", "image_set_digest": "set1", "voice_signature": "voice1", "casting_fit": "pass", "reference_text_free": "pass", "visible_observations": "短发白衬衫，浅暖色背景，人物正视镜头。", "casting_observations": "选择明亮偏薄的轻声交流，适配穿搭分享角色。", "reference_text_observations": "逐张检查生产参考图，未观察到任何文字和标志。"}
        validate_character_review(project, evidence)
        for key in ("canonical_sha256", "image_set_digest", "voice_signature", "casting_fit", "reference_text_free"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_character_review(project, {**evidence, key: "changed"})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class TextReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "last-frame-flash.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=128x128:d=1:r=24", "-vf", "drawbox=color=red:t=fill:enable='eq(n,23)'", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(self.video)], check=True, capture_output=True)
        extract_review_frames(self.video, self.root / "frames", [0.5])
        self.manifest = self.root / "frames/manifest.json"
        self.evidence = {"text_review": {"manifest_file": str(self.manifest), "manifest_sha256": sha256_file(self.manifest), "reviewer": "agent_visual", "full_video_viewed": True, "all_sampled_frames_viewed": True, "last_frame_viewed": True, "text_present": False, "observations": "纯色测试视频，模拟已检查每个必要位置的画面。"}}

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_last_frame_includes_single_frame_flash(self):
        manifest = validate_text_review(self.evidence, self.video)
        last = next(frame for frame in manifest["frames"] if "last" in frame["roles"])
        self.assertEqual(last["frame_index"], 23)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", last["path"], "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
        self.assertGreater(raw[0], 200)
        self.assertLess(raw[2], 50)
        self.assertTrue(any("boundary" in frame["roles"] for frame in manifest["frames"]))

    def test_incomplete_viewing_or_detected_text_cannot_pass(self):
        for key, value in (("full_video_viewed", False), ("last_frame_viewed", False), ("all_sampled_frames_viewed", False), ("text_present", True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                evidence = copy.deepcopy(self.evidence)
                evidence["text_review"][key] = value
                validate_text_review(evidence, self.video)

    def test_changed_video_and_changed_review_frame_are_rejected(self):
        other = self.root / "other.mp4"
        other.write_bytes(self.video.read_bytes() + b"different")
        with self.assertRaisesRegex(ValueError, "视频不一致"):
            validate_text_review(self.evidence, other)
        manifest = json.loads(self.manifest.read_text())
        Path(manifest["frames"][-1]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "画面文件发生变化"):
            validate_text_review(self.evidence, self.video)


if __name__ == "__main__":
    unittest.main()
