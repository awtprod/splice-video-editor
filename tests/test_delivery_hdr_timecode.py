"""HDR10 and HLG signalling, MaxCLL/MaxFALL measurement, timecode tracks, metadata, burn-ins,
caption sidecars, 16-bit still sources and the hardware-encoder fallback."""
from array import array
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import warnings
from unittest import mock

import delivery
from interchange.timecode import timecode_frames
from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

W, H = 256, 144


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=300)


def probe(path, *extra):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', *extra, '-of', 'json',
                                               str(path)], timeout=60))


def pq_signal(nits):
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    y = (nits / 10000) ** m1
    return ((c1 + c2 * y) / (1 + c3 * y)) ** m2


class Case(unittest.TestCase):
    def setUp(self):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        self.media = Path(tempfile.mkdtemp(prefix='delivery-hdr-', dir=MEDIA_ROOT))
        self.addCleanup(shutil.rmtree, self.media, True)
        self.out = Path(tempfile.mkdtemp(prefix='delivery-hdr-out-'))
        self.addCleanup(shutil.rmtree, self.out, True)

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))

    def neutral_source(self, name, background, peak, transfer, *, frames=24):
        """A neutral grey 10-bit frame (code ``background``) with a 16 x 8 patch at code ``peak``, tagged BT.2020."""
        y = array('H', [background] * (W * H))
        for row in range(40, 48):
            for column in range(100, 116):
                y[row * W + column] = peak
        chroma = array('H', [512] * (W * H))
        raw = self.media / f'{name}.yuv'
        raw.write_bytes((y.tobytes() + chroma.tobytes() + chroma.tobytes()) * frames)
        clip = self.media / f'{name}.mov'
        ffmpeg('-f', 'rawvideo', '-pix_fmt', 'yuv444p10le', '-s', f'{W}x{H}', '-r', '24', '-i', str(raw), '-f', 'lavfi',
               '-i', 'sine=d=1:sample_rate=48000', '-shortest', '-vf',
               f'setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc:range=tv',
               '-c:v', 'prores_ks', '-profile:v', '4', '-pix_fmt', 'yuv444p10le', '-c:a', 'pcm_s24le', str(clip))
        raw.unlink()
        return clip

    def project(self, clip, seconds=1, **extra):
        return dict(name='HDR', width=W, height=H, fps=24, media=[dict(id='m', name=clip.name, src=self.src(clip))],
                    clips=[dict(type='video', mediaId='m', track=1, start=0, duration=seconds, **{'in': 0})], **extra)


class HDR(Case):
    def side_data(self, path):
        frames = json.loads(subprocess.check_output(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '%+#1', '-show_frames', '-of', 'json', str(path)],
            timeout=60))['frames']
        return {item.get('side_data_type'): item for item in frames[0].get('side_data_list', [])}

    def test_pq_eotf_reference_points(self):
        for nits in (0.1, 100, 203, 1000, 4000, 10000):
            self.assertAlmostEqual(render_movie.pq_to_nits(pq_signal(nits)), nits, delta=nits * 1e-6 + 1e-6)

    def test_measurement_of_a_synthetic_frame_with_a_known_peak(self):
        # The measuring pass itself, on exact 10-bit 4:2:0 PQ frames: 100-nit grey with a
        # 16 x 8 patch at 1000 nits in the first frame and at 400 nits in the second.
        background = round(64 + 876 * pq_signal(100))
        frames = []
        for nits in (1000, 400):
            y = array('H', [background] * (W * H))
            for row in range(40, 48):
                for column in range(100, 116):
                    y[row * W + column] = round(64 + 876 * pq_signal(nits))
            frames.append(y.tobytes() + array('H', [512] * (W * H // 2)).tobytes())
        target = self.out / 'levels'
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'yuv420p10le', '-s', f'{W}x{H}', '-i', 'pipe:0',
                   *render_movie.light_level_filter_args(('bt2020', 'smpte2084', 'bt2020nc'), target)]
        subprocess.run(command, input=b''.join(frames), check=True, timeout=60)
        max_cll, max_fall = render_movie._read_light_levels(target)
        peak = render_movie.pq_to_nits((round(64 + 876 * pq_signal(1000)) - 64) / 876)
        grey = render_movie.pq_to_nits((background - 64) / 876)
        self.assertAlmostEqual(max_cll, peak, delta=1)
        self.assertAlmostEqual(max_fall, (128 * peak + (W * H - 128) * grey) / (W * H), delta=1)

    def test_maxcll_maxfall_measured_on_a_frame_with_a_known_peak(self):
        # 100-nit grey with a 1000-nit patch (16 x 8 of 256 x 144 pixels).
        background, peak = (round(64 + 876 * pq_signal(nits)) for nits in (100, 1000))
        clip = self.neutral_source('known-peak', background, peak, 'smpte2084')
        target = self.out / 'hdr10.mp4'
        report = {}
        render_to_file(self.project(clip), target, options=dict(codec='hevc', profile='main10', colorSpace='source'), report=report)
        # The coded values themselves, through the EOTF: what the measurement must find.
        expected_peak = render_movie.pq_to_nits((peak - 64) / 876)
        expected_average = (16 * 8 * expected_peak + (W * H - 16 * 8) * render_movie.pq_to_nits((background - 64) / 876)) / (W * H)
        levels = report['lightLevels']
        self.assertEqual(levels['method'], 'measured')
        # Through a ProRes 4444 source and the 10-bit pipeline: within 3 PQ codes (about 3 % at 1000 nits).
        self.assertAlmostEqual(levels['maxCll'], expected_peak, delta=expected_peak * .03)
        self.assertAlmostEqual(levels['maxFall'], expected_average, delta=max(1.5, expected_average * .015))
        side = self.side_data(target)
        light = side['Content light level metadata']
        self.assertEqual((light['max_content'], light['max_average']), (levels['maxCll'], levels['maxFall']))
        display = side['Mastering display metadata']
        self.assertEqual(display['max_luminance'], '10000000/10000')
        self.assertEqual(display['min_luminance'], '50/10000')
        self.assertEqual(display['red_x'], '34000/50000')  # Display P3 red, ST 2086 units
        stream = next(s for s in probe(target)['streams'] if s['codec_type'] == 'video')
        self.assertEqual((stream['color_primaries'], stream['color_transfer'], stream['color_space'], stream['pix_fmt'],
                          stream['codec_tag_string']), ('bt2020', 'smpte2084', 'bt2020nc', 'yuv420p10le', 'hvc1'))

    def test_burn_in_white_in_a_pq_delivery(self):
        clip = self.neutral_source('dark', 160, 160, 'smpte2084')
        target = self.out / 'burn-in.mp4'
        render_to_file(self.project(clip), target, options=dict(codec='hevc', profile='main10',
                       colorSpace='source', burnIns=dict(fields=['timecode', 'clipName'], size=8)))
        peak = self.side_data(target)['Content light level metadata']['max_content']
        self.assertAlmostEqual(peak, 203, delta=25)

    def test_entered_light_levels_and_a_rec2020_display(self):
        clip = self.neutral_source('entered', 500, 700, 'smpte2084')
        target = self.out / 'entered.mp4'
        report = {}
        render_to_file(self.project(clip), target, report=report,
                       options=dict(codec='hevc', colorSpace='pq', hdr=dict(display='bt2020', maxLuminance=4000, minLuminance=0.0001,
                                                                             lightLevels='manual', maxCll=2400, maxFall=310)))
        side = self.side_data(target)
        self.assertEqual((side['Content light level metadata']['max_content'], side['Content light level metadata']['max_average']),
                         (2400, 310))
        self.assertEqual(side['Mastering display metadata']['max_luminance'], '40000000/10000')
        self.assertEqual(side['Mastering display metadata']['red_x'], '35400/50000')
        self.assertEqual(report['lightLevels']['method'], 'entered')

    def test_sdr_program_in_hevc_main10_has_no_hdr10_metadata(self):
        clip = self.media / 'sdr.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={W}x{H}:r=24:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip))
        target = self.out / 'sdr.mp4'
        render_to_file(self.project(clip), target, options=dict(codec='hevc', profile='main10'))
        self.assertNotIn('Mastering display metadata', self.side_data(target))
        stream = next(s for s in probe(target)['streams'] if s['codec_type'] == 'video')
        self.assertEqual((stream['color_primaries'], stream['color_transfer']), ('bt709', 'bt709'))

    def test_hardware_request_keeps_hdr10_side_data(self):
        clip = self.neutral_source('hardware-pq', 500, 700, 'smpte2084')
        for color_space in ('pq', 'source'):
            with self.subTest(color_space=color_space):
                options = dict(codec='hevc', encoder='nvenc', colorSpace=color_space,
                               hdr=dict(display='bt2020', lightLevels='manual', maxCll=1800, maxFall=275))
                target = self.out / (color_space + '.mp4')
                report = {}
                with self.assertWarnsRegex(UserWarning, 'HDR10 static metadata'), \
                     mock.patch.object(render_movie, '_encoder_command', wraps=render_movie._encoder_command) as commands:
                    render_to_file(self.project(clip), target, options=options, report=report)
                self.assertEqual([call.args[0]['encoder'] for call in commands.call_args_list], ['libx265'])
                self.assertTrue(report['hardwareFallback'])
                side = self.side_data(target)
                self.assertEqual(side['Mastering display metadata']['red_x'], '35400/50000')
                levels = side['Content light level metadata']
                self.assertEqual((levels['max_content'], levels['max_average']), (1800, 275))

    def test_hlg_passthrough_is_tagged_hlg(self):
        clip = self.neutral_source('hlg', 400, 800, 'arib-std-b67')
        target = self.out / 'hlg.mp4'
        render_to_file(self.project(clip), target, options=dict(codec='hevc', colorSpace='hlg'))
        stream = next(s for s in probe(target)['streams'] if s['codec_type'] == 'video')
        self.assertEqual((stream['color_primaries'], stream['color_transfer'], stream['color_space']),
                         ('bt2020', 'arib-std-b67', 'bt2020nc'))
        self.assertNotIn('Mastering display metadata', self.side_data(target), 'HDR10 metadata is for PQ only')

    def test_hlg_output_refuses_material_it_would_have_to_convert(self):
        clip = self.neutral_source('pq-for-hlg', 400, 800, 'smpte2084')
        with self.assertRaisesRegex(ValueError, 'does not convert other material to HLG'):
            render_to_file(self.project(clip), self.out / 'x.mp4', options=dict(codec='hevc', colorSpace='hlg'))

    def test_av1_hdr10(self):
        if delivery.unavailable_reason(dict(codec='av1')):
            self.skipTest('libsvtav1: ' + delivery.unavailable_reason(dict(codec='av1')))
        clip = self.neutral_source('av1', 500, 800, 'smpte2084')
        target = self.out / 'av1.mp4'
        render_to_file(self.project(clip), target, options=dict(codec='av1', colorSpace='pq',
                                                                hdr=dict(lightLevels='manual', maxCll=1000, maxFall=200)))
        stream = next(s for s in probe(target)['streams'] if s['codec_type'] == 'video')
        self.assertEqual((stream['color_primaries'], stream['color_transfer']), ('bt2020', 'smpte2084'))


class Timecode(Case):
    def source(self, rate):
        clip = self.media / f'tc-{rate.replace("/", "-")}.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={W}x{H}:r={rate}:d=3', '-f', 'lavfi', '-i', 'sine=d=3', '-shortest', '-c:v', 'libx264',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(clip))
        return clip

    def timecode_of(self, path):
        info = probe(path)
        tags = [s.get('tags', {}).get('timecode') for s in info['streams']] + [info['format'].get('tags', {}).get('timecode')]
        return next(tag for tag in tags if tag)

    def test_start_timecode_at_every_broadcast_rate(self):
        # (rate, fps, drop, start label, frames into the range, label of the range's first frame)
        cases = [('24000/1001', 24000 / 1001, None, '01:00:00:00', 30, '01:00:01:06'), ('24', 24, None, '10:00:00:00', 47, '10:00:01:23'),
                 ('25', 25, None, '00:59:59:00', 25, '01:00:00:00'), ('30000/1001', 30000 / 1001, True, '00:00:59;28', 2, '00:01:00;02'),
                 ('30000/1001', 30000 / 1001, False, '00:00:59:28', 2, '00:01:00:00'), ('50', 50, None, '01:00:00:00', 50, '01:00:01:00'),
                 ('60000/1001', 60000 / 1001, True, '00:09:59;56', 4, '00:10:00;00'), ('60', 60, None, '23:59:59:59', 1, '00:00:00:00')]
        from fractions import Fraction
        for rate, fps, drop, start, skip, expected in cases:
            with self.subTest(rate=rate, drop=drop):
                exact = Fraction(rate)
                frames = timecode_frames(start, exact, drop)
                clip = self.source(rate)
                project = dict(self.project(clip, seconds=2), fps=fps, startTimecode=frames)
                if drop is not None:
                    project['dropFrame'] = drop
                for container in ('mov', 'mp4'):
                    whole = self.out / f'whole.{container}'
                    render_to_file(project, whole, options=dict(codec='prores' if container == 'mov' else 'h264'))
                    self.assertEqual(self.timecode_of(whole), start)
                ranged = self.out / 'range.mov'
                render_to_file(project, ranged, options=dict(codec='prores', range=dict(start=skip / fps, end=2.0)))
                self.assertEqual(self.timecode_of(ranged), expected, 'a range starts at its own first frame')
                # A frame deep into the file: its label is the start label plus its frame number.
                deep = int(round(1.4 * fps)) - skip
                self.assertEqual(delivery.timecode_label(timecode_frames(expected, exact, drop) + deep, exact, drop),
                                 delivery.timecode_label(frames + skip + deep, exact, drop))

    def test_drop_frame_labels_skip_the_right_numbers(self):
        from fractions import Fraction
        rate = Fraction(30000, 1001)
        self.assertEqual(delivery.timecode_label(1800, rate, True), '00:01:00;02')
        self.assertEqual(delivery.timecode_label(17982, rate, True), '00:10:00;00')
        self.assertEqual(delivery.timecode_label(107892, rate, True), '01:00:00;00')
        self.assertEqual(delivery.timecode_label(107892, rate, False), '00:59:56:12')
        self.assertEqual(delivery.timecode_label(24 * 86400, Fraction(24), False), '00:00:00:00', 'wraps at 24 hours')

    def test_mxf_timecode_and_metadata(self):
        clip = self.source('25')
        project = dict(self.project(clip, seconds=1), fps=25, startTimecode=25 * 3600 * 10)
        target = self.out / 'master.mxf'
        render_to_file(project, target, options=dict(codec='dnxhr', profile='hq', container='mxf', reel='A001', title='Feature'))
        info = probe(target)
        self.assertEqual(info['format']['tags']['timecode'], '10:00:00:00')
        mov = self.out / 'master.mov'
        render_to_file(project, mov, options=dict(codec='prores', reel='A001', title='Feature', comment='Picture lock v3'))
        info = probe(mov)
        self.assertEqual(info['format']['tags']['title'], 'Feature')
        self.assertEqual(info['format']['tags']['comment'], 'Picture lock v3')
        data = next(s for s in info['streams'] if s['codec_type'] == 'data')
        self.assertEqual(data['tags'].get('reel_name'), 'A001')

    def test_timecode_track_can_be_left_out(self):
        clip = self.source('25')
        target = self.out / 'plain.mp4'
        render_to_file(dict(self.project(clip), fps=25), target, options=dict(codec='h264', timecode=False))
        self.assertFalse(any(s['codec_type'] == 'data' for s in probe(target)['streams']))


class BurnIns(Case):
    def test_sequence_and_repeated_camera_clip_wrap_at_midnight(self):
        from fractions import Fraction
        for rate, midnight, before in [(Fraction(24), '00:00:00:00', '23:59:59:21'),
                                        (Fraction(30000, 1001), '00:00:00;00', '23:59:59;27')]:
            with self.subTest(rate=rate):
                clip = self.media / f'camera-{round(rate)}.mov'
                ffmpeg('-f', 'lavfi', '-i', f'color=gray:s={W}x{H}:r={rate}:d=0.5',
                       '-c:v', 'prores_ks', '-timecode', midnight, str(clip))
                project = self.project(clip, seconds=float(3/rate))
                project.update(fps=float(rate), dropFrame=';' in midnight)
                options = dict(codec='prores', burnIns=dict(fields=['sourceTimecode', 'timecode'],
                               position='bottom-left', clipNamePosition='top-left'))
                reference = self.out / 'midnight.mov'
                render_to_file(project, reference, options=options)
                expected = self.frame(reference, 0)
                following = self.frame(reference, 1)

                project['startTimecode'] = timecode_frames(before, rate)
                project['clips'].append(dict(project['clips'][0], start=float(3/rate)))
                target = self.out / 'across-midnight.mov'
                render_to_file(project, target, options=options)
                actual = self.frame(target, 3)
                for field, region in [('camera', slice(W*H//2)), ('sequence', slice(W*H//2, None))]:
                    with self.subTest(field=field):
                        self.assertTrue(expected[region] != following[region], 'The reference burn must advance')
                        self.assertTrue(actual[region] == expected[region], 'The burn should restart at midnight')

    def frame(self, path, number, pix='gray'):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'select=eq(n\\,{number}),format={pix}',
                                        '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'], timeout=60)

    def region(self, data, x0, y0, x1, y1):
        return [data[y * W + x] for y in range(y0, y1) for x in range(x0, x1)]

    def test_burn_ins_are_drawn_the_same_in_every_codec_and_off_by_default(self):
        clip = self.media / 'grey.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'color=c=0x404040:s={W}x{H}:r=24:d=1', '-c:v', 'libx264', '-crf', '0', '-pix_fmt', 'yuv444p', str(clip))
        project = dict(self.project(clip), startTimecode=86400)
        burn = dict(fields=['timecode', 'clipName', 'frame', 'watermark'], watermark='REVIEW', size=8, safeMargins=True)
        bottom = (0, H - 40, W, H)  # timecode and frame number rows
        top = (0, 0, W // 2, 30)    # clip name
        deltas = {}
        for name, options in (('prores.mov', dict(codec='prores')), ('h264.mp4', dict(codec='h264', crf=14)), ('png', dict(codec='png'))):
            frames = {}
            for burned in (False, True):
                target = self.out / (('burn-' if burned else 'plain-') + name)
                render_to_file(project, target, options=dict(options, **({'burnIns': burn} if burned else {})))
                path = sorted(target.glob('*.png'))[10] if target.is_dir() else target
                frames[burned] = self.frame(path, 0 if target.is_dir() else 10)
            # What the burn-in changed, in luma, per codec (each codec's own colour rounding cancels out).
            deltas[name] = {box: [b - a for a, b in zip(self.region(frames[False], *box), self.region(frames[True], *box))]
                            for box in (bottom, top)}
            for box in (bottom, top):
                self.assertGreater(max(map(abs, deltas[name][box])), 100, f'{name}: the burn-in is drawn')
        for name in ('h264.mp4', 'png'):
            for box in (bottom, top):
                difference = [abs(a - b) for a, b in zip(deltas['prores.mov'][box], deltas[name][box])]
                # H.264 is lossy 4:2:0 even at CRF 14; the RGB PNG goes through one more conversion to luma.
                self.assertLess(sum(difference) / len(difference), 4 if name == 'h264.mp4' else 3, f'{name} burn-in matches ProRes')
        plain = self.out / 'plain-again.mov'
        render_to_file(project, plain, options=dict(codec='prores'))
        self.assertEqual(self.frame(plain, 10), self.frame(self.out / 'plain-prores.mov', 10), 'no burn-in unless asked')

    def test_upper_track_owns_name_and_source_timecode_only_during_overlap(self):
        burn = dict(media={'a': dict(name='Base', _sourceTimecodeInfo=(120, 24, False)),
                           'b': dict(name='Insert', _sourceTimecodeInfo=(900, 24, False))},
                    settings=dict(fields=['clipName', 'sourceTimecode']), timecode=0, drop=False, font='')
        clips = [dict(type='video', mediaId='a', track=1, start=0, duration=3),
                 dict(type='video', mediaId='b', track=2, start=1, duration=1)]
        spans = render_movie._window_burn(burn, clips, 0, 0, 24)['clips']
        self.assertEqual([tuple(span) for span in spans],
                         [(0, 24, 'Base', '00:00:05:00'), (24, 48, 'Insert', '00:00:36:12'), (48, 72, 'Base', '00:00:05:00')])

    def test_hidden_lower_clip_name_does_not_change_exported_pixels(self):
        source = self.media / 'plates.mkv'
        ffmpeg('-f', 'lavfi', '-i', f'color=gray:s={W}x{H}:r=24:d=1', '-c:v', 'ffv1', str(source))
        project = self.project(source)
        project['media'].append(dict(project['media'][0], id='top', name='Cutaway'))
        project['clips'].append(dict(type='video', track=2, mediaId='top', start=.25, duration=.5))
        frames = []
        for label in ('Long lower plate', 'X'):
            project['media'][0]['name'] = label
            target = self.out / (label + '.mov')
            render_to_file(project, target, options=dict(codec='prores', burnIns=dict(fields=['clipName'])))
            frames.append([self.frame(target, index) for index in (3, 12, 21)])
        self.assertNotEqual(frames[0][0], frames[1][0])
        self.assertEqual(frames[0][1], frames[1][1])
        self.assertNotEqual(frames[0][2], frames[1][2])

    def test_burn_in_timecode_advances_frame_by_frame(self):
        clip = self.media / 'grey2.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'color=c=0x404040:s={W}x{H}:r=24:d=1', '-c:v', 'libx264', '-crf', '0', str(clip))
        target = self.out / 'tc.mov'
        render_to_file(self.project(clip), target, options=dict(codec='prores', burnIns=dict(fields=['timecode'], size=8)))
        box = (0, H - 40, W, H)
        regions = [self.region(self.frame(target, n), *box) for n in (0, 1, 2, 2)]
        self.assertNotEqual(regions[0], regions[1])
        self.assertNotEqual(regions[1], regions[2])
        self.assertEqual(regions[2], regions[3])

    def test_source_burn_keeps_its_own_drop_frame_convention(self):
        from fractions import Fraction
        for rate, label, source_drop in ((Fraction(30000, 1001), '01:00:59:29', False),
                                         (Fraction(60000, 1001), '01:00:59;59', True)):
            with self.subTest(label=label):
                clip = self.media / f'camera-{round(rate)}.mov'
                ffmpeg('-f', 'lavfi', '-i', f'color=gray:s={W}x{H}:r={rate}:d=0.4',
                       '-c:v', 'prores_ks', '-timecode', label, str(clip))
                project = dict(self.project(clip, seconds=0.3), fps=float(rate),
                               startTimecode=timecode_frames(label, rate))
                frames = []
                for source in (False, True):
                    project['dropFrame'] = not source_drop if source else source_drop
                    if source and source_drop:
                        # Imported projects can record 59.94 while the camera uses 60000/1001.
                        project['fps'] = round(float(rate), 2)
                    target = self.out / f'{source}.mov'
                    burn = dict(fields=['sourceTimecode' if source else 'timecode'], position='top-left')
                    render_to_file(project, target, options=dict(codec='prores', burnIns=burn))
                    frames.append([self.frame(target, n) for n in (0, 1)])
                self.assertNotEqual(frames[0][0], frames[0][1])
                self.assertTrue(frames[0] == frames[1], f'Source burn must follow {label} across the minute boundary')


class Sources(Case):
    def test_tiff_exr_and_dpx_stills_render_with_their_depth(self):
        ramp = self.media / 'ramp.rgb48'
        values = array('H', [])
        for row in range(H):
            for column in range(W):
                level = (column + row * W) * 64 % 65536
                values.extend((level, level, level))
        ramp.write_bytes(values.tobytes())
        for suffix, pix in (('.tif', 'rgb48le'), ('.dpx', 'rgb48le'), ('.exr', 'gbrpf32le')):
            with self.subTest(suffix=suffix):
                still = self.media / f'ramp{suffix}'
                encode = ['-c:v', 'exr', '-pix_fmt', 'gbrpf32le'] if suffix == '.exr' else ['-pix_fmt', pix]
                ffmpeg('-f', 'rawvideo', '-pix_fmt', 'rgb48le', '-s', f'{W}x{H}', '-i', str(ramp), *encode, '-frames:v', '1', str(still))
                target = self.out / f'still{suffix}.mov'
                render_to_file(self.project(still), target, options=dict(codec='prores'))
                data = array('H', subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(target), '-vf', 'format=gray10le',
                                                           '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'], timeout=60))
                self.assertGreater(len(set(data)), 256, f'{suffix} keeps more than 8 bits')

    def test_render_movie_resolves_media_through_the_public_resolver(self):
        import media_links
        from unittest import mock
        clip = self.media / 'linked.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={W}x{H}:r=24:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip))
        with mock.patch.object(media_links, 'resolve_media', wraps=media_links.resolve_media) as resolver:
            render_to_file(self.project(clip), self.out / 'r.mp4', options=dict(codec='h264'))
        self.assertTrue(resolver.called)


class Captions(Case):
    def test_srt_and_webvtt_sidecars_and_embedded_tracks(self):
        clip = self.media / 'cap.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={W}x{H}:r=24:d=2', '-f', 'lavfi', '-i', 'sine=d=2', '-shortest', '-c:v', 'libx264',
               '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(clip))
        project = dict(self.project(clip, seconds=2), captions=[dict(start=.5, duration=1, text='Hello --> world'),
                                                                dict(start=1.5, duration=.25, text='Two\nlines')])
        target = self.out / 'film.mp4'
        report = {}
        render_to_file(project, target, options=dict(codec='h264', subtitleLanguage='eng', captionFiles=['srt', 'vtt']), report=report)
        self.assertEqual(report['sidecars'], ['film.srt', 'film.vtt'])
        vtt = (self.out / 'film.vtt').read_text(encoding='utf-8')
        self.assertTrue(vtt.startswith('WEBVTT\n\n'))
        self.assertIn('00:00:00.500 --> 00:00:01.500\nHello → world', vtt)
        self.assertIn('00:00:01.500 --> 00:00:01.750\nTwo\nlines', vtt)
        self.assertIn('00:00:00,500 --> 00:00:01,500', (self.out / 'film.srt').read_text(encoding='utf-8'))
        subtitle = next(s for s in probe(target)['streams'] if s['codec_type'] == 'subtitle')
        self.assertEqual((subtitle['codec_name'], subtitle['tags']['language']), ('mov_text', 'eng'))
        mxf = self.out / 'film.mxf'
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            render_to_file(project, mxf, options=dict(codec='dnxhr', container='mxf'))
        self.assertTrue(any('sidecar SRT' in str(item.message) for item in caught))
        self.assertTrue((self.out / 'film.srt').exists())
        self.assertFalse(any(s['codec_type'] == 'subtitle' for s in probe(mxf)['streams']))


class HardwareFallback(Case):
    def test_a_failing_hardware_encoder_restarts_in_software(self):
        clip = self.media / 'hw.mp4'
        ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={W}x{H}:r=24:d=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clip))
        target = self.out / 'hw.mp4'
        report = {}
        failing = dict(delivery.HARDWARE['nvenc'], h264='h264_splice_missing')
        from unittest import mock
        with mock.patch.dict(delivery.HARDWARE, {'nvenc': failing}), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            render_to_file(self.project(clip), target, options=dict(codec='h264', encoder='nvenc'), report=report)
        self.assertTrue(report.get('hardwareFallback'))
        self.assertTrue(any('restarted with the software encoder' in str(item.message) for item in caught))
        stream = next(s for s in probe(target)['streams'] if s['codec_type'] == 'video')
        self.assertEqual((stream['codec_name'], int(stream['nb_frames'])), ('h264', 24))


if __name__ == '__main__':
    unittest.main()
