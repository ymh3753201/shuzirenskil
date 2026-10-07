from __future__ import annotations

import argparse
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import subprocess
import tempfile
from unittest.mock import patch

import sys

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from provider import probe_endpoint  # noqa: E402
from image_transport import local_image_data_uri  # noqa: E402
from workflow import _api_key, _load_video_config, _provider_reference_payload, _require_snapshot_provider, _validate_config, _verify_pre_submit_auth  # noqa: E402


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"not found"}')

    def log_message(self, format: str, *args: object) -> None:
        return


class UnauthorizedProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"message":"Invalid token"}')

    def log_message(self, format: str, *args: object) -> None:
        return


class ProviderChainTests(unittest.TestCase):
    def test_primary_chain_is_91topgo_and_fallback_is_mikuapi(self) -> None:
        chain = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json")
        self.assertEqual(chain["provider_id"], "91topgo")
        self.assertEqual(chain["create_path"], "/v1/videos")
        self.assertEqual(chain["duration_field"], "seconds")
        self.assertFalse(chain["supports_image_to_video"])
        self.assertTrue(chain["supports_reference_images"])
        self.assertFalse(chain["requires_public_https_images"])
        self.assertEqual(chain["local_image_transport"], "jpeg_data_uri")
        fallback = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json", "mikuapi")
        self.assertEqual(fallback["provider_id"], "mikuapi")
        self.assertEqual(fallback["create_path"], "/v1/videos/generations")

    def test_91topgo_validation_rejects_image_to_video_mode(self) -> None:
        config = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json")
        _validate_config(config)
        config["supports_image_to_video"] = True
        with self.assertRaises(ValueError):
            _validate_config(config)

    def test_health_probe_does_not_create_video(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = probe_endpoint(
                f"http://127.0.0.1:{server.server_port}",
                "/v1/videos/__shuzirenskil_healthcheck__",
                "test-key",
                3,
            )
            self.assertEqual(result["status_code"], 404)
        finally:
            server.shutdown()
            server.server_close()

    def test_invalid_91topgo_key_is_blocked_before_paid_submit(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), UnauthorizedProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json")
            config["base_url"] = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaisesRegex(ValueError, "检查失败"):
                _verify_pre_submit_auth(config, "invalid-test-key", 3)
        finally:
            server.shutdown()
            server.server_close()

    def test_local_reference_becomes_bounded_jpeg_data_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "reference.png"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=0.1", "-frames:v", "1", str(image)],
                check=True,
                capture_output=True,
            )
            value = local_image_data_uri(image, max_bytes=800000)
            self.assertTrue(value.startswith("data:image/jpeg;base64,"))
            self.assertLessEqual(len(value.split(",", 1)[1].encode("ascii")) * 3 // 4, 800000)

    def test_91topgo_reference_payload_accepts_local_path_without_public_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "reference.png"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=0.1", "-frames:v", "1", str(image)],
                check=True,
                capture_output=True,
            )
            import hashlib
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            payload = _provider_reference_payload(
                {"local_path": str(image), "sha256": digest},
                _load_video_config(SKILL_DIR / "assets" / "provider-chain.json"),
            )
            self.assertTrue(payload["url"].startswith("data:image/jpeg;base64,"))

    def test_explicit_env_without_provider_key_uses_matching_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("SHUZIRENSKIL_API_KEY=legacy-invalid\n", encoding="utf-8")
            env_file.chmod(0o600)
            config = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json")
            # Historical snapshots still contain this unsafe alias.
            config["api_key_env_aliases"] = ["SHUZIRENSKIL_API_KEY"]
            args = argparse.Namespace(env_file=env_file, project_dir=Path(temp_dir))
            with patch("private_env._keychain_value", return_value="keychain-valid"):
                self.assertEqual(_api_key(config, args), "keychain-valid")

    def test_provider_specific_env_key_can_override_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text("SHUZIRENSKIL_91TOPGO_API_KEY=explicit-valid\n", encoding="utf-8")
            env_file.chmod(0o600)
            config = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json")
            args = argparse.Namespace(env_file=env_file, project_dir=Path(temp_dir))
            with patch("private_env._keychain_value", return_value="keychain-valid"):
                self.assertEqual(_api_key(config, args), "explicit-valid")

    def test_invalid_mikuapi_key_is_blocked_before_paid_submit(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), UnauthorizedProbeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = _load_video_config(SKILL_DIR / "assets" / "provider-chain.json", "mikuapi")
            config["base_url"] = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaisesRegex(ValueError, "已阻止付费提交"):
                _verify_pre_submit_auth(config, "invalid-test-key", 3)
        finally:
            server.shutdown()
            server.server_close()

    def test_submit_cannot_silently_switch_approved_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "已锁定视频 Provider 91topgo"):
            _require_snapshot_provider({"provider_id": "91topgo"}, "mikuapi")
        _require_snapshot_provider({"provider_id": "91topgo"}, "91topgo")


if __name__ == "__main__":
    unittest.main()
