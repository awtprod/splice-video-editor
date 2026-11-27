"""Shared grade contract for the browser monitor and FFmpeg tables."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import premiere  # noqa: E402
from color_grade import tables, validate_grade  # noqa: E402
from interchange.cut import build_cut  # noqa: E402
from render_movie import REC709, _picture_grade, render_to_file  # noqa: E402


def _ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1', *map(str, arguments)],
                   check=True, timeout=60)


def _frame_rgb(path, seconds):
    pixel = subprocess.check_output(
        ['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{seconds:.3f}', '-i', str(path),
         '-frames:v', '1', '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
        timeout=30)
    return list(pixel[:3])


def _cube_lut(gain):
    """Build a minimal 2x2x2 LUT dict that scales each channel by ``gain``."""
    data = []
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                data.extend([min(1.0, r * gain[0]), min(1.0, g * gain[1]), min(1.0, b * gain[2])])
    return {'name': 'Scale', 'size': 2, 'domainMin': [0, 0, 0], 'domainMax': [1, 1, 1], 'data': data}


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

    def test_source_range_allowlist(self):
        with tempfile.TemporaryDirectory(prefix='splice-range-allow-') as directory:
            clip = {'id': 'c1', 'start': 0, 'duration': 1, 'color': {'brightness': 110}}
            for allowed in ('tv', 'pc'):
                chain = _picture_grade(
                    clip, {'_sourceRange': allowed, '_sourceProfile': REC709},
                    Path(directory), f'clip-{allowed}', REC709,
                    fps=25, zero=0, frames=25, reverse=False, hdr_master=False)
                self.assertIn(f':range={allowed},', chain)
            with self.assertRaises(ValueError):
                _picture_grade(
                    clip, {'_sourceRange': 'invalid', '_sourceProfile': REC709},
                    Path(directory), 'clip-invalid', REC709,
                    fps=25, zero=0, frames=25, reverse=False, hdr_master=False)

    def test_interchange_reports_color_matrix_override(self):
        project = {
            'name': 'Color matrix note', 'width': 64, 'height': 36, 'fps': 25,
            'media': [{'id': 'm1', 'name': 'clip.mp4', 'src': '/media/clip.mp4',
                       'type': 'video', 'duration': 2, 'colorMatrix': 'bt601'}],
            'clips': [{'id': 'c1', 'mediaId': 'm1', 'type': 'video', 'track': 1,
                       'start': 0, 'duration': 2, 'in': 0, 'color': {}, 'volume': 100,
                       'transition': 'none'}],
        }
        cut = build_cut(project, {'unknownTimecode': 'zero'})
        self.assertTrue(any('color matrix override' in note.lower() for note in cut.report.general),
                        f'Expected colorMatrix override note in {cut.report.general}')


class ColorRenderParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-color-parity-')
        root = Path(cls.temp.name)
        cls.media = root / 'media'
        cls.media.mkdir()
        cls.warm_lut = _cube_lut((1.0, 0.5, 0.25))

        # Generate small HLG, PQ, SDR-TV, and SDR-PC test media (64x36, 25fps, 2s).
        for name, transfer in (('hlg.mov', 'arib-std-b67'), ('pq.mov', 'smpte2084')):
            _ffmpeg(
                '-f', 'lavfi', '-i', 'color=c=#787878:s=64x36:r=25:d=2',
                '-vf', ('format=gbrpf32le,'
                        'setparams=color_primaries=bt709:color_trc=bt709:colorspace=gbr:range=pc,'
                        'zscale=t=linear:p=bt2020,format=gbrpf32le,'
                        f'zscale=t={transfer}:m=bt2020nc:r=limited:npl=203,format=yuv422p10le'),
                '-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le',
                '-color_primaries', 'bt2020', '-color_trc', transfer,
                '-colorspace', 'bt2020nc', '-color_range', 'tv',
                cls.media / name)
        for name, rng, col in (('sdr_tv.mp4', 'tv', '#708090'), ('sdr_pc.mp4', 'pc', '#708090')):
            _ffmpeg(
                '-f', 'lavfi', '-i', f'color=c={col}:s=64x36:r=25:d=2',
                '-vf', f'scale=in_range=full:out_range={rng},format=yuv420p',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-color_primaries', 'bt709', '-color_trc', 'bt709',
                '-colorspace', 'bt709', '-color_range', rng,
                cls.media / name)

        cls.patch = mock.patch.object(premiere, 'MEDIA_ROOT', cls.media.resolve())
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.temp.cleanup()

    def _project(self, media, clips):
        return {'name': 'Color parity', 'width': 64, 'height': 36, 'fps': 25,
                'media': media, 'clips': clips}

    def test_hdr_animated_primaries_in_rec709_export(self):
        cases = (
            ('hlg.mov', {'codec': 'prores', 'colorSpace': 'rec709'}, '.mov'),
            ('pq.mov', {'codec': 'h264'}, '.mp4'),
        )
        for name, opts, ext in cases:
            with self.subTest(source=name, codec=opts['codec']):
                media = [{'id': 'm1', 'name': name, 'src': f'/media/{name}', 'type': 'video', 'duration': 2}]
                clip = {
                    'id': 'c1', 'mediaId': 'm1', 'type': 'video', 'track': 1,
                    'start': 0, 'duration': 2, 'in': 0, 'volume': 100, 'transition': 'none',
                    'color': {'exposure': 0.5},
                    'keyframes': {'exposure': [{'time': 0.0, 'value': -1.0}, {'time': 1.8, 'value': 1.0}]},
                }
                output = Path(self.temp.name) / f'anim_{name}_{opts["codec"]}{ext}'
                render_to_file(self._project(media, [clip]), output, options=opts)
                early = _frame_rgb(output, 0.1)
                late = _frame_rgb(output, 1.7)
                self.assertGreater(late[1] - early[1], 45,
                                   f'{name} ({opts["codec"]}) animated exposure must brighten over time: {early} -> {late}')

    def test_still_exposure_ramp_keeps_transparent_pixels(self):
        name = 'half-transparent.png'
        _ffmpeg('-f', 'lavfi', '-i', 'color=gray:s=64x36,format=rgba',
                '-vf', "geq=r='128':g='128':b='128':a='if(lt(X,W/2),255,0)'", '-frames:v', '1', self.media/name)
        _ffmpeg('-f', 'lavfi', '-i', 'color=red:s=64x36', '-frames:v', '1', self.media/'red.png')
        media = [{'id': f, 'name': f, 'src': f'/media/{f}', 'type': 'video', 'duration': 2} for f in (name, 'red.png')]
        background = {'id': 'bg', 'mediaId': 'red.png', 'type': 'video', 'track': 1, 'in': 0, 'start': 0, 'duration': 1}
        clip = {**background, 'id': 'c', 'mediaId': name, 'track': 2,
                'keyframes': {'exposure': [{'time': 0, 'value': -1}, {'time': .96, 'value': 1}]}}
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                output = Path(self.temp.name)/f'still-{reverse}.mov'
                render_to_file(self._project(media, [background, {**clip, 'reverse': reverse}]), output, options={'codec': 'prores'})
                planes = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-f', 'rawvideo',
                                                  '-pix_fmt', 'rgb24', 'pipe:1'], timeout=30)
                self.assertEqual(len(planes), 25*64*36*3)
                def sample(frame, x):
                    return planes[((frame*36+18)*64+x)*3]
                self.assertGreater(sample(12, 16)-sample(0, 16), 15)
                self.assertGreater(sample(24, 16)-sample(12, 16), 15)
                self.assertGreater(min(sample(frame, 48) for frame in range(25)), 245)

    def test_bypassed_sdr_grade_in_pq_master(self):
        media = [{'id': 'm', 'src': '/media/sdr_tv.mp4', 'type': 'video', 'duration': 2}]
        clip = {'id': 'c', 'type': 'video', 'mediaId': 'm', 'track': 1, 'in': 0, 'start': 0, 'duration': .4}
        options = {'codec': 'prores', 'colorSpace': 'pq'}
        bypass = {'exposure': 1.5, 'lut': self.warm_lut, 'grade': {'bypass': True}}
        reference = None
        for name, color in [('neutral', {}), ('bypass', {'grade': {'bypass': True}}), ('graded-bypass', bypass)]:
            output = Path(self.temp.name) / (name + '.mov')
            render_to_file(self._project(media, [{**clip, 'color': color}]), output, options=options.copy())
            frames = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:v:0',
                                              '-pix_fmt', 'yuv422p10le', '-f', 'rawvideo', '-'], timeout=30)
            self.assertTrue(frames)
            if reference is None:
                reference = frames
            else:
                self.assertEqual(frames, reference)
        for item, color in [(media[0], {'exposure': 1}), ({**media[0], 'inputLut': self.warm_lut}, bypass),
                            ({**media[0], 'colorMatrix': 'bt601'}, bypass)]:
            with self.assertRaisesRegex(ValueError, 'HDR master'):
                render_to_file(self._project([item], [{**clip, 'color': color}]), output, options=options.copy())

    def test_hdr_bypass_preserves_tone_mapping(self):
        for name in ('hlg.mov', 'pq.mov'):
            with self.subTest(source=name):
                media = [{'id': 'm1', 'name': name, 'src': f'/media/{name}', 'type': 'video', 'duration': 2}]
                ungraded = {'id': 'c1', 'mediaId': 'm1', 'type': 'video', 'track': 1,
                            'start': 0, 'duration': 1.2, 'in': 0, 'volume': 100, 'transition': 'none', 'color': {}}
                bypassed = {
                    **ungraded,
                    'color': {'exposure': 1.5, 'brightness': 140, 'lut': self.warm_lut,
                              'grade': {'bypass': True, 'gain': [1.3, 0.7, 0.5, 1]}},
                    'keyframes': {'exposure': [{'time': 0.0, 'value': -1.5}, {'time': 1.0, 'value': 1.5}]},
                }
                out_ungraded = Path(self.temp.name) / f'ungraded_{name}.mov'
                out_bypassed = Path(self.temp.name) / f'bypassed_{name}.mov'
                render_to_file(self._project(media, [ungraded]), out_ungraded,
                               options={'codec': 'prores', 'colorSpace': 'rec709'})
                render_to_file(self._project(media, [bypassed]), out_bypassed,
                               options={'codec': 'prores', 'colorSpace': 'rec709'})
                a = _frame_rgb(out_ungraded, 0.6)
                b = _frame_rgb(out_bypassed, 0.6)
                delta = max(abs(x - y) for x, y in zip(a, b))
                self.assertLessEqual(delta, 2, f'{name} bypassed export must match ungraded tone-map: {a} vs {b}')

    def test_adjustment_clip_input_lut_export(self):
        media = [
            {'id': 'bg', 'name': 'sdr_tv.mp4', 'src': '/media/sdr_tv.mp4', 'type': 'video', 'duration': 2},
            {'id': 'adj', 'name': 'Adjust', 'type': 'video', 'adjustment': True,
             'nativeGenerator': 1414680150, 'inputLut': self.warm_lut, 'duration': 2},
        ]
        base_clip = {'id': 'c1', 'mediaId': 'bg', 'type': 'video', 'track': 1,
                     'start': 0, 'duration': 1.2, 'in': 0, 'volume': 100, 'transition': 'none', 'color': {}}
        adj_clip = {'id': 'c2', 'mediaId': 'adj', 'type': 'video', 'track': 2,
                    'start': 0, 'duration': 1.2, 'in': 0, 'volume': 100, 'transition': 'none',
                    'opacity': 100, 'color': {}}
        out_plain = Path(self.temp.name) / 'adj_plain.mov'
        out_lut = Path(self.temp.name) / 'adj_lut.mov'
        render_to_file(self._project(media, [base_clip]), out_plain, options={'codec': 'prores'})
        render_to_file(self._project(media, [base_clip, adj_clip]), out_lut, options={'codec': 'prores'})
        plain = _frame_rgb(out_plain, 0.6)
        graded = _frame_rgb(out_lut, 0.6)
        self.assertGreater(plain[2] - graded[2], 40,
                           f'Adjustment inputLut must tint blue channel down: {plain} -> {graded}')

    def test_ordinary_tv_and_pc_ranges_and_internal_key_stripping(self):
        for name in ('sdr_tv.mp4', 'sdr_pc.mp4'):
            with self.subTest(source=name):
                # Ordinary media with an extraneous leading-underscore field is sanitized
                # so the probed file metadata governs the FFmpeg graph.
                media = [{'id': 'm1', 'name': name, 'src': f'/media/{name}', 'type': 'video',
                          'duration': 2, '_sourceRange': 'invalid'}]
                clip = {'id': 'c1', 'mediaId': 'm1', 'type': 'video', 'track': 1,
                        'start': 0, 'duration': 1.0, 'in': 0, 'volume': 100, 'transition': 'none',
                        'color': {'brightness': 115}, '_internalFlag': True}
                output = Path(self.temp.name) / f'range_{name}.mp4'
                render_to_file(self._project(media, [clip]), output, options={'codec': 'h264'})
                rgb = _frame_rgb(output, 0.5)
                self.assertGreater(max(rgb), 100, f'{name} graded render produced non-black frame: {rgb}')
