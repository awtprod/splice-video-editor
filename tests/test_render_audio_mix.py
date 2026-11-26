"""Mixed sound measured in the decoded PCM: each clip plays its own steady tone,
and the level of every tone is read back block by block (a single-bin DFT), so a
clip that goes silent, plays at the wrong time or with the wrong envelope fails.

Round 2, finding 1: a speed-changed clip that shared a 60 s audio chunk with any
other clip went silent (the chunk mixer reused the last slice's end for every
speed-changed slice). Finding 7: how a dip to black sounds for embedded camera
sound and for the same sound as a linked clip.
"""
from array import array
import contextlib
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

AMPLITUDE = .25
RATE = 48000


def ffmpeg(*arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def left_channel(path):
    samples = array('f', subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vn', '-ac', '2',
                                                  '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1'], timeout=120))
    return samples[0::2]


def tone_level(samples, frequency, begin, end):
    """Amplitude of a steady tone between two timeline seconds."""
    first, stop = round(begin*RATE), round(end*RATE)
    block = samples[first:stop]
    step = 2*math.pi*frequency/RATE
    real = sum(value*math.cos(step*(first+k)) for k, value in enumerate(block))
    imaginary = sum(value*math.sin(step*(first+k)) for k, value in enumerate(block))
    return 2*math.hypot(real, imaginary)/max(1, len(block))


class ToneCase(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def tone(self, frequency, rate=RATE, seconds=40, video=False, later=None):
        """Media playing one steady tone (``later``: a second frequency from source second 8);
        with ``video`` a camera file (picture plus embedded sound)."""
        path = self.directory/(f'tone-{frequency}-{later}-{rate}' + ('.mov' if video else '.wav'))
        if not path.exists():
            wave = f'{AMPLITUDE}*sin(2*PI*{frequency}*t)'
            if later:
                wave = f'if(lt(t,8),{wave},{AMPLITUDE}*sin(2*PI*{later}*t))'
            sound = f"aevalsrc='{wave}|{wave}':s={rate}:d={seconds}"
            if video:
                ffmpeg('-f', 'lavfi', '-i', f'color=gray:s=32x18:r=25:d={seconds}', '-f', 'lavfi', '-i', sound,
                       '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'pcm_f32le', '-shortest', str(path))
            else:
                ffmpeg('-f', 'lavfi', '-i', sound, '-c:a', 'pcm_f32le', str(path))
        return '/media/' + str(path.relative_to(MEDIA_ROOT))

    def render(self, clips, media, *, codec='wav', chunk=None, events=None, name='mix', **options):
        suffix = {'wav': '.wav', 'h264': '.mp4', 'prores': '.mov'}[codec]
        output = self.directory/(name+suffix)
        patches = {}
        if chunk: patches['AUDIO_CHUNK_SECONDS'] = chunk
        if events: patches['AUDIO_CHUNK_EVENTS'] = events
        with mock.patch.multiple(render_movie, **patches) if patches else contextlib.nullcontext():
            render_to_file(dict(width=32, height=18, fps=25, media=media, clips=clips), output,
                           options={'codec': codec, 'limiter': False, **options})
        return left_channel(output)

    def assertLevels(self, samples, frequency, expected, tolerance=.012, block=.1):
        """``expected(t)`` is the tone's gain at timeline second t (0..1); checked on every block."""
        wrong = []
        end = len(samples)/RATE
        at = 0.0
        while at+block <= end+1e-9:
            want = AMPLITUDE*expected(at+block/2)
            got = tone_level(samples, frequency, at, at+block)
            if abs(got-want) > tolerance:
                wrong.append((round(at, 3), round(want, 4), round(got, 4)))
            at += block
        self.assertEqual(wrong, [], f'{frequency} Hz: {len(wrong)} blocks off (start s, expected, measured)')


def inside(*spans):
    return lambda t: 1.0 if any(a <= t < b for a, b in spans) else 0.0


class SpeedChangedSound(ToneCase):
    """Finding 1: speed-changed sound must play whenever it shares a chunk with other clips."""

    def test_speed_changed_clip_sharing_a_chunk_with_a_plain_clip(self):
        media = [dict(id='a', src=self.tone(375)), dict(id='b', src=self.tone(625, 44100))]
        clips = [dict(type='audio', mediaId='a', track=1, start=0, duration=8, speed=.5, **{'in': 2}),
                 dict(type='audio', mediaId='b', track=2, start=8, duration=1, **{'in': 4})]
        for codec in ('wav', 'h264'):
            with self.subTest(codec=codec):
                samples = self.render(clips, media, codec=codec, name=f'shared-{codec}')
                tolerance = .012 if codec == 'wav' else .03
                self.assertLevels(samples, 375, inside((0, 8)), tolerance)
                self.assertLevels(samples, 625, inside((8, 9)), tolerance)

    def test_two_speed_changed_clips_with_a_crossfade(self):
        media = [dict(id='a', src=self.tone(375)), dict(id='b', src=self.tone(625))]
        # A dissolve of 2 s centred on the cut at 5 s: A fades 4..6, B rises 4..6.
        clips = [dict(type='audio', mediaId='a', track=1, start=0, duration=5, speed=.6, **{'in': 1}),
                 dict(type='audio', mediaId='b', track=1, start=5, duration=5, speed=1.4, transition='dissolve',
                      transitionDuration=2, transitionBefore=1, **{'in': 6})]
        samples = self.render(clips, media)
        self.assertLevels(samples, 375, lambda t: 1 if t < 4 else max(0, 1-(t-4)/2))
        self.assertLevels(samples, 625, lambda t: 0 if t < 4 else min(1, (t-4)/2))

    def test_fades_on_a_speed_changed_clip_next_to_other_clips(self):
        media = [dict(id='a', src=self.tone(375)), dict(id='b', src=self.tone(625))]
        clips = [dict(type='audio', mediaId='a', track=1, start=1, duration=6, speed=1.5, fadeIn=1, fadeOut=2, **{'in': 1}),
                 dict(type='audio', mediaId='b', track=2, start=0, duration=2, **{'in': 1}),
                 dict(type='audio', mediaId='b', track=2, start=6.5, duration=1, **{'in': 9})]
        samples = self.render(clips, media)
        self.assertLevels(samples, 375, lambda t: 0 if t < 1 or t >= 7 else min(1, t-1, (7-t)/2))
        self.assertLevels(samples, 625, inside((0, 2), (6.5, 7.5)))

    def test_speed_changed_clip_across_seams_while_clips_start_and_end_around_it(self):
        media = [dict(id='a', src=self.tone(375)), dict(id='b', src=self.tone(625, 44100))]
        spans = ((0, .6), (1, 2), (3.2, 3.5), (4.1, 4.3), (5, 6.2), (7.3, 8))
        clips = [dict(type='audio', mediaId='a', track=1, start=.5, duration=7, speed=.7, **{'in': 2})]
        clips += [dict(type='audio', mediaId='b', track=2+k % 2, start=a, duration=b-a, **{'in': 1+k}) for k, (a, b) in enumerate(spans)]
        single = self.render(clips, media, name='single')
        chunked = self.render(clips, media, chunk=1.37, name='chunked')
        for samples in (single, chunked):
            self.assertLevels(samples, 375, inside((.5, 7.5)), block=.05)
            self.assertLevels(samples, 625, inside(*spans), block=.05)
        self.assertEqual(len(single), len(chunked))
        self.assertLessEqual(max(abs(a-b) for a, b in zip(single, chunked)), 1e-6)

    def test_speed_changed_clips_around_the_events_per_chunk_split(self):
        media = [dict(id='a', src=self.tone(375)), dict(id='b', src=self.tone(625)), dict(id='c', src=self.tone(875))]
        # 40 short clips start inside the first minute: the 32-event limit splits
        # the chunk near 6.4 s. Clip a spans that split (rendered whole first);
        # clip c lies inside the second chunk next to the last short clips.
        spans = [(.2*k, .2*k+.15) for k in range(40)]
        clips = [dict(type='audio', mediaId='a', track=1, start=.3, duration=7, speed=1.3, **{'in': 1}),
                 dict(type='audio', mediaId='c', track=3, start=6.9, duration=4, speed=.8, **{'in': 1})]
        clips += [dict(type='audio', mediaId='b', track=2, start=a, duration=b-a, **{'in': 1+a}) for a, b in spans]
        calls = []
        real = render_movie._render_audio_chunk

        def counted(*arguments, **keywords):
            calls.append((arguments[1]+arguments[2])/RATE)
            return real(*arguments, **keywords)
        with mock.patch.object(render_movie, '_render_audio_chunk', counted):
            samples = self.render(clips, media)
        self.assertEqual(len(calls), 2, 'the event limit did not split the chunk')
        self.assertTrue(.3 < calls[1] < 6.9, f'split at {calls[1]} s')
        self.assertLevels(samples, 375, inside((.3, 7.3)), block=.05)
        self.assertLevels(samples, 875, inside((6.9, 10.9)), block=.05)
        self.assertLevels(samples, 625, inside(*spans), block=.05)

    def test_range_export_keeps_the_envelope_of_a_speed_changed_clip(self):
        # Finding 5: a range that starts inside a speed-changed clip pre-renders
        # it from the range start; its fades and volume keyframes must keep the
        # clip's own clock (12-25 dB off when the shift was dropped).
        media = [dict(id='a', src=self.tone(375)), dict(id='b', src=self.tone(625, 44100))]
        keys = {'volume': [dict(time=0, value=100), dict(time=8, value=30), dict(time=16, value=160)]}
        for speed, source in ((.5, 'a'), (1.5, 'b')):
            frequency = 375 if source == 'a' else 625
            clips = [dict(type='audio', mediaId=source, track=1, start=1, duration=20, speed=speed, fadeIn=3, fadeOut=6,
                          keyframes=keys, **{'in': 2})]
            full = self.render(clips, media, name='full')
            for begin, end in ((7, 19), (13, 21)):
                with self.subTest(speed=speed, range=(begin, end)):
                    ranged = self.render(clips, media, name='range', range={'start': begin, 'end': end})
                    offset = round(begin*RATE)
                    self.assertEqual(len(ranged), round((end-begin)*RATE))
                    shifted = full[offset:offset+len(ranged)]
                    for at in [k*.25 for k in range(int((end-begin)/.25))]:
                        want = tone_level(shifted, frequency, at, at+.25)
                        got = tone_level(ranged, frequency, at, at+.25)
                        self.assertLess(abs(got-want), .05*want+.004, f'{begin+at:.2f} s: range {got:.4f}, full export {want:.4f}')


class DipSound(ToneCase):
    """Finding 7: a dip to black is a picture transition. Its sound follows the
    clip that carries it (the monitor's rule, engine.js audibility): the incoming
    clip fades in over the transition and the outgoing clip plays to its out
    point. A linked sound clip carrying the same 'fade' transition renders the
    same samples as the embedded sound; a linked sound clip carrying a
    'dissolve' is a different edit (a crossfade) and sounds like one."""

    def test_embedded_and_linked_sound_of_a_dip_render_the_same_samples(self):
        camera = self.tone(375, video=True, later=625)
        media = [dict(id='cam', src=camera)]
        picture = [dict(type='video', mediaId='cam', track=1, start=0, duration=2, **{'in': 3}),
                   dict(type='video', mediaId='cam', track=1, start=2, duration=2, transition='fade',
                        transitionDuration=.8, transitionBefore=.4, **{'in': 10})]
        linked = [dict(clip, embeddedAudio=False) for clip in picture]
        linked += [dict(clip, type='audio') for clip in picture]
        embedded_sound = self.render(picture, media, name='embedded')
        linked_sound = self.render(linked, media, name='linked')
        self.assertEqual(len(embedded_sound), len(linked_sound))
        self.assertEqual(max(abs(a-b) for a, b in zip(embedded_sound, linked_sound)), 0)
        # The outgoing clip plays at full level to the cut; the incoming one fades in over 1.6..2.4 s.
        self.assertLevels(embedded_sound, 375, inside((0, 2)))
        self.assertLevels(embedded_sound, 625, lambda t: 0 if t < 1.6 else min(1, (t-1.6)/.8))
        crossfade = [dict(clip, transition='dissolve') if clip['type'] == 'audio' and clip.get('transition') else clip
                     for clip in linked]
        self.assertNotEqual(list(self.render(crossfade, media, name='crossfade')), list(linked_sound))


if __name__ == '__main__':
    unittest.main()
