"""Clip effects: the export renders speed ramps, eased keyframes, crops, masks, chroma keys, blend
modes and stabilization with the same maths as the monitor (effects.js), refuses malformed
effects, keeps stabilization path-confined, and discloses what interchange and Resolve cannot carry.
"""
import array
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_movie  # noqa: E402
import premiere  # noqa: E402
import effects_engine as E  # noqa: E402
from render_movie import render_to_file  # noqa: E402
from interchange.api import export_timeline  # noqa: E402

W, H, FPS = 160, 96, 25


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-threads', '1', *map(str, arguments)], check=True, timeout=180)


def js(*calls):
    result = subprocess.run(['node', str(ROOT/'tests'/'effects-js.cjs')], input=json.dumps({'calls': [list(call) for call in calls]}),
                            text=True, capture_output=True, timeout=60, check=True)
    return [entry.get('value', entry) for entry in json.loads(result.stdout)['results']]


def frames_rgb(path, width=W, height=H, alpha=False):
    """Every frame decoded as a player does (tags honoured), 8-bit RGB(A)."""
    fmt, size = ('gbrap', 4) if alpha else ('gbrp', 3)
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf', f'setparams=range=tv,zscale,format={fmt}',
                          '-f', 'rawvideo', '-pix_fmt', 'rgba' if alpha else 'rgb24', '-'], capture_output=True, check=True, timeout=120).stdout
    step = width*height*size
    return [raw[i:i+step] for i in range(0, len(raw)-step+1, step)], size


def patch_avg(frame, x, y, size=3, width=W, r=2):
    total = [0]*size
    for yy in range(y-r, y+r):
        for xx in range(x-r, x+r):
            for c in range(size):
                total[c] += frame[(yy*width+xx)*size+c]
    return [value/(4*r*r) for value in total]


def pcm(path):
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'],
                         capture_output=True, check=True, timeout=120).stdout
    return array.array('f', raw)


def tone_at(samples, seconds, span=.12):
    start, end = int((seconds-span/2)*48000), int((seconds+span/2)*48000)
    window = samples[start:end]
    crossings = sum(1 for a, b in zip(window, window[1:]) if (a < 0) != (b < 0))
    rms = math.sqrt(sum(v*v for v in window)/max(1, len(window)))
    return crossings/2/span, rms


def frame_color(f):
    # Neighbouring frames differ by 22 codes in red: robust to the codec's rounding.
    return [20+22*(f % 10), 128, 20+22*(f//10)]


class SharedMaths(unittest.TestCase):
    """effects.js and effects_engine.py compute the same numbers (bezier solver to 1e-5)."""

    def curves(self):
        rng = random.Random(7)
        curves = [[{'time': 0, 'value': 0, 'ease': True}, {'time': 1, 'value': 10}, {'time': 2, 'value': 0, 'interp': 'easeOut'}, {'time': 3, 'value': 5}]]
        for _ in range(25):
            points, t = [], 0.0
            for _ in range(rng.randint(2, 6)):
                interp = rng.choice(['linear', 'hold', 'easeIn', 'easeOut', 'easeInOut', 'bezier', None])
                point = {'time': round(t, 4), 'value': round(rng.uniform(0, 400), 3)}
                if interp:
                    point['interp'] = interp
                if interp == 'bezier':
                    point['outHandle'] = [round(rng.random(), 3), round(rng.uniform(-1, 2), 3)]
                if rng.random() < .5:
                    point['inHandle'] = [round(rng.random(), 3), round(rng.uniform(-1, 2), 3)]
                points.append(point)
                t += rng.uniform(.05, 1.5)
            curves.append(points)
        return curves

    def test_keyframe_curves_ramps_and_windows_agree(self):
        worst = 0.0
        for points in self.curves():
            end = points[-1]['time']
            times = [end*i/37-.2 for i in range(41)]
            calls = [('curveValue', points, t, 0) for t in times]
            ramp = [{**p, 'value': max(0.0, p['value'])} for p in points]
            calls += [('rampSource', ramp, t) for t in times]
            calls += [('windowPoints', points, end*.23, end*.81)]
            got = js(*calls)
            expected = [E.curve_value(points, t, 0) for t in times] + [E.ramp_source(E.ordered_points(ramp), t) for t in times]
            for a, b in zip(got[:-1], expected):
                worst = max(worst, abs(a-b))
            window = E.window_points(points, end*.23, end*.81)
            self.assertEqual(len(got[-1]), len(window))
            for a, b in zip(got[-1], window):
                self.assertEqual(set(a), set(b))
                worst = max(worst, abs(a['time']-b['time']), abs(a['value']-b['value']))
            # The window reproduces the curve it was cut from.
            for t in times:
                if 0 <= t <= end*.58:
                    self.assertAlmostEqual(E.curve_value(window, t, 0), E.curve_value(points, t+end*.23, 0), places=6)
        self.assertLess(worst, 1e-5)
        print(f'\nEFFECTS bezier/ramp/window JS vs Python: max difference {worst:.2e}')

    def test_ramps_are_monotonic_and_presets_keep_the_source_span(self):
        for points in self.curves():
            ramp = E.ordered_points([{**p, 'value': abs(p['value'])} for p in points])
            values = [E.ramp_source(ramp, t/50) for t in range(-10, 200)]
            self.assertTrue(all(b >= a-1e-12 for a, b in zip(values, values[1:])), 'source time never goes back')
        for preset in ('smoothSlowMo', 'fastToSlow', 'speedUp', 'freeze'):
            result = js(('presetRamp', preset, 2.5))[0]
            self.assertAlmostEqual(E.ramp_source(E.ordered_points(result['keys']), result['duration']), 2.5, places=6)
            self.assertAlmostEqual(E.ramp_duration(E.ordered_points(result['keys']), 2.5), result['duration'], places=6)

    def test_pixel_maths_agree(self):
        rng = random.Random(3)
        masks = [{'shape': 'rectangle', 'x': 40, 'y': 55, 'width': 50, 'height': 30, 'rotation': 25, 'feather': 6, 'expansion': 2, 'opacity': 80, 'invert': False},
                 {'shape': 'ellipse', 'x': 60, 'y': 45, 'width': 30, 'height': 70, 'rotation': -40, 'feather': 0, 'expansion': -3, 'opacity': 100, 'invert': True},
                 {'shape': 'polygon', 'x': 50, 'y': 50, 'width': 120, 'height': 80, 'rotation': 10, 'feather': 4, 'expansion': 0, 'opacity': 100, 'invert': False,
                  'points': [[-30, -35], [35, -20], [20, 35], [-25, 30], [0, 0]]}]
        points = [(rng.random()*1.2-.1, rng.random()*1.2-.1) for _ in range(120)]
        params = [E.mask_params(m, {}) for m in masks]
        js_params = js(*[('maskParams', m, {}) for m in masks])
        calls = [('masksAlpha', js_params, u, v, 16/9) for u, v in points]
        crop = {'left': 12, 'right': 8, 'top': 20, 'bottom': 5, 'feather': 7}
        calls += [('cropAlpha', js(('cropParams', crop, {}))[0], u, v, 16/9) for u, v in points]
        key = {'color': '#20c040', 'similarity': 12, 'smoothness': 20, 'spill': 70, 'choke': 25, 'feather': 50, 'invert': False}
        k_js = js(('keyParams', key, 'bt709'))[0]
        chroma = [(rng.random(), rng.random()) for _ in range(60)]
        calls += [('keyAlpha', k_js, cb, cr) for cb, cr in chroma] + [('despill', k_js, cb, cr, [0, 0]) for cb, cr in chroma]
        pairs = [(rng.random(), rng.random()) for _ in range(30)]
        calls += [('blendChannel', mode, b, s) for mode in E.BLEND_MODES for b, s in pairs]
        got = js(*calls)
        k = E.key_params(E.validate_effects({'key': key})['key'], 'bt709')
        expected = [E.masks_alpha(params, u, v, 16/9) for u, v in points]
        expected += [E.crop_alpha(E.crop_params(E.validate_effects({'crop': crop})['crop'], {}), u, v, 16/9) for u, v in points]
        expected += [E.key_alpha(k, cb, cr) for cb, cr in chroma] + [list(E.despill(k, cb, cr)) for cb, cr in chroma]
        expected += [E.blend_channel(mode, b, s) for mode in E.BLEND_MODES for b, s in pairs]
        worst = 0.0
        for a, b in zip(got, expected):
            if isinstance(b, list):
                worst = max(worst, *(abs(x-y) for x, y in zip(a, b)))
            else:
                worst = max(worst, abs(a-b))
        self.assertLess(worst, 1e-9)
        self.assertTrue(any(0 < value < 1 for value in expected[:2*len(points)]), 'feathered edges are sampled')

    def test_grade_keyframes_ease(self):
        clip = {'keyframes': {'exposure': [{'time': 0, 'value': -1, 'interp': 'easeIn'}, {'time': 1, 'value': 1}]}}
        kind = E.grade_segment_kind(clip, 0)
        self.assertEqual(kind[0], 'bezier')
        self.assertLess(E.segment_fraction(kind, .5), .4)
        self.assertEqual(js(('gradeSegmentKind', clip, 0))[0], list(kind))


class Validation(unittest.TestCase):
    """Malformed effects are refused, with the same rules in the editor and the export."""

    BAD = [
        {'blend': 'burn'}, {'crop': {'left': float('nan')}}, {'crop': {'left': 70, 'right': 40}}, {'masks': [{}]},
        {'masks': [{'shape': 'star'}]}, {'masks': [{'shape': 'ellipse', 'x': 1e9}]}, {'masks': [{'shape': 'polygon', 'points': [[0, 0]]}]},
        {'masks': [{'shape': 'polygon', 'points': [[0, 0]]*40}]}, {'masks': [{'shape': 'ellipse'}]*9},
        {'key': {'color': "#00ff00':a='1"}}, {'key': {'color': 'green'}}, {'key': {'similarity': -1}}, {'key': {'invert': 'yes'}},
        {'stabilize': {'analysis': '../../etc/passwd'}}, {'stabilize': {'analysis': 'A'*32}}, {'stabilize': {'smoothing': 0}},
        {'stabilize': {'autoZoom': 1.5}}, {'stabilize': {'source': [3, 1]}}, {'speedAudio': 'chipmunk'}, {'glow': 1}, [], 'x',
    ]

    def test_bad_effects_are_refused_everywhere(self):
        for bad in self.BAD:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    E.validate_effects(bad)
        refusals = js(*[('validateEffects', bad) for bad in self.BAD if not (isinstance(bad, dict) and 'crop' in bad and bad['crop'].get('left') != bad['crop'].get('left'))])
        for bad, answer in zip([b for b in self.BAD if not (isinstance(b, dict) and 'crop' in b and b['crop'].get('left') != b['crop'].get('left'))], refusals):
            with self.subTest(js=bad):
                self.assertIsInstance(answer, dict, f'{bad} accepted by effects.js')
                self.assertIn('error', answer)

    def test_bad_keyframes_and_ramps_are_refused(self):
        clip = {'type': 'video', 'start': 0, 'duration': 1, 'in': 0, 'mediaId': 'm', 'track': 1}
        for keys in ({'speed': [{'time': 0, 'value': -5}]}, {'speed': [{'time': 0, 'value': 100, 'interp': 'wobble'}, {'time': 1, 'value': 100}]},
                     {'speed': [{'time': 0, 'value': 100, 'interp': 'bezier', 'outHandle': [2, 0]}, {'time': 1, 'value': 50}]},
                     {'opacity': [{'time': 0, 'value': 10, 'inHandle': [0.5]}, {'time': 1, 'value': 50}]}):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                points = keys.get('speed') or keys['opacity']
                if 'speed' in keys:
                    E.clip_ramp({**clip, 'keyframes': keys})
                else:
                    render_movie._keyframe_expr({**clip, 'keyframes': keys}, 'opacity', 100, 0, 100, 't')
        with self.assertRaisesRegex(ValueError, 'Turn off Reverse'):
            E.clip_ramp({**clip, 'reverse': True, 'keyframes': {'speed': [{'time': 0, 'value': 50}]}})
        multicam = {'name': 'm', 'width': 64, 'height': 36, 'fps': 25, 'media': [
            {'id': 'a', 'src': '/media/a.mp4', 'type': 'video', 'duration': 4},
            {'id': 'mc', 'type': 'video', 'duration': 4, 'multicam': {'angles': [{'mediaId': 'a', 'offset': 0}]}}],
            'clips': [{**clip, 'mediaId': 'mc', 'keyframes': {'speed': [{'time': 0, 'value': 50}]}}]}
        with self.assertRaisesRegex(ValueError, 'multicam'):
            render_movie._expand_editorial_clips(multicam)

    def test_nothing_from_a_project_reaches_a_filtergraph_unformatted(self):
        # Every number is re-formatted by the export; strings are allow-listed (blend, shapes) or hex-checked.
        effects = E.validate_effects({'key': {'color': '#ABCDEF'}, 'masks': [{'shape': 'polygon', 'points': [[1, 2], [3, 4], [5, 6]]}]})
        self.assertEqual(effects['key']['color'], '#abcdef')
        alpha = E.fit_effects({'keyframes': {}}, E.effects_for({'effects': effects}), 'bt709', 't', 25)
        self.assertNotIn("'", alpha.split("a='", 1)[1].rsplit("'", 1)[0])


class EffectRenders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-effects-')
        cls.media = Path(cls.temp.name)/'media'
        cls.media.mkdir()
        cls.patch = mock.patch.object(premiere, 'MEDIA_ROOT', cls.media.resolve())
        cls.patch.start()
        # Nested renders put their intermediates under render_movie's MEDIA_ROOT.
        cls.patch_render = mock.patch.object(render_movie, 'MEDIA_ROOT', cls.media.resolve())
        cls.patch_render.start()
        tags = ['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709']
        def source(name, graph, seconds=4, audio='anullsrc=r=48000:cl=stereo'):
            ffmpeg('-f', 'lavfi', '-i', graph, '-f', 'lavfi', '-i', audio, '-t', str(seconds),
                   '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-qp', '0', '-g', '1', *tags,
                   '-c:a', 'aac', '-shortest', cls.media/name)
        source('bg.mp4', f'color=c=0x3060c0:s={W}x{H}:r={FPS}')
        source('top.mp4', f'color=c=0xc87828:s={W}x{H}:r={FPS}')
        source('green.mp4', f'color=c=0x20c040:s={W}x{H}:r={FPS},drawbox=x=60:y=30:w=40:h=36:c=0xc04060:t=fill,drawbox=x=8:y=8:w=30:h=24:c=0x50c060:t=fill')
        source('frames.mp4', f"color=c=black:s={W}x{H}:r={FPS},format=gbrp,geq=r='20+22*mod(N,10)':g='128':b='20+22*floor(N/10)'", seconds=4)
        source('tone.mp4', f'color=c=gray:s={W}x{H}:r={FPS}', seconds=6, audio='sine=f=1000:r=48000')
        cls.items = {name: {'id': name, 'name': name+'.mp4', 'src': f'/media/{name}.mp4', 'type': 'video', 'duration': 6 if name == 'tone' else 4}
                     for name in ('bg', 'top', 'green', 'frames', 'tone')}

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.patch_render.stop()
        cls.temp.cleanup()

    def project(self, clips, media=None):
        return {'name': 'Effects', 'width': W, 'height': H, 'fps': FPS, 'media': media or list(self.items.values()), 'clips': clips}

    def clip(self, media, track=1, start=0.0, duration=1.0, **extra):
        return {'id': f'{media}-{track}-{start}', 'type': 'video', 'mediaId': media, 'track': track, 'start': start, 'duration': duration,
                'in': extra.pop('in', .2), 'embeddedAudio': False, 'transition': 'none', **extra}

    def render(self, clips, name, **options):
        out = Path(self.temp.name)/name
        render_to_file(self.project(clips), out, options={'codec': 'prores', **options})
        return out

    def test_speed_ramp_samples_exact_source_frames_in_prores_and_h264(self):
        keys = [{'time': 0, 'value': 100, 'interp': 'easeInOut'}, {'time': .4, 'value': 30, 'interp': 'hold'},
                {'time': .8, 'value': 30, 'interp': 'bezier', 'outHandle': [.2, .8]}, {'time': 1.2, 'value': 250, 'inHandle': [.5, 0]},
                {'time': 1.5, 'value': 0, 'interp': 'hold'}, {'time': 1.7, 'value': 100}]
        clip = self.clip('frames', duration=2.0, keyframes={'speed': keys}, **{'in': .4})
        points = E.ordered_points(keys)
        for codec, name in (('prores', 'ramp.mov'), ('h264', 'ramp.mp4')):
            frames, size = frames_rgb(self.render([clip], name, codec=codec))
            self.assertEqual(len(frames), 50)
            for k, frame in enumerate(frames):
                rgb = patch_avg(frame, 80, 48, size)
                shown = min(range(100), key=lambda f: sum((a-b)**2 for a, b in zip(frame_color(f), rgb)))
                expected = math.floor((.4+E.ramp_source(points, k/FPS)+1e-6)*FPS)
                self.assertEqual(shown, expected, f'{codec} frame {k}')
        # A window that starts inside the ramp (long exports render in windows) shows the same frames.
        window = {**clip, 'start': 0.0, 'duration': 1.2, 'in': .4+E.ramp_source(points, .8), '_keyframeShift': .8}
        self.assertAlmostEqual(E.clip_ramp(window).source(.4), E.ramp_source(points, 1.2)-E.ramp_source(points, .8))

    def test_speed_ramp_sound_follows_the_same_curve(self):
        keys = [{'time': 0, 'value': 50, 'interp': 'hold'}, {'time': 1, 'value': 200, 'interp': 'hold'}, {'time': 2, 'value': 0, 'interp': 'hold'}, {'time': 2.5, 'value': 100}]
        base = self.clip('tone', duration=3.0, keyframes={'speed': keys}, **{'in': .2, 'embeddedAudio': True})
        results = {}
        for mode in ('varispeed', 'pitch', 'mute'):
            out = self.render([{**base, 'effects': {'speedAudio': mode}}], f'tone-{mode}.mov')
            samples = pcm(out)
            self.assertAlmostEqual(len(samples)/48000, 3.0, delta=.01)
            results[mode] = [tone_at(samples, t) for t in (.5, 1.5, 2.25, 2.75)]
        vari, pitch, mute = results['varispeed'], results['pitch'], results['mute']
        self.assertAlmostEqual(vari[0][0], 500, delta=25)     # half speed: an octave down
        self.assertAlmostEqual(vari[1][0], 2000, delta=60)    # double speed: an octave up
        self.assertAlmostEqual(pitch[0][0], 1000, delta=25)   # pitch kept
        self.assertAlmostEqual(pitch[1][0], 1000, delta=25)
        for mode in (vari, pitch):
            self.assertLess(mode[2][1], .01, 'a freeze is silent')
            self.assertAlmostEqual(mode[3][0], 1000, delta=25)
        self.assertTrue(all(rms < 1e-4 for _, rms in mute))

    def test_crop_masks_and_a_masked_adjustment_clip(self):
        crop = self.clip('top', track=2, effects={'crop': {'left': 25, 'right': 0, 'top': 0, 'bottom': 30}})
        frames, size = frames_rgb(self.render([self.clip('bg'), crop], 'crop.mov'))
        frame = frames[12]
        self.assertLess(abs(patch_avg(frame, 10, 20, size)[0]-48), 4, 'cropped away: background')
        self.assertLess(abs(patch_avg(frame, 100, 20, size)[0]-200), 4, 'kept: the layer')
        self.assertLess(abs(patch_avg(frame, 100, 85, size)[0]-48), 4, 'cropped bottom: background')
        masked = self.clip('top', track=2, effects={'masks': [{'shape': 'ellipse', 'x': 50, 'y': 50, 'width': 40, 'height': 50},
                                                              {'shape': 'polygon', 'x': 50, 'y': 50, 'width': 100, 'height': 100,
                                                               'points': [[-50, -50], [-20, -50], [-50, -20]]}]})
        frames, size = frames_rgb(self.render([self.clip('bg'), masked], 'mask.mov'))
        frame = frames[12]
        self.assertLess(abs(patch_avg(frame, 80, 48, size)[0]-200), 4, 'inside the ellipse')
        self.assertLess(abs(patch_avg(frame, 150, 48, size)[0]-48), 4, 'outside both')
        self.assertLess(abs(patch_avg(frame, 6, 6, size)[0]-200), 6, 'the triangle reveals the corner (masks add up)')
        inverted = self.clip('top', track=2, effects={'masks': [{'shape': 'ellipse', 'x': 50, 'y': 50, 'width': 40, 'height': 50, 'invert': True}]})
        frame = frames_rgb(self.render([self.clip('bg'), inverted], 'inverted.mov'))[0][12]
        self.assertLess(abs(patch_avg(frame, 80, 48, size)[0]-48), 4, 'an inverted mask hides its inside')
        self.assertLess(abs(patch_avg(frame, 150, 48, size)[0]-200), 4, 'and shows its outside')
        adjustment = {'id': 'adj', 'type': 'video', 'mediaId': 'adjustment', 'track': 2, 'start': 0, 'duration': 1, 'in': 0, 'transition': 'none',
                      'color': {'saturation': 0}, 'effects': {'masks': [{'shape': 'rectangle', 'x': 25, 'y': 50, 'width': 50, 'height': 100}]}}
        media = [*self.items.values(), {'id': 'adjustment', 'type': 'video', 'name': 'Adjustment', 'src': '', 'duration': 3600,
                                        'nativeGenerator': 1414680150, 'adjustment': True}]
        out = Path(self.temp.name)/'window.mov'
        render_to_file(self.project([self.clip('bg'), adjustment], media), out, options={'codec': 'prores'})
        frame = frames_rgb(out)[0][12]
        left, right = patch_avg(frame, 30, 48), patch_avg(frame, 130, 48)
        self.assertLess(max(left)-min(left), 3, 'graded (desaturated) inside the power window')
        self.assertGreater(right[2]-right[0], 100, 'untouched outside it')

    def test_chroma_key_with_spill_and_real_alpha_in_prores_4444(self):
        key = {'key': {'color': '#20c040', 'similarity': 8, 'smoothness': 20, 'spill': 90}}
        out = Path(self.temp.name)/'key4444.mov'
        render_to_file(self.project([self.clip('green', effects=key)]), out, options={'codec': 'prores', 'profile': '4444', 'alpha': True})
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries', 'stream=pix_fmt,codec_tag_string', '-of', 'csv=p=0', str(out)],
                               capture_output=True, text=True, check=True).stdout
        self.assertIn('yuva444p', probe)
        frames, size = frames_rgb(out, alpha=True)
        frame = frames[12]
        self.assertLess(patch_avg(frame, 140, 80, size)[3], 3, 'the green screen is transparent')
        self.assertGreater(patch_avg(frame, 80, 48, size)[3], 252, 'the subject is opaque')
        spill = patch_avg(frame, 22, 20, size)
        self.assertTrue(8 < spill[3] < 250, 'the green-tinted patch is partly keyed')
        plain = frames_rgb(self.render([self.clip('green')], 'green.mov'))[0][12]
        self.assertLess(spill[1]-max(spill[0], spill[2]), patch_avg(plain, 22, 20)[1]-max(patch_avg(plain, 22, 20)[0], patch_avg(plain, 22, 20)[2])-10,
                        'spill suppression pulls green out of the kept edge')

    def test_every_blend_mode_matches_the_shared_formula(self):
        bg = patch_avg(frames_rgb(self.render([self.clip('bg')], 'bg.mov'))[0][12], 80, 48)
        top = patch_avg(frames_rgb(self.render([self.clip('top')], 'top.mov'))[0][12], 80, 48)
        for mode in E.BLEND_MODES:
            with self.subTest(mode=mode):
                layer = self.clip('top', track=2, effects={'blend': mode}, opacity=80)
                got = patch_avg(frames_rgb(self.render([self.clip('bg'), layer], f'blend-{mode}.mov'))[0][12], 80, 48)
                expected = [255*(b/255+(E.blend_channel(mode, b/255, s/255)-b/255)*.8) for b, s in zip(bg, top)]
                self.assertLess(max(abs(a-b) for a, b in zip(got, expected)), 2.5, (mode, got, expected))

    def test_disabled_clips_and_bypassed_effects(self):
        fx = {'blend': 'difference', 'masks': [{'shape': 'ellipse', 'x': 50, 'y': 50, 'width': 20, 'height': 20}]}
        off = self.clip('top', track=2, effects=fx, enabled=False)
        frame = frames_rgb(self.render([self.clip('bg'), off], 'off.mov'))[0][12]
        alone = frames_rgb(self.render([self.clip('bg')], 'alone.mov'))[0][12]
        self.assertLess(max(abs(a-b) for a, b in zip(patch_avg(frame, 80, 48), patch_avg(alone, 80, 48))), 1.5, 'a disabled clip renders nothing')
        bypassed = self.clip('top', track=2, effects={**fx, 'bypass': True})
        frame = frames_rgb(self.render([self.clip('bg'), bypassed], 'bypass.mov'))[0][12]
        self.assertLess(abs(patch_avg(frame, 4, 4)[0]-200), 4, 'bypassed effects leave the clip as it is')

    def test_nested_sequences_apply_outer_effects_to_the_composite(self):
        child = self.project([self.clip('bg', duration=1), self.clip('top', track=2, duration=1, effects={'crop': {'left': 50, 'right': 0, 'top': 0, 'bottom': 0}})])
        nest = {'id': 'nest', 'type': 'video', 'name': 'Nest', 'duration': 1, 'nestedProject': child, 'nestedSequence': 'n1'}
        outer = {'id': 'outer', 'type': 'video', 'mediaId': 'nest', 'track': 1, 'start': 0, 'duration': 1, 'in': 0, 'embeddedAudio': False,
                 'transition': 'none', 'effects': {'masks': [{'shape': 'rectangle', 'x': 50, 'y': 25, 'width': 100, 'height': 50}]}}
        out = Path(self.temp.name)/'nest.mov'
        render_to_file(self.project([outer], [*self.items.values(), nest]), out, options={'codec': 'prores'})
        frame = frames_rgb(out)[0][12]
        self.assertLess(abs(patch_avg(frame, 20, 20)[2]-192), 5, 'inside: the child composite (background half)')
        self.assertLess(abs(patch_avg(frame, 140, 20)[0]-200), 5, 'inside: the child composite (cropped layer half)')
        self.assertLess(max(patch_avg(frame, 80, 80)), 3, 'outside the outer mask: black')

    def test_stabilization_reduces_shake_and_honours_the_trim(self):
        if not E.vidstab_available():
            self.skipTest('FFmpeg without vidstab')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=480x288:r=25:d=6', '-vf',
               "crop=320:192:x='80+36*(random(1)-0.5)':y='48+24*(random(2)-0.5)',format=yuv420p",
               '-c:v', 'libx264', '-qp', '0', '-g', '1', self.media/'shaky.mp4')
        stabilizer = E.Stabilizer()
        status = stabilizer.start('/media/shaky.mp4', 1.0, 4.0)
        deadline = time.monotonic()+120
        while status['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.2)
            status = stabilizer.status(status['analysis'])
        self.assertEqual(status['state'], 'done', status)
        self.assertEqual(status['frames'], 75)
        media = [*self.items.values(), {'id': 'shaky', 'name': 'shaky.mp4', 'src': '/media/shaky.mp4', 'type': 'video', 'duration': 6}]
        base = {'id': 's', 'type': 'video', 'mediaId': 'shaky', 'track': 1, 'start': 0, 'duration': 2.0, 'in': 1.5,
                'embeddedAudio': False, 'transition': 'none'}
        shakes = {}
        for name, effects in (('plain', {}), ('stabilized', {'stabilize': {'analysis': status['analysis'], 'smoothing': 30}}),
                              ('tripod', {'stabilize': {'analysis': status['analysis'], 'tripod': True}})):
            out = Path(self.temp.name)/f'{name}.mov'
            render_to_file({'name': 's', 'width': 320, 'height': 192, 'fps': 25, 'media': media, 'clips': [{**base, 'effects': effects}]}, out,
                           options={'codec': 'prores'})
            shakes[name] = E.shake_metric(out, 0, 2)
        print(f"\nEFFECTS stabilization: shake {shakes['plain']:.2f} -> {shakes['stabilized']:.2f} (smoothed), {shakes['tripod']:.2f} (tripod) px/frame at 160 px")
        self.assertLess(shakes['stabilized'], shakes['plain']*.65)
        self.assertLess(shakes['tripod'], shakes['plain']*.8)
        # The stabilized picture starts at the trimmed In point: frame 0 is source frame 1.5 s.
        stabilized, meta = E.stabilized_media(status['analysis'], {'smoothing': 30, 'tripod': True})
        self.assertAlmostEqual(meta['offset'], 1.0, places=3)
        with self.assertRaisesRegex(ValueError, 'past its analyzed range'):
            render_to_file({'name': 's', 'width': 320, 'height': 192, 'fps': 25, 'media': media,
                            'clips': [{**base, 'in': .2, 'effects': {'stabilize': {'analysis': status['analysis']}}}]},
                           Path(self.temp.name)/'past.mov', options={'codec': 'prores'})
        with mock.patch.object(E, 'vidstab_available', return_value=False), self.assertRaisesRegex(ValueError, 'libvidstab'):
            stabilizer.start('/media/shaky.mp4', 0, 1)

    def test_interchange_discloses_effects(self):
        clips = [self.clip('bg', duration=2.0, keyframes={'speed': [{'time': 0, 'value': 100, 'interp': 'easeIn'}, {'time': 1, 'value': 50}],
                                                           'opacity': [{'time': 0, 'value': 0, 'interp': 'easeInOut'}, {'time': 1, 'value': 100}],
                                                           'mask.0.x': [{'time': 0, 'value': 10}, {'time': 1, 'value': 90}]},
                           effects={'crop': {'left': 10}, 'masks': [{'shape': 'ellipse'}], 'key': {'color': '#00ff00', 'enabled': False},
                                    'blend': 'multiply', 'stabilize': {'analysis': 'a'*32}, 'bypass': True})]
        result = export_timeline(self.project(clips), {'formats': ['fcpxml', 'xmeml', 'edl', 'otio']})
        report = result['report']
        for phrase in ('speed ramp is exported as a constant', 'eased, held or bezier keyframes (opacity)', 'keyframes for "mask.0.x"',
                       'crop (bypassed)', 'masks (bypassed)', 'chroma key (bypassed)', 'multiply blend mode (bypassed)',
                       'stabilization (bypassed)', 'not carried by timeline interchange'):
            self.assertIn(phrase, report)
        self.assertEqual(len(result['files']), 4)   # FCPXML, XMEML, OTIO and the video EDL


@unittest.skipUnless(shutil.which('node'), 'node')
class StabilizeServer(unittest.TestCase):
    """The stabilize routes: only Splice's own window, only media Splice knows, ids not paths."""

    @classmethod
    def setUpClass(cls):
        from test_media_server import ServerProcess
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-stabilize-server-')
        cls.server = ServerProcess(Path(cls.temp.name)/'data')
        media = Path(cls.temp.name)/'data'/'media'/'abc'
        media.mkdir(parents=True, exist_ok=True)
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=160x96:r=25:d=2', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', media/'clip.mp4')

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.temp.cleanup()

    def test_routes(self):
        server = self.server
        status, body = server.json('GET', '/api/effects/capabilities')
        self.assertEqual(status, 200)
        self.assertIn('vidstab', body)
        status, _ = server.json('POST', '/api/effects/stabilize', {'src': '/media/abc/clip.mp4', 'in': 0, 'out': 1}, token=False)
        self.assertEqual(status, 403, 'a request without the page token is refused')
        status, _, _ = server.request('POST', '/api/effects/stabilize', {'src': '/media/abc/clip.mp4', 'in': 0, 'out': 1},
                                      headers={'Origin': 'http://evil.example'})
        self.assertEqual(status, 403, 'another origin is refused')
        status, body = server.json('POST', '/api/effects/stabilize', {'src': '/media/../../../etc/passwd', 'in': 0, 'out': 1})
        self.assertEqual(status, 400, body)
        status, body = server.json('POST', '/api/effects/stabilize', {'src': '/media/abc/clip.mp4', 'in': 1, 'out': 0})
        self.assertEqual(status, 400, body)
        for bad in ('../x', 'A'*32, '0'*31, "0'"*16):
            status, body = server.json('GET', '/api/effects/stabilize?id='+bad)
            self.assertIn(status, (400, 404, 422), (bad, body))
        status, body = server.json('POST', '/api/effects/stabilize/preview', {'id': '../../x', 'settings': {}})
        self.assertEqual(status, 400, body)
        if not body.get('error') or not E.vidstab_available():
            return
        status, body = server.json('POST', '/api/effects/stabilize', {'src': '/media/abc/clip.mp4', 'in': .2, 'out': 1.6})
        self.assertEqual(status, 200, body)
        deadline = time.monotonic()+60
        while body['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.2)
            status, body = server.json('GET', '/api/effects/stabilize?id='+body['analysis'])
        self.assertEqual(body['state'], 'done', body)
        status, preview = server.json('POST', '/api/effects/stabilize/preview', {'id': body['analysis'], 'settings': {'smoothing': 10}})
        self.assertEqual(status, 200, preview)
        status, _, data = server.request('GET', preview['src'])
        self.assertEqual(status, 200)
        self.assertGreater(len(data), 1000)


if __name__ == '__main__':
    unittest.main()
