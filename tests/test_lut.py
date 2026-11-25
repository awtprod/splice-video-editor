import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from lut import MAX_SIZE, cube_text
from render_movie import _lut_filter

JS_HELPER = Path(__file__).with_name('lut-js.cjs')


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

    def test_hdr_lut_preserves_rgb_above_reference_white(self):
        lut = {'size': 2, 'data': [value for b in (0,1) for g in (0,1) for r in (0,1) for value in (r,g,b)]}
        samples = [1.58,1.88]*3
        with tempfile.TemporaryDirectory() as folder:
            for low, high in ((0,1),(0,2),(.25,2.25)):
                with self.subTest(low=low,high=high):
                    lut['domainMin'], lut['domainMax'] = [low]*3, [high]*3
                    filters = _lut_filter({'lut':lut,'lutIntensity':50}, Path(folder)/'clip-0.cube', preserve_headroom=True)
                    output = subprocess.check_output([
                        'ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','gbrpf32le',
                        '-video_size','2x1','-i','pipe:0','-filter_complex',f'[0:v]null{filters}[result]',
                        '-map','[result]','-frames:v','1','-pix_fmt','gbrpf32le','-f','rawvideo','pipe:1',
                    ], input=struct.pack('<6f',*samples), timeout=10)
                    for measured, original in zip(struct.unpack('<6f',output), samples):
                        mapped = min(1,max(0,(original-low)/(high-low)))
                        self.assertAlmostEqual(measured, (original+mapped)/2, places=5)


def js(request):
    """Run one request through the browser LUT code (tests/lut-js.cjs) under Node."""
    result = subprocess.run(['node', str(JS_HELPER)], input=json.dumps(request), capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def cube_file(size, sample, header=''):
    rows = [f'LUT_3D_SIZE {size}'+header]
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                rows.append(' '.join(f'{value:.8f}' for value in sample(red/(size-1), green/(size-1), blue/(size-1))))
    return '\n'.join(rows)+'\n'


def warm(red, green, blue):
    """A non-linear grade with cross-talk, so interpolation errors show up."""
    return (min(1, red**.8*1.05+.1*green), green**1.2*.9+.05*blue, max(0, blue*.8+.15*red*green))


COLORS = [(0,0,0),(255,255,255),(255,0,0),(0,255,0),(0,0,255),(128,128,128),(64,32,200),
          (200,180,30),(17,99,250),(250,5,130),(33,66,99),(240,240,16)]


def ffmpeg_pixels(colors, filters):
    pixels = bytes(value for color in colors for value in (*color, 255))
    output = subprocess.check_output([
        'ffmpeg','-nostdin','-v','error','-f','rawvideo','-pixel_format','rgba','-video_size',f'{len(colors)}x1',
        '-i','pipe:0','-filter_complex',f'[0:v]null{filters},format=rgba[result]','-map','[result]',
        '-frames:v','1','-f','rawvideo','pipe:1'], input=pixels, timeout=60)
    return [tuple(output[i*4:i*4+3]) for i in range(len(colors))]


@unittest.skipUnless(shutil.which('node'), 'Node is needed to run the browser LUT code')
class LargeLUTs(unittest.TestCase):
    def test_limits_match_between_browser_and_export(self):
        self.assertEqual(js({'op':'limits'})['limits']['size'], MAX_SIZE)
        self.assertEqual(MAX_SIZE, 65)

    def test_65_point_cube_loads_and_exports_the_same_samples(self):
        text = cube_file(65, warm, '\nTITLE "Warm"\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1')
        self.assertGreater(len(text), 4_000_000, 'larger than the old file limit')
        lut = js({'op':'parse','text':text,'title':'warm65.cube'})['lut']
        self.assertEqual((lut['size'], len(lut['data']), lut['title']), (65, 65**3*3, 'warm65.cube'))
        self.assertEqual(lut['domainMin'], [0,0,0]); self.assertEqual(lut['domainMax'], [1,1,1])
        exported = cube_text(lut, 100).splitlines()[3:]
        original = text.splitlines()[-65**3:]
        self.assertEqual(len(exported), 65**3)
        for index in range(0, 65**3, 997):
            for got, want in zip(exported[index].split(), original[index].split()):
                self.assertAlmostEqual(float(got), float(want), places=7)
        # One axis further is refused on both sides.
        with self.assertRaisesRegex(ValueError, 'shape'):
            cube_text({'size':66,'data':[0]*(66**3*3)})
        self.assertIn('2–65', js({'op':'parse','text':'LUT_3D_SIZE 66\n'})['error'])

    def test_1d_cube_matches_ffmpeg_lut1d(self):
        # Three independent curves; 5 points each is within the 3D limit, so the expansion is exact.
        curve = [(0,0,.1),(.15,.3,.2),(.4,.5,.55),(.7,.9,.8),(1,1,1)]
        text = 'TITLE "curves"\nLUT_1D_SIZE 5\n'+'\n'.join(' '.join(map(str, row)) for row in curve)+'\n'
        lut = js({'op':'parse','text':text})['lut']
        self.assertEqual((lut['size'], len(lut['data'])), (5, 5**3*3))
        with tempfile.TemporaryDirectory() as folder:
            one_d = Path(folder)/'curve.cube'; one_d.write_text(text)
            expected = ffmpeg_pixels(COLORS, f',lut1d=file={one_d}:interp=linear')
            for intensity in (100, 40):
                graded = js({'op':'apply','lut':lut,'intensity':intensity,'pixels':COLORS})['pixels']
                for color, original, want, got in zip(COLORS, COLORS, expected, graded):
                    for channel in range(3):
                        reference = original[channel]+(want[channel]-original[channel])*intensity/100
                        self.assertAlmostEqual(got[channel], reference, delta=1.5, msg=f'{color} channel {channel} at {intensity}%')

    def test_long_1d_curve_is_resampled_to_the_3d_limit(self):
        count = 1024
        rows = '\n'.join(f'{i/(count-1)} {(i/(count-1))**2} {1-i/(count-1)}' for i in range(count))
        lut = js({'op':'parse','text':f'LUT_1D_SIZE {count}\n{rows}\n'})['lut']
        self.assertEqual(lut['size'], 65)
        self.assertAlmostEqual(lut['data'][3*(32+65*(0+65*0))], .5, places=6)       # red axis, mid-point
        self.assertAlmostEqual(lut['data'][3*(0+65*(32+65*0))+1], .25, delta=.002)   # green axis squared curve
        self.assertAlmostEqual(lut['data'][3*(0+65*(0+65*32))+2], .5, places=6)      # blue axis, falling

    def test_browser_preview_matches_ffmpeg_at_full_and_partial_intensity(self):
        text = cube_file(65, warm)
        lut = js({'op':'parse','text':text})['lut']
        with tempfile.TemporaryDirectory() as folder:
            for intensity in (100, 50, 20):
                with self.subTest(intensity=intensity):
                    filters = _lut_filter({'lut':lut,'lutIntensity':intensity}, Path(folder)/'clip-0.cube')
                    expected = ffmpeg_pixels(COLORS, filters)
                    graded = js({'op':'apply','lut':lut,'intensity':intensity,'pixels':COLORS})['pixels']
                    for color, want, got in zip(COLORS, expected, graded):
                        for channel in range(3):
                            self.assertAlmostEqual(got[channel], want[channel], delta=1, msg=f'{color} channel {channel}')

    def test_custom_domain_parses_and_previews_like_the_export(self):
        text = cube_file(17, warm, '\nDOMAIN_MIN 0 0.25 0.125\nDOMAIN_MAX 0.5 0.75 0.875')
        lut = js({'op':'parse','text':text})['lut']
        self.assertEqual((lut['domainMin'], lut['domainMax']), ([0,.25,.125], [.5,.75,.875]))
        with tempfile.TemporaryDirectory() as folder:
            for intensity in (100, 50):
                filters = _lut_filter({'lut':lut,'lutIntensity':intensity}, Path(folder)/'clip-0.cube')
                expected = ffmpeg_pixels(COLORS, filters)
                graded = js({'op':'apply','lut':lut,'intensity':intensity,'pixels':COLORS})['pixels']
                for color, want, got in zip(COLORS, expected, graded):
                    for channel in range(3):
                        self.assertAlmostEqual(got[channel], want[channel], delta=1, msg=f'{color} at {intensity}%')


@unittest.skipUnless(shutil.which('node'), 'Node is needed to run the browser LUT code')
class HostileLUTFiles(unittest.TestCase):
    def rejected(self, text, pattern):
        reply = js({'op':'parse','text':text})
        self.assertIn('error', reply, text[:80])
        self.assertRegex(reply['error'], pattern)

    def test_sizes_are_bounded_before_any_allocation(self):
        for size in ('1', '0', '-3', '66', '1000000', '99999999999999999999', '2.5', 'abc', '0x10', '1e2', ''):
            with self.subTest(size=size):
                self.rejected(f'LUT_3D_SIZE {size}\n', 'points|size')
        self.rejected('LUT_1D_SIZE 65537\n', '2–65536')
        self.rejected('LUT_3D_SIZE 2 3\n', 'size')

    def test_samples_must_be_plain_finite_decimals_in_range(self):
        header = 'LUT_3D_SIZE 2\n'
        rows = ['0 0 0']*7
        for bad in ('NaN 0 0', '0 Infinity 0', '-Infinity 0 0', '1e999 0 0', '0x10 0 0', '0 0 0 0', '0 0', '5 0 0', '-4.1 0 0', 'a b c', '1,5 0 0'):
            with self.subTest(sample=bad):
                self.rejected(header+'\n'.join(rows+[bad]), 'sample')

    def test_sample_counts_must_match_the_size(self):
        self.rejected('LUT_3D_SIZE 2\n'+'0 0 0\n'*7, 'count')
        self.rejected('LUT_3D_SIZE 2\n'+'0 0 0\n'*9, 'too many')
        self.rejected('0 0 0\nLUT_3D_SIZE 2\n', 'size must appear')
        self.rejected('', 'count')
        self.rejected('LUT_1D_SIZE 4\n0 0 0\n1 1 1\n', 'count')
        self.rejected('LUT_3D_SIZE 2\nLUT_1D_SIZE 2\n', 'one LUT')
        self.rejected('LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n'+'0 0 0\n'*8, 'one LUT')

    def test_domains_must_be_finite_and_ordered(self):
        body = 'LUT_3D_SIZE 2\n'+'0 0 0\n'*8
        self.rejected('DOMAIN_MIN 0 0 NaN\n'+body, 'domain')
        self.rejected('DOMAIN_MAX 1 1\n'+body, 'domain')
        self.rejected('DOMAIN_MAX 1000 1 1\n'+body, 'domain')
        self.rejected('DOMAIN_MIN 1 0 0\nDOMAIN_MAX 1 1 1\n'+body, 'no usable range')

    def test_file_and_line_budgets(self):
        self.rejected('#'*16_000_001, 'too large')
        self.rejected('\n'*300_000, 'too many lines')
        self.rejected('LUT_3D_SIZE 2\n'+'0 0 0\n'*300_000, 'too many')

    def test_comments_titles_and_blank_lines_are_ignored(self):
        text = '# made by a test\r\nTITLE "My look"\r\n\r\nLUT_3D_SIZE 2 # two points\r\n'+'0.5 0.5 0.5\r\n'*8
        lut = js({'op':'parse','text':text,'title':'file.cube'})['lut']
        self.assertEqual((lut['size'], lut['title'], set(lut['data'])), (2, 'file.cube', {.5}))

    def test_project_luts_are_validated_the_same_way_as_the_export(self):
        good = {'size':2,'data':[.5]*24}
        self.assertNotIn('error', js({'op':'validate','lut':good,'intensity':100}))
        bad = [({**good,'size':66},'shape'), ({**good,'data':[.5]*23},'shape'), ({**good,'data':[.5]*23+[None]},'sample'),
               ({**good,'data':[.5]*23+[9]},'sample'), ({**good,'domainMin':[1,0,0],'domainMax':[1,1,1]},'domain'),
               ({**good,'domainMin':[0,0]},'domain')]
        for lut, kind in bad:
            with self.subTest(lut=str(lut)[:60]):
                self.assertIn('error', js({'op':'validate','lut':lut,'intensity':100}))
                with self.assertRaises(ValueError):
                    cube_text(lut)
        self.assertIn('error', js({'op':'validate','lut':good,'intensity':101}))
        self.assertIn('error', js({'op':'validate','lut':good,'intensity':-1}))


if __name__ == '__main__':
    unittest.main()
