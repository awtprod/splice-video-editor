"""Exactness of exported pictures and sound, measured on decoded frames and PCM.

Sources carry their own frame number in six luma blocks, so every exported
frame can be checked against the frame a player shows at the sample time.
"""
from array import array
import bisect
import contextlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
from unittest import mock

from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

W, H = 96, 32
BLOCKS, BASE, STEP, LOW = 6, 8, 30, 16


def ffmpeg(*arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def index_expression():
    return '+'.join(f'if(between(X,{16*b},{16*b+15}),{LOW}+{STEP}*mod(floor(N/{BASE**b}),{BASE}),0)' for b in range(BLOCKS))


def numbered(path, rate, seconds, *, pts=None, codec=('-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p'), extra=()):
    """A source whose frame N shows the number N."""
    graph = f"geq=lum='{index_expression()}':cb=128:cr=128"
    if pts:
        graph += f",setpts='({pts})/TB'"
    ffmpeg('-f', 'lavfi', '-i', f'color=black:s={W}x{H}:r={rate}:d={seconds}', '-vf', graph,
           *(['-fps_mode', 'passthrough'] if pts else []), *codec, *extra, str(path), timeout=300)
    return path


def decode_indices(movie):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie), '-fps_mode', 'passthrough', '-vf', 'extractplanes=y',
                                   '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'], timeout=120)
    size = W*H
    shown = []
    for start in range(0, len(raw)-size+1, size):
        frame = raw[start:start+size]
        value = 0
        for block in range(BLOCKS):
            total = sum(sum(frame[y*W+16*block+4:y*W+16*block+12]) for y in range(8, H-8))
            digit = round((total/(8*(H-16))-LOW)/STEP)
            value += max(0, min(BASE-1, digit))*BASE**block
        shown.append(value)
    return shown


def presentation_times(path):
    """Exact presentation times (pts x time base) from the container start."""
    from fractions import Fraction
    data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                               'frame=best_effort_timestamp:stream=time_base:format=start_time',
                                               '-of', 'json', str(path)], timeout=60))
    base = Fraction(data['streams'][0]['time_base'])
    start = Fraction(data['format'].get('start_time', '0'))
    return [float(int(frame['best_effort_timestamp'])*base-start) for frame in data['frames']]


def on_screen(times, at):
    """Frame a player shows at source time ``at``: the last one starting at or before it."""
    return max(0, bisect.bisect_right(times, at+1e-9)-1)


def frame_bytes(movie, width, height):
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie), '-pix_fmt', 'yuv444p',
                                   '-f', 'rawvideo', 'pipe:1'], timeout=120)
    size = width*height*3
    return [raw[start:start+size] for start in range(0, len(raw)-size+1, size)]


class ExactCase(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))

    def cuts(self, source, fps, cuts, **options):
        """Render (in, frames, reverse) cuts back to back; return [(timeline frame, expected, shown)]."""
        times = presentation_times(source)
        clips, rows, frame = [], [], 0
        for number, (source_in, count, reverse) in enumerate(cuts):
            # One media item per cut: each cut gets its own seeked input, as
            # cuts far apart in a camera file do.
            clips.append(dict(type='video', mediaId=f'm{number}', track=1, start=frame/fps, duration=count/fps,
                              reverse=reverse, **{'in': source_in}))
            for k in range(count):
                # The monitor's rule (engine.js sourceTime).
                at = source_in + ((count-k)/fps - 1e-6 if reverse else k/fps)
                rows.append((frame+k, on_screen(times, at)))
            frame += count
        output = self.directory / 'cuts.mp4'
        media = [dict(id=f'm{number}', src=self.src(source)) for number in range(len(cuts))]
        render_to_file(dict(width=W, height=H, fps=fps, media=media, clips=clips),
                       output, options={'crf': 14, **options})
        shown = decode_indices(output)
        self.assertEqual(len(shown), frame)
        return [(at, expected, shown[at]) for at, expected in rows]

    def assertAllShown(self, rows):
        wrong = [row for row in rows if row[1] != row[2]]
        self.assertEqual(wrong, [], f'{len(wrong)} of {len(rows)} frames differ (timeline frame, expected, shown)')


class FrameSampling(ExactCase):
    def test_reversed_cut_on_source_frames_matches_the_monitor(self):
        # Finding 3: 24p in 24p, in = 389/24, 13 frames reversed shows 401..389.
        source = numbered(self.directory / 'c24.mp4', 24, 20, extra=('-bf', '3'))
        rows = self.cuts(source, 24, [(389/24, 13, True), (389/24, 13, False), (120/24, 5, True)])
        self.assertEqual([row[1] for row in rows[:13]], list(range(401, 388, -1)))
        self.assertAllShown(rows)

    def test_reversed_cuts_on_frame_boundaries_at_other_rates(self):
        source = numbered(self.directory / 'c2997.mp4', '30000/1001', 20, extra=('-bf', '3'))
        rng = random.Random(3)
        cuts = [(rng.randrange(10, 500)*1001/30000, rng.choice((1, 2, 3, 8, 13)), True) for _ in range(12)]
        for fps in (24000/1001, 30000/1001):
            with self.subTest(fps=fps):
                self.assertAllShown(self.cuts(source, fps, cuts))

    def test_coarse_time_base_does_not_move_the_cut(self):
        # Finding 4: FFmpeg rounds the seek offset to the stream time base.
        source = numbered(self.directory / 'tb25.mp4', 25, 20, extra=('-g', '5', '-bf', '0', '-video_track_timescale', '25'))
        cuts = [(k/25 - eps, 2, False) for k in range(10, 400, 23) for eps in (.001, .01, .03)]
        self.assertAllShown(self.cuts(source, 25, cuts))

    def test_variable_frame_rate_with_a_600_tick_time_base(self):
        pattern = [8, 4, 5, 4, 4, 8, 5, 5, 4, 4]  # 1/120 s steps: 1/15, 1/30 and 1/24 s frames
        total = sum(pattern)
        nested = '0'
        for k in range(len(pattern)-1, 0, -1):
            nested = f'if(eq(mod(N,{len(pattern)}),{k}),{sum(pattern[:k])},{nested})'
        source = numbered(self.directory / 'vfr600.mp4', 120, 30, pts=f'(floor(N/{len(pattern)})*{total}+{nested})/120',
                          extra=('-bf', '3', '-video_track_timescale', '600'))
        rng = random.Random(11)
        cuts = [(rng.uniform(1, 120), rng.choice((2, 3, 5)), rng.random() < .3) for _ in range(40)]
        for fps, speed in ((24, 1), (25, 1)):
            with self.subTest(fps=fps):
                self.assertAllShown(self.cuts(source, fps, cuts))

    def test_transport_stream_seek_keeps_the_frame_before_a_keyframe(self):
        # AVCHD-style MPEG-TS seeks by decode time; it needs a wider seek margin.
        source = numbered(self.directory / 'cam.ts', 25, 16, extra=('-g', '5', '-bf', '3'))
        cuts = [(k/25 - eps, 2, False) for k in range(10, 380, 25) for eps in (.001, .03, .045)]
        self.assertAllShown(self.cuts(source, 25, cuts))

    def test_container_start_that_is_not_on_the_picture_time_base(self):
        # Round 2, finding 5: the container starts at the audio's first sample
        # (about 0.011 s), the picture at 0.25 s on a 1/600 time base. Ignoring the
        # container start moved 151 of 2,799 frames in the fix-check.
        plain = numbered(self.directory / 'plain.mp4', 30000/1001, 24, extra=('-g', '12', '-bf', '2'))
        source = self.directory / 'off-start.mov'
        ffmpeg('-itsoffset', '0.25', '-i', str(plain), '-itsoffset', '0.011995', '-f', 'lavfi', '-i', 'sine=r=44100:d=20',
               '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'pcm_s16le', '-video_track_timescale', '600', str(source))
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'stream=start_time:format=start_time',
                                                   '-of', 'json', str(source)], timeout=30))
        start = float(info['format']['start_time'])
        self.assertTrue(.005 < start < .02 and abs(start*600-round(start*600)) > .1, start)
        rng = random.Random(21)
        cuts = [(rng.uniform(.3, 22), rng.choice((2, 3, 4)), rng.random() < .3) for _ in range(60)]
        for fps in (24, 30000/1001, 25):
            with self.subTest(fps=fps):
                self.assertAllShown(self.cuts(source, fps, cuts))

    def test_fractional_clip_start_samples_from_the_clip_clock(self):
        # Timeline frame f shows source in + (f/fps - start): the first frame is
        # the first whole frame at or after the start, sampled 0.6 frame in.
        source = numbered(self.directory / 'c60.mp4', 60, 10)
        times = presentation_times(source)
        start = 10.4/24
        project = dict(width=W, height=H, fps=24, media=[dict(id='m', src=self.src(source))],
                       clips=[dict(type='video', mediaId='m', track=1, start=start, duration=20/24, **{'in': 2.0})])
        output = self.directory / 'frac.mp4'
        render_to_file(project, output, options={'crf': 14})
        shown = decode_indices(output)
        self.assertEqual(shown[10], 0, 'the frame before a fractional start shows the clip')
        for frame in range(11, 31):
            self.assertEqual(shown[frame], on_screen(times, 2.0 + frame/24 - start), f'timeline frame {frame}')

    def test_media_linked_in_place_renders_frame_exact(self):
        import media_links
        outside = tempfile.TemporaryDirectory(dir=MEDIA_ROOT.parent, prefix='camera-card-')
        self.addCleanup(outside.cleanup)
        pattern_pts = '(floor(N/3)*10+if(eq(mod(N,3),1),4,if(eq(mod(N,3),2),7,0)))/120'
        source = numbered(Path(outside.name) / 'A001.mp4', 120, 20, pts=pattern_pts,
                          extra=('-bf', '3', '-video_track_timescale', '120'))
        saved = dict(REGISTRY=media_links.REGISTRY)
        media_links.REGISTRY = Path(outside.name) / 'linked-media.json'
        media_links._cache.update(stamp=None, links={}, recovered=None)

        def restore():
            media_links.REGISTRY = saved['REGISTRY']
            media_links._cache.update(stamp=None, links={}, recovered=None)
        self.addCleanup(restore)
        entry = media_links.link(str(source))
        self.assertTrue(media_links.is_linked(entry['src']))
        times = presentation_times(source)
        output = self.directory / 'linked.mp4'
        clips = [dict(type='video', mediaId='v', track=1, start=0, duration=2, **{'in': 10.0}),
                 dict(type='video', mediaId='v', track=1, start=2, duration=1, reverse=True, **{'in': 3.0})]
        render_to_file(dict(width=W, height=H, fps=25, media=[dict(id='v', src=entry['src'])], clips=clips), output,
                       options={'crf': 14})
        shown = decode_indices(output)
        expected = ([on_screen(times, 10.0 + k/25) for k in range(50)] +
                    [on_screen(times, 3.0 + (25-k)/25 - 1e-6) for k in range(25)])
        self.assertEqual(shown, expected)
        self.assertEqual(list(MEDIA_ROOT.glob('**/A001.mp4')), [], 'linked media must not be copied')


class WindowSeams(ExactCase):
    """A windowed export must equal a single-window export of the same timeline."""
    SIZE = (128, 72)

    def compare(self, clips, fps=24, codec='prores'):
        source = getattr(self, 'source', None)
        if source is None:
            source = self.source = numbered(self.directory / 'seam.mp4', 24, 20)
        project = dict(width=self.SIZE[0], height=self.SIZE[1], fps=fps, media=[dict(id='c', src=self.src(source))],
                       clips=clips)
        results = {}
        for mode, limits in (('windowed', (6, 24, 768*1024*1024)), ('single', (10**6, 10**6, 10**12))):
            output = self.directory / f'{mode}.mov'
            with mock.patch.multiple(render_movie, VIDEO_WINDOW_SECONDS=limits[0], VIDEO_WINDOW_CUTS=limits[1],
                                     REVERSE_BUFFER_BYTES=limits[2]):
                render_to_file(project, output, options={'codec': codec})
            results[mode] = frame_bytes(output, *self.SIZE)
        windowed, single = results['windowed'], results['single']
        self.assertEqual(len(windowed), len(single))
        differing = [number for number, (a, b) in enumerate(zip(windowed, single))
                     if max(abs(x-y) for x, y in zip(a, b)) > 2]
        self.assertEqual(differing, [], 'frames that change with windowing')
        return windowed

    def test_transition_ending_at_the_cut_on_a_window_seam(self):
        # Finding 1: the cut at 6 s is the window seam; "end at cut" alignment.
        base = dict(type='video', mediaId='c', track=1, speed=1)
        for transition in ('dissolve', 'wipe', 'fade'):
            with self.subTest(transition=transition):
                self.compare([dict(base, start=5, duration=1, **{'in': 2}),
                              dict(base, start=6, duration=2, **{'in': 10}, transition=transition,
                                   transitionDuration=.5, transitionBefore=.5)])

    def test_animated_scale_with_opacity_wipe_or_dip(self):
        # Finding 7: the clip crosses the 6 s seam.
        base = dict(type='video', mediaId='c', track=1, start=4, duration=4, **{'in': 2})
        scale = {'scale': [dict(time=0, value=100), dict(time=4, value=90)]}
        cases = {
            'opacity': [dict(base, opacity=60, keyframes=scale)],
            'wipe': [dict(base, start=0, duration=4), dict(base, transition='wipe', transitionDuration=3, keyframes=scale)],
            'dip': [dict(base, start=0, duration=4),
                    dict(base, transition='fade', transitionDuration=3, transitionBefore=1, keyframes=scale)],
        }
        for name, clips in cases.items():
            with self.subTest(name):
                self.compare(clips)

    def test_keyframes_across_a_seam_read_the_clip_clock(self):
        clip = dict(type='video', mediaId='c', track=2, start=3, duration=6, **{'in': 1}, transform=dict(scale=50),
                    keyframes={'opacity': [dict(time=0, value=100), dict(time=6, value=0)],
                               'x': [dict(time=0, value=-40), dict(time=6, value=40)],
                               'y': [dict(time=0, value=-20), dict(time=6, value=20)]})
        under = dict(type='video', mediaId='c', track=1, start=0, duration=10, **{'in': 8})
        self.compare([under, clip])

    def test_clip_overlapping_a_window_by_one_frame(self):
        clips = [dict(type='video', mediaId='c', track=1, start=0, duration=6+1/24, **{'in': 1}),
                 dict(type='video', mediaId='c', track=1, start=6+1/24, duration=1, **{'in': 9}),
                 dict(type='video', mediaId='c', track=2, start=6-1/24, duration=1/24, **{'in': 12}, transform=dict(scale=50))]
        self.compare(clips)

    def test_rotation_keyframes_with_a_lead_in_on_a_seam(self):
        # The lead-in starts on the 6 s seam, so the seam moves to the cut's
        # second frame and the clip's rotation continues in the next window.
        # rotate's c=none left the uncovered corners unwritten: a reused
        # frame buffer showed the previous frame's corner pixels there.
        base = dict(type='video', mediaId='c', track=1, start=6+3/24, duration=3, **{'in': 10},
                    keyframes={'rotation': [dict(time=0, value=20), dict(time=3, value=90)]})
        for transition in ('dissolve', 'wipe', 'fade'):
            with self.subTest(transition=transition):
                self.compare([dict(base, transition=transition, transitionDuration=3/24, transitionBefore=3/24)])

    def test_wipe_is_fully_revealed_on_its_last_frame(self):
        # 4 frames at 24 fps: T reaches 4/24 on the frame after the wipe,
        # which printed to 6 decimals (0.166667) is above T, so the mask
        # still hid the right edge of that frame.
        moving = {'x': [dict(time=0, value=60), dict(time=3, value=10)]}
        clip = dict(type='video', mediaId='c', track=1, start=6+2/24, duration=3, **{'in': 10}, keyframes=moving)
        wiped = self.compare([dict(clip, transition='wipe', transitionDuration=4/24, transitionBefore=2/24)])
        plain = self.compare([clip])
        self.assertEqual(wiped[148:], plain[148:])

    def test_transition_starting_one_frame_after_a_seam(self):
        base = dict(type='video', mediaId='c', track=1)
        for transition in ('dissolve', 'wipe', 'fade'):
            with self.subTest(transition=transition):
                self.compare([dict(base, start=4, duration=2+2/24, **{'in': 2}),
                              dict(base, start=6+2/24, duration=2, **{'in': 10}, transition=transition,
                                   transitionDuration=4/24, transitionBefore=1/24)])


class ChunkSeams(ExactCase):
    """Chunked audio must equal a single-chunk mix of the same timeline."""

    def sweep(self, name='sweep.wav', rate=48000, seconds=40):
        path = self.directory / name
        ffmpeg('-f', 'lavfi', '-i', "aevalsrc='0.5*sin(2*PI*(100*t+(1900/80)*t*t))|0.5*sin(2*PI*(150*t+(1700/80)*t*t))'"
               f':s={rate}:d={seconds}', '-c:a', 'pcm_f32le', str(path))
        return path

    def mix(self, clips, media, chunk=None, events=None, **options):
        output = self.directory / 'mix.wav'
        patches = {}
        if chunk: patches['AUDIO_CHUNK_SECONDS'] = chunk
        if events: patches['AUDIO_CHUNK_EVENTS'] = events
        with mock.patch.multiple(render_movie, **patches) if patches else contextlib.nullcontext():
            render_to_file(dict(width=64, height=36, fps=24, media=media, clips=clips), output,
                           options={'codec': 'wav', **options})
        return array('f', subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output),
                                                   '-f', 'f32le', '-c:a', 'pcm_f32le', 'pipe:1'], timeout=120))

    def assertSameMix(self, chunked, single, tolerance=1e-6):
        self.assertEqual(len(chunked), len(single))
        worst = max(abs(a-b) for a, b in zip(chunked, single))
        self.assertLessEqual(worst, tolerance)

    def test_speed_changed_sweep_across_chunk_seams(self):
        # Finding 2: atempo restarted at every seam (a 0.81 jump on a 0.5 sweep).
        source = self.sweep()
        media = [dict(id='s', src=self.src(source))]
        for speed in (1.5, .7, 2.5):
            for reverse in (False, True):
                with self.subTest(speed=speed, reverse=reverse):
                    clips = [dict(type='audio', mediaId='s', track=1, start=.25, duration=8, speed=speed, reverse=reverse,
                                  **{'in': 2})]
                    single = self.mix(clips, media)
                    chunked = self.mix(clips, media, chunk=1.37)
                    self.assertSameMix(chunked, single)

    def test_event_limit_split_inside_resampled_and_speed_changed_clips(self):
        sweep = self.sweep()
        sweep44 = self.sweep('sweep44.wav', 44100)
        media = [dict(id='s', src=self.src(sweep)), dict(id='t', src=self.src(sweep44))]
        clips = [dict(type='audio', mediaId=('s', 't')[k % 2], track=1+k % 8, start=.137*k, duration=3.3, volume=40,
                      speed=(1, 1, 1.25)[k % 3], **{'in': 1+k*.3}) for k in range(40)]
        self.assertSameMix(self.mix(clips, media, events=8), self.mix(clips, media, events=10**6))

    def test_limiter_state_carries_across_seams(self):
        source = self.sweep()
        media = [dict(id='s', src=self.src(source))]
        clips = [dict(type='audio', mediaId='s', track=k+1, start=0, duration=6, volume=150, **{'in': k}) for k in range(3)]
        self.assertSameMix(self.mix(clips, media, chunk=1.37), self.mix(clips, media))

    def test_chunks_are_a_minute_long(self):
        source = self.sweep(seconds=150)
        calls = []
        real = render_movie._render_audio_chunk

        def counted(*arguments, **keywords):
            calls.append(arguments[3])
            return real(*arguments, **keywords)
        with mock.patch.object(render_movie, '_render_audio_chunk', counted):
            self.mix([dict(type='audio', mediaId='s', track=1, start=0, duration=130, **{'in': 1})],
                     [dict(id='s', src=self.src(source))])
        self.assertEqual(calls, [60*48000, 60*48000, 10*48000])

    def test_slowed_clip_plays_to_its_last_sample(self):
        # Finding 8: atempo dropped its last fragment (845 silent samples at 0.7x).
        source = self.sweep()
        samples = self.mix([dict(type='audio', mediaId='s', track=1, start=.25, duration=6, speed=.7, **{'in': 1})],
                           [dict(id='s', src=self.src(source))])
        left = samples[0::2]
        self.assertEqual(len(left), 300000)
        self.assertGreater(max(abs(value) for value in left[-200:]), .1)

    def test_range_export_has_every_sample(self):
        # Finding 8: -t truncated a range by one sample.
        source = self.sweep()
        fps = 60000/1001
        project = dict(width=64, height=36, fps=fps, media=[dict(id='s', src=self.src(source))],
                       clips=[dict(type='audio', mediaId='s', track=1, start=0, duration=10, **{'in': 1})])
        # 99,299.2 samples long; the sample grid gives it 99,300 (a -t of the duration truncated to 99,299).
        first, stop = 3, 127
        expected = round(stop*48000/fps)-round(first*48000/fps)
        for codec, suffix in (('wav', '.wav'), ('prores', '.mov')):
            with self.subTest(codec=codec):
                output = self.directory / f'range{suffix}'
                render_to_file(project, output, options={'codec': codec, 'range': {'start': first/fps, 'end': stop/fps}})
                samples = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-vn',
                                                   '-f', 's32le', '-ac', '1', 'pipe:1'], timeout=60)
                self.assertEqual(len(samples)//4, expected)


if __name__ == '__main__':
    unittest.main()
