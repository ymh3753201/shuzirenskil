"""Offline installer checks using isolated directories; no user installation writes."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_installer', ROOT/'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root/'中文 安装包'
        self.package.mkdir()
        shutil.copytree(ROOT/'shuzirenskil', self.package/'shuzirenskil')
        shutil.copy2(ROOT/'MANIFEST.sha256.json', self.package/'MANIFEST.sha256.json')
        self.project = self.root/'中文 视频项目'
        self.project.mkdir()
        self.path_patch = patch.object(installer, '__file__', str(self.package/'install.py'))
        self.path_patch.start()
        self.target = self.project/'.agents/skills/shuzirenskil'

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def test_install_exact_files_to_unicode_project(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.project)
        manifest = json.loads((self.package/'MANIFEST.sha256.json').read_text(encoding="utf-8"))['files']
        for name, digest in manifest.items():
            installed = self.target/Path(name).relative_to('shuzirenskil')
            self.assertEqual(hashlib.sha256(installed.read_bytes()).hexdigest(), digest)

    def test_existing_skill_is_preserved(self):
        self.target.mkdir(parents=True)
        original = self.target/'user-file.txt'
        original.write_text('keep me', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, '已有同名'):
            installer.install(self.project)
        self.assertEqual(original.read_text(encoding="utf-8"), 'keep me')

    def test_changed_package_is_rejected_before_install(self):
        (self.package/'shuzirenskil/SKILL.md').write_text('changed', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, '校验失败'):
            installer.install(self.project)
        self.assertFalse(self.target.exists())

    def test_finder_and_python_caches_are_not_installed(self):
        cache = self.package/'shuzirenskil/scripts/__pycache__'
        cache.mkdir(exist_ok=True)
        (cache/'temporary.pyc').write_bytes(b'cache')
        (self.package/'shuzirenskil/.DS_Store').write_bytes(b'finder')
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.project)
        self.assertFalse((self.target/'scripts/__pycache__').exists())
        self.assertFalse((self.target/'.DS_Store').exists())

    def test_missing_file_is_rejected(self):
        (self.package/'shuzirenskil/SKILL.md').unlink()
        with self.assertRaisesRegex(ValueError, '不完整'):
            installer.install(self.project)
        self.assertFalse(self.target.exists())

    def test_validator_failure_leaves_no_partial_install(self):
        with patch.object(installer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['validator'])):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install(self.project)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_missing_project_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '项目目录不存在'):
            installer.install(self.root/'not-created')


if __name__ == '__main__':
    unittest.main()
