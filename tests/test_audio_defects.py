"""The audio defects the last renderer validation found (audio finishing item 7), each with
the validator's probe (val-a-r3/probes xphase.py, xtail.py, lagcheck.py) adapted to a test,
and item 8: what sound does under each picture transition, embedded and linked alike."""
import cmath
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from audio_fixtures import AudioCase, pcm, tone_level  # noqa: E402

RATE = 48000


def phase(samples, frequency, first, stop):
    total = sum(samples[2*n]*cmath.exp(-2j*math.pi*frequency*n/RATE) for n in range(first, stop))
    return cmath.phase(total)


def wrap(angle):
    return (angle+math.pi) % (2*math.pi)-math.pi


class SpeedChangedCrossfade(AudioCase):
    def media(self):
        a = self.source('a.wav', "aevalsrc='0.2*sin(2*PI*480*t)|0.2*sin(2*PI*480*t)':s=48000", 20)
        b = self.source('b.wav', "aevalsrc='0.2*sin(2*PI*1500*t)|0.2*sin(2*PI*1500*t)':s=48000", 20)
        return [dict(id='A', src=self.src(a)), dict(id='B', src=self.src(b))]

    def clips(self, speed, before=0.0, duration=.4, keys=None):
        a = dict(type='audio', mediaId='A', track=-1, start=0, duration=2, speed=speed, **{'in': 5})
        if keys: a['keyframes'] = {'volume': keys}
        return [a, dict(type='audio', mediaId='B', track=-1, start=2, duration=2, transition='dissolve', transitionDuration=duration,
                        transitionBefore=before, **{'in': 5})]

    def test_outgoing_speed_changed_sound_keeps_its_phase_through_the_cut(self):
        # 7a: body and crossfade tail were separate tempo runs: a phase jump (click) at the cut.
        for speed in (.75, .278, 1.6):
            with self.subTest(speed=speed):
                samples = pcm(self.render(self.clips(speed), self.media(), name=f'x{speed}', limiter=False))
                # The tone's apparent frequency is 480 Hz (atempo keeps pitch); phase advance across
                # the cut must equal the advance over the same span before it.
                cut = 2*RATE
                before = wrap(phase(samples, 480, cut-480, cut)-phase(samples, 480, cut-960, cut-480))
                across = wrap(phase(samples, 480, cut, cut+480)-phase(samples, 480, cut-480, cut))
                self.assertLess(abs(math.degrees(wrap(across-before))), 12)
                left = samples[0::2]
                jump = max(abs(left[n+1]-left[n]) for n in range(cut-3, cut+3))
                typical = max(abs(left[n+1]-left[n]) for n in range(cut-2000, cut-1000))
                self.assertLess(jump, typical*1.3)

    def test_outgoing_tail_continues_the_keyframe_ramp(self):
        # 7b: the tail used the last keyframe's value while the body ramped toward a keyframe past
        # the clip's end: a level step at the cut.
        keys = [dict(time=0, value=100), dict(time=3, value=0)]
        samples = pcm(self.render(self.clips(1.0, duration=.8, keys=keys), self.media(), name='keys', limiter=False))
        # Clip-local ramp 100 -> 0 over 3 s: at the cut (2 s) 33 %; the crossfade halves it 0.4 s later.
        for t in (1.8, 1.95, 2.05, 2.2, 2.35):
            want = .2*(1-t/3)*max(0, min(1, 1-(t-2)/.8))
            self.assertAlmostEqual(tone_level(samples, 480, t-.025, t+.025), want, delta=.003, msg=f'{t} s')

    def test_chunked_and_single_renders_are_the_same_waveform(self):
        # 7c: pre-rendered and directly mixed paths aligned the tempo filter differently (up to
        # 0.16 apart). Stateful events now always pre-render from a fixed event-local point.
        a44 = self.source('a44.wav', "aevalsrc='0.2*sin(2*PI*480*t)+0.1*sin(2*PI*2000*t)|0.2*sin(2*PI*480*t)':s=44100", 20, rate=44100)
        media = [*self.media(), dict(id='C', src=self.src(a44))]
        clips = [dict(type='audio', mediaId='A', track=-1, start=0, duration=3, speed=.7, **{'in': 1}),
                 dict(type='audio', mediaId='C', track=-2, start=.5, duration=4, **{'in': 2}),
                 dict(type='audio', mediaId='B', track=-3, start=1.2, duration=3, speed=1.37, reverse=True, **{'in': 3}),
                 dict(type='audio', mediaId='A', track=-1, start=3, duration=2.5, speed=2.2, transition='dissolve', transitionDuration=.6,
                      transitionBefore=.2, **{'in': 4})]
        single = pcm(self.render(clips, media, name='single', limiter=False))
        for chunk in (2.3, .9):
            with self.subTest(chunk=chunk):
                chunked = pcm(self.render(clips, media, name=f'c{chunk}', limiter=False, chunk=chunk, events=2))
                self.assertEqual(len(chunked), len(single))
                self.assertLess(max(abs(x-y) for x, y in zip(single, chunked)), 1.2e-7)

    def test_clip_placement_does_not_depend_on_shared_inputs(self):
        # 7d: a +-1 sample placement depending on which clips shared a seeked input.
        click = self.source('click.wav', "aevalsrc='if(eq(mod(n,4801),0),0.5,0)|if(eq(mod(n,4801),0),0.5,0)':s=48000", 30)
        media = [dict(id='K', src=self.src(click))]
        # These in points put the clicks one sample apart on dev (found by a search over in points).
        alone = [dict(type='audio', mediaId='K', track=-1, start=1, duration=2, **{'in': 8.5694694})]
        shared = alone+[dict(type='audio', mediaId='K', track=-2, start=1.5, duration=2, volume=0, **{'in': 6.2558213})]
        first = pcm(self.render(alone, media, name='alone', limiter=False))[0::2]
        both = pcm(self.render(shared, media, name='shared', limiter=False))[0::2]
        self.assertEqual([n for n, v in enumerate(first) if v > .1], [n for n, v in enumerate(both) if v > .1])


class SoundUnderPictureTransitions(AudioCase):
    """Item 8, one rule for embedded and linked sound: dissolve and wipe crossfade (same length
    and alignment), a dip fades out then in, a cut cuts; audioTransition overrides it."""

    def camera(self):
        from audio_fixtures import ffmpeg
        path = self.directory/'camera.mov'
        if not path.exists():
            sound = "aevalsrc='if(lt(t,8),0.25*sin(2*PI*375*t),0.25*sin(2*PI*625*t))|if(lt(t,8),0.25*sin(2*PI*375*t),0.25*sin(2*PI*625*t))':s=48000:d=20"
            ffmpeg('-f', 'lavfi', '-i', 'color=gray:s=32x18:r=25:d=20', '-f', 'lavfi', '-i', sound, '-c:v', 'libx264', '-preset', 'ultrafast',
                   '-c:a', 'pcm_f32le', '-shortest', str(path))
        return [dict(id='cam', src=self.src(path))]

    def pair(self, transition, before, linked, override=None):
        second = dict(type='video', mediaId='cam', track=1, start=2, duration=2, transition=transition, transitionDuration=.8,
                      transitionBefore=before, **{'in': 10})
        if override: second['audioTransition'] = override
        picture = [dict(type='video', mediaId='cam', track=1, start=0, duration=2, linkGroup='a', **{'in': 3}), dict(second, linkGroup='b')]
        if not linked:
            return [{k: v for k, v in clip.items() if k != 'linkGroup'} for clip in picture]
        sound = [dict(type='audio', mediaId='cam', track=-1, start=clip['start'], duration=2, linkGroup=clip['linkGroup'], **{'in': clip['in']})
                 for clip in picture]
        if override: sound[1]['audioTransition'] = override
        return [dict(clip, embeddedAudio=False) for clip in picture]+sound

    def levels(self, samples, frequency, expected):
        for t in [k*.05+.025 for k in range(80)]:
            self.assertAlmostEqual(tone_level(samples, frequency, t-.025, t+.025), .25*expected(t)*math.cos(math.pi/4)*math.sqrt(2),
                                   delta=.012, msg=f'{frequency} Hz at {t:.3f} s')

    def test_each_transition_sounds_the_same_embedded_and_linked(self):
        media = self.camera()
        crossfade = lambda before: (lambda t: 1 if t < 2-before else max(0, 1-(t-2+before)/.8),
                                    lambda t: 0 if t < 2-before else min(1, (t-2+before)/.8))
        dip = lambda before: (lambda t: 1 if t < 2-before else max(0, (2-t)/before) if before else (1 if t < 2 else 0),
                              lambda t: 0 if t < 2 else min(1, (t-2)/(.8-before)) if .8-before > 0 else 1)
        cut = (lambda t: 1 if t < 2 else 0, lambda t: 0 if t < 2 else 1)
        cases = [('dissolve', .4, crossfade(.4)), ('dissolve', 0, crossfade(0)), ('wipe', .2, crossfade(.2)),
                 ('fade', .4, dip(.4)), ('fade', .8, dip(.8)), ('none', 0, cut)]
        for transition, before, (outgoing, incoming) in cases:
            with self.subTest(transition=transition, before=before):
                embedded = pcm(self.render(self.pair(transition, before, False), media, name='e', limiter=False))
                linked = pcm(self.render(self.pair(transition, before, True), media, name='l', limiter=False))
                self.assertEqual(len(embedded), len(linked))
                self.assertLess(max(abs(a-b) for a, b in zip(embedded, linked)), 1e-7)
                self.levels(embedded, 375, outgoing)
                self.levels(embedded, 625, incoming)

    def test_the_sound_transition_can_be_overridden(self):
        media = self.camera()
        for override, picture, (outgoing, incoming) in (
                ('cut', 'dissolve', (lambda t: 1 if t < 2 else 0, lambda t: 0 if t < 2 else 1)),
                ('crossfade', 'fade', (lambda t: 1 if t < 1.6 else max(0, 1-(t-1.6)/.8), lambda t: 0 if t < 1.6 else min(1, (t-1.6)/.8))),
                ('dip', 'dissolve', (lambda t: 1 if t < 1.6 else max(0, (2-t)/.4), lambda t: 0 if t < 2 else min(1, (t-2)/.4)))):
            for linked in (False, True):
                with self.subTest(override=override, linked=linked):
                    samples = pcm(self.render(self.pair(picture, .4, linked, override), media, name='o', limiter=False))
                    self.levels(samples, 375, outgoing)
                    self.levels(samples, 625, incoming)


if __name__ == '__main__':
    unittest.main()
