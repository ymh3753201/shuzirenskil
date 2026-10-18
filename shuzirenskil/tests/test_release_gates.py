"""Regression cases found by the real 45-second candidate audit; no paid calls."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import media
import workflow


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = '售价199元。今天介绍温和清洁。洗脸后及时保湿。白天外出做好防晒。'
        self.project = {'script': self.script, 'route': 'video_generation',
            'project_id': 'synthetic-audit', 'settings': {'subtitle_choice': 'disabled', 'aspect_ratio': '9:16'},
            'state': {'stage': 'clean_review_passed'},
            'plan': {'duration_mode': 'exact', 'target_delivery_seconds': 45,
                     'delivery_max_seconds': 45, 'delivery_duration_tolerance_seconds': 1.35}}
        self.args = argparse.Namespace(project_dir=self.root, evidence_file=self.root/'evidence.json')
        self.transcript = self.root/'transcript.txt'
        self.args.evidence_file.write_text(json.dumps({'speech_transcript_file': str(self.transcript)}))

    def tearDown(self):
        self.temp.cleanup()

    def load_transcript(self, text):
        self.transcript.write_text(text)
        with patch.object(workflow, 'load_project', return_value=self.project):
            return workflow._load_review_evidence('clean', self.args, {})

    def test_missing_sentence_cannot_pass_similarity_gate(self):
        with self.assertRaisesRegex(ValueError, '实际听写与确认稿不一致'):
            self.load_transcript(self.script.replace('白天外出做好防晒。', ''))

    def test_changed_price_is_rejected_even_with_high_similarity(self):
        with self.assertRaises(ValueError):
            self.load_transcript(self.script.replace('199', '1999'))

    def test_punctuation_and_spacing_do_not_change_spoken_text(self):
        _, _, similarity = self.load_transcript(self.script.replace('。', '！\n '))
        self.assertEqual(similarity, 1.0)

    def test_rounding_to_one_cannot_hide_a_word_change(self):
        self.project['script'] = '甲'*30000 + '价格九元'
        with self.assertRaises(ValueError):
            self.load_transcript('甲'*30000 + '价格十元')

    def test_old_review_cannot_bypass_new_policy(self):
        with patch.object(workflow, 'load_project', return_value=self.project), \
             patch.object(workflow, 'read_json', return_value={'status': 'pass'}):
            with self.assertRaisesRegex(ValueError, '旧版检查'):
                workflow.command_finalize(self.args)
        self.assertFalse((self.root/'delivery-manifest.json').exists())

    def test_changed_plan_invalidates_review(self):
        review = {'status': 'pass', 'review_policy_version': '2.0-exact-transcript-full-decode',
                  'review_contract_digest': workflow._review_contract_digest(self.project)}
        self.project['plan']['target_delivery_seconds'] = 42
        with patch.object(workflow, 'load_project', return_value=self.project), \
             patch.object(workflow, 'read_json', return_value=review):
            with self.assertRaisesRegex(ValueError, '时长/设置已变化'):
                workflow.command_finalize(self.args)

    def test_stitch_persists_failed_duration_before_manual_review(self):
        self.project['state']['stage'] = 'clips_downloaded'
        candidate = self.root/'outputs/final.clean.mp4'
        candidate.parent.mkdir()
        candidate.write_bytes(b'synthetic')
        jobs = {'jobs': [{'status': 'downloaded', 'clip_path': str(self.root/'clip.mp4')}]}
        with patch.object(workflow, 'load_project', return_value=self.project), \
             patch.object(workflow, 'read_json', return_value=jobs), \
             patch.object(workflow, 'stitch', return_value={'output': {'duration_seconds': 41.917}}), \
             patch('builtins.print'):
            workflow.command_stitch(self.args)
        report = json.loads((self.root/'stitch-report.json').read_text())
        self.assertFalse(report['duration_checks']['matches_exact_duration_intent'])
        self.assertEqual(self.project['state']['quality_status'], 'duration_failed_manual_review_pending')

    def test_failed_silence_analysis_cannot_be_reported_as_no_silence(self):
        def fail_run(command, **kwargs):
            self.assertTrue(kwargs['check'])
            self.assertIn('-xerror', command)
            raise subprocess.CalledProcessError(1, command)
        with patch.object(media, 'technical_summary', return_value={'duration_seconds': 15}), \
             patch.object(media, 'require_command', return_value='ffmpeg'), \
             patch.object(media.subprocess, 'run', side_effect=fail_run):
            for analysis in (lambda: media.detect_tail_silence(self.root/'clip.mp4'),
                             lambda: media.analyze_speech_pacing(self.root/'clip.mp4', '测试', 260)):
                with self.assertRaises(subprocess.CalledProcessError):
                    analysis()

    def test_entirely_silent_clip_is_rejected_without_trimming(self):
        with patch.object(media, 'technical_summary', return_value={'duration_seconds': 15}), \
             patch.object(media, 'detect_tail_silence', return_value={'tail_silence_seconds': 15, 'last_silence_start_seconds': 0}), \
             patch.object(media, 'run') as command:
            with self.assertRaisesRegex(ValueError, '整段静音'):
                media.trim_tail_silence(self.root/'in.mp4', self.root/'out.mp4', self.root)
            command.assert_not_called()

    def test_full_decode_rejects_error_log_even_with_zero_exit(self):
        with patch.object(media, 'require_command', return_value='ffmpeg'), \
             patch.object(media, 'run', return_value=subprocess.CompletedProcess([], 0, '', 'decode error')):
            with self.assertRaisesRegex(ValueError, '解码发现错误'):
                media.validate_full_decode(self.root/'clip.mp4')

    def test_decode_process_failure_becomes_blocking_validation_error(self):
        with patch.object(media, 'require_command', return_value='ffmpeg'), \
             patch.object(media, 'run', side_effect=subprocess.CalledProcessError(1, ['ffmpeg'])):
            with self.assertRaisesRegex(ValueError, '解码失败'):
                media.validate_full_decode(self.root/'clip.mp4')


if __name__ == '__main__':
    unittest.main()
