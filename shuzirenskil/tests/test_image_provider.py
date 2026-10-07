from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKFLOW = SKILL_DIR / "scripts" / "workflow.py"


class ImageHandler(BaseHTTPRequestHandler):
    image_bytes = b""
    payloads: list[dict] = []
    paths: list[str] = []

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        type(self).paths.append(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).payloads.append(payload)
        body = json.dumps({"data": [{"b64_json": base64.b64encode(type(self).image_bytes).decode("ascii")}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ImageProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = self.root / "source.png"
        self.source = source
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=1024x1536:d=0.1", "-frames:v", "1", str(source)
        ], check=True, capture_output=True)
        ImageHandler.image_bytes = source.read_bytes()
        ImageHandler.payloads = []
        ImageHandler.paths = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def run_cli(self, project: Path, *args: str, expect: int = 0, env_file: Path | None = None) -> subprocess.CompletedProcess[str]:
        command = ["python3", str(WORKFLOW), "--project-dir", str(project)]
        if env_file:
            command += ["--env-file", str(env_file)]
        cli_args = list(args)
        if cli_args and cli_args[0] == "prepare" and "--video-prompt-spec" not in cli_args:
            cli_args.append("--allow-legacy-performance-plan")
        result = subprocess.run([*command, *cli_args], text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def test_plan_confirmation_directly_generates_and_binds_third_party_image(self) -> None:
        project = self.root / "project"
        script = self.root / "script.txt"
        script.write_text("这是一段用于测试图片流程的完整口播。", encoding="utf-8")
        prompt = self.root / "prompt.txt"
        prompt.write_text("一位成年中文数字人面向镜头，固定室内场景和柔和灯光，竖屏构图，画面中没有任何文字。", encoding="utf-8")
        env_file = self.root / ".env"
        env_file.write_text(
            f"OPENAI_BASE_URL=http://127.0.0.1:{self.server.server_port}/v1\nOPENAI_API_KEY=test-only-image-key\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        self.run_cli(project, "prepare", "--name", "image", "--script-file", str(script), "--duration", "10")
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        result = self.run_cli(
            project,
            "generate-image",
            "--prompt-file", str(prompt),
            "--provider-image-url", "https://example.com/generated.png",
            env_file=env_file,
        )
        self.assertIn('"additional_user_confirmation_before_image_generation": false', result.stdout)
        state = json.loads((project / "project.json").read_text(encoding="utf-8"))["state"]
        self.assertEqual(state["stage"], "awaiting_image_confirmation")
        self.assertEqual(len(ImageHandler.payloads), 1)
        self.assertEqual(ImageHandler.payloads[0]["model"], "gpt-image-2")
        self.assertEqual(ImageHandler.paths, ["/v1/images/generations"])
        self.assertEqual(set(ImageHandler.payloads[0]), {"model", "prompt", "size", "n"})
        self.assertTrue((project / "assets" / "production" / "canonical.png").is_file())

    def test_builtin_image_binds_without_image_api_key_or_provider_post(self) -> None:
        project = self.root / "builtin-project"
        script = self.root / "builtin-script.txt"
        script.write_text("这是一段用于测试内置生图流程的完整口播。", encoding="utf-8")
        prompt = self.root / "builtin-prompt.txt"
        prompt.write_text("一位成年中文数字人面向镜头，固定室内场景和柔和灯光，竖屏构图，画面中没有任何文字。", encoding="utf-8")
        self.run_cli(project, "prepare", "--name", "builtin", "--script-file", str(script), "--duration", "10")
        self.run_cli(project, "bind-generated-image", "--image-file", str(self.source), "--prompt-file", str(prompt), expect=1)
        self.run_cli(project, "confirm-plan", "--approved-by", "user")
        result = self.run_cli(project, "bind-generated-image", "--image-file", str(self.source), "--prompt-file", str(prompt))
        self.assertIn('"provider": "codex_builtin_image"', result.stdout)
        saved = json.loads((project / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"]["stage"], "awaiting_image_confirmation")
        self.assertEqual(saved["image_generation"]["provider"], "codex_builtin_image")
        self.assertEqual(saved["canonical_reference"]["source"], "codex_builtin_image")
        self.assertTrue((project / "assets" / "production" / "canonical.png").is_file())
        self.assertEqual(ImageHandler.payloads, [])


if __name__ == "__main__":
    unittest.main()
