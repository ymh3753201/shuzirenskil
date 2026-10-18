import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from planner import plan_segments

SCRIPT = 'Доставка грузов из Китая в Россию. Планируете отправку? Напишите нам, какой груз, его вес и объём, откуда и куда нужна доставка. Обсудим детали вашей перевозки. Свяжитесь с нами сегодня.'


class RussianPlanningTests(unittest.TestCase):
    def test_complete_russian_copy_remains_one_fifteen_second_request(self):
        plan = plan_segments(SCRIPT, 15, duration_mode='exact')
        self.assertEqual(plan['segments'][0]['script'], SCRIPT)
        self.assertEqual(plan['minimum_paid_segment_count'], 1)
        self.assertEqual(plan['planned_request_total_seconds'], 15)
        self.assertEqual(plan['sentence_count'], 5)
        self.assertTrue(plan['segments'][0]['ends_on_complete_sentence'])
        self.assertEqual(plan['timing_assessment']['status'], 'ready')
        self.assertGreater(plan['estimated_speech_seconds'], 12.75)

    def test_incomplete_russian_sentence_still_blocked(self):
        with self.assertRaisesRegex(ValueError, '完整句子'):
            plan_segments('Доставка грузов', 15, duration_mode='exact')

    def test_long_russian_sentence_still_blocked(self):
        with self.assertRaisesRegex(ValueError, '单句超出'):
            plan_segments(('доставка ' * 80) + '.', 15, duration_mode='exact')
