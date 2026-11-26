"""Shared grade contract for the browser monitor and FFmpeg tables."""
import json
import subprocess
import unittest
from pathlib import Path

from color_grade import tables, validate_grade


class ColorGradeTests(unittest.TestCase):
    def test_python_and_browser_tables(self):
        color = {'contrast': 145, 'saturation': 80, 'grade': {
            'pivot': .3, 'gamma': [.15, 0, -.05, .1], 'gain': [1.1, 1, .9, 1],
            'curves': {'master': [[0, 0], [.5, .6], [1, 1]]}}}
        result = subprocess.run(['node', Path(__file__).with_name('color-grade-js.cjs')],
                                input=json.dumps({'op': 'tables', 'color': color}), text=True,
                                capture_output=True, timeout=15, check=True)
        browser = json.loads(result.stdout)
        expected = tables(color)
        for key in ('pre', 'matrix', 'post'):
            def numbers(value):
                if isinstance(value, list):
                    for entry in value:
                        yield from numbers(entry)
                else:
                    yield value
            first, second = list(numbers(browser[key])), list(numbers(expected[key]))
            self.assertEqual(len(first), len(second))
            self.assertLess(max(abs(a-b) for a, b in zip(first, second)), 1e-6)

    def test_grade_boundaries(self):
        for bad in ({'pivot': float('nan')}, {'gain': [1, 1, float('inf'), 0]},
                    {'lift': [1.01, 0, 0, 0]}, {'curves': {'red': [[0, 0]] * 10000}},
                    {'hueSat': [[.2, 1], [.1, 1]]}):
            with self.subTest(bad=list(bad)), self.assertRaises(ValueError):
                validate_grade(bad)
