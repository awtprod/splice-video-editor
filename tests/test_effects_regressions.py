"""Regressions found validating clip effects:

- a stabilized clip's sound crossfades and dips out exactly as the same clip unstabilized;
- ProRes 4444 with alpha stores the source colour at partial alpha (opacity, soft key edges);
- an export window that starts inside a speed ramp shows the curve's frames;
- the export honours the key's choke, and a mask that is switched off draws nothing.
"""
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
        cls.items = [{'id': 'top', 'name': 'top.mp4', 'src': '/media/top.mp4', 'type': 'video', 'duration': 2},
                     {'id': 'edge', 'name': 'edge.mp4', 'src': '/media/edge.mp4', 'type': 'video', 'duration': 1}]

    def render(self, clip, name):
        out = self.out(name)
        render_to_file({'name': 'a', 'width': W, 'height': H, 'fps': FPS, 'media': self.items, 'clips': [clip]}, out,
                       options={'codec': 'prores', 'profile': '4444', 'alpha': True})
        return out

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
        while stabilizer.jobs[analysis].get('_process') is None and time.monotonic() < deadline:
            time.sleep(.05)
        process = stabilizer.jobs[analysis]['_process']
        self.assertEqual(stabilizer.cancel(analysis)['state'], 'cancelled')
        process.wait(10)
        self.assertIsNotNone(process.poll(), 'the vidstab pass stops')
        deadline = time.monotonic()+10
        while self.leftovers(analysis) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertEqual(self.leftovers(analysis), [])
        self.assertFalse((E.stabilize_store()/analysis/'analysis.json').exists())
        self.assertEqual(stabilizer.status(analysis)['state'], 'cancelled')
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            stabilizer.cancel('../x')
        # Analyzing again starts a new job.
        self.assertEqual(stabilizer.start('/media/long.mp4', 0, 39)['state'], 'running')
        stabilizer.cancel(analysis)

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


if __name__ == '__main__':
    unittest.main()
