"""Clip, track and master effects in rendered exports (audio finishing item 3).

The preview side of the parity (the browser's IIRFilterNodes and the dynamics AudioWorklet
against FFmpeg) is measured in tests/audio-finishing.cjs; this file checks the export itself
and that both languages compute the same coefficients."""
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
from audio_fixtures import AudioCase, ffmpeg, pcm, tone_level  # noqa: E402
import audio_dsp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EQ = {'bands': [{'type': 'highpass', 'freq': 80, 'q': .7071}, {'type': 'peak', 'freq': 1000, 'q': 1.4, 'gain': 6},
                {'type': 'lowshelf', 'freq': 150, 'q': .8, 'gain': -4}, {'type': 'highshelf', 'freq': 8000, 'q': .7, 'gain': 3},
                {'type': 'lowpass', 'freq': 15000, 'q': .9}]}


def node_sections(eq, rate):
    script = f"const d=require({json.dumps(str(ROOT/'audio-dsp.js'))});process.stdout.write(JSON.stringify(d.eqSections({json.dumps(eq)},{rate})))"
    return json.loads(subprocess.check_output(['node', '-e', script], timeout=30))


class Coefficients(unittest.TestCase):
    def test_python_and_browser_compute_the_same_biquads(self):
        for rate in (44100, 48000, 96000):
            for band in [*EQ['bands'], {'type': 'peak', 'freq': 19000, 'q': 18, 'gain': -24}, {'type': 'lowshelf', 'freq': 20, 'q': .1, 'gain': 24}]:
                with self.subTest(rate=rate, band=band):
                    eq = {'bands': [band]}
                    python = audio_dsp.eq_sections(eq, rate)
                    browser = node_sections(eq, rate)
                    self.assertEqual(len(python), len(browser))
                    for left, right in zip(python, browser):
                        for a, b in zip(left, right):
                            self.assertAlmostEqual(a, b, delta=1e-12)

    def test_ffmpeg_runs_the_designed_response(self):
        chain = ','.join(audio_dsp.ffmpeg_filters(audio_dsp.normalize({'eq': EQ})))
        length = 1 << 16
        from array import array
        response = array('d', subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f"aevalsrc='if(eq(n,0),1,0)':s=48000:d={length/48000}",
                                                       '-af', chain, '-f', 'f64le', '-c:a', 'pcm_f64le', '-'], timeout=60))[:length]
        sections = audio_dsp.eq_sections(EQ, 48000)
        worst = 0.0
        for index in range(61):
            frequency = 20*10**(3*index/60)
            w = 2*math.pi*frequency/48000
            real = sum(value*math.cos(w*n) for n, value in enumerate(response) if abs(value) > 1e-14)
            imaginary = sum(value*math.sin(w*n) for n, value in enumerate(response) if abs(value) > 1e-14)
            worst = max(worst, abs(20*math.log10(math.hypot(real, imaginary))-audio_dsp.magnitude_db(sections, frequency, 48000)))
        self.assertLess(worst, .01)

    def test_settings_are_validated_with_units(self):
        for fx, message in (({'eq': {'bands': [{'type': 'notch', 'freq': 100}]}}, 'type'), ({'eq': {'bands': [{'type': 'peak', 'freq': 5}]}}, r'\(Hz\)'),
                            ({'comp': {'ratio': 40}}, 'ratio'), ({'comp': {'attack': 0}}, r'\(ms\)'), ({'gate': {'range': 6}}, r'\(dB\)'),
                            ({'denoise': {'amount': 60}}, r'\(dB\)'), ({'reverb': {}}, 'Invalid'), ({'eq': {'bands': [{'type': 'peak', 'freq': 100}]*9}}, '8 bands')):
            with self.subTest(fx=fx), self.assertRaisesRegex(ValueError, message):
                audio_dsp.normalize(fx)


class RenderedEffects(AudioCase):
    def tones(self):
        # 100 Hz, 1 kHz and 10 kHz at -20 dBFS each.
        return self.source('tones.wav', "aevalsrc='0.1*sin(2*PI*100*t)+0.1*sin(2*PI*1000*t)+0.1*sin(2*PI*10000*t)|"
                           "0.1*sin(2*PI*100*t)+0.1*sin(2*PI*1000*t)+0.1*sin(2*PI*10000*t)':s=48000", 8)

    def test_clip_and_track_eq_render_the_designed_gain_at_each_frequency(self):
        media = [dict(id='t', src=self.src(self.tones()))]
        eq = {'bands': [{'type': 'peak', 'freq': 1000, 'q': 2, 'gain': 6}, {'type': 'highshelf', 'freq': 6000, 'q': .7, 'gain': -9}]}
        sections = audio_dsp.eq_sections(eq, 48000)
        for where in ('clip', 'track'):
            with self.subTest(where=where):
                clip = dict(type='audio', mediaId='t', track=-1, start=0, duration=7, **({'audioFx': {'eq': eq}} if where == 'clip' else {}))
                project = {'trackMix': {'-1': {'fx': {'eq': eq}}}} if where == 'track' else None
                samples = pcm(self.render([clip], media, name=where, limiter=False, project=project))
                for frequency in (100, 1000, 10000):
                    expected = .1*10**(audio_dsp.magnitude_db(sections, frequency, 48000)/20)
                    self.assertAlmostEqual(tone_level(samples, frequency, 2, 3), expected, delta=expected*.003)

    def test_effects_do_not_depend_on_chunking(self):
        media = [dict(id='t', src=self.src(self.tones()))]
        fx = {'eq': {'bands': [{'type': 'peak', 'freq': 40, 'q': 12, 'gain': 9}]}, 'comp': {'threshold': -30, 'ratio': 4, 'attack': 5, 'release': 300}}
        clips = [dict(type='audio', mediaId='t', track=-1, start=0, duration=7, audioFx=fx),
                 dict(type='audio', mediaId='t', track=-2, start=.37, duration=6, **{'in': .5})]
        project = {'trackMix': {'-2': {'fx': {'comp': {'threshold': -26, 'ratio': 6, 'attack': 2, 'release': 400},
                                              'eq': {'bands': [{'type': 'lowshelf', 'freq': 200, 'q': .7, 'gain': 4}]}}}}}
        single = pcm(self.render(clips, media, name='single', limiter=False, project=project))
        chunked = pcm(self.render(clips, media, name='chunked', limiter=False, chunk=1.3, project=project))
        self.assertEqual(len(single), len(chunked))
        self.assertLess(max(abs(a-b) for a, b in zip(single, chunked)), 1e-6)

    def test_compressor_static_curve(self):
        # A square wave has a constant |x|, so the peak detector settles on its level exactly.
        comp = {'threshold': -20, 'ratio': 4, 'attack': 1, 'release': 50, 'knee': 0, 'makeup': 2}
        for level in (-30, -20, -14, -8, -2):
            with self.subTest(level=level):
                amplitude = 10**(level/20)
                wave = f'{amplitude}*if(lt(mod(t*250,1),0.5),1,-1)'
                source = self.source(f'square{level}.wav', f"aevalsrc='{wave}|{wave}':s=48000", 4)
                samples = pcm(self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=3, audioFx={'comp': comp})],
                                          [dict(id='s', src=self.src(source))], name=f'c{level}', limiter=False))
                settled = max(abs(value) for value in samples[2*48000*2:2*48000*2+4000])
                out = 20*math.log10(settled)
                expected = (level if level <= -20 else -20+(level+20)/4)+2
                self.assertAlmostEqual(out, expected, delta=.05)

    def test_compressor_timing_is_reported_honestly(self):
        # Step from -40 to -6 dBFS: time to reach 90 % of the final gain reduction (in dB).
        comp = {'threshold': -20, 'ratio': 8, 'attack': 20, 'release': 200, 'knee': 0, 'makeup': 0}
        wave = 'if(lt(t,1),0.01,0.5)*if(lt(mod(t*500,1),0.5),1,-1)'
        source = self.source('step.wav', f"aevalsrc='{wave}|{wave}':s=48000", 3)
        samples = pcm(self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=2.5, audioFx={'comp': comp})],
                                  [dict(id='s', src=self.src(source))], name='step', limiter=False))
        left = samples[0::2]
        final = 20*math.log10(max(abs(v) for v in left[int(2.2*48000):int(2.3*48000)])/.5)
        reached = next(n for n in range(48000, len(left)) if 20*math.log10(max(abs(v) for v in left[n:n+48])/.5) <= .9*final)
        attack_ms = (reached-48000)/48
        # FFmpeg's attack is the detector's coefficient time (1/(attack*rate/4000) per sample):
        # 90 % of the reduction arrives in about half the nominal attack.
        self.assertGreater(attack_ms, 2)
        self.assertLess(attack_ms, 20)
        print(f'\nAUDIO compressor 20 ms attack: 90 % of gain reduction after {attack_ms:.1f} ms', flush=True)

    def test_gate_holds_its_range_below_threshold(self):
        wave = 'if(lt(mod(t,2),1),0.003,0.3)*if(lt(mod(t*500,1),0.5),1,-1)'
        source = self.source('gate.wav', f"aevalsrc='{wave}|{wave}':s=48000", 4)
        gate = {'threshold': -30, 'ratio': 9000, 'attack': 1, 'release': 20, 'range': -24}
        samples = pcm(self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=4, audioFx={'gate': gate})],
                                  [dict(id='s', src=self.src(source))], name='gated', limiter=False))
        quiet = max(abs(value) for value in samples[2*int(.5*48000):2*int(.9*48000)])
        loud = max(abs(value) for value in samples[2*int(1.5*48000):2*int(1.9*48000)])
        self.assertAlmostEqual(20*math.log10(quiet/.003), -24, delta=.2)
        self.assertAlmostEqual(20*math.log10(loud/.3), 0, delta=.2)

    def test_denoise_lowers_hiss_and_keeps_the_dialogue_tone(self):
        source = self.source('noisy.wav', "aevalsrc='0.2*sin(2*PI*700*t)*gt(mod(t,2),1)+0.01*(random(0)-0.5)|"
                             "0.2*sin(2*PI*700*t)*gt(mod(t,2),1)+0.01*(random(1)-0.5)':s=48000", 8)
        media = [dict(id='s', src=self.src(source))]
        plain = pcm(self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=8)], media, name='plain', limiter=False))
        cleaned = pcm(self.render([dict(type='audio', mediaId='s', track=-1, start=0, duration=8,
                                        audioFx={'denoise': {'amount': 20, 'floor': -50}})], media, name='clean', limiter=False))
        rms = lambda samples, a, b: math.sqrt(sum(v*v for v in samples[2*int(a*48000):2*int(b*48000)])/(2*int((b-a)*48000)))
        self.assertLess(20*math.log10(rms(cleaned, 4.2, 4.9)/rms(plain, 4.2, 4.9)), -10, 'hiss between lines')
        self.assertAlmostEqual(tone_level(cleaned, 700, 5.2, 5.8), tone_level(plain, 700, 5.2, 5.8), delta=.2*.12)

    def test_track_compressor_acts_on_the_track_sum_and_master_eq_on_the_mix(self):
        tone = self.source('t1k.wav', "aevalsrc='0.2*sin(2*PI*1000*t)':s=48000", 6, channels=1)
        media = [dict(id='t', src=self.src(tone))]
        clips = [dict(type='audio', mediaId='t', track=-1, start=0, duration=5), dict(type='audio', mediaId='t', track=-1, start=2.5, duration=2.5, **{'in': 1})]
        # Two overlapping clips on one track? Clips on a track do not overlap; use two tracks routed to one stem instead.
        clips[1]['track'] = -2
        comp = {'threshold': -24, 'ratio': 10, 'attack': 1, 'release': 50, 'knee': 0, 'makeup': 0}
        project = {'trackMix': {'-1': {'fx': {'comp': comp}}}, 'masterFx': {'eq': {'bands': [{'type': 'peak', 'freq': 1000, 'q': 1, 'gain': -6}]}}}
        samples = pcm(self.render(clips, media, name='bus', limiter=False, project=project))
        pan = math.cos(math.pi/4)
        master = 10**(-6/20)
        compressed = 10**((-24+(20*math.log10(.2*pan)+24)/10)/20)
        self.assertAlmostEqual(tone_level(samples, 1000, 1, 2), compressed*master, delta=.004)
        # From 2.5 s A2's uncompressed tone adds to it (same phase: its in point is a whole number of cycles).
        self.assertAlmostEqual(tone_level(samples, 1000, 3, 4), (compressed+.2*pan)*master, delta=.006)


if __name__ == '__main__':
    unittest.main()
