"""Every delivery format through the export job route, decoded back and checked.

Each case renders a generated 10-bit test pattern (a 2-D ramp with more than 256 luma levels, a
moving bar, a 1 kHz tone) through ExportJobs, the code the /api/exports route calls, then reads the
file with ffprobe and FFmpeg: codec, profile, pixel format, bit depth (a 10-bit delivery must keep
more than 256 distinct luma levels), colour primaries/transfer/matrix/range, alpha where promised,
frame count and duration, the timecode track, and the audio codec and layout. A case is skipped,
with the reason, only when this machine's FFmpeg cannot encode it (delivery.unavailable_reason).
"""
from array import array
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

import delivery
from export_jobs import ExportJobs
from premiere import MEDIA_ROOT

W, H, FPS, SECONDS = 256, 144, 24, 2


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=300)


def probe(path):
    return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                                              timeout=60))


def ramp_frame():
    """A 4:4:4 10-bit frame: luma steps through 877 legal codes in raster order, neutral chroma."""
    y = array('H', ((64 + (x + row * W) % 877) for row in range(H) for x in range(W)))
    u = array('H', [512] * (W * H))
    return y.tobytes() + u.tobytes() + u.tobytes()


def make_sources(directory):
    """A 10-bit ramp ProRes 4444 clip with a moving bar and a tone, plus an RGBA PNG."""
    raw = directory / 'ramp.yuv'
    raw.write_bytes(ramp_frame())
    clip = directory / 'ramp.mov'
    ffmpeg('-f', 'rawvideo', '-pix_fmt', 'yuv444p10le', '-s', f'{W}x{H}', '-r', str(FPS), '-stream_loop', str(FPS * SECONDS + 4),
           '-i', str(raw), '-f', 'lavfi', '-i', f'sine=f=1000:sample_rate=48000:d={SECONDS + 1}',
           '-f', 'lavfi', '-i', f'color=c=white:s=8x20:r={FPS},format=yuv444p10le',
           '-filter_complex', f"[0:v][2:v]overlay=x='mod(n*4,{W})':y=0:shortest=1[v]", '-map', '[v]', '-map', '1:a',
           '-t', str(SECONDS + 0.2), '-c:v', 'prores_ks', '-profile:v', '4', '-pix_fmt', 'yuv444p10le', '-c:a', 'pcm_s24le',
           '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', str(clip))
    raw.unlink()
    still = directory / 'logo.png'
    # Opaque red box (x 64-191, y 32-111) on a fully transparent frame.
    ffmpeg('-f', 'lavfi', '-i', f"color=c=black:s={W}x{H},format=rgba,geq=r=255:g=0:b=0:a='255*between(X,64,191)*between(Y,32,111)'",
           '-frames:v', '1', str(still))
    return clip, still


class DeliveryCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        cls.media = Path(tempfile.mkdtemp(prefix='delivery-formats-', dir=MEDIA_ROOT))
        cls.clip, cls.still = make_sources(cls.media)
        cls.jobs = ExportJobs()

    @classmethod
    def tearDownClass(cls):
        cls.jobs.shutdown()
        shutil.rmtree(cls.media, ignore_errors=True)

    def src(self, path):
        return '/media/' + str(Path(path).relative_to(MEDIA_ROOT))

    def project(self, *, start_timecode=0, fps=FPS, drop=None, alpha_only=False, extra=None):
        media = [dict(id='m', name='ramp.mov', src=self.src(self.clip)), dict(id='p', name='logo.png', src=self.src(self.still))]
        clips = [] if alpha_only else [dict(type='video', mediaId='m', track=1, start=0, duration=SECONDS, **{'in': 0}),
                                       dict(type='audio', mediaId='m', track=1, start=0, duration=SECONDS, **{'in': 0})]
        if alpha_only:
            clips = [dict(type='video', mediaId='p', track=1, start=0, duration=SECONDS, **{'in': 0}),
                     dict(type='title', track=4, start=0, duration=SECONDS, title='TITLE',
                          titleStyle=dict(fontSize=20, y=80))]
        project = dict(name='Delivery test', width=W, height=H, fps=fps, startTimecode=start_timecode, media=media, clips=clips)
        if drop is not None:
            project['dropFrame'] = drop
        project.update(extra or {})
        return project

    def render(self, project, options):
        reason = delivery.unavailable_reason(options)
        if reason:
            self.skipTest(f'{options}: {reason}')
        identifier = self.jobs.start(project, options, folder_name='frames')
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = self.jobs.status(identifier)
            if status['state'] != 'rendering':
                break
            time.sleep(.1)
        self.assertEqual(status['state'], 'complete', status.get('error'))
        output, sidecars = self.jobs.outputs(identifier)
        keep = Path(tempfile.mkdtemp(prefix='delivery-out-'))
        self.addCleanup(shutil.rmtree, keep, True)
        target = keep / output.name
        shutil.move(str(output), str(target))
        for sidecar in sidecars:
            shutil.move(str(sidecar), str(keep / sidecar.name))
        self.jobs.release(identifier)
        return target, status

    def video(self, info):
        return next(s for s in info['streams'] if s['codec_type'] == 'video')

    def levels(self, path, pix='gray10le', frame=5):
        """Distinct luma (or gray) values in one decoded frame."""
        data = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'select=eq(n\\,{frame}),format={pix}',
                                        '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'], timeout=60)
        values = array('H' if pix.endswith('le') else 'B', data)
        return len(set(values))

    def frames(self, path):
        out = subprocess.check_output(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                       'stream=nb_read_frames', '-of', 'csv=p=0', str(path)], text=True, timeout=120)
        return int(out.strip().split(',')[0])

    def check_common(self, path, info, *, codec, pix, profile=None, primaries='bt709', depth=None, timecode='00:00:00:00',
                     audio='aac', frames=FPS * SECONDS, fps=FPS):
        stream = self.video(info)
        self.assertEqual(stream['codec_name'], codec)
        self.assertEqual(stream['pix_fmt'], pix)
        if profile:
            self.assertEqual(stream.get('profile'), profile)
        self.assertEqual((stream.get('color_primaries'), stream.get('color_transfer'), stream.get('color_space')),
                         (primaries, 'bt709', 'bt709'))
        if codec in ('prores', 'dnxhd'):
            # ProRes and DNxHR in MOV carry no range flag (QuickTime 'nclc'); they are legal-range by definition.
            self.assertIn(stream.get('color_range'), ('tv', None))
        else:
            self.assertEqual(stream.get('color_range'), 'tv')
        self.assertEqual(self.frames(path), frames)
        self.assertAlmostEqual(float(info['format']['duration']), frames / fps, delta=1 / fps + 0.03)
        tags = [s.get('tags', {}).get('timecode') for s in info['streams']] + [info['format'].get('tags', {}).get('timecode')]
        self.assertIn(timecode, tags)
        sound = next(s for s in info['streams'] if s['codec_type'] == 'audio')
        self.assertEqual(sound['codec_name'], audio)
        self.assertEqual(sound['channels'], 2)
        if depth == 10:
            self.assertGreater(self.levels(path), 256, 'a 10-bit delivery keeps more than 256 luma levels')
        elif depth == 8:
            self.assertLessEqual(self.levels(path, 'gray'), 256)


class LongGop(DeliveryCase):
    def test_h264_profiles_and_containers(self):
        for profile, pix, label, depth in (('high', 'yuv420p', 'High', 8), ('high10', 'yuv420p10le', 'High 10', 10),
                                           ('high422', 'yuv422p10le', 'High 4:2:2', 10)):
            for container in ('mp4', 'mov'):
                with self.subTest(profile=profile, container=container):
                    path, _ = self.render(self.project(), dict(codec='h264', profile=profile, container=container, crf=18))
                    info = probe(path)
                    self.check_common(path, info, codec='h264', pix=pix, profile=label, depth=depth,
                                      audio='aac' if container == 'mp4' else 'pcm_s24le')
                    self.assertEqual(self.video(info).get('level'), 31 if profile else None)

    def test_h264_bitrate_single_and_two_pass_hit_the_target(self):
        # Grain the encoder cannot predict, so a bitrate target is what decides the size.
        noisy = self.media / 'noise.mp4'
        if not noisy.exists():
            ffmpeg('-f', 'lavfi', '-i', f'testsrc2=s={W}x{H}:r={FPS}:d={SECONDS + 0.2},noise=alls=60:allf=t', '-f', 'lavfi',
                   '-i', f'sine=d={SECONDS + 0.2}', '-c:v', 'libx264', '-qp', '0', '-c:a', 'aac', '-shortest', str(noisy))
        project = self.project()
        project['media'].append(dict(id='n', name='noise.mp4', src=self.src(noisy)))
        for clip in project['clips']:
            clip['mediaId'] = 'n'
        for mode in ('bitrate', 'twopass'):
            with self.subTest(mode=mode):
                path, _ = self.render(project, dict(codec='h264', rateControl=mode, videoBitrate=800))
                info = probe(path)
                self.check_common(path, info, codec='h264', pix='yuv420p', profile='High', depth=8)
                rate = int(self.video(info)['bit_rate'])
                # Two seconds of pure grain is the hardest case for one pass; two passes land close.
                self.assertLess(abs(rate - 800_000) / 800_000, .3 if mode == 'bitrate' else .15,
                                f'{mode}: {rate} bit/s for an 800 kbit/s target')

    def test_web_mp4_is_faststart(self):
        path, _ = self.render(self.project(), dict(codec='h264'))
        head = path.read_bytes()[:4096]
        self.assertLess(head.find(b'moov'), head.find(b'mdat') if b'mdat' in head else 10**9, 'moov before mdat')

    def test_hevc_main_and_main10_tagged_hvc1(self):
        for profile, pix, label, depth in (('main', 'yuv420p', 'Main', 8), ('main10', 'yuv420p10le', 'Main 10', 10)):
            with self.subTest(profile=profile):
                path, _ = self.render(self.project(), dict(codec='hevc', profile=profile))
                info = probe(path)
                self.check_common(path, info, codec='hevc', pix=pix, profile=label, depth=depth)
                self.assertEqual(self.video(info)['codec_tag_string'], 'hvc1')

    def test_av1_main10(self):
        path, _ = self.render(self.project(), dict(codec='av1', profile='main10', crf=30))
        info = probe(path)
        self.check_common(path, info, codec='av1', pix='yuv420p10le', profile='Main', depth=10)


class Intra(DeliveryCase):
    def test_prores_profiles(self):
        # FFmpeg's ProRes decoder reports 4444 as 12-bit (the format's decode depth); prores_ks encodes
        # it from 10-bit input.
        for profile, tag, label, pix in (('proxy', 'apco', 'Proxy', 'yuv422p10le'), ('lt', 'apcs', 'LT', 'yuv422p10le'),
                                         ('standard', 'apcn', 'Standard', 'yuv422p10le'), ('hq', 'apch', 'HQ', 'yuv422p10le'),
                                         ('4444', 'ap4h', '4444', 'yuv444p12le'), ('4444xq', 'ap4x', 'XQ', 'yuv444p12le')):
            with self.subTest(profile=profile):
                path, _ = self.render(self.project(start_timecode=86400), dict(codec='prores', profile=profile))
                info = probe(path)
                self.check_common(path, info, codec='prores', pix=pix, profile=label, depth=10, timecode='01:00:00:00',
                                  audio='pcm_s24le')
                self.assertEqual(self.video(info)['codec_tag_string'], tag)

    def test_prores_4444_alpha_from_a_title_over_nothing_and_a_png(self):
        path, _ = self.render(self.project(alpha_only=True), dict(codec='prores', profile='4444', alpha=True))
        self.assertEqual(self.video(probe(path))['pix_fmt'], 'yuva444p12le')
        alpha = array('H', subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', 'alphaextract,format=gray16le',
                                                     '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'], timeout=60))
        corner, inside = alpha[2 * W + 2], alpha[60 * W + 128]
        self.assertLess(corner, 1024, 'nothing on the timeline there: transparent')
        self.assertGreater(inside, 64000, 'the PNG is opaque there')
        without, _ = self.render(self.project(alpha_only=True), dict(codec='prores', profile='4444'))
        self.assertEqual(self.video(probe(without))['pix_fmt'], 'yuv444p12le', 'alpha only when asked for')

    def test_nested_sequence_keeps_transparent_areas(self):
        child = self.project(alpha_only=True)
        parent = self.project(extra=dict(media=[dict(id='nested', nestedProject=child)],
                                         clips=[dict(type='video', track=1, mediaId='nested', start=0, duration=1)]))
        for options in [dict(codec='prores', profile='4444', alpha=True), dict(codec='png', alpha=True)]:
            with self.subTest(codec=options['codec']):
                path, _ = self.render(parent, options)
                frame = sorted(path.glob('*.png'))[0] if path.is_dir() else path
                alpha = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(frame), '-vf', 'alphaextract,format=gray',
                                                 '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'], timeout=60)
                self.assertLess(alpha[2*W + 2], 5)
                self.assertGreater(alpha[60*W + 128], 250)

    def test_alpha_refused_where_the_format_has_none(self):
        with self.assertRaisesRegex(ValueError, 'cannot carry alpha'):
            delivery.normalize(dict(codec='prores', profile='hq', alpha=True))
        with self.assertRaisesRegex(ValueError, 'cannot carry alpha'):
            delivery.normalize(dict(codec='dnxhr', profile='444', alpha=True))

    def test_8bit_dnxhr_preserves_alternating_chroma_rows(self):
        source = self.media / 'chroma-stripes.mkv'
        ffmpeg('-f', 'lavfi', '-i', f"nullsrc=s={W}x{H},format=yuv422p,geq=lum=128:cb='64+128*mod(Y,2)':cr=128",
               '-t', '1', '-c:v', 'ffv1', str(source))
        project = self.project()
        project['media'][0]['src'] = self.src(source)
        project['clips'] = [dict(type='video', track=1, mediaId='m', start=0, duration=.5)]
        for profile in ('lb', 'sq', 'hq'):
            with self.subTest(profile=profile):
                path, _ = self.render(project, dict(codec='dnxhr', profile=profile))
                plane = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', 'extractplanes=u',
                                                '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'], timeout=60)
                center = H//2 * (W//2) + W//4
                self.assertGreater(plane[center + W//2] - plane[center], 90)

    def test_nested_sequence_preserves_delivery_chroma(self):
        # One-pixel Cb rows disappear at 4:2:0 and one-pixel Cr columns disappear at 4:2:2.
        source = self.media / 'nested-chroma.mkv'
        ffmpeg('-f', 'lavfi', '-i', f"nullsrc=s={W}x{H},format=yuv444p,"
               "geq=lum=128:cb='80+96*mod(Y,2)':cr='80+96*mod(X,2)'",
               '-t', '0.5', '-c:v', 'ffv1', str(source))
        clip = dict(type='video', track=1, mediaId='m', start=0, duration=.5)
        child = self.project(extra=dict(media=[dict(id='m', src=self.src(source))], clips=[clip]))
        parent = self.project(extra=dict(media=[dict(id='m', nestedProject=child)], clips=[clip]))
        for codec, profile in [('dnxhr', 'hq'), ('prores', '4444'), ('png', '8')]:
            with self.subTest(codec=codec, profile=profile):
                path, _ = self.render(parent, dict(codec=codec, profile=profile))
                frame = sorted(path.glob('*.png'))[0] if path.is_dir() else path
                pixels = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(frame), '-frames:v', '1',
                                                  '-pix_fmt', 'yuv444p', '-f', 'rawvideo', 'pipe:1'], timeout=60)
                center = H//2 * W + W//2
                cb = W*H + center
                self.assertGreater(pixels[cb + W] - pixels[cb], 70)
                if codec != 'dnxhr':
                    cr = 2*W*H + center
                    self.assertGreater(pixels[cr + 1] - pixels[cr], 70)

    def test_dnxhr_profiles_in_mov_and_mxf(self):
        for profile, label, pix, depth in (('lb', 'DNXHR LB', 'yuv422p', 8), ('sq', 'DNXHR SQ', 'yuv422p', 8), ('hq', 'DNXHR HQ', 'yuv422p', 8),
                                           ('hqx', 'DNXHR HQX', 'yuv422p10le', 10), ('444', 'DNXHR 444', 'yuv444p10le', 10)):
            for container in ('mov', 'mxf'):
                with self.subTest(profile=profile, container=container):
                    path, _ = self.render(self.project(start_timecode=90000, fps=25),
                                          dict(codec='dnxhr', profile=profile, container=container))
                    info = probe(path)
                    self.check_common(path, info, codec='dnxhd', pix=pix, profile=label, depth=depth, timecode='01:00:00:00',
                                      audio='pcm_s24le', frames=25 * SECONDS, fps=25)
                    if container == 'mxf':
                        self.assertEqual(info['format']['format_name'], 'mxf')

    def test_dnxhr_size_and_mxf_rate_are_refused_before_the_job(self):
        spec = delivery.normalize(dict(codec='dnxhr', profile='hq', container='mxf'))
        self.assertTrue(any('at least 256' in p for p in delivery.frame_problems(spec, 160, 90, 24)))
        self.assertTrue(any('MXF does not carry 12' in p for p in delivery.frame_problems(spec, 1920, 1080, 12)))
        with self.assertRaisesRegex(ValueError, 'DNxHR needs a frame of at least'):
            self.jobs_start_and_wait(dict(self.project(), width=160, height=90), dict(codec='dnxhr'))

    def jobs_start_and_wait(self, project, options):
        identifier = self.jobs.start(project, options)
        while self.jobs.status(identifier)['state'] == 'rendering':
            time.sleep(.1)
        status = self.jobs.status(identifier)
        self.jobs.release(identifier)
        if status['state'] == 'failed':
            raise ValueError(status['error'])
        return status


class ImageSequences(DeliveryCase):
    def check_sequence(self, folder, extension, pix, *, start=0, padding=7, count=FPS * SECONDS):
        files = sorted(folder.glob(f'*.{extension}'))
        self.assertEqual(len(files), count)
        self.assertEqual(files[0].name, f'{folder.name}_{start:0{padding}d}.{extension}')
        self.assertEqual(files[-1].name, f'{folder.name}_{start + count - 1:0{padding}d}.{extension}')
        stream = probe(files[3])['streams'][0]
        self.assertEqual(stream['pix_fmt'], pix)
        sound = folder / f'{folder.name}.wav'
        info = probe(sound)
        self.assertEqual(info['streams'][0]['codec_name'], 'pcm_s24le')
        self.assertAlmostEqual(float(info['format']['duration']), count / FPS, delta=.002)
        return files

    def test_png_8_and_16_bit_with_numbering(self):
        folder, _ = self.render(self.project(), dict(codec='png', profile='8', startNumber=86400, padding=8))
        self.check_sequence(folder, 'png', 'rgb24', start=86400, padding=8)
        folder, _ = self.render(self.project(), dict(codec='png', profile='16'))
        files = self.check_sequence(folder, 'png', 'rgb48be')
        self.assertGreater(self.levels(files[5], 'gray16le', frame=0), 256, 'a 16-bit PNG keeps the 10-bit ramp')

    def test_png_alpha(self):
        folder, _ = self.render(self.project(alpha_only=True), dict(codec='png', profile='16', alpha=True))
        files = sorted(folder.glob('*.png'))
        self.assertEqual(probe(files[0])['streams'][0]['pix_fmt'], 'rgba64be')

    def test_tiff_16_dpx_10_and_exr_half(self):
        # OpenEXR half float decodes as gbrpf16le.
        for codec, extension, pix in (('tiff', 'tif', 'rgb48le'), ('dpx', 'dpx', 'gbrp10le'), ('exr', 'exr', 'gbrpf16le')):
            with self.subTest(codec=codec):
                folder, _ = self.render(self.project(), dict(codec=codec))
                files = self.check_sequence(folder, extension, pix)
                if codec != 'exr':
                    self.assertGreater(self.levels(files[5], 'gray16le', frame=0), 256)


class Audio(DeliveryCase):
    def test_pcm_and_aac_choices(self):
        for codec, audio, expected in (('prores', 'pcm16', 'pcm_s16le'), ('prores', 'pcm24', 'pcm_s24le'), ('prores', 'pcm32f', 'pcm_f32le'),
                                       ('hevc', 'aac', 'aac'), ('dnxhr', 'pcm16', 'pcm_s16le')):
            with self.subTest(codec=codec, audio=audio):
                path, _ = self.render(self.project(), dict(codec=codec, audioCodec=audio))
                sound = next(s for s in probe(path)['streams'] if s['codec_type'] == 'audio')
                self.assertEqual((sound['codec_name'], sound['channels'], sound['sample_rate']), (expected, 2, '48000'))

    def test_wav_bit_depths(self):
        for audio, expected in (('pcm16', 'pcm_s16le'), ('pcm24', 'pcm_s24le'), ('pcm32f', 'pcm_f32le')):
            with self.subTest(audio=audio):
                path, _ = self.render(self.project(), dict(codec='wav', audioCodec=audio))
                self.assertEqual(probe(path)['streams'][0]['codec_name'], expected)

    def test_aac_bitrate(self):
        path, _ = self.render(self.project(), dict(codec='h264', audioCodec='aac', audioBitrate=320))
        sound = next(s for s in probe(path)['streams'] if s['codec_type'] == 'audio')
        self.assertGreater(int(sound['bit_rate']), 200_000)


class Validation(unittest.TestCase):
    def test_every_option_is_checked_at_the_boundary(self):
        bad = [dict(codec='h265'), dict(codec='h264', profile='main10'), dict(codec='prores', container='mp4'),
               dict(codec='h264', rateControl='vbr'), dict(codec='h264', crf=99, rateControl='crf', profile='high'),
               dict(codec='h264', videoBitrate=10, rateControl='bitrate'), dict(codec='h264', speed='ludicrous'),
               dict(codec='prores', speed='fast'), dict(codec='h264', encoder='cuda'), dict(codec='h264', encoder='nvenc', rateControl='twopass'),
               dict(codec='h264', colorSpace='pq'), dict(codec='hevc', profile='main', colorSpace='pq'), dict(codec='h264', audioCodec='pcm24'),
               dict(codec='dnxhr', container='mxf', audioCodec='pcm32f'), dict(codec='h264', reel='A/B'), dict(codec='h264', title='x' * 201),
               dict(codec='h264', comment='line\nbreak'), dict(codec='png', startNumber=-1), dict(codec='png', padding=12),
               dict(codec='h264', startNumber=3), dict(codec='h264', outputSize=dict(width=101, height=100)),
               dict(codec='h264', fit='fill'), dict(codec='h264', burnIns=dict(fields=['nope'])),
               dict(codec='h264', burnIns=dict(fields=['watermark'])), dict(codec='hevc', colorSpace='pq', hdr=dict(maxLuminance=50)),
               dict(codec='hevc', colorSpace='pq', hdr=dict(lightLevels='manual', maxCll=100, maxFall=400)),
               dict(codec='prores', hdr=dict()), dict(codec='h264', timecode='yes'), dict(codec='h264', alpha=1),
               dict(codec='h264', captionFiles=['ass'])]
        for options in bad:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    delivery.normalize(options)

    def test_legacy_requests_keep_their_meaning(self):
        spec = delivery.normalize(dict(codec='h264', crf=20, audioBitrate=192))
        self.assertEqual((spec['encoder'], spec['pix_fmt'], spec['rate'], spec['container'], spec['audio']),
                         ('libx264', 'yuv420p', dict(mode='crf', crf=20, kbps=None, speed='veryfast'), 'mp4', 'aac'))
        self.assertEqual(delivery.normalize(dict(codec='prores'))['profile'], 'hq')
        with self.assertRaisesRegex(ValueError, 'quality'):
            delivery.normalize(dict(codec='h264', crf=40))

    def test_h264_level_follows_frame_size_and_rate(self):
        self.assertEqual(delivery.h264_level(1920, 1080, 24), '4.0')
        self.assertEqual(delivery.h264_level(1920, 1080, 60), '4.2')
        self.assertEqual(delivery.h264_level(3840, 2160, 30), '5.1')
        self.assertEqual(delivery.h264_level(3840, 2160, 60), '5.2')
        self.assertEqual(delivery.h264_level(8192, 4320, 120), '6.2')
        self.assertIsNone(delivery.h264_level(16384, 8640, 30))


if __name__ == '__main__':
    unittest.main()
