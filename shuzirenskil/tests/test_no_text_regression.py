"""No provider calls. Regressions for text leakage and quality-gate bypasses."""
import argparse
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from _common import atomic_write_json, read_json, sha256_file
from text_policy import OPENING, ENDING, validate_direction, require_guarded_prompt
from video_prompt import build_video_prompt, default_prompt_spec
from generation_gates import require_no_quality_failure, require_original_dimensions, record_confirmed_text_failure
from quality_contract import validate_text_review
import test_quality_contract as quality_fixture
import workflow


class PromptRegressions(unittest.TestCase):
    def test_reported_phrase_is_only_in_spoken_audio(self):
        spec = default_prompt_spec()
        spec['negative_constraints'] = ['避免抖动']
        spec['performance_beats'] = [{'segment_index': 1, 'trigger_phrase': '长聊能更快了', 'expression': '短暂轻挑眉'}]
        script = 'AI助手，长聊能更快了！后续只传新增内容。'
        for version in ('2.0', '3.0'):
            spec['schema_version'] = version
            spec.pop('voice_casting', None)
            prompt = build_video_prompt(segment={'index': 1, 'script': script}, language='中文',
                continuity=None, prompt_spec=spec, generation_mode='image-to-video')
            require_guarded_prompt(prompt)
            self.assertTrue(prompt.startswith(OPENING))
            self.assertTrue(prompt.endswith(ENDING))
            self.assertEqual(prompt.count('长聊能更快了'), 1)
            self.assertIn('<SPOKEN_AUDIO_ONLY>\n' + script + '\n</SPOKEN_AUDIO_ONLY>', prompt)

    def test_conflicting_visual_directions_fail_before_compiling(self):
        for text in ('右侧显示大字字幕', '片尾生成总结字卡', 'Add large captions',
                     'No subtitles, but show a title card', '不要字幕，但显示标题',
                     '不出现文字并添加标题', '身后的屏幕写着“欢迎”', '包装印有文字',
                     '无袖服装上印有文字', '没有背景音乐且添加字幕'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate_direction(text)
        for text in ('禁止字幕、标题、文字', '不得生成字幕', '不得在画面中添加字幕', 'No text or subtitles', '自然微笑，不改变语速'):
            validate_direction(text)

    def test_script_can_discuss_subtitles_without_requesting_visual_text(self):
        script = '今天教你添加字幕。'
        prompt = build_video_prompt(segment={'index': 1, 'script': script}, language='中文',
            continuity=None, prompt_spec=default_prompt_spec(), generation_mode='image-to-video')
        self.assertIn(script, prompt)
        spec = default_prompt_spec();spec['performance'] = '右下角添加字幕'
        with self.assertRaises(ValueError):
            build_video_prompt(segment={'index': 1, 'script': script}, language='中文',
                continuity=None, prompt_spec=spec, generation_mode='image-to-video')

    def test_old_or_truncated_saved_request_cannot_submit(self):
        for value in ('原来只有一句禁止字幕', OPENING + '\n台词', ENDING, None):
            with self.assertRaises(ValueError):require_guarded_prompt(value)


class FailureRegressions(unittest.TestCase):
    def test_poll_fallback_cannot_bypass_known_quality_failure(self):
        from fallback import submit_with_fallback
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp, patch('fallback.create_task') as post:
            root = Path(tmp)
            atomic_write_json(root / 'quality-failure-report.json', {'status': 'fail'})
            with self.assertRaisesRegex(ValueError, '明确质量缺陷'):
                submit_with_fallback({}, {}, root / 'ledger.json', None, {}, {},
                    argparse.Namespace(project_dir=root), Mock(), Mock(), Mock(), terminal_failure=True)
            post.assert_not_called()

    def test_both_failure_report_names_block_all_legacy_mutating_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('quality-failure.json', 'quality-failure-report.json'):
                atomic_write_json(root / name, {'status': 'fail', 'checks': {'no_generated_text': {'status': 'fail'}}})
                for action in (workflow.command_submit, workflow.command_advance, workflow.command_stitch, workflow.command_finalize):
                    with self.subTest(name=name, action=action.__name__), self.assertRaisesRegex(ValueError, '明确质量缺陷'):
                        action(argparse.Namespace(project_dir=root))
                (root / name).unlink()

    def test_confirmed_text_is_persisted_but_suspicion_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);video = root / 'clip.mp4';video.write_bytes(b'fingerprint fixture')
            review = {'reviewer': 'agent_visual', 'text_present': None, 'observations': '模拟检查证据，非实际视频验收。'}
            record_confirmed_text_failure(root, video, review, 'contract')
            require_no_quality_failure(root)
            review['text_present'] = True
            record_confirmed_text_failure(root, video, review, 'contract')
            with self.assertRaises(ValueError):require_no_quality_failure(root)
            self.assertEqual(read_json(root / 'quality-failure.json')['video_sha256'], sha256_file(video))
            self.assertTrue(list((root / 'quality-failures').glob('*.json')))

    def test_reported_704_width_is_rejected_before_scaling(self):
        for width, height, allowed in ((704, 1280, False), (720, 1280, True), (1280, 720, False), (0, 0, False)):
            with patch('generation_gates.technical_summary', return_value={'width': width, 'height': height}):
                if allowed:
                    require_original_dimensions(Path('fixture'), {'resolution': '720p', 'aspect_ratio': '9:16'})
                else:
                    with self.assertRaises(ValueError):
                        require_original_dimensions(Path('fixture'), {'resolution': '720p', 'aspect_ratio': '9:16'})


class FinalOCRRegressions(unittest.TestCase):
    def setUp(self):
        self.fixture = quality_fixture.TextReviewTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()

    def test_final_review_cannot_bypass_missing_or_incomplete_ocr(self):
        f = self.fixture
        for update in ({'text_qc_file': ''}, {'text_qc_sha256': 'stale'}, {'reviewed_frame_indices': [0]}):
            evidence = copy.deepcopy(f.evidence);evidence['text_review'].update(update)
            with self.assertRaises(ValueError):validate_text_review(evidence, f.video)
        e = copy.deepcopy(f.evidence);path = Path(e['text_review']['text_qc_file']);report = read_json(path)
        for update in ({'decoded_frames': 1}, {'status': 'incomplete'}, {'video_sha256': 'stale'}):
            atomic_write_json(path, {**report, **update});e['text_review']['text_qc_sha256'] = sha256_file(path)
            with self.assertRaises(ValueError):validate_text_review(e, f.video)

    def test_single_last_frame_text_requires_individual_review(self):
        f = self.fixture;e = copy.deepcopy(f.evidence);r = e['text_review']
        path = Path(r['text_qc_file']);report = read_json(path)
        report.update(status='text_detected', detections=[{'frame_index': 23, 'text': '结尾字幕'}])
        atomic_write_json(path, report);r['text_qc_sha256'] = sha256_file(path)
        with self.assertRaises(ValueError):validate_text_review(e, f.video)
        r['ocr_findings_review'] = [{'detection_index': 0, 'verdict': 'true_text', 'observations': '测试夹具：确认最后一帧的疑似文字为真实文字。'}]
        with self.assertRaises(ValueError):validate_text_review(e, f.video)
        # Only a specific genuine false positive can pass (fixture is solid red).
        r['ocr_findings_review'][0].update(verdict='false_positive', observations='模拟误检核对：该夹具末帧只有红色，没有字符。')
        validate_text_review(e, f.video)


class StudioFailureRegressions(unittest.TestCase):
    def test_confirmed_caption_stops_next_post_and_requires_separate_revision_approval(self):
        import test_studio as studio_fixture
        f = studio_fixture.StudioTests();f.setUp()
        try:
            s = f.prepare();f.approve(s);s.submit('segment_0001');s.poll('segment_0001')
            _, record = s.job('segment_0001')
            evidence = {'contract_digest': s.contract['digest'], 'video_sha256': record['clip_sha256'],
                'text_review': {'reviewer': 'agent_visual', 'text_present': True,
                                'observations': '仅测试状态机：模拟确认人物下方出现字幕。'}}
            with self.assertRaises(ValueError):s.review('segment_0001', evidence)
            for action in (s.advance, s.assemble, s.deliver, lambda: s.submit('segment_0002')):
                with self.assertRaises(ValueError):action()
            self.assertEqual(len(f.remote.calls), 1)
            revision = {'contract_digest': s.contract['digest'], 'job_id': 'segment_0001',
                'video_sha256': record['clip_sha256'], 'approved_by': 'synthetic_test_runner',
                'reason': '模拟用户另行授权一次重做，旧片原样保留。'}
            with self.assertRaises(ValueError):s.revise('segment_0001', revision)
            f.approve(s)
            s.revise('segment_0001', revision)
            s.submit('segment_0001')
            self.assertEqual(len(f.remote.calls), 2)
            self.assertTrue(list((s.root / 'quality-failures').glob('*.json')))
        finally:f.tearDown()
