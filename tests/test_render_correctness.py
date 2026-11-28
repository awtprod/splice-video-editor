"""Renderer correctness checks measured on decoded pixels, PCM samples and ffprobe output."""
from array import array
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest

from premiere import MEDIA_ROOT
from render_movie import render_to_file


def ffmpeg(*arguments, timeout=60):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


class RenderCase(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=MEDIA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.output = self.directory / 'export.mp4'

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))

    def rgb(self, movie, time, size=(64, 64)):
        """Mean (R, G, B) of the exported frame at ``time``."""
        raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-ss', f'{time:.6f}', '-i', str(movie),
                                       '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'], timeout=30)
        pixels = size[0]*size[1]
        self.assertEqual(len(raw), pixels*3)
        return tuple(sum(raw[channel::3])/pixels for channel in range(3))

    def frames(self, movie, pixel_format='gray'):
        """Every decoded frame of ``movie`` as bytes, in order. 'gray' is the coded luma plane."""
        convert = ['-vf', 'extractplanes=y'] if pixel_format == 'gray' else []
        return subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie), *convert,
                                        '-pix_fmt', pixel_format, '-f', 'rawvideo', 'pipe:1'], timeout=60)

    def pcm(self, movie, start=0, duration=None):
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(start), '-i', str(movie)]
        if duration: command += ['-t', str(duration)]
        return array('f', subprocess.check_output([*command, '-vn', '-f', 'f32le', '-ac', '2', 'pipe:1'], timeout=60))

    def probe(self, movie, entries):
        return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', entries, '-of', 'json',
                                                   str(movie)], timeout=30))


class PictureSampling(RenderCase):
    def test_variable_frame_rate_cut_shows_the_frame_on_screen_at_its_in_point(self):
        # Red is presented from 0.000 s; the first blue frame starts at 0.520 s.
        source = self.directory / 'vfr.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=black:s=64x64:r=25:d=1.2',
               '-vf', "geq=r='if(eq(N\\,0)\\,255\\,0)':g=0:b='if(eq(N\\,0)\\,0\\,255)',"
                      "setpts='if(eq(N,0),0,(0.48+N*0.04)/TB)'",
               '-fps_mode', 'passthrough', '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p',
               '-video_track_timescale', '1000', str(source))
        times = [float(row['pts_time']) for row in self.probe(source, 'frame=pts_time')['frames']]
        self.assertEqual(times[:2], [0.0, 0.52])
        project = dict(width=64, height=64, fps=25, media=[dict(id='vfr', src=self.src(source))],
                       clips=[dict(type='video', mediaId='vfr', track=1, start=0, duration=.16, **{'in': .2})])
        render_to_file(project, self.output)
        raw = self.frames(self.output, 'rgb24')
        frame = 64*64*3
        self.assertEqual(len(raw), 4*frame)
        for number in range(4):
            red, _, blue = (sum(raw[number*frame+channel:(number+1)*frame:3])/(64*64) for channel in range(3))
            self.assertGreater(red, 200, f'frame {number} is not the red frame shown at its source time')
            self.assertLess(blue, 60)

    def test_25p_clip_in_23976_sequence_uses_the_frame_a_player_shows(self):
        # Each source frame carries its own number in two luma blocks.
        source = self.directory / 'numbered.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=black:s=64x32:r=25:d=40',
               '-vf', "geq=lum='if(lt(X,32),16+12*mod(N,16),16+12*mod(floor(N/16),16))':cb=128:cr=128",
               '-c:v', 'libx264', '-qp', '0', '-g', '50', str(source), timeout=120)
        fps = 24000/1001
        source_in = 1.3
        project = dict(width=64, height=32, fps=fps, media=[dict(id='n', src=self.src(source))],
                       clips=[dict(type='video', mediaId='n', track=1, start=0, duration=30, **{'in': source_in})])
        render_to_file(project, self.output, options={'range': {'start': 595/fps, 'end': 605/fps}})
        raw = self.frames(self.output)
        size = 64*32
        self.assertEqual(len(raw), 10*size)
        for offset in range(10):
            timeline_frame = 595+offset
            image = raw[offset*size:(offset+1)*size]
            low = sum(image[y*64+x] for y in range(8, 24) for x in range(8, 24))/256
            high = sum(image[y*64+x] for y in range(8, 24) for x in range(40, 56))/256
            shown = round((low-16)/12) + 16*round((high-16)/12)
            # A player shows the last source frame whose start is at or before t.
            expected = math.floor((source_in + timeline_frame/fps)*25 + 1e-6)
            self.assertEqual(shown % 256, expected % 256, f'timeline frame {timeline_frame}')


class ReversedSeam(RenderCase):
    def setUp(self):
        super().setUp()
        self.ramp = self.directory / 'ramp.mp4'
        # Luma rises 20 code values per second of source time.
        ffmpeg('-f', 'lavfi', '-i', 'color=black:s=64x64:r=25:d=10',
               '-vf', "geq=lum='16+20*T':cb=128:cr=128", '-c:v', 'libx264', '-qp', '0', str(self.ramp), timeout=120)
        self.clip = dict(type='video', mediaId='ramp', track=1, start=4.0, duration=8.0, reverse=True,
                         transition='dissolve', transitionDuration=1.0, transitionBefore=.5, **{'in': 1.0})
        self.project = dict(width=64, height=64, fps=25, media=[dict(id='ramp', src=self.src(self.ramp))],
                            clips=[self.clip])

    def luma(self, time):
        return self.rgb(self.output, time)[1]

    def expected(self, time):
        # Reversed playback over source 1.0..9.5 s, starting at 3.5 s on the timeline.
        source = 1.0 + (8.0+.5) - (time-3.5) - 1e-6
        # Video-range luma 16+20*t, decoded to full-range RGB.
        return 20*math.floor(source*25)/25*255/219

    def test_reversed_clip_with_centered_transition_spans_window_seams(self):
        render_to_file(self.project, self.output)
        for time in (5.0, 5.5, 5.96, 6.0, 6.04, 8.0, 11.0):
            with self.subTest(time=time):
                self.assertAlmostEqual(self.luma(time), self.expected(time), delta=4)

    def test_reversed_clip_keyframes_past_the_seam_are_sliced(self):
        self.clip['keyframes'] = {'opacity': [{'time': 0, 'value': 100}, {'time': 7.0, 'value': 100}]}
        render_to_file(self.project, self.output)
        self.assertAlmostEqual(self.luma(5.5), self.expected(5.5), delta=4)


class Limiter(RenderCase):
    def render(self, amplitude, **options):
        source = self.directory / f'sine-{amplitude}.wav'
        ffmpeg('-f', 'lavfi', '-i', f'aevalsrc={amplitude}*sin(2*PI*997*t)|{amplitude}*sin(2*PI*997*t):s=48000:d=2',
               '-c:a', 'pcm_f32le', str(source))
        project = dict(width=64, height=64, fps=25, media=[dict(id='tone', src=self.src(source))],
                       clips=[dict(type='audio', mediaId='tone', track=-1, start=0, duration=2)])
        output = self.directory / 'mix.wav'
        render_to_file(project, output, options={'codec': 'wav', **options})
        return self.pcm(output, .5, 1), self.pcm(source, .5, 1)

    def test_limiter_off_passes_full_scale_program(self):
        mixed, original = self.render(1.0, limiter=False)
        self.assertGreater(max(abs(sample) for sample in mixed), .9999)
        self.assertLess(max(abs(a-b) for a, b in zip(mixed, original)), 1e-5)

    def test_default_limiter_holds_minus_one_dbfs_ceiling(self):
        mixed, _ = self.render(1.0)
        self.assertLess(max(abs(sample) for sample in mixed), 10**(-1/20)+1e-4)
        self.assertGreater(max(abs(sample) for sample in mixed), 10**(-1/20)-.01)

    def test_program_below_the_ceiling_passes_unchanged(self):
        mixed, original = self.render(.5)
        self.assertEqual(len(mixed), len(original))
        self.assertLess(max(abs(a-b) for a, b in zip(mixed, original)), 1e-5)

    def test_chunked_mix_is_seamless_across_chunk_boundaries(self):
        from unittest import mock
        import render_movie
        source = self.directory / 'long.wav'
        ffmpeg('-f', 'lavfi', '-i', 'aevalsrc=0.5*sin(2*PI*997*t)*(1+0.5*sin(2*PI*3*t))/1.5|0.5*sin(2*PI*451*t):s=48000:d=4',
               '-c:a', 'pcm_f32le', str(source))
        original = self.pcm(source)
        output = self.directory / 'mix.wav'
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                project = dict(width=64, height=64, fps=25, media=[dict(id='tone', src=self.src(source))],
                               clips=[dict(type='audio', mediaId='tone', track=-1, start=.5, duration=3, reverse=reverse,
                                           **{'in': .5})])
                with mock.patch.object(render_movie, 'AUDIO_CHUNK_SECONDS', 1):
                    render_to_file(project, output, options={'codec': 'wav'})
                mixed = self.pcm(output)
                self.assertEqual(len(mixed), 2*48000*round(3.5*25)//25)
                frames = 3*48000
                for frame in range(0, frames, 7):
                    expected = (2*(24000+frames-1-frame) if reverse else 2*(24000+frame))
                    for channel in (0, 1):
                        self.assertAlmostEqual(mixed[2*(24000+frame)+channel], original[expected+channel], delta=2e-4,
                                               msg=f'sample {frame} channel {channel}')

    def test_ceiling_is_an_export_option(self):
        mixed, _ = self.render(1.0, limiterCeiling=-6)
        self.assertLess(max(abs(sample) for sample in mixed), 10**(-6/20)+1e-4)
        with self.assertRaisesRegex(ValueError, 'limiter ceiling'):
            self.render(1.0, limiterCeiling=3)


class Delivery(RenderCase):
    def untagged(self, name, color):
        source = self.directory / name
        ffmpeg('-f', 'lavfi', '-i', f'color={color}:s=64x64:r=25:d=1', '-c:v', 'libx264', '-qp', '0', str(source))
        stream = self.probe(source, 'stream=color_primaries,color_transfer,color_space')['streams'][0]
        self.assertNotIn('color_primaries', stream)
        return source

    def test_sdr_exports_are_tagged_rec709_when_sources_are_untagged(self):
        source = self.untagged('plain.mp4', 'gray')
        tagged = self.directory / 'tagged.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=gray:s=64x64:r=25:d=1', '-c:v', 'libx264', '-qp', '0',
               '-color_primaries', 'smpte170m', '-color_trc', 'smpte170m', '-colorspace', 'smpte170m', str(tagged))
        project = dict(width=64, height=64, fps=25, media=[dict(id='a', src=self.src(source)), dict(id='b', src=self.src(tagged))],
                       clips=[dict(type='video', mediaId='a', track=1, start=0, duration=.4)])
        rec709 = dict(color_primaries='bt709', color_transfer='bt709', color_space='bt709')
        for codec, suffix in (('h264', '.mp4'), ('prores', '.mov')):
            for clips in (project['clips'], [*project['clips'], dict(type='video', mediaId='b', track=1, start=.4, duration=.4)]):
                with self.subTest(codec=codec, sources=len(clips)):
                    output = self.directory / f'sdr{suffix}'
                    render_to_file({**project, 'clips': clips}, output, options={'codec': codec})
                    stream = self.probe(output, 'stream=color_primaries,color_transfer,color_space,color_range')['streams'][0]
                    self.assertEqual({key: stream.get(key) for key in rec709}, rec709)
                    frame = self.probe(output, 'frame=color_range,color_primaries')['frames'][0]
                    self.assertEqual((frame['color_range'], frame['color_primaries']), ('tv', 'bt709'))
                    if codec == 'h264':
                        self.assertEqual(stream['color_range'], 'tv')

    def test_subtitle_language_comes_from_the_export_options(self):
        source = self.untagged('plain.mp4', 'gray')
        project = dict(width=64, height=64, fps=25, media=[dict(id='a', src=self.src(source))],
                       clips=[dict(type='video', mediaId='a', track=1, start=0, duration=1)],
                       captions=[dict(start=.1, duration=.5, text='Hola')])
        for options, language in (({}, 'und'), ({'subtitleLanguage': 'spa'}, 'spa')):
            with self.subTest(language=language):
                render_to_file(project, self.output, options=options)
                streams = self.probe(self.output, 'stream=codec_type:stream_tags=language')['streams']
                subtitle = next(stream for stream in streams if stream['codec_type'] == 'subtitle')
                self.assertEqual(subtitle['tags']['language'], language)
        with self.assertRaisesRegex(ValueError, 'subtitle language'):
            render_to_file(project, self.output, options={'subtitleLanguage': 'english'})


class TrackVisibility(RenderCase):
    def test_hidden_tracks_contribute_no_picture_but_keep_their_audio(self):
        red = self.directory / 'red.mp4'
        blue = self.directory / 'blue.mp4'
        ffmpeg('-f', 'lavfi', '-i', 'color=red:s=64x64:r=25:d=1', '-c:v', 'libx264', '-qp', '0', str(red))
        ffmpeg('-f', 'lavfi', '-i', 'color=blue:s=64x64:r=25:d=1', '-f', 'lavfi', '-i', 'sine=frequency=500:sample_rate=48000:d=1',
               '-c:v', 'libx264', '-qp', '0', '-c:a', 'pcm_s16le', '-shortest', str(blue.with_suffix('.mov')))
        blue = blue.with_suffix('.mov')
        project = dict(width=64, height=64, fps=25,
                       media=[dict(id='red', src=self.src(red)), dict(id='blue', src=self.src(blue))],
                       clips=[dict(type='video', mediaId='red', track=1, start=0, duration=1),
                              dict(type='video', mediaId='blue', track=2, start=0, duration=1),
                              dict(type='title', track=4, start=0, duration=1, title='MMMMMMMM',
                                   titleStyle={'color': '#00ff00', 'fontSize': 40, 'y': 50})])
        render_to_file(project, self.output)
        _, green, blue_level = self.rgb(self.output, .5)
        self.assertGreater(blue_level, 60)
        self.assertGreater(green, 10)
        project['trackState'] = {'video:2': {'hidden': True}, 'title:4': {'hidden': True, 'locked': False}}
        render_to_file(project, self.output)
        red_level, green, blue_level = self.rgb(self.output, .5)
        self.assertGreater(red_level, 200)
        self.assertLess(green, 3)
        self.assertLess(blue_level, 30)
        samples = self.pcm(self.output, .2, .5)
        self.assertGreater(max(abs(sample) for sample in samples), .05, 'hiding picture must not mute the clip audio')
        project['trackState'] = {'video:2': {'hidden': 'yes'}}
        with self.assertRaisesRegex(ValueError, 'track visibility'):
            render_to_file(project, self.output)


if __name__ == '__main__':
    unittest.main()
