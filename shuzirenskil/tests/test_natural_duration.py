"""Reference duration must not change pacing, text, or request allocation."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from planner import plan_segments
from capability_planner import plan_narration
from model_registry import get_profile
from workflow import build_parser, _duration_review_checks


class NaturalDurationTests(unittest.TestCase):
    script = '今天一起了解自然护肤。洗脸后及时做好保湿。白天外出注意防晒。'

    def test_reference_targets_neither_pad_nor_compress_legacy_speech(self):
        baseline = plan_segments(self.script, duration_mode='content-fit', speech_rate_cpm=260)
        for target in (1, 45, 180):
            plan = plan_segments(self.script, target, duration_mode='reference', speech_rate_cpm=260)
            self.assertEqual(plan['segments'], baseline['segments'])
            self.assertEqual(plan['speech_rate_cpm'], 260)
            self.assertEqual(plan['reference_duration_seconds'], target)
            self.assertEqual(plan['timing_assessment']['status'], 'ready')

    def test_reference_does_not_change_multi_model_segments(self):
        profile = get_profile('minimax-h3-reference')
        baseline = plan_narration(self.script, profile, speech_rate_cpm=260)
        for target in (1, 45, 180):
            plan = plan_narration(self.script, profile, target=target, mode='reference', speech_rate_cpm=260)
            self.assertEqual(plan['segments'], baseline['segments'])
            self.assertEqual(plan['target_seconds'], target)

    def test_short_complete_candidate_passes_reference_duration(self):
        plan = {'duration_mode':'reference','target_delivery_seconds':45,
                'delivery_max_seconds':45,'planned_request_total_seconds':45}
        self.assertTrue(all(_duration_review_checks(41.917, plan, 'video_generation').values()))
        plan.update(duration_mode='exact', delivery_duration_tolerance_seconds=1.35)
        self.assertFalse(all(_duration_review_checks(41.917, plan, 'video_generation').values()))

    def test_longer_content_passes_reference_but_not_explicit_maximum(self):
        plan = {'duration_mode':'reference','target_delivery_seconds':15,
                'delivery_max_seconds':15,'planned_request_total_seconds':20}
        self.assertTrue(all(_duration_review_checks(20.042, plan, 'video_generation').values()))
        plan['duration_mode'] = 'content-fit'
        self.assertFalse(all(_duration_review_checks(20.042, plan, 'video_generation').values()))

    def test_new_cli_defaults_are_reference(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(['plan','--script-file','draft.txt','--duration','45']).duration_mode, 'reference')
        self.assertEqual(parser.parse_args(['prepare','--name','test','--script-file','draft.txt','--duration','45']).duration_mode, 'reference')

    def test_existing_video_without_target_uses_its_natural_duration(self):
        plan = {'duration_mode':'reference','target_delivery_seconds':None,'delivery_max_seconds':None}
        self.assertTrue(all(_duration_review_checks(41.917, plan, 'postproduction_only').values()))

    def test_invalid_reference_duration_still_rejected(self):
        for target in (0, -1, True):
            with self.assertRaises(ValueError):
                plan_segments(self.script, target, duration_mode='reference')
            with self.assertRaises(ValueError):
                plan_narration(self.script, get_profile('wan3-reference'), target=target, mode='reference')
