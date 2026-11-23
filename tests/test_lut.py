import subprocess
import tempfile
import unittest
from pathlib import Path

from lut import cube_text
from render_movie import _lut_filter


class LUTSerialization(unittest.TestCase):
    def test_intensity_blends_with_identity_for_ffmpeg(self):
        lut = {'size': 2, 'data': [1-v for blue in (0,1)
                                   for green in (0,1) for red in (0,1)
                                   for v in (red,green,blue)]}
        lines = cube_text(lut, 50).splitlines()
        self.assertEqual(lines[0], 'LUT_3D_SIZE 2')
        self.assertEqual(lines[3], '0.50000000 0.50000000 0.50000000')
        self.assertEqual(len(lines), 11)

    def test_invalid_project_lut_rejected(self):
        with self.assertRaisesRegex(ValueError, 'domain'):
            cube_text({'size':2,'data':[0]*24,'domainMin':'bad'})
        with self.assertRaisesRegex(ValueError, 'sample'):
            cube_text({'size':2,'data':[0]*23+[float('nan')]})

    def test_export_normalizes_each_input_channel_and_blends_after_lookup(self):
        lut = {'size': 2, 'data': [value for b in (0,1) for g in (0,1) for r in (0,1) for value in (r,g,b)],
               'domainMin': [0,.25,.125], 'domainMax': [.5,.75,.875]}
        source = bytes([64,128,192,128,192,32,224,255])
        with tempfile.TemporaryDirectory() as folder:
            for intensity, expected in ((0, source), (50, [96,128,203,128,224,16,240,255]),
                                        (100, [128,128,214,128,255,0,255,255])):
                with self.subTest(intensity=intensity):
                    filters = _lut_filter({'lut':lut,'lutIntensity':intensity}, Path(folder)/'clip-0.cube')
                    output = subprocess.check_output([
                        'ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','rgba',
                        '-video_size','2x1','-i','pipe:0','-filter_complex',f'[0:v]null{filters},format=rgba[result]',
                        '-map','[result]','-frames:v','1','-f','rawvideo','pipe:1',
                    ], input=source, timeout=10)
                    self.assertEqual(len(output), len(expected))
                    for actual, reference in zip(output, expected):
                        self.assertAlmostEqual(actual, reference, delta=1)


if __name__ == '__main__':
    unittest.main()
