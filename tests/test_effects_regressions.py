"""Regressions found validating clip effects:

- a stabilized clip's sound crossfades and dips out exactly as the same clip unstabilized;
- ProRes 4444 with alpha stores the source colour at partial alpha (opacity, soft key edges);
- an export window that starts inside a speed ramp shows the curve's frames;
- the export honours the key's choke, and a mask that is switched off draws nothing;
- every stabilized frame (export and monitor preview) is the source frame the plain export shows
  at that time, on MP4, MOV and MPEG-TS, and the plain export of an MPEG-TS file follows its
  container clock whether or not its input is seeked;
- asking for an analysis again (running or done) answers, and cancel waits for the analysis thread.
"""
import array
import json
import math
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

import premiere  # noqa: E402
import render_movie  # noqa: E402
import effects_engine as E  # noqa: E402
from render_movie import render_to_file  # noqa: E402
from test_effects import ffmpeg, frames_rgb, patch_avg, pcm  # noqa: E402

W, H, FPS = 160, 96, 25
TAGS = ['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709']


def stored_yuva(path, frame=0, width=W, height=H):
    """One frame's stored Y'CbCrA planes (8-bit), as the file holds them."""
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf', f'select=eq(n\\,{frame})', '-frames:v', '1',
                          '-f', 'rawvideo', '-pix_fmt', 'yuva444p', '-'], capture_output=True, check=True, timeout=120).stdout
    n = width*height
    return [raw[plane*n:(plane+1)*n] for plane in range(4)]


def goertzel(samples, frequency, rate=48000):
    w = 2*math.pi*frequency/rate
    c, s1, s2 = 2*math.cos(w), 0.0, 0.0
    for value in samples:
        s1, s2 = value+c*s1-s2, s1
    return math.sqrt(max(0.0, s1*s1+s2*s2-c*s1*s2))*2/len(samples)


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-effects-regressions-')
        cls.media = Path(cls.temp.name)/'media'
        cls.media.mkdir()
        cls.patches = [mock.patch.object(premiere, 'MEDIA_ROOT', cls.media.resolve()),
                       mock.patch.object(render_movie, 'MEDIA_ROOT', cls.media.resolve())]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in cls.patches:
            patch.stop()
        cls.temp.cleanup()

    def out(self, name):
        return Path(self.temp.name)/name


class StabilizedSound(Fixture):
    """The stabilized picture reads other media; the clip's own sound must not lose its transitions."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not E.vidstab_available():
            raise unittest.SkipTest('FFmpeg without vidstab')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=480x288:r=25:d=6', '-f', 'lavfi', '-i', 'sine=f=1000:r=48000:d=6', '-vf',
               "crop=320:192:x='80+36*(random(1)-0.5)':y='48+24*(random(2)-0.5)',format=yuv420p",
               '-c:v', 'libx264', '-qp', '0', '-g', '1', '-c:a', 'pcm_s16le', '-shortest', cls.media/'a.mov')
        ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=320x192:r=25:d=6', '-f', 'lavfi', '-i', 'sine=f=400:r=48000:d=6',
               '-c:v', 'libx264', '-qp', '0', '-g', '1', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-shortest', cls.media/'b.mov')
        stabilizer = E.Stabilizer()
        status = stabilizer.start('/media/a.mov', 0.5, 4.5)
        deadline = time.monotonic()+180
        while status['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.2)
            status = stabilizer.status(status['analysis'])
        if status['state'] != 'done':
            raise AssertionError(status)
        cls.analysis = status['analysis']
        cls.items = [{'id': 'a', 'name': 'a.mov', 'src': '/media/a.mov', 'type': 'video', 'duration': 6},
                     {'id': 'b', 'name': 'b.mov', 'src': '/media/b.mov', 'type': 'video', 'duration': 6}]

    def levels(self, transition, stabilized, times):
        effects = {'stabilize': {'analysis': self.analysis}} if stabilized else {}
        a = {'id': 'A', 'type': 'video', 'mediaId': 'a', 'track': 1, 'start': 0, 'duration': 2, 'in': 1.0, 'transition': 'none',
             'effects': effects}
        b = {'id': 'B', 'type': 'video', 'mediaId': 'b', 'track': 1, 'start': 2, 'duration': 2, 'in': 1.0, 'transition': transition,
             'transitionDuration': 1.0, 'transitionBefore': .5}
        out = self.out(f'{transition}-{stabilized}.mov')
        render_to_file({'name': 'x', 'width': 320, 'height': 192, 'fps': 25, 'media': self.items, 'clips': [a, b]}, out,
                       options={'codec': 'prores'})
        samples = pcm(out)
        return [goertzel(samples[int((t-.02)*48000):int((t+.02)*48000)], 1000) for t in times]

    def check(self, transition, times):
        plain, stabilized = self.levels(transition, False, times), self.levels(transition, True, times)
        for t, a, b in zip(times, plain, stabilized):
            if a < 1e-3:
                self.assertLess(b, 1e-3, f'{transition} at {t}s: the stabilized clip still sounds')
            else:
                self.assertAlmostEqual(20*math.log10(b/a), 0, delta=.5, msg=f'{transition} at {t}s: {b:.4f} vs {a:.4f}')
        return plain

    def test_a_dissolve_crossfades_the_stabilized_clips_sound(self):
        plain = self.check('dissolve', [1.3, 1.6, 1.7, 1.8, 1.9, 1.98, 2.02, 2.1, 2.2, 2.3, 2.4, 2.6])
        self.assertLess(plain[-2], plain[0]*.2, 'the crossfade fades the outgoing sound')

    def test_a_dip_fades_the_stabilized_clips_sound_out(self):
        plain = self.check('fade', [1.3, 1.6, 1.7, 1.8, 1.9, 1.98, 2.02, 2.2])
        self.assertLess(plain[5], plain[0]*.2, 'the dip fades the outgoing sound out before the cut')


class AlphaDelivery(Fixture):
    """ProRes 4444 with alpha stores straight colour: partial alpha never darkens the colour."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ffmpeg('-f', 'lavfi', '-i', f'color=c=0xc87828:s={W}x{H}:r={FPS}', '-t', '2',
               '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-qp', '0', '-g', '1', *TAGS,
               cls.media/'top.mp4')
        # A subject (205,150,120) blending into green screen (40,190,70) across x = 40..120.
        ffmpeg('-f', 'lavfi', '-i', f"color=c=black:s={W}x{H}:r={FPS},format=gbrp,geq="
               "r='205+(40-205)*clip((X-40)/80,0,1)':g='150+(190-150)*clip((X-40)/80,0,1)':b='120+(70-120)*clip((X-40)/80,0,1)'",
               '-t', '1', '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv444p', '-c:v', 'libx264', '-qp', '0', '-g', '1',
               *TAGS, cls.media/'edge.mp4')
        for name, graph in (('gbg', "geq=r='40+X':g='60+Y':b='200-X/2'"), ('gtop', "geq=r='220-X/2':g='90+Y':b='60+X/3'")):
            ffmpeg('-f', 'lavfi', '-i', f'color=c=black:s={W}x{H}:r={FPS},format=gbrp,{graph}', '-t', '1',
                   '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv444p', '-c:v', 'libx264', '-qp', '0', '-g', '1', *TAGS,
                   cls.media/f'{name}.mp4')
        cls.items = [{'id': 'top', 'name': 'top.mp4', 'src': '/media/top.mp4', 'type': 'video', 'duration': 2},
                     {'id': 'edge', 'name': 'edge.mp4', 'src': '/media/edge.mp4', 'type': 'video', 'duration': 1},
                     *({'id': name, 'name': name+'.mp4', 'src': f'/media/{name}.mp4', 'type': 'video', 'duration': 1} for name in ('gbg', 'gtop'))]

    def render(self, clip, name, clips=None, alpha=True):
        out = self.out(name)
        render_to_file({'name': 'a', 'width': W, 'height': H, 'fps': FPS, 'media': self.items, 'clips': clips or [clip]}, out,
                       options={'codec': 'prores', 'profile': '4444', **({'alpha': True} if alpha else {})})
        return out

    def test_blend_modes_keep_their_precision_with_alpha(self):
        # A 4444 alpha delivery composites at 16 bits: a blended layer there must match the same
        # composite delivered without alpha (FFmpeg's alphamerge had rounded it to 8 bits).
        def clip(media, track, **extra):
            return {'id': media, 'type': 'video', 'mediaId': media, 'track': track, 'start': 0, 'duration': .4, 'in': 0,
                    'embeddedAudio': False, 'transition': 'none', **extra}
        for mode in ('multiply', 'screen', 'lighten'):
            clips = [clip('gbg', 1), clip('gtop', 2, effects={'blend': mode})]
            plain, = frames_rgb(self.render(None, f'blend-{mode}.mov', clips, alpha=False))[0][:1]
            alpha, size = frames_rgb(self.render(None, f'blend-{mode}-alpha.mov', clips), alpha=True)
            alpha = alpha[0]
            for c in range(3):
                d = [alpha[i*4+c]-plain[i*3+c] for i in range(W*H)]
                self.assertLess(abs(sum(d)/len(d)), .3, f'{mode}: channel {c} mean difference {sum(d)/len(d):.2f}')
                self.assertLessEqual(max(map(abs, d)), 4, f'{mode}: channel {c}')
            self.assertEqual(min(alpha[3::4]), 255, f'{mode}: the opaque composite stays opaque')

    def test_opacity_alone_keeps_the_source_colour(self):
        for opacity, alpha in ((62, 158), (30, 76)):
            clip = {'id': 'c', 'type': 'video', 'mediaId': 'top', 'track': 1, 'start': 0, 'duration': 1, 'in': 0,
                    'embeddedAudio': False, 'transition': 'none', 'opacity': opacity}
            planes = stored_yuva(self.render(clip, f'opacity{opacity}.mov'), frame=12)
            at = 48*W+80
            stored = [plane[at] for plane in planes]
            for got, want, name in zip(stored, (128, 85, 166, alpha), "Y'CbCrA"):
                self.assertAlmostEqual(got, want, delta=1, msg=f'opacity {opacity}: {name} {got} (stored {stored})')

    def test_a_soft_key_edge_keeps_the_source_colour(self):
        clip = {'id': 'k', 'type': 'video', 'mediaId': 'edge', 'track': 1, 'start': 0, 'duration': .4, 'in': 0, 'embeddedAudio': False,
                'transition': 'none', 'effects': {'key': {'color': '#28be46', 'similarity': 6, 'smoothness': 40, 'spill': 0}}}
        planes = stored_yuva(self.render(clip, 'edge.mov'))
        source = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.media/'edge.mp4'), '-frames:v', '1',
                                 '-f', 'rawvideo', '-pix_fmt', 'yuv444p', '-'], capture_output=True, check=True, timeout=60).stdout
        partial = 0
        for x in range(40, 121, 5):
            at = 48*W+x
            alpha = planes[3][at]
            if not 12 <= alpha <= 243:
                continue
            partial += 1
            for plane in range(3):
                self.assertAlmostEqual(planes[plane][at], source[plane*W*H+at], delta=3,
                                       msg=f'x {x}, alpha {alpha}: plane {plane} {planes[plane][at]} vs source {source[plane*W*H+at]}')
        self.assertGreaterEqual(partial, 4, 'the key has a soft edge')


class SixteenBitDelivery(Fixture):
    """A 16-bit delivery of a clip with a grade and effects keeps 16-bit precision (the matte was
    re-attached with alphamerge, which rounded the graded colour to 8 bits)."""
    def test_grade_and_effects_keep_sixteen_bits(self):
        width, height = 256, 64
        ffmpeg('-f', 'lavfi', '-i', f"nullsrc=s={width}x{height}:r=25,format=yuv444p16le,"
               f"geq=lum='4096+8*mod(X+Y*{width},4096)':cb=32768:cr=32768", '-frames:v', '4', '-c:v', 'ffv1', self.media/'ramp16.mkv')
        item = {'id': 'r', 'name': 'ramp16.mkv', 'src': '/media/ramp16.mkv', 'type': 'video', 'duration': 4/25}
        for name, extra in (('grade', {'color': {'exposure': 0.1}}), ('grade and crop', {'color': {'exposure': 0.1}, 'effects': {'crop': {'left': 1}}})):
            clip = {'id': 'c', 'type': 'video', 'mediaId': 'r', 'track': 1, 'start': 0, 'duration': 4/25, 'in': 0, 'opacity': 75,
                    'transition': 'none', **extra}
            out = self.out(f"{name.replace(' ', '-')}.tif")
            render_to_file({'name': 'd', 'width': width, 'height': height, 'fps': 25, 'media': [item], 'clips': [clip]}, out,
                           options={'codec': 'tiff'})
            frame = sorted(out.glob('*.tif'))[1]      # a TIFF delivery is a folder of frames
            raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(frame), '-vf', 'format=gray16le', '-frames:v', '1',
                                  '-f', 'rawvideo', '-'], capture_output=True, check=True, timeout=60).stdout
            codes = len(set(array.array('H', raw)))
            self.assertGreater(codes, 2000, f'{name}: {codes} distinct levels of a 4096-step ramp')


class WindowsAndKeys(Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ffmpeg('-f', 'lavfi', '-i', f"color=c=black:s={W}x{H}:r={FPS},format=gbrp,geq=r='20+20*mod(N,10)':g='128':b='20+14*floor(N/10)'",
               '-t', '6', '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-qp', '0', '-g', '1',
               *TAGS, cls.media/'frames.mp4')
        ffmpeg('-f', 'lavfi', '-i', f'color=c=0x50b070:s=64x64:r={FPS}', '-t', '1',
               '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv444p', '-c:v', 'libx264', '-qp', '0', '-g', '1', *TAGS,
               cls.media/'near.mp4')
        ffmpeg('-f', 'lavfi', '-i', f'color=c=0xc87828:s={W}x{H}:r={FPS}', '-t', '2',
               '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-qp', '0', '-g', '1', *TAGS,
               cls.media/'top.mp4')
        cls.items = [{'id': 'f', 'name': 'frames.mp4', 'src': '/media/frames.mp4', 'type': 'video', 'duration': 6},
                     {'id': 'near', 'name': 'near.mp4', 'src': '/media/near.mp4', 'type': 'video', 'duration': 1},
                     {'id': 'top', 'name': 'top.mp4', 'src': '/media/top.mp4', 'type': 'video', 'duration': 2}]

    def test_export_windows_that_start_inside_a_ramp_show_the_curves_frames(self):
        keys = [{'time': 0, 'value': 60, 'interp': 'easeIn'}, {'time': .6, 'value': 220, 'interp': 'bezier', 'outHandle': [.3, .9]},
                {'time': 1.2, 'value': 0, 'interp': 'hold'}, {'time': 1.6, 'value': 0, 'interp': 'easeOut'},
                {'time': 2.2, 'value': 140}, {'time': 3.0, 'value': 90}]
        clip = {'id': 'c', 'type': 'video', 'mediaId': 'f', 'track': 1, 'start': 0.4, 'duration': 3, 'in': .3, 'embeddedAudio': False,
                'transition': 'none', 'keyframes': {'speed': keys}}
        filler = {'id': 'z', 'type': 'video', 'mediaId': 'f', 'track': 1, 'start': 0, 'duration': .4, 'in': 0, 'embeddedAudio': False,
                  'transition': 'none'}
        out = self.out('windows.mov')
        # Windows of 1.32 s: the second and third start inside the ramp (at 0.92 s and 2.24 s of the clip).
        with mock.patch.object(render_movie, 'VIDEO_WINDOW_SECONDS', 1.32):
            render_to_file({'name': 'w', 'width': W, 'height': H, 'fps': FPS, 'media': self.items, 'clips': [filler, clip]}, out,
                           options={'codec': 'prores'})
        frames, size = frames_rgb(out)
        points = E.ordered_points(keys)
        color = lambda f: [20+20*(f % 10), 128, 20+14*(f//10)]  # noqa: E731
        wrong = []
        for k in range(10, 85):
            rgb = patch_avg(frames[k], 80, 48, size)
            shown = min(range(150), key=lambda f: sum((a-b)**2 for a, b in zip(color(f), rgb)))
            expected = math.floor((.3+E.ramp_source(points, (k-10)/FPS)+1e-6)*FPS)
            if shown != expected:
                wrong.append((k, shown, expected))
        self.assertEqual(wrong, [], 'frames (output, shown, expected source frame)')

    def test_the_export_honours_the_keys_choke(self):
        raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(self.media/'near.mp4'), '-frames:v', '1',
                              '-f', 'rawvideo', '-pix_fmt', 'yuv444p', '-'], capture_output=True, check=True, timeout=60).stdout
        at = 32*64+32
        cb, cr = raw[64*64+at]/255, raw[2*64*64+at]/255
        alphas = []
        for choke in (0, 30, 60):
            key = {'color': '#20c040', 'similarity': 4, 'smoothness': 40, 'choke': choke}
            reference = E.key_alpha(E.key_params(E.validate_effects({'key': key})['key'], 'bt709'), cb, cr)*255
            out = self.out(f'choke{choke}.mov')
            render_to_file({'name': 'k', 'width': 64, 'height': 64, 'fps': FPS, 'media': self.items,
                            'clips': [{'id': 'x', 'type': 'video', 'mediaId': 'near', 'track': 1, 'start': 0, 'duration': .4, 'in': 0,
                                       'embeddedAudio': False, 'transition': 'none', 'effects': {'key': key}}]},
                           out, options={'codec': 'prores', 'profile': '4444', 'alpha': True})
            alpha = stored_yuva(out, width=64, height=64)[3][at]
            self.assertAlmostEqual(alpha, reference, delta=3, msg=f'choke {choke}')
            alphas.append(alpha)
        self.assertTrue(alphas[0] > alphas[1]+20 > alphas[2]+40, f'choke tightens the matte: {alphas}')

    def test_a_mask_switched_off_draws_nothing(self):
        def render(masks, name):
            clip = {'id': 'm', 'type': 'video', 'mediaId': 'top', 'track': 1, 'start': 0, 'duration': .4, 'in': 0, 'embeddedAudio': False,
                    'transition': 'none', 'effects': {'masks': masks}}
            out = self.out(name)
            render_to_file({'name': 'm', 'width': W, 'height': H, 'fps': FPS, 'media': self.items, 'clips': [clip]}, out,
                           options={'codec': 'prores', 'profile': '4444', 'alpha': True})
            return stored_yuva(out)
        mask = {'shape': 'ellipse', 'x': 50, 'y': 50, 'width': 30, 'height': 30, 'feather': 5}
        on, off = render([mask], 'mask-on.mov'), render([{**mask, 'enabled': False}], 'mask-off.mov')
        corner = 5*W+5
        self.assertLess(on[3][corner], 5, 'an enabled mask hides the corner')
        self.assertGreater(off[3][corner], 250, 'a disabled mask hides nothing')
        self.assertEqual(off[3], bytes([off[3][corner]])*(W*H), 'a disabled mask leaves the whole picture opaque')


class ExportRefusals(Fixture):
    """The export refuses what the editor refuses at load (effects.js MESSAGES), word for word."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ffmpeg('-f', 'lavfi', '-i', 'color=c=gray:s=64x36:r=25:d=4', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', cls.media/'a.mp4')

    def test_refusals_match_the_editor(self):
        source = ROOT/'effects.js'
        messages = subprocess.run(['node', '-e', "global.window={};(0,eval)(require('fs').readFileSync(process.argv[1],'utf8'));"
                                   "process.stdout.write(JSON.stringify(window.SpliceEffects.MESSAGES))", str(source)],
                                  capture_output=True, text=True, check=True, timeout=60).stdout
        import json
        messages = json.loads(messages)
        self.assertEqual(messages['multicamRamp'], E.MULTICAM_RAMP)
        self.assertEqual(messages['stabilizeSource'], E.STABILIZE_SOURCE)
        plain = {'id': 'a', 'name': 'a.mp4', 'src': '/media/a.mp4', 'type': 'video', 'duration': 4}
        multicam = {'id': 'm', 'name': 'M', 'type': 'video', 'duration': 4, 'multicam': {'angles': [{'mediaId': 'a'}]}}
        clip = {'id': 'c', 'type': 'video', 'mediaId': 'a', 'track': 1, 'start': 0, 'duration': 1, 'in': 0, 'transition': 'none'}
        def refused(clips, message):
            with self.assertRaises(ValueError) as caught:
                render_to_file({'name': 'r', 'width': 64, 'height': 36, 'fps': FPS, 'media': [plain, multicam], 'clips': clips},
                               self.out('refused.mov'), options={'codec': 'prores'})
            self.assertEqual(str(caught.exception), message)
        stabilize = {'stabilize': {'analysis': '0'*32}, 'bypass': True}
        refused([{**clip, 'mediaId': 'm', 'effects': stabilize}], E.STABILIZE_SOURCE)
        refused([{**clip, 'mediaId': 'm', 'enabled': False, 'effects': {'stabilize': {'analysis': '0'*32}}}], E.STABILIZE_SOURCE)
        refused([{**clip, 'keyframes': {'mask.8.x': [{'time': 0, 'value': 50}]}}],
                'Invalid keyframes: mask.8.x (a clip has at most 8 masks)')


class StabilizeJobs(Fixture):
    """Analyses stop when cancelled, cancelled or failed renders leave no partial files, and two
    renders of one stabilized clip at once both succeed."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not E.vidstab_available():
            raise unittest.SkipTest('FFmpeg without vidstab')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=25:d=40', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-preset', 'ultrafast',
               '-g', '1', cls.media/'long.mp4')
        ffmpeg('-f', 'lavfi', '-i', 'testsrc2=s=480x288:r=25:d=6', '-vf',
               "crop=320:192:x='80+36*(random(1)-0.5)':y='48+24*(random(2)-0.5)',format=yuv420p",
               '-c:v', 'libx264', '-qp', '0', '-g', '1', cls.media/'shaky.mp4')

    def wait(self, stabilizer, status, seconds=180):
        deadline = time.monotonic()+seconds
        while status['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.1)
            status = stabilizer.status(status['analysis'])
        return status

    def leftovers(self, analysis):
        directory = E.stabilize_store()/analysis
        return sorted(path.name for path in directory.glob('*part*')) if directory.exists() else []

    def test_an_analysis_stops_when_cancelled(self):
        stabilizer = E.Stabilizer()
        status = stabilizer.start('/media/long.mp4', 0, 39)
        analysis = status['analysis']
        deadline = time.monotonic()+60
        while (stabilizer.jobs[analysis].get('_process') is None or not self.leftovers(analysis)) and time.monotonic() < deadline:
            time.sleep(.05)
        process = stabilizer.jobs[analysis]['_process']
        self.assertTrue(self.leftovers(analysis), 'the pass is writing its motion file')
        # cancel() settles only once FFmpeg has exited and the job's files are gone.
        self.assertEqual(stabilizer.cancel(analysis)['state'], 'cancelled')
        self.assertIsNotNone(process.poll(), 'the vidstab pass has exited')
        self.assertFalse(stabilizer.jobs.get(analysis, {}).get('_thread'), 'the job is settled')
        self.assertEqual(self.leftovers(analysis), [])
        self.assertFalse((E.stabilize_store()/analysis/'analysis.json').exists())
        self.assertEqual(stabilizer.status(analysis)['state'], 'cancelled')
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            stabilizer.cancel('../x')
        # Analyzing again starts a new job.
        self.assertEqual(stabilizer.start('/media/long.mp4', 0, 39)['state'], 'running')
        stabilizer.cancel(analysis)

    def test_asking_again_answers_while_running_and_when_done(self):
        import threading
        stabilizer = E.Stabilizer()

        def answers(call, *arguments):
            result = []
            thread = threading.Thread(target=lambda: result.append(call(*arguments)), daemon=True)
            thread.start()
            thread.join(10)
            self.assertFalse(thread.is_alive(), f'{call.__name__} did not answer')
            return result[0]
        status = answers(stabilizer.start, '/media/long.mp4', 0, 39)
        # Cancelled in a thread of its own: a deadlocked service must fail this test, not hang it.
        self.addCleanup(lambda: threading.Thread(target=stabilizer.cancel, args=(status['analysis'],), daemon=True).start())
        self.assertEqual(status['state'], 'running')
        again = answers(stabilizer.start, '/media/long.mp4', 0, 39)
        self.assertEqual((again['state'], again['analysis']), ('running', status['analysis']))
        self.assertEqual(answers(stabilizer.status, status['analysis'])['state'], 'running')
        self.assertEqual(answers(stabilizer.cancel, status['analysis'])['state'], 'cancelled')
        done = self.wait(stabilizer, answers(stabilizer.start, '/media/shaky.mp4', .5, 4.5))
        self.assertEqual(done['state'], 'done', done)
        self.assertEqual(answers(stabilizer.start, '/media/shaky.mp4', .5, 4.5)['state'], 'done')
        # A new service (a restart) finds the finished analysis on disk.
        self.assertEqual(answers(E.Stabilizer().start, '/media/shaky.mp4', .5, 4.5)['state'], 'done')
        self.assertEqual(answers(stabilizer.status, done['analysis'])['state'], 'done')

    def test_cancel_waits_for_the_analysis_thread(self):
        import threading
        released, finished = threading.Event(), threading.Event()

        class Slow(E.Stabilizer):
            def _detect(self, *arguments):
                try:
                    super()._detect(*arguments)
                finally:
                    released.wait(30)       # the thread is still cleaning up
                    finished.set()
        stabilizer = Slow()
        analysis = stabilizer.start('/media/long.mp4', 0, 39)['analysis']
        deadline = time.monotonic()+60
        while stabilizer.jobs[analysis].get('_process') is None and time.monotonic() < deadline:
            time.sleep(.05)
        timer = threading.Timer(.8, released.set)
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(stabilizer.cancel(analysis)['state'], 'cancelled')
        self.assertTrue(finished.is_set(), 'cancel returned before the analysis thread ended')

    def test_renders_leave_no_partial_files_and_may_run_together(self):
        import threading
        stabilizer = E.Stabilizer()
        status = self.wait(stabilizer, stabilizer.start('/media/shaky.mp4', .5, 4.5))
        self.assertEqual(status['state'], 'done', status)
        analysis = status['analysis']
        # A cancelled export removes its partial file: FFmpeg has written part of it when the export
        # is cancelled (the real FFmpeg, stopped, and a stand-in that writes first, then is stopped).
        def cancelled_midway(command, cancelled=None, heartbeat=None):
            Path(command[-1]).write_bytes(b'partial')
            raise InterruptedError('Export cancelled')
        with mock.patch.object(render_movie, '_run_ffmpeg', cancelled_midway), self.assertRaises(InterruptedError):
            E.stabilized_media(analysis, {'smoothing': 25})
        self.assertEqual(self.leftovers(analysis), [])
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            E.stabilized_media(analysis, {'smoothing': 26}, stop)
        self.assertEqual(self.leftovers(analysis), [])
        self.assertFalse(list((E.stabilize_store()/analysis).glob('export-26*')))
        # Inside an export, the partial file lives in the export's own nested folder (named with the
        # job's prefix), so the job's owner cleanup and startup reaping remove it.
        seen = []
        def in_job_folder(command, cancelled=None, heartbeat=None):
            partial = Path(command[-1])
            seen.append(partial.parent.name)
            partial.write_bytes(b'partial')
            raise InterruptedError('Export cancelled')
        project = {'name': 's', 'width': 320, 'height': 192, 'fps': 25,
                   'media': [{'id': 'shaky', 'name': 'shaky.mp4', 'src': '/media/shaky.mp4', 'type': 'video', 'duration': 6}],
                   'clips': [{'id': 's', 'type': 'video', 'mediaId': 'shaky', 'track': 1, 'start': 0, 'duration': 2.0, 'in': 1.0,
                              'embeddedAudio': False, 'transition': 'none', 'effects': {'stabilize': {'analysis': analysis, 'smoothing': 27}}}]}
        with mock.patch.object(render_movie, '_run_ffmpeg', in_job_folder), self.assertRaises(InterruptedError):
            render_to_file(project, self.out('stopped.mov'), options={'codec': 'prores'}, nested_prefix='splice-nested-testjob-')
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith('splice-nested-testjob-'), seen)
        self.assertEqual([path.name for path in self.media.iterdir() if path.name.startswith('splice-nested-')], [])
        self.assertEqual(self.leftovers(analysis), [])
        # Two exports of the same clip at once each write their own partial file; both succeed.
        results, errors = [], []
        def export():
            try:
                results.append(E.stabilized_media(analysis, {'smoothing': 40})[0])
            except Exception as error:   # collected for the assertion below
                errors.append(error)
        threads = [threading.Thread(target=export) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(240)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertTrue(results[0].exists())
        self.assertEqual(self.leftovers(analysis), [])
        # The monitor preview is a job: the call returns at once.
        started = time.monotonic()
        first = stabilizer.preview(analysis, {'smoothing': 12})
        self.assertLess(time.monotonic()-started, 2)
        self.assertEqual(first['state'], 'running')
        deadline = time.monotonic()+120
        result = first
        while result['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.1)
            result = stabilizer.preview(analysis, {'smoothing': 12})
        self.assertEqual(result['state'], 'done', result)
        self.assertTrue(result['src'].startswith('/media/'))
        self.assertEqual(self.leftovers(analysis), [])


# Frame-coded source: frame N is a flat colour (red steps 20 a frame, blue 12 every ten frames)
# under a white grid and a yellow box, cropped with random jitter (shaky).
CODE_SIZE = (320, 192)
CODE_FPS = 30


def frame_code(r, b):
    return round((r-20)/20) % 10+10*round((b-20)/12)


def frame_codes(path):
    """(code, distance from the code's nominal colour) of every frame: the median red and blue of
    the pixels showing the flat colour (green near 128: not grid, box or black border)."""
    width, height = CODE_SIZE
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf',
                          f'scale={width}:{height},setparams=range=tv:colorspace=bt709:color_trc=bt709:color_primaries=bt709,'
                          'zscale,format=gbrp', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True, timeout=180).stdout
    size = width*height*3
    out = []
    for i in range(0, len(raw)-size+1, size):
        frame = raw[i:i+size]
        flat = [(frame[j], frame[j+2]) for j in range(0, size, 3*5) if 110 < frame[j+1] < 146]
        if not flat:
            out.append((-1, 999))
            continue
        r = sorted(v[0] for v in flat)[len(flat)//2]
        b = sorted(v[1] for v in flat)[len(flat)//2]
        code = frame_code(r, b)
        out.append((code, max(abs(r-(20+20*(code % 10))), abs(b-(20+12*(code//10))))))
    return out


def coded_source(path, extra=()):
    """Six seconds, shaky, long-GOP H.264 (one keyframe a second, B-frames) with AAC sound."""
    width, height = CODE_SIZE
    ffmpeg('-f', 'lavfi', '-i', f"color=c=black:s={width+160}x{height+96}:r={CODE_FPS},format=gbrp,"
           "geq=r='20+20*mod(N,10)':g='128':b='20+12*floor(N/10)'",
           '-f', 'lavfi', '-i', 'sine=f=440:r=48000', '-t', '6',
           '-vf', f"drawgrid=w=24:h=24:t=2:c=white@1,drawbox=x=100:y=60:w=40:h=40:c=yellow:t=fill,"
           f"crop={width}:{height}:x='80+36*(random(1)-0.5)':y='48+24*(random(2)-0.5)',"
           "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           '-c:v', 'libx264', '-crf', '12', '-g', '30', '-bf', '2', '-sc_threshold', '0', '-colorspace', 'bt709',
           '-c:a', 'aac', '-b:a', '128k', *extra, path)


def frame_times(path):
    """Container start and every video frame's presentation time (file timestamps)."""
    data = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                      'frame=pts_time:format=start_time', '-of', 'json', str(path)],
                                     capture_output=True, text=True, check=True, timeout=120).stdout)
    return float(data['format']['start_time']), sorted(float(f['pts_time']) for f in data['frames'])


class StabilizedFrames(Fixture):
    """vidstab after a trim filter that drops leading frames mixed other source times into the
    result (whole wrong frames, double exposures); the stabilized clip must show, frame for frame,
    what the plain clip shows."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not E.vidstab_available():
            raise unittest.SkipTest('FFmpeg without vidstab')
        for name, extra in (('shaky.mp4', ()), ('shaky.mov', ()), ('shaky.ts', ('-f', 'mpegts'))):
            coded_source(cls.media/name, extra)

    def analyze(self, name, low, high):
        stabilizer = E.Stabilizer()
        status = stabilizer.start(f'/media/{name}', low, high)
        deadline = time.monotonic()+180
        while status['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.1)
            status = stabilizer.status(status['analysis'])
        self.assertEqual(status['state'], 'done', status)
        return status

    def test_every_stabilized_frame_is_the_next_source_frame(self):
        status = self.analyze('shaky.mp4', 0.5, 5.5)
        self.assertEqual(status['frames'], 150)
        settings = E._transform_settings({'smoothing': 15})
        for preview, name in ((False, 'frames.mov'), (True, 'frames.mp4')):
            target = self.out(name)
            E.render_stabilized(status['analysis'], settings, target, preview=preview)
            codes = frame_codes(target)
            label = 'preview' if preview else 'export'
            self.assertEqual([code for code, _ in codes], list(range(15, 165)), f'{label}: frames out of order')
            worst = max(distance for _, distance in codes)
            self.assertLessEqual(worst, 8, f'{label}: a frame is a mix of two source frames ({worst} codes off)')

    def test_the_stabilized_clip_shows_the_plain_clips_frames(self):
        width, height = CODE_SIZE
        for name in ('shaky.mp4', 'shaky.mov', 'shaky.ts'):
            status = self.analyze(name, 0.3, 5.0)
            start, times = frame_times(self.media/name)
            item = {'id': 'm', 'name': name, 'src': f'/media/{name}', 'type': 'video', 'duration': 6}
            # 0.3 s: an MPEG-TS input is not seeked; 1.033 s: 0.3 ms before a frame boundary.
            for source_in in (0.3, 1.033):
                seen = {}
                for label, effects in (('plain', {}), ('stabilized', {'stabilize': {'analysis': status['analysis'], 'smoothing': 15}})):
                    clip = {'id': 'A', 'type': 'video', 'mediaId': 'm', 'track': 1, 'start': 0, 'duration': 1.6, 'in': source_in,
                            'transition': 'none', 'effects': effects}
                    out = self.out(f'{name}-{source_in}-{label}.mov')
                    render_to_file({'name': 's', 'width': width, 'height': height, 'fps': 25, 'media': [item], 'clips': [clip]}, out,
                                   options={'codec': 'prores'})
                    seen[label] = [code for code, _ in frame_codes(out)]
                # The plain clip shows the frame on screen at each source time, on the container's clock.
                expected = [max(k for k, t in enumerate(times) if t <= start+source_in+j/25+1e-6) for j in range(40)]
                self.assertEqual(seen['plain'], expected, f'{name} in {source_in}: plain export')
                self.assertEqual(seen['stabilized'], seen['plain'], f'{name} in {source_in}: stabilized export')



def long_source(path, seconds, codec, extra=(), timing=''):
    """Like coded_source, ``seconds`` long without sound, with any codec settings; the frame code
    counts modulo 200. ``timing``: filters that retime the frames (variable frame rate)."""
    width, height = CODE_SIZE
    ffmpeg('-f', 'lavfi', '-i', f"color=c=black:s={width+160}x{height+96}:r={CODE_FPS},format=gbrp,"
           "geq=r='20+20*mod(N,10)':g='128':b='20+12*floor(mod(N,200)/10)'", '-t', str(seconds),
           '-vf', f"drawgrid=w=24:h=24:t=2:c=white@1,drawbox=x=100:y=60:w=40:h=40:c=yellow:t=fill,"
           f"crop={width}:{height}:x='80+36*(random(1)-0.5)':y='48+24*(random(2)-0.5)'{timing},"
           "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
           *codec, '-colorspace', 'bt709', '-an', *extra, path)


class StabilizedTiming(Fixture):
    """A phone's variable frame rate (a stabilized clip was one frame off: its intermediate was
    retimed to a constant rate) and an MPEG-TS whose GOP is longer than the seek margin (the
    analysis, the stabilized picture and the plain export all started up to a GOP late)."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not E.vidstab_available():
            raise unittest.SkipTest('FFmpeg without vidstab')
        # +-6 ms jitter, a frame dropped every 97, 90 kHz, B-frames.
        long_source(cls.media/'phone.mp4', 7, ['-c:v', 'libx264', '-crf', '12', '-x264-params', 'keyint=60:bframes=2',
                                                 '-fps_mode', 'passthrough', '-enc_time_base', '1:90000', '-video_track_timescale', '90000'],
                    timing=",settb=1/90000,setpts='(N/30+0.006*sin(N*2.3)+floor(N/97)/30)/TB'")
        # x264's default GOP (250 frames, 8.3 s) and a container start of 31.38 s.
        long_source(cls.media/'long.ts', 12, ['-c:v', 'libx264', '-crf', '12', '-x264-params',
                                              'keyint=250:min-keyint=250:bframes=3:scenecut=0'], ('-f', 'mpegts', '-output_ts_offset', '30'))

    def analyze(self, name, low, high):
        return StabilizedFrames.analyze(self, name, low, high)

    def check(self, name, analysis, start, times, in_points, plain_only=()):
        """Stabilized and plain exports at each in point show the frame on screen at each source time
        (container clock); ``plain_only`` in points are checked without stabilization."""
        width, height = CODE_SIZE
        on_screen = lambda x: max(k for k, t in enumerate(times) if t <= start+x+1e-6) % 200  # noqa: E731
        item = {'id': 'm', 'name': name, 'src': f'/media/{name}', 'type': 'video', 'duration': 12}
        for source_in in (*in_points, *plain_only):
            kinds = (('plain', {}),) if source_in in plain_only else (
                ('plain', {}), ('stabilized', {'stabilize': {'analysis': analysis, 'smoothing': 15}}))
            expected = [on_screen(source_in+j/25) for j in range(30)]
            for label, effects in kinds:
                clip = {'id': 'A', 'type': 'video', 'mediaId': 'm', 'track': 1, 'start': 0, 'duration': 1.2, 'in': source_in,
                        'transition': 'none', 'effects': effects}
                out = self.out(f'{name}-{source_in}-{label}.mov')
                render_to_file({'name': 's', 'width': width, 'height': height, 'fps': 25, 'media': [item], 'clips': [clip]}, out,
                               options={'codec': 'prores'})
                self.assertEqual([code for code, _ in frame_codes(out)], expected, f'{name} in {source_in}: {label} export')

    def check_preview(self, name, analysis, start, times, source_in):
        """The monitor's stabilized proxy: every analyzed frame, at its own time from the first."""
        status_first = max(k for k, t in enumerate(times) if t <= start+source_in+1e-6)
        preview = self.out(f'{name}-preview.mp4')
        E.render_stabilized(analysis, E._transform_settings({'smoothing': 15}), preview, preview=True)
        codes = [code for code, _ in frame_codes(preview)]
        self.assertEqual(codes, [k % 200 for k in range(status_first, status_first+len(codes))], f'{name}: preview frames')
        _, shown = frame_times(preview)
        worst = max(abs(t-(times[status_first+i]-times[status_first])) for i, t in enumerate(shown))
        self.assertLess(worst, .0005, f'{name}: a preview frame is {worst*1000:.2f} ms from its source time')
        return len(codes)

    def test_a_variable_frame_rate_clip_keeps_every_frame_time(self):
        start, times = frame_times(self.media/'phone.mp4')
        status = self.analyze('phone.mp4', 1.1, 5.6)
        self.assertEqual(self.check_preview('phone.mp4', status['analysis'], start, times, 1.1), status['frames'])
        # 3.0337 s: just after a jittered frame boundary.
        self.check('phone.mp4', status['analysis'], start, times, (1.1, 3.0337, 4.2))

    def test_a_long_gop_transport_stream_stabilizes_the_range_asked_for(self):
        start, times = frame_times(self.media/'long.ts')
        self.assertGreater(start, 30, 'the stream starts at its offset')
        status = self.analyze('long.ts', 3.1, 10.6)
        first = max(k for k, t in enumerate(times) if t <= start+3.1+1e-6)
        last = max(k for k, t in enumerate(times) if t < start+10.6-1e-6)
        self.assertEqual(status['frames'], last-first+1, 'every frame of the range is analyzed')
        self.assertAlmostEqual(status['offset'], times[first]-start, places=4)
        self.check_preview('long.ts', status['analysis'], start, times, 3.1)
        # The plain export of the same file follows its container clock too (it showed one frame
        # held for the whole clip, or black).
        self.check('long.ts', status['analysis'], start, times, (3.1, 7.77), plain_only=(1.2, 5.0, 9.0))


if __name__ == '__main__':
    unittest.main()
