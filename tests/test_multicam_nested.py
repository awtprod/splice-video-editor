"""Export, interchange, and Resolve parity for native nested sequences, multicam clips, and subclips.

Every video/audio assertion decodes the real FFmpeg output into RGB frames and float PCM samples
and checks exact colors and tone frequencies, including direct equality between un-flattened and
flattened multicam exports.
"""
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from interchange import cut as interchange_cut  # noqa: E402
from premiere import MEDIA_ROOT, open_project, save_project  # noqa: E402
from render_movie import render_to_file  # noqa: E402
from test_render_audio_mix import AMPLITUDE, left_channel, tone_level  # noqa: E402

W, H, FPS = 64, 36, 25
HAVE_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


def ffmpeg(*arguments, timeout=120):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=timeout)


def frames_rgb(movie):
    raw = subprocess.check_output(
        ['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie), '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
        timeout=120,
    )
    size = W * H * 3
    return [raw[start:start + size] for start in range(0, len(raw) - size + 1, size)]


def mean_rgb(frame):
    pixels = [frame[(y * W + x) * 3:(y * W + x) * 3 + 3] for y in range(4, H - 4) for x in range(4, W - 4)]
    return tuple(round(sum(pixel[channel] for pixel in pixels) / len(pixels)) for channel in range(3))


def dominant_channel(frame):
    r, g, b = mean_rgb(frame)
    if max(r, g, b) < 25:
        return 'black'
    if r > 180 and g < 60 and b < 60:
        return 'red'
    if g > 180 and r < 60 and b < 60:
        return 'green'
    if b > 180 and r < 60 and g < 60:
        return 'blue'
    return f'other({r},{g},{b})'


@unittest.skipUnless(HAVE_FFMPEG, 'needs FFmpeg to generate and decode test media')
class MulticamNestedExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='splice-mn-', dir=MEDIA_ROOT)
        cls.directory = Path(cls.temp.name)
        cls.media = [
            dict(id=name, name=f'{name}.mov', type='video', duration=8.0, fps=FPS, audioChannels=2,
                 src=cls._make_camera(name, color, frequency, duration=8.0))
            for name, color, frequency in (('red', 'red', 375), ('green', 'lime', 625), ('blue', 'blue', 875))
        ]
        # A 6-second multi-segment clip: 0..2s red/375Hz, 2..4s green/625Hz, 4..6s blue/875Hz.
        cls.segmented = dict(
            id='segmented', name='segmented.mov', type='video', duration=6.0, fps=FPS, audioChannels=2,
            src=cls._make_segmented(),
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def _make_camera(cls, name, color, frequency, duration=8.0):
        path = cls.directory / f'{name}.mov'
        sound = f"aevalsrc='{AMPLITUDE}*sin(2*PI*{frequency}*t)|{AMPLITUDE}*sin(2*PI*{frequency}*t)':s=48000:d={duration}"
        ffmpeg(
            '-f', 'lavfi', '-i', f'color={color}:s={W}x{H}:r={FPS}:d={duration}',
            '-f', 'lavfi', '-i', sound,
            '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p',
            '-c:a', 'pcm_f32le', '-shortest', str(path),
        )
        return '/media/' + str(path.relative_to(MEDIA_ROOT))

    @classmethod
    def _make_segmented(cls):
        parts = []
        for idx, (color, freq) in enumerate((('red', 375), ('lime', 625), ('blue', 875))):
            part = cls.directory / f'seg-{idx}.mov'
            sound = f"aevalsrc='{AMPLITUDE}*sin(2*PI*{freq}*t)|{AMPLITUDE}*sin(2*PI*{freq}*t)':s=48000:d=2"
            ffmpeg(
                '-f', 'lavfi', '-i', f'color={color}:s={W}x{H}:r={FPS}:d=2',
                '-f', 'lavfi', '-i', sound,
                '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p',
                '-c:a', 'pcm_f32le', '-shortest', str(part),
            )
            parts.append(part)
        manifest = cls.directory / 'seg-list.txt'
        manifest.write_text(''.join(f"file '{p.name}'\n" for p in parts), encoding='utf-8')
        out = cls.directory / 'segmented.mov'
        ffmpeg('-f', 'concat', '-safe', '0', '-i', str(manifest), '-c', 'copy', str(out))
        return '/media/' + str(out.relative_to(MEDIA_ROOT))

    def _render(self, project, name, codec='prores'):
        out = self.directory / f'{name}.{"wav" if codec == "wav" else "mov"}'
        render_to_file(project, out, options={'codec': codec, 'limiter': False})
        return out

    def _vclip(self, cid, media_id, start, duration, src_in=0.0, track=1, **extra):
        return dict(id=cid, type='video', mediaId=media_id, track=track, start=start, duration=duration,
                    **{'in': src_in}, **extra)

    # -- 1. Native Nested Sequences --------------------------------------------------------------

    def test_native_nested_sequence_trim_edit_and_disabled_clip(self):
        child = dict(
            name='Nested Sequence 1', width=W, height=H, fps=FPS,
            media=copy.deepcopy(self.media),
            clips=[
                self._vclip('c1', 'red', 0.0, 1.0),
                self._vclip('c2', 'green', 1.0, 1.0),
                self._vclip('c3', 'blue', 2.0, 1.0),
                # Disabled V2 overlay inside nest must not appear in video or audio.
                self._vclip('c4', 'blue', 0.0, 2.0, track=2, enabled=False),
            ],
        )
        nest_media = dict(
            id='nest-m1', name='Nested Sequence 1', type='video', src='', duration=3.0, fps=FPS,
            audioChannels=2, nestedSequence='nest-1', nestedProject=child,
        )
        # Outer clip trims 0.4s from head and lasts 1.6s -> covers child 0.4..2.0s (0.6s red + 1.0s green).
        parent = dict(
            name='Main', width=W, height=H, fps=FPS,
            media=[*copy.deepcopy(self.media), nest_media],
            clips=[self._vclip('n1', 'nest-m1', 0.0, 1.6, src_in=0.4, nestedSequence='nest-1')],
        )
        movie = self._render(parent, 'nest-trimmed')
        shown = frames_rgb(movie)
        self.assertEqual(len(shown), 40)
        # Frames 0..14 (0.0..0.6s) are red; frames 15..39 (0.6..1.6s) are green.
        self.assertEqual({dominant_channel(f) for f in shown[:15]}, {'red'})
        self.assertEqual({dominant_channel(f) for f in shown[15:]}, {'green'})
        sound = left_channel(movie)
        self.assertAlmostEqual(tone_level(sound, 375, 0.05, 0.55), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(sound, 875, 0.05, 0.55), 0.005)
        self.assertAlmostEqual(tone_level(sound, 625, 0.65, 1.55), AMPLITUDE, delta=0.01)

        # Edit inside the nest: swap green clip c2 to blue -> re-render reflects the edit!
        child['clips'][1]['mediaId'] = 'blue'
        edited_movie = self._render(parent, 'nest-edited')
        edited_frames = frames_rgb(edited_movie)
        self.assertEqual({dominant_channel(f) for f in edited_frames[:15]}, {'red'})
        self.assertEqual({dominant_channel(f) for f in edited_frames[15:]}, {'blue'})
        edited_sound = left_channel(edited_movie)
        self.assertAlmostEqual(tone_level(edited_sound, 875, 0.65, 1.55), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(edited_sound, 625, 0.65, 1.55), 0.005)

    def test_shortened_nest_keeps_black_and_silence_through_parent_out(self):
        child = dict(name='Short', width=W, height=H, fps=FPS, media=self.media,
                     clips=[self._vclip('inside', 'red', 0, 1)])
        nest = dict(id='nest', name='Short', type='video', duration=2, nestedSequence='s', nestedProject=child)
        parent = dict(name='Main', width=W, height=H, fps=FPS, media=[nest],
                      clips=[self._vclip('outside', 'nest', 0, 2)])
        for has_picture in (True, False):
            if not has_picture:
                child['clips'] = []
            movie = self._render(parent, f'shortened-{has_picture}')
            frames = frames_rgb(movie)
            self.assertEqual(len(frames), 2 * FPS)
            self.assertEqual(dominant_channel(frames[5]), 'red' if has_picture else 'black')
            self.assertEqual(dominant_channel(frames[-1]), 'black')
            self.assertLess(max(abs(x) for x in left_channel(movie)[60_000:85_000]), .001)

    def test_depth_two_nesting_and_depth_limit_and_cycle_guard(self):
        # Depth 2: root -> level1 -> level2 (green 1.0s).
        level2 = dict(
            name='Level 2', width=W, height=H, fps=FPS, media=copy.deepcopy(self.media),
            clips=[self._vclip('l2', 'green', 0.0, 1.0)],
        )
        l2_media = dict(id='m-l2', name='Level 2', type='video', src='', duration=1.0, fps=FPS,
                        nestedSequence='nest-l2', nestedProject=level2)
        level1 = dict(
            name='Level 1', width=W, height=H, fps=FPS, media=[*copy.deepcopy(self.media), l2_media],
            clips=[self._vclip('l1', 'm-l2', 0.0, 1.0, nestedSequence='nest-l2')],
        )
        l1_media = dict(id='m-l1', name='Level 1', type='video', src='', duration=1.0, fps=FPS,
                        nestedSequence='nest-l1', nestedProject=level1)
        root = dict(
            name='Root', width=W, height=H, fps=FPS, media=[*copy.deepcopy(self.media), l1_media],
            clips=[self._vclip('r1', 'm-l1', 0.0, 1.0, nestedSequence='nest-l1')],
        )
        movie = self._render(root, 'nest-depth2')
        shown = frames_rgb(movie)
        self.assertEqual(len(shown), 25)
        self.assertEqual({dominant_channel(f) for f in shown}, {'green'})
        self.assertAlmostEqual(tone_level(left_channel(movie), 625, 0.1, 0.9), AMPLITUDE, delta=0.01)

        # Depth 5 (exceeds 4 levels) must be refused cleanly.
        deep = dict(name='Leaf', width=W, height=H, fps=FPS, media=copy.deepcopy(self.media),
                    clips=[self._vclip('leaf', 'red', 0.0, 0.4)])
        for level in range(5):
            mid = f'deep-m-{level}'
            sid = f'nest-deep-{level}'
            deep = dict(
                name=f'Deep {level}', width=W, height=H, fps=FPS,
                media=[dict(id=mid, name=f'Deep {level}', type='video', src='', duration=0.4, fps=FPS,
                            nestedSequence=sid, nestedProject=deep)],
                clips=[self._vclip(f'c-{level}', mid, 0.0, 0.4, nestedSequence=sid)],
            )
        with self.assertRaisesRegex(ValueError, 'four levels'):
            self._render(deep, 'nest-too-deep')

        # Circular nested sequence ID must be refused cleanly.
        inner_cycle = dict(
            name='Cycle Inner', width=W, height=H, fps=FPS,
            media=[dict(id='m-cyc', name='Cycle', type='video', src='', duration=0.4, fps=FPS,
                        nestedSequence='nest-loop',
                        nestedProject=dict(name='Leaf', width=W, height=H, fps=FPS, media=copy.deepcopy(self.media),
                                           clips=[self._vclip('x', 'red', 0.0, 0.4)]))],
            clips=[self._vclip('c-in', 'm-cyc', 0.0, 0.4, nestedSequence='nest-loop')],
        )
        outer_cycle = dict(
            name='Cycle Outer', width=W, height=H, fps=FPS,
            media=[dict(id='m-out', name='Cycle', type='video', src='', duration=0.4, fps=FPS,
                        nestedSequence='nest-loop', nestedProject=inner_cycle)],
            clips=[self._vclip('c-out', 'm-out', 0.0, 0.4, nestedSequence='nest-loop')],
        )
        with self.assertRaisesRegex(ValueError, 'Circular'):
            self._render(outer_cycle, 'nest-cycle')

    # -- 2. Multicam Export Parity ---------------------------------------------------------------

    def test_multicam_outlasts_selected_angle(self):
        angles = [dict(mediaId='red', offset=0, duration=2), dict(mediaId='blue', offset=0, duration=8)]
        multicam = dict(id='mc', type='video', duration=8, multicam=dict(angles=angles, audioMode='follow'))
        for source_in in (0, 2):
            with self.subTest(source_in=source_in):
                project = dict(width=W, height=H, fps=FPS, media=[*self.media, multicam],
                               clips=[self._vclip('cut', 'mc', 0, 4, src_in=source_in)])
                movie = self._render(project, f'short-angle-{source_in}')
                frames = frames_rgb(movie)
                sound = left_channel(movie)
                self.assertEqual(len(frames), 4 * FPS)
                self.assertEqual(len(sound), 4 * 48000)
                self.assertEqual(dominant_channel(frames[0]), 'red' if source_in == 0 else 'black')
                if source_in == 0:
                    self.assertAlmostEqual(tone_level(sound, 375, .1, 1.9), AMPLITUDE, delta=.01)
                silent_from = 2 if source_in == 0 else 0
                self.assertEqual({dominant_channel(f) for f in frames[silent_from * FPS:]}, {'black'})
                self.assertLess(max(abs(s) for s in sound[silent_from * 48000:]), .001)

    def test_multicam_follow_vs_camera1_and_parity_with_flattened(self):
        angles = [
            dict(mediaId='red', name='Cam A', offset=0.0, duration=8.0),
            dict(mediaId='green', name='Cam B', offset=0.0, duration=8.0),
            dict(mediaId='blue', name='Cam C', offset=0.0, duration=8.0),
        ]

        def mc_project(audio_mode, audio_angle=0):
            mc_item = dict(
                id='mc-1', name='Multicam 1', type='video', src='', duration=8.0, fps=FPS, audioChannels=2,
                multicam=dict(angles=copy.deepcopy(angles), syncMethod='start',
                              audioMode=audio_mode, audioAngle=audio_angle),
            )
            return dict(
                name='MC Edit', width=W, height=H, fps=FPS,
                media=[*copy.deepcopy(self.media), mc_item],
                clips=[
                    self._vclip('s1', 'mc-1', 0.0, 0.8, src_in=0.0, multicamAngle=0),
                    self._vclip('s2', 'mc-1', 0.8, 0.8, src_in=0.8, multicamAngle=1),
                    self._vclip('s3', 'mc-1', 1.6, 0.8, src_in=1.6, multicamAngle=2),
                ],
            )

        # (a) Audio follows active angle: red/375Hz -> green/625Hz -> blue/875Hz.
        follow_movie = self._render(mc_project('follow'), 'mc-follow')
        follow_frames = frames_rgb(follow_movie)
        self.assertEqual(len(follow_frames), 60)
        self.assertEqual({dominant_channel(f) for f in follow_frames[:20]}, {'red'})
        self.assertEqual({dominant_channel(f) for f in follow_frames[20:40]}, {'green'})
        self.assertEqual({dominant_channel(f) for f in follow_frames[40:60]}, {'blue'})
        follow_pcm = left_channel(follow_movie)
        self.assertAlmostEqual(tone_level(follow_pcm, 375, 0.05, 0.75), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(follow_pcm, 625, 0.05, 0.75), 0.005)
        self.assertAlmostEqual(tone_level(follow_pcm, 625, 0.85, 1.55), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(follow_pcm, 875, 0.85, 1.55), 0.005)
        self.assertAlmostEqual(tone_level(follow_pcm, 875, 1.65, 2.35), AMPLITUDE, delta=0.01)

        # Must match the flattened plain timeline frame-for-frame and tone-for-tone!
        flattened_follow = dict(
            name='MC Flat Follow', width=W, height=H, fps=FPS, media=copy.deepcopy(self.media),
            clips=[
                self._vclip('f1', 'red', 0.0, 0.8, src_in=0.0),
                self._vclip('f2', 'green', 0.8, 0.8, src_in=0.8),
                self._vclip('f3', 'blue', 1.6, 0.8, src_in=1.6),
            ],
        )
        flat_follow_movie = self._render(flattened_follow, 'mc-flat-follow')
        self.assertEqual(follow_frames, frames_rgb(flat_follow_movie))
        flat_pcm = left_channel(flat_follow_movie)
        self.assertEqual(len(follow_pcm), len(flat_pcm))
        max_diff = max(abs(a - b) for a, b in zip(follow_pcm, flat_pcm))
        self.assertLess(max_diff, 1e-4)

        # (b) Camera 1 audio mode: video switches red -> green -> blue, audio stays 375 Hz!
        cam1_movie = self._render(mc_project('camera1'), 'mc-cam1')
        cam1_frames = frames_rgb(cam1_movie)
        self.assertEqual(cam1_frames, follow_frames)
        cam1_pcm = left_channel(cam1_movie)
        self.assertAlmostEqual(tone_level(cam1_pcm, 375, 0.05, 2.35), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(cam1_pcm, 625, 0.85, 1.55), 0.005)
        self.assertLess(tone_level(cam1_pcm, 875, 1.65, 2.35), 0.005)

        # Flattened Camera 1 parity check!
        flattened_cam1 = dict(
            name='MC Flat Cam1', width=W, height=H, fps=FPS, media=copy.deepcopy(self.media),
            clips=[
                self._vclip('f1', 'red', 0.0, 0.8, src_in=0.0, embeddedAudio=False),
                self._vclip('f2', 'green', 0.8, 0.8, src_in=0.8, embeddedAudio=False),
                self._vclip('f3', 'blue', 1.6, 0.8, src_in=1.6, embeddedAudio=False),
                dict(id='a1', type='audio', mediaId='red', track=-1, start=0.0, duration=2.4, **{'in': 0.0}),
            ],
        )
        flat_cam1_movie = self._render(flattened_cam1, 'mc-flat-cam1')
        self.assertEqual(cam1_frames, frames_rgb(flat_cam1_movie))
        flat_cam1_pcm = left_channel(flat_cam1_movie)
        self.assertLess(max(abs(a - b) for a, b in zip(cam1_pcm, flat_cam1_pcm)), 1e-4)

        # (c) Fixed angle audio mode (Cam C / 875 Hz).
        fixed_pcm = left_channel(self._render(mc_project('fixed', audio_angle=2), 'mc-fixed'))
        self.assertAlmostEqual(tone_level(fixed_pcm, 875, 0.05, 2.35), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(fixed_pcm, 375, 0.05, 2.35), 0.005)

        # (d) All angles audio mode: all 3 tones audible simultaneously.
        all_pcm = left_channel(self._render(mc_project('all'), 'mc-all'))
        for freq in (375, 625, 875):
            self.assertGreater(tone_level(all_pcm, freq, 0.1, 2.3), AMPLITUDE * 0.5)

    def test_multicam_timecode_sync_offsets_and_validation(self):
        # Segmented clip is 0..2s red/375, 2..4s green/625, 4..6s blue/875.
        # Angle 0 starts at offset 0.0; Angle 1 (also segmented) has TC offset 2.0s
        # so at multicam time t=2.0..4.0s:
        #   - Angle 0 reads source 2.0..4.0s -> GREEN / 625 Hz
        #   - Angle 1 reads source 0.0..2.0s -> RED / 375 Hz
        mc_item = dict(
            id='mc-tc', name='TC Synced', type='video', src='', duration=8.0, fps=FPS, audioChannels=2,
            multicam=dict(
                syncMethod='timecode', audioMode='follow', audioAngle=0,
                angles=[
                    dict(mediaId='segmented', name='Cam 1', offset=0.0, duration=6.0),
                    dict(mediaId='segmented', name='Cam 2', offset=2.0, duration=6.0),
                ],
            ),
        )
        proj = dict(
            name='MC TC', width=W, height=H, fps=FPS,
            media=[copy.deepcopy(self.segmented), mc_item],
            clips=[
                # 0.0..0.8s on timeline = mc time 2.0..2.8s on Angle 0 -> GREEN/625Hz
                self._vclip('c1', 'mc-tc', 0.0, 0.8, src_in=2.0, multicamAngle=0),
                # 0.8..1.6s on timeline = mc time 2.8..3.6s on Angle 1 -> source 0.8..1.6s -> RED/375Hz
                self._vclip('c2', 'mc-tc', 0.8, 0.8, src_in=2.8, multicamAngle=1),
            ],
        )
        movie = self._render(proj, 'mc-tc')
        shown = frames_rgb(movie)
        self.assertEqual(len(shown), 40)
        self.assertEqual({dominant_channel(f) for f in shown[:20]}, {'green'})
        self.assertEqual({dominant_channel(f) for f in shown[20:]}, {'red'})
        pcm = left_channel(movie)
        self.assertAlmostEqual(tone_level(pcm, 625, 0.05, 0.75), AMPLITUDE, delta=0.01)
        self.assertAlmostEqual(tone_level(pcm, 375, 0.85, 1.55), AMPLITUDE, delta=0.01)

        # Invalid angle index is refused cleanly.
        bad = copy.deepcopy(proj)
        bad['clips'][0]['multicamAngle'] = 5
        with self.assertRaisesRegex(ValueError, 'angle index'):
            self._render(bad, 'mc-bad-angle')

    # -- 3. Subclip Export Parity ----------------------------------------------------------------

    def test_subclip_export_uses_parent_at_exact_offset(self):
        # Segmented parent: 0..2s red/375, 2..4s green/625, 4..6s blue/875.
        # Subclip covers 2.0..4.0s (green/625), placed on timeline with in=0.4, duration=1.2 -> 2.4..3.6s (green/625).
        sub = dict(
            id='sub-1', name='Green Take', type='video', src=self.segmented['src'],
            parentMediaId='segmented', subclipIn=2.0, subclipOut=4.0, subclipLimits=True,
            duration=2.0, fps=FPS, audioChannels=2,
        )
        proj = dict(
            name='Subclip Test', width=W, height=H, fps=FPS,
            media=[copy.deepcopy(self.segmented), sub],
            clips=[self._vclip('sc1', 'sub-1', 0.0, 1.2, src_in=0.4)],
        )
        movie = self._render(proj, 'subclip-green')
        shown = frames_rgb(movie)
        self.assertEqual(len(shown), 30)
        self.assertEqual({dominant_channel(f) for f in shown}, {'green'})
        pcm = left_channel(movie)
        self.assertAlmostEqual(tone_level(pcm, 625, 0.1, 1.1), AMPLITUDE, delta=0.01)
        self.assertLess(tone_level(pcm, 375, 0.1, 1.1), 0.005)
        self.assertLess(tone_level(pcm, 875, 0.1, 1.1), 0.005)

        # A second generation of subclip adds its own source offset on native save.
        proj['media'].append(dict(sub, id='sub-2', parentMediaId='sub-1', subclipIn=.2, subclipOut=1.8))
        proj['clips'][0]['mediaId'] = 'sub-2'
        reopened = open_project(save_project(proj))
        self.assertAlmostEqual(next(c['in'] for c in reopened['clips'] if c['type'] == 'video'), 2.6)
        self.assertAlmostEqual(proj['clips'][0]['in'], .4)

        # Circular subclip reference is refused cleanly.
        cyc = dict(
            name='Bad Subclip', width=W, height=H, fps=FPS,
            media=[
                dict(id='sa', name='A', type='video', src='', parentMediaId='sb', subclipIn=0.0, subclipOut=1.0, duration=1.0),
                dict(id='sb', name='B', type='video', src='', parentMediaId='sa', subclipIn=0.0, subclipOut=1.0, duration=1.0),
            ],
            clips=[self._vclip('c', 'sa', 0.0, 0.5)],
        )
        with self.assertRaisesRegex(ValueError, 'Circular subclip'):
            self._render(cyc, 'subclip-cycle')

    # -- 4. Interchange & Resolve Honest Compatibility -------------------------------------------

    def test_interchange_subclip_resolution_and_multicam_nested_reports(self):
        parent_media = dict(
            id='p1', name='CamA.mov', type='video', src=self.segmented['src'],
            path='/footage/CamA.mov', duration=6.0, fps=FPS, rate='25/1', timecode='01:00:00:00',
            width=W, height=H, audioStreams=1, audioStreamDetails=[{'index': 0, 'channels': 2, 'layout': 'stereo', 'codec': 'pcm'}],
        )
        sub_media = dict(
            id='sub1', name='CamA Subclip', type='video', src=self.segmented['src'],
            parentMediaId='p1', subclipIn=2.0, subclipOut=4.0, duration=2.0,
        )
        mc_media = dict(
            id='mc1', name='MC 1', type='video', src='', duration=6.0,
            multicam=dict(angles=[dict(mediaId='p1', name='CamA', offset=0.0, duration=6.0)], audioMode='camera1', audioAngle=0),
        )
        nest_media = dict(
            id='n1', name='Nest 1', type='video', src='', duration=2.0,
            nestedSequence='nest-abc', nestedProject=dict(name='Nest 1', width=W, height=H, fps=FPS, media=[], clips=[]),
        )
        proj = dict(
            name='Interchange Test', width=W, height=H, fps=FPS,
            media=[parent_media, sub_media, mc_media, nest_media],
            clips=[
                self._vclip('c-sub', 'sub1', 0.0, 1.0, src_in=0.4),
                self._vclip('c-mc', 'mc1', 1.0, 1.0, src_in=0.0, multicamAngle=0),
                self._vclip('c-nest', 'n1', 2.0, 1.0, src_in=0.0, nestedSequence='nest-abc'),
            ],
        )
        cut = interchange_cut.build_cut(proj)
        self.assertEqual(len(cut.events), 1)
        ev = cut.events[0]
        self.assertEqual(ev.source.id, 'p1')
        self.assertEqual(ev.name, 'CamA Subclip')
        # clip.in (0.4) + subclipIn (2.0) = 2.4s exact source_in!
        self.assertAlmostEqual(float(ev.source_in), 2.4, places=6)
        joined = '\n'.join(cut.report.general)
        self.assertIn('un-flattened multicam', joined)
        self.assertIn('nested sequence', joined)


@unittest.skipUnless(HAVE_FFMPEG, 'needs FFmpeg to generate Resolve seed media')
class ResolveCompatibilityTests(unittest.TestCase):
    def test_resolve_save_writes_subclip_at_offset_and_reports_nested_multicam_and_label(self):
        import resolve_fixtures as F
        from resolve import importer, save, source, writer

        PIC, SND = 'E:\\Show\\pic.mov', 'E:\\Show\\snd.wav'
        with tempfile.TemporaryDirectory(prefix='splice-mn-rw-') as tmp:
            root = Path(tmp)
            pic, snd = F.make_media(root)
            library = root / 'library'
            db, _ids = F.seed_library(library, (PIC, str(pic)), (SND, str(snd)))
            kept = root / 'kept.db'
            shutil.copyfile(db, kept)

            model = importer.read(source.load_library(db, root))
            project = importer.to_splice(model, 0, None, {'format': 'library', 'path': str(db)})
            for item in project['media']:
                local = {PIC: pic, SND: snd}.get(item.get('path'))
                if local:
                    item.update(src='file:' + str(local), offline=False)

            fps = project['fps']
            frame = 1 / fps
            pic_media = next(m for m in project['media'] if m.get('path') == PIC)
            end = max(c['start'] + c['duration'] for c in project['clips'])

            # Add a subclip (in=5 frames), an un-flattened multicam clip, and a native nested sequence.
            project['media'].extend([
                dict(id='sub-r1', name='Pic Subclip', type='video', src=pic_media['src'],
                     parentMediaId=pic_media['id'], subclipIn=5 * frame, subclipOut=20 * frame, duration=15 * frame),
                dict(id='mc-r1', name='MC 1', type='video', src='', duration=10 * frame,
                     multicam=dict(angles=[dict(mediaId=pic_media['id'], name='Cam A', offset=0, duration=10 * frame)],
                                   audioMode='camera1', audioAngle=0)),
                dict(id='nest-r1', name='Nest 1', type='video', src='', duration=10 * frame,
                     nestedSequence='nest-1',
                     nestedProject=dict(name='Nest 1', width=1920, height=1080, fps=fps, media=[], clips=[])),
            ])
            # Place subclip at timeline end with clip.in = 3 frames -> effective Resolve source in = 8 frames!
            project['clips'].extend([
                dict(id='clip-sub', type='video', track=1, start=end, duration=6 * frame,
                     **{'in': 3 * frame}, mediaId='sub-r1', speed=1, embeddedAudio=False, colorLabel='blue'),
                dict(id='clip-mc', type='video', track=1, start=end + 6 * frame, duration=4 * frame,
                     **{'in': 0}, mediaId='mc-r1', speed=1, multicamAngle=0),
                dict(id='clip-nest', type='video', track=1, start=end + 10 * frame, duration=4 * frame,
                     **{'in': 0}, mediaId='nest-r1', speed=1, nestedSequence='nest-1'),
            ])

            payload = {
                'library': str(library), 'name': 'Slice L Resolve Check', 'timelines': [project],
                'seed': {'kind': 'library', 'sha256': project['resolveSource']['sha256'],
                         'path': project['resolveSource']['path']},
                'pathMap': [{'from': str(root), 'to': 'E:\\Show'}], 'confirm': True,
            }
            result = save.run(payload, originals=lambda kind, sha: kept if kind == 'library' else None,
                              scratch=root, resolve_media=lambda src: src[5:])
            self.assertTrue(result['written'])
            self.assertEqual(result['selfCheck'], 'passed')

            not_written = {line['what'] for line in result['lines'] if line['status'] == writer.NOT_WRITTEN}
            self.assertTrue(any('Nested sequences' in w for w in not_written), not_written)
            self.assertTrue(any('Un-flattened multicam' in w for w in not_written), not_written)
            self.assertTrue(any('color labels' in w.lower() for w in not_written), not_written)

            # Reopen written Resolve project and confirm the subclip landed at exact source frame 8!
            reopened = importer.to_splice(
                importer.read(source.load_library(Path(result['project']['path']), root)), 0,
            )
            sub_reopened = [c for c in reopened['clips']
                            if c['type'] == 'video' and abs(c['start'] - end) < 0.5 * frame]
            self.assertEqual(len(sub_reopened), 1)
            self.assertEqual(round(sub_reopened[0]['in'] * fps), 8)


if __name__ == '__main__':
    unittest.main()
