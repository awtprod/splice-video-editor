"""Clip effects on camera RAW clips: a key, masks and a blend mode composite over the RAW-developed
frame (the clip's own rawSettings), a speed ramp samples the right source frames from the RAW
intermediate, and stabilization of a RAW clip is refused clearly everywhere."""
import math
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import media_links  # noqa: E402
import media_proxy  # noqa: E402
import render_movie  # noqa: E402
import effects_engine as E  # noqa: E402
from tests import raw_fixtures  # noqa: E402
from tests.test_camera_raw import _IsolatedDataDirMixin, _extract_rgb_frame  # noqa: E402

W, H, FPS = 256, 96, 24
SETTINGS = {'decodeUsing': 'clip', 'iso': 800, 'exposure': 1.0, 'kelvin': 7200, 'tint': 10, 'colorSpace': 'rec709',
            'gamma': 'rec709', 'highlightRecovery': True, 'debayerQuality': 'full'}
BACKGROUND = (48, 96, 192)


def patch(rgb, col, row):
    """Mean RGB of the interior of chart patch (col 0..2, row 0..1)."""
    x0, x1 = col*W//3+16, (col+1)*W//3-16
    y0, y1 = row*H//2+12, (row+1)*H//2-10
    total, count = [0, 0, 0], 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            for c in range(3):
                total[c] += rgb[(y*W+x)*3+c]
            count += 1
    return [value/count for value in total]


def marker_frame(rgb):
    """The sequence's frame marker (a white 4x4 block at x = 4 x frame on rows 0..3), read at the
    centre of its bright run: +1 EV and full debayer bloom it by a pixel either side."""
    bright = [x for x in range(W-8) if min(rgb[(1*W+x)*3:(1*W+x)*3+3]) > 200]
    if not bright:
        return None
    return round(((bright[0]+bright[-1])/2-1.5)/4)


def near(a, b, tolerance):
    return max(abs(x-y) for x, y in zip(a, b)) <= tolerance


class EffectsOnRaw(_IsolatedDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        first = raw_fixtures.write_cinemadng_sequence(self.tmp/'R001_seq', prefix='R001_', frames=48, fps=FPS, bit_depth=12,
                                                      width=W, height=H, with_wav=True, reel='R001')
        src = media_links.link_many([str(first)], validate=media_proxy.decodable)[0]['src']
        info = media_proxy.describe(src)
        self.assertTrue(info['raw'] and info['rawDecoderAvailable'], info)
        self.raw = {**info, 'id': 'raw', 'name': first.name, 'src': src}
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=0x{:02x}{:02x}{:02x}:s={}x{}:r={}:d=3'.format(*BACKGROUND, W, H, FPS),
                        '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p', '-c:v', 'libx264', '-qp', '0', '-g', '1',
                        '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709', str(self.media_dir/'bg.mp4')],
                       check=True, timeout=120)
        self.bg = {'id': 'bg', 'name': 'bg.mp4', 'src': '/media/bg.mp4', 'type': 'video', 'duration': 3, 'width': W, 'height': H}

    def clip(self, track=1, **extra):
        return {'id': f'c{track}', 'type': 'video', 'track': track, 'mediaId': 'raw', 'start': 0.0, 'in': 0.25, 'duration': .5,
                'transition': 'none', 'embeddedAudio': False, 'rawSettings': SETTINGS, **extra}

    def background(self):
        return {'id': 'b', 'type': 'video', 'track': 1, 'mediaId': 'bg', 'start': 0.0, 'in': 0, 'duration': .5, 'transition': 'none',
                'embeddedAudio': False}

    def render(self, clips, name):
        out = self.tmp/name
        render_movie.render_to_file({'name': name, 'width': W, 'height': H, 'fps': FPS, 'media': [self.raw, self.bg], 'clips': clips},
                                    out, options={'codec': 'prores', 'profile': 'hq', 'limiter': False})
        return _extract_rgb_frame(out, frame_index=3)[2]

    def test_key_masks_and_blend_composite_over_the_developed_frame(self):
        developed = self.render([self.clip()], 'developed.mov')
        camera = self.render([self.clip(rawSettings={'decodeUsing': 'camera', 'debayerQuality': 'full'})], 'camera.mov')
        self.assertFalse(near(patch(developed, 0, 0), patch(camera, 0, 0), 6), 'the clip settings change the developed picture')
        background = patch(self.render([self.background()], 'bg.mov'), 1, 1)
        # A mask over the left column: the developed picture there, the background elsewhere.
        mask = {'shape': 'rectangle', 'x': 100/6, 'y': 50, 'width': 100/3, 'height': 100, 'feather': 0}
        masked = self.render([self.background(), self.clip(track=2, effects={'masks': [mask]})], 'masked.mov')
        for row in (0, 1):
            self.assertTrue(near(patch(masked, 0, row), patch(developed, 0, row), 3), ('mask inside', row))
            self.assertTrue(near(patch(masked, 2, row), background, 3), ('mask outside', row))
        # A key on the developed colour of one patch: that patch shows the background, the rest stays.
        target = patch(developed, 1, 0)
        key = {'color': '#{:02x}{:02x}{:02x}'.format(*(round(v) for v in target)), 'similarity': 6, 'smoothness': 4, 'spill': 0}
        keyed = self.render([self.background(), self.clip(track=2, effects={'key': key})], 'keyed.mov')
        self.assertTrue(near(patch(keyed, 1, 0), background, 6), (patch(keyed, 1, 0), background))
        for col, row in ((0, 0), (2, 0), (0, 1)):
            self.assertTrue(near(patch(keyed, col, row), patch(developed, col, row), 4), ('kept', col, row))
        # Multiply over the background: the shared formula with the developed colours.
        blended = self.render([self.background(), self.clip(track=2, effects={'blend': 'multiply'})], 'blend.mov')
        for col, row in ((0, 0), (1, 0), (2, 1)):
            expected = [255*E.blend_channel('multiply', b/255, s/255) for b, s in zip(background, patch(developed, col, row))]
            self.assertTrue(near(patch(blended, col, row), expected, 3), ('multiply', col, row, patch(blended, col, row), expected))

    def test_a_speed_ramp_samples_the_right_raw_frames(self):
        keys = [{'time': 0, 'value': 100, 'interp': 'easeInOut'}, {'time': .4, 'value': 250}, {'time': .8, 'value': 50}]
        points = E.ordered_points(keys)
        clip = {**self.clip(), 'in': 12/FPS, 'duration': .8, 'keyframes': {'speed': keys}}
        self.assertGreater(12/FPS+E.ramp_source(points, .8), 12/FPS+.8+2/FPS, 'the ramp reads past duration x speed')
        out = self.tmp/'ramp.mov'
        render_movie.render_to_file({'name': 'ramp', 'width': W, 'height': H, 'fps': FPS, 'media': [self.raw], 'clips': [clip]}, out,
                                    options={'codec': 'prores', 'profile': 'hq', 'limiter': False})
        wrong = []
        for k in range(round(.8*FPS)):
            _, _, rgb = _extract_rgb_frame(out, frame_index=k)
            expected = math.floor((12/FPS+E.ramp_source(points, k/FPS))*FPS+1e-6)
            shown = marker_frame(rgb)
            if shown != expected:
                wrong.append((k, shown, expected))
        self.assertEqual(wrong, [], '(output frame, RAW frame shown, RAW frame the curve reads)')

    def test_stabilizing_a_raw_clip_is_refused_clearly(self):
        stabilize = {'stabilize': {'analysis': '0'*32}}
        for effects in (stabilize, {**stabilize, 'bypass': True}):
            with self.assertRaises(ValueError) as caught:
                render_movie.render_to_file({'name': 's', 'width': W, 'height': H, 'fps': FPS, 'media': [self.raw],
                                             'clips': [self.clip(effects=effects)]}, self.tmp/'s.mov', options={'codec': 'prores'})
            self.assertEqual(str(caught.exception), E.STABILIZE_RAW)
        if E.vidstab_available():
            with self.assertRaises(ValueError) as caught:
                E.Stabilizer().start(self.raw['src'], 0, 1)
            self.assertEqual(str(caught.exception), E.STABILIZE_RAW)
        messages = subprocess.run(['node', '-e', "global.window={};(0,eval)(require('fs').readFileSync(process.argv[1],'utf8'));"
                                   "const E=window.SpliceEffects;try{E.validateEffectsSource({type:'video',effects:{stabilize:{}}},{raw:true});"
                                   "console.log('accepted')}catch(e){console.log(e.message)}", str(ROOT/'effects.js')],
                                  capture_output=True, text=True, check=True, timeout=60).stdout.strip()
        self.assertEqual(messages, E.STABILIZE_RAW, 'the editor refuses it with the same words')


if __name__ == '__main__':
    unittest.main()
