import unittest

from lut import cube_text


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


if __name__ == '__main__':
    unittest.main()
