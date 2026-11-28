"""Clip enable state in exports (round 2, finding 6).

Contract: a clip with ``enabled`` False contributes no picture and no sound (its
embedded sound included); absent or True means enabled; a linked partner is a
separate clip with its own flag. A transition next to a disabled clip still
happens, against nothing: a dissolve or wipe to or from a disabled clip mixes
the enabled clip with the layers beneath it (black on the lowest track), and a
dip still dips the enabled side to or from black. Checked on decoded pixels and
PCM.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_render_audio_mix import AMPLITUDE, left_channel, tone_level  # noqa: E402

W, H, FPS = 64, 36, 25


def ffmpeg(*arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def frames_rgb(movie):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie), '-pix_fmt', 'rgb24',
                                   '-f', 'rawvideo', 'pipe:1'], timeout=120)
    size = W*H*3
    return [raw[start:start+size] for start in range(0, len(raw)-size+1, size)]


def mean_rgb(frame, x0=0, x1=W):
    pixels = [frame[(y*W+x)*3:(y*W+x)*3+3] for y in range(4, H-4) for x in range(x0+2, x1-2)]
    return tuple(round(sum(pixel[channel] for pixel in pixels)/len(pixels)) for channel in range(3))


class EnabledCase(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        # Camera files: a solid colour picture with a steady tone as embedded sound.
        self.media = [dict(id=name, src=self.camera(name, color, frequency))
                      for name, color, frequency in (('red', 'red', 375), ('green', 'lime', 625), ('blue', 'blue', 875))]

    def camera(self, name, color, frequency):
        path = self.directory/f'{name}.mov'
        sound = f"aevalsrc='{AMPLITUDE}*sin(2*PI*{frequency}*t)|{AMPLITUDE}*sin(2*PI*{frequency}*t)':s=48000:d=20"
        ffmpeg('-f', 'lavfi', '-i', f'color={color}:s={W}x{H}:r={FPS}:d=20', '-f', 'lavfi', '-i', sound,
               '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', '-c:a', 'pcm_f32le', '-shortest', str(path))
        return '/media/' + str(path.relative_to(MEDIA_ROOT))

    def render(self, clips, name, codec='prores', **options):
        output = self.directory/(name + ('.wav' if codec == 'wav' else '.mov'))
        render_to_file(dict(width=W, height=H, fps=FPS, media=self.media, clips=clips), output,
                       options={'codec': codec, 'limiter': False, **options})
        return output

    def video(self, media, start, duration, track=1, **extra):
        return dict(type='video', mediaId=media, track=track, start=start, duration=duration, **{'in': 1}, **extra)

    def assertSamePicture(self, movie, reference):
        shown, wanted = frames_rgb(movie), frames_rgb(reference)
        self.assertEqual(len(shown), len(wanted))
        wrong = [number for number, (a, b) in enumerate(zip(shown, wanted)) if a != b]
        self.assertEqual(wrong, [], 'frames that differ from the timeline without the disabled clip')


class DisabledClips(EnabledCase):
    def test_disabled_v2_clip_over_v1_shows_v1_and_is_silent(self):
        clips = [self.video('red', 0, 2), self.video('green', .4, 1, track=2, enabled=False)]
        movie = self.render(clips, 'disabled')
        self.assertSamePicture(movie, self.render(clips[:1], 'reference'))
        sound = left_channel(movie)
        self.assertLess(tone_level(sound, 625, .4, 1.4), .002, 'the disabled clip is heard')
        self.assertAlmostEqual(tone_level(sound, 375, .4, 1.4), AMPLITUDE, delta=.005)
        # Absent or True is enabled.
        shown = frames_rgb(self.render([clips[0], dict(clips[1], enabled=True)], 'enabled'))
        self.assertGreater(mean_rgb(shown[25])[1], 200)

    def test_disabled_audio_clip_is_silent(self):
        clips = [dict(type='audio', mediaId='red', track=1, start=0, duration=2, **{'in': 1}),
                 dict(type='audio', mediaId='green', track=2, start=0, duration=2, enabled=False, **{'in': 1})]
        sound = left_channel(self.render(clips, 'audio', codec='wav'))
        self.assertLess(tone_level(sound, 625, 0, 2), .002)
        self.assertAlmostEqual(tone_level(sound, 375, 0, 2), AMPLITUDE, delta=.005)

    def test_disabled_picture_with_embedded_sound_next_to_its_enabled_linked_sound(self):
        # V1: the green camera clip, disabled, with its embedded sound on. A2: the
        # blue camera's sound, linked to it and enabled.
        clips = [self.video('green', 0, 2, enabled=False, linkGroup='g'),
                 dict(type='audio', mediaId='blue', track=2, start=0, duration=2, linkGroup='g', **{'in': 1})]
        movie = self.render(clips, 'linked')
        sound = left_channel(movie)
        self.assertLess(tone_level(sound, 625, 0, 2), .002, 'embedded sound of a disabled clip is heard')
        self.assertAlmostEqual(tone_level(sound, 875, 0, 2), AMPLITUDE, delta=.005)
        self.assertTrue(all(max(frame) < 20 for frame in frames_rgb(movie)), 'a disabled clip is drawn')

    def test_disabled_clip_with_opacity_keyframes(self):
        keys = {'opacity': [{'time': 0, 'value': 20}, {'time': 1, 'value': 100}]}
        clips = [self.video('red', 0, 2), self.video('green', 0, 2, track=2, keyframes=keys, enabled=False)]
        self.assertSamePicture(self.render(clips, 'keys'), self.render(clips[:1], 'reference'))

    def test_disabled_clip_across_a_window_seam_and_in_a_range(self):
        clips = [self.video('red', 0, 5), self.video('green', .5, 4, track=2, enabled=False),
                 self.video('blue', 1, 3, track=3, opacity=50, enabled=False)]
        with mock.patch.multiple(render_movie, VIDEO_WINDOW_SECONDS=1, VIDEO_WINDOW_CUTS=1):
            movie = self.render(clips, 'windowed')
            ranged = self.render(clips, 'ranged', range={'start': 1.2, 'end': 3.4})
            reference = self.render(clips[:1], 'reference')
            reference_range = self.render(clips[:1], 'reference-range', range={'start': 1.2, 'end': 3.4})
        self.assertSamePicture(movie, reference)
        self.assertSamePicture(ranged, reference_range)
        sound = left_channel(movie)
        for frequency in (625, 875):
            self.assertLess(tone_level(sound, frequency, 0, 5), .002)

    def test_disabled_clips_inside_nested_material(self):
        child = dict(name='Inner', width=W, height=H, fps=FPS, media=self.media,
                     clips=[self.video('red', 0, 2), self.video('green', 0, 2, track=2, enabled=False)])
        nested = dict(id='nest', name='Inner', nestedSequence='inner', nestedProject=child)
        project = dict(width=W, height=H, fps=FPS, media=[*self.media, nested],
                       clips=[dict(type='video', mediaId='nest', track=1, start=0, duration=1.6, nestedSequence='inner', **{'in': .2})])
        movie = self.directory/'nested.mov'
        render_to_file(project, movie, options={'codec': 'prores', 'limiter': False})
        shown = frames_rgb(movie)
        self.assertEqual(len(shown), 40)
        self.assertTrue(all(mean_rgb(frame)[0] > 200 and mean_rgb(frame)[1] < 20 for frame in shown), 'inner disabled clip drawn')
        sound = left_channel(movie)
        self.assertLess(tone_level(sound, 625, 0, 1.6), .002)
        self.assertAlmostEqual(tone_level(sound, 375, 0, 1.6), AMPLITUDE, delta=.005)
        # Nested material used only by a disabled clip is not rendered at all.
        project['clips'] = [dict(project['clips'][0], enabled=False), self.video('blue', 0, 1.6, track=2)]
        calls = []
        real = render_movie._render_prepared
        with mock.patch.object(render_movie, '_render_prepared', lambda *a, **k: calls.append(a) or real(*a, **k)):
            render_to_file(project, movie, options={'codec': 'prores', 'limiter': False})
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(mean_rgb(frame)[2] > 200 for frame in frames_rgb(movie)))


class TransitionsAgainstNothing(EnabledCase):
    """The rule for a transition next to a disabled clip, on the lowest track (black beneath)."""

    def middle(self, clips, name):
        shown = frames_rgb(self.render(clips, name))
        return shown, shown[50]   # 2.0 s: the middle of a 0.8 s transition centred on the cut at 2 s

    def test_dissolve_to_a_disabled_clip_fades_the_outgoing_clip_to_black(self):
        clips = [self.video('red', 0, 2), self.video('green', 2, 2, transition='dissolve', transitionDuration=.8,
                                                    transitionBefore=.4, enabled=False)]
        shown, middle = self.middle(clips, 'out')
        self.assertLess(abs(mean_rgb(middle)[0]-mean_rgb(shown[0])[0]/2), 12)
        self.assertEqual(mean_rgb(middle)[1], 0)
        self.assertTrue(all(max(frame) < 20 for frame in shown[61:]), 'the disabled clip is drawn after the dissolve')
        sound = left_channel(self.render(clips, 'out-sound', codec='wav'))
        self.assertLess(tone_level(sound, 625, 1.5, 4), .002)
        # The outgoing sound still fades out over the transition (1.6..2.4 s).
        self.assertAlmostEqual(tone_level(sound, 375, 1.9, 2.1), AMPLITUDE/2, delta=.01)
        self.assertLess(tone_level(sound, 375, 2.5, 4), .002)

    def test_dissolve_from_a_disabled_clip_fades_the_incoming_clip_up_from_black(self):
        clips = [self.video('red', 0, 2, enabled=False), self.video('green', 2, 2, transition='dissolve',
                                                                    transitionDuration=.8, transitionBefore=.4)]
        shown, middle = self.middle(clips, 'in')
        self.assertTrue(all(max(frame) < 20 for frame in shown[:39]))
        self.assertEqual(mean_rgb(middle)[0], 0, 'the disabled clip shows through the dissolve')
        self.assertLess(abs(mean_rgb(middle)[1]-mean_rgb(shown[-1])[1]/2), 12)
        sound = left_channel(self.render(clips, 'in-sound', codec='wav'))
        self.assertLess(tone_level(sound, 375, 0, 4), .002)
        self.assertAlmostEqual(tone_level(sound, 625, 1.9, 2.1), AMPLITUDE/2, delta=.01)

    def test_wipe_to_a_disabled_clip_uncovers_black_from_the_left(self):
        clips = [self.video('red', 0, 2), self.video('green', 2, 2, transition='wipe', transitionDuration=.8,
                                                    transitionBefore=.4, enabled=False)]
        shown, middle = self.middle(clips, 'wipe')
        self.assertLess(max(mean_rgb(middle, 0, W//2-4)), 20, 'the left half is not uncovered')
        self.assertGreater(mean_rgb(middle, W//2+4, W)[0], 200, 'the right half lost the outgoing clip')
        self.assertTrue(all(max(frame) < 20 for frame in shown[60:]))

    def test_wipe_from_a_disabled_clip_wipes_the_incoming_clip_on_over_black(self):
        clips = [self.video('red', 0, 2, enabled=False), self.video('green', 2, 2, transition='wipe',
                                                                    transitionDuration=.8, transitionBefore=.4)]
        shown, middle = self.middle(clips, 'wipe-in')
        self.assertGreater(mean_rgb(middle, 0, W//2-4)[1], 200)
        self.assertLess(max(mean_rgb(middle, W//2+4, W)), 20)

    def test_dip_next_to_a_disabled_clip_dips_the_enabled_side(self):
        out = [self.video('red', 0, 2), self.video('green', 2, 2, transition='fade', transitionDuration=.8,
                                                  transitionBefore=.4, enabled=False)]
        shown, _ = self.middle(out, 'dip-out')
        self.assertLess(abs(mean_rgb(shown[45])[0]-mean_rgb(shown[0])[0]/2), 15)   # 1.8 s: halfway down
        self.assertTrue(all(max(frame) < 20 for frame in shown[50:]))
        into = [self.video('red', 0, 2, enabled=False), self.video('green', 2, 2, transition='fade', transitionDuration=.8,
                                                                   transitionBefore=.4)]
        shown, _ = self.middle(into, 'dip-in')
        self.assertTrue(all(max(frame) < 20 for frame in shown[:51]))
        self.assertLess(abs(mean_rgb(shown[55])[1]-mean_rgb(shown[-1])[1]/2), 15)   # 2.2 s: halfway up


if __name__ == '__main__':
    unittest.main()
