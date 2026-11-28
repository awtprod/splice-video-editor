"""Camera RAW through multicam, nested sequences and subclips.

A RAW clip reached through a multicam angle, a nested sequence or a subclip must develop with the
timeline clip's own rawSettings, exactly as the same clip cut straight into the timeline does, in
ProRes and in H.264. RAW intermediates are BT.709-tagged, so the H.264 delivery's untagged-to-709
conform must leave them alone: an H.264 RAW export decodes to the same colours as its ProRes master.
"""
from __future__ import annotations

import array
import json
import subprocess
import unittest
from unittest import mock

import media_links
import media_proxy
import raw_bayer
import raw_decode
import render_movie
from tests import raw_fixtures
from tests.test_camera_raw import _IsolatedDataDirMixin, _extract_rgb_frame, _sample_patch_rgb

W, H, FPS, FRAMES = 128, 96, 24, 12
SETTINGS = {'decodeUsing': 'clip', 'iso': 800, 'exposure': 1.0, 'kelvin': 7200, 'tint': 10,
            'colorSpace': 'rec709', 'gamma': 'rec709', 'highlightRecovery': True, 'debayerQuality': 'full'}
DELIVERIES = (('prores', {'codec': 'prores', 'profile': 'hq', 'limiter': False}, '.mov'),
              ('h264', {'codec': 'h264', 'limiter': False}, '.mp4'))


def _marker_frame(rgb, width):
    """The source frame number: the fixture's white 4x4 marker sits at x = 4 * frame. The centre of
    the bright run is used, since a brightened development bleeds the demosaic a pixel to the left."""
    bright = [x for x in range(width - 8) if min(rgb[(width + x) * 3:(width + x) * 3 + 3]) > 200]
    return round((sum(bright) / len(bright) - 1.5) / 4) if bright else None


def _patches(path, frame=2):
    width, height, rgb = _extract_rgb_frame(path, frame_index=frame)
    return [_sample_patch_rgb(width, height, rgb, col, row) for row in range(2) for col in range(3)]


def _exact_patches(path, frame=2):
    """Like _patches, but decoded to RGB with accurate rounding: the default conversion is up to ~2 codes
    low on a 10-bit ProRes, too much slack for an absolute check against the reference develop."""
    out = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf',
                          f'select=eq(n\\,{frame}),scale=flags=bicubic+accurate_rnd+full_chroma_int',
                          '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                         capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    assert len(out) == W * H * 3, f'{path}: expected a {W}x{H} frame'
    return [_sample_patch_rgb(W, H, out, col, row) for row in range(2) for col in range(3)]


def _delta(a, b):
    return max(abs(x - y) for pa, pb in zip(a, b) for x, y in zip(pa, pb))


def _tags(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=color_space,color_primaries,color_transfer', '-of', 'json', str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)['streams'][0]


class TestRawThroughEditorialStructures(_IsolatedDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        first = raw_fixtures.write_cinemadng_sequence(
            self.tmp / 'B001_seq', prefix='B001_', frames=FRAMES, fps=FPS, bit_depth=12,
            width=W, height=H, with_wav=True, reel='B001', timecode=(1, 0, 0, 0))
        self.src = media_links.link_many([str(first)], validate=media_proxy.decodable)[0]['src']
        info = media_proxy.describe(self.src)
        self.assertTrue(info['raw'])
        self.raw_item = {**info, 'id': 'raw1', 'name': first.name, 'src': self.src, 'type': 'video'}
        self.first = first
        # A non-RAW second camera for the multicam.
        self.other = self.media_dir / 'cam-b.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i',
                        f'color=c=0x204060:s={W}x{H}:r={FPS}:d={FRAMES / FPS}', '-f', 'lavfi', '-i',
                        f'sine=frequency=330:sample_rate=48000:duration={FRAMES / FPS}', '-shortest',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.other)],
                       check=True, timeout=120)
        self.other_item = {'id': 'camb', 'name': 'cam-b.mp4', 'src': '/media/cam-b.mp4', 'type': 'video',
                           'duration': FRAMES / FPS, 'width': W, 'height': H, 'fps': FPS}
        self.references = {}

    def _project(self, media, clips, name='RAW editorial'):
        return {'name': name, 'width': W, 'height': H, 'fps': FPS, 'media': media, 'clips': clips}

    def _clip(self, media_id, **extra):
        clip = {'id': 'c1', 'type': 'video', 'track': 1, 'mediaId': media_id, 'start': 0.0, 'in': 0.0,
                'duration': 0.375, 'transition': 'none', 'rawSettings': dict(SETTINGS)}
        clip.update(extra)
        return clip

    def _render(self, project, label, options, suffix):
        out = self.tmp / f'{label}{suffix}'
        calls = []
        real = raw_decode.decode_to_intermediate

        def spy(*args, **kwargs):
            calls.append(args[2] if len(args) > 2 else kwargs.get('settings'))
            return real(*args, **kwargs)

        with mock.patch('raw_decode.decode_to_intermediate', side_effect=spy):
            render_movie.render_to_file(project, out, options=dict(options))
        self.assertTrue(out.is_file(), label)
        self.assertTrue(calls, f'{label}: the RAW clip was never developed')
        for settings in calls:
            self.assertEqual(settings, SETTINGS, f'{label}: the RAW clip lost its rawSettings')
        return out

    def _reference(self, kind, options, suffix):
        """The RAW clip cut straight into the timeline with the same settings (and, for contrast, without)."""
        if kind not in self.references:
            plain = self._render(self._project([self.raw_item], [self._clip('raw1', **{'in': 0.125})]),
                                 f'plain-{kind}', options, suffix)
            default = self.tmp / f'default-{kind}{suffix}'
            render_movie.render_to_file(
                self._project([self.raw_item], [self._clip('raw1', **{'in': 0.125, 'rawSettings': {'decodeUsing': 'camera'}})]),
                default, options=dict(options))
            self.assertGreater(_delta(_patches(plain), _patches(default)), 12,
                               'the clip settings must visibly change the development')
            self.references[kind] = plain
        return self.references[kind]

    def _assert_matches_reference(self, out, kind, options, suffix, label):
        reference = self._reference(kind, options, suffix)
        delta = _delta(_patches(out), _patches(reference))
        self.assertLessEqual(delta, 2.5, f'{label} {kind}: developed {delta:.2f} codes off the plain RAW clip')
        for n in range(3):
            width, _, rgb = _extract_rgb_frame(out, frame_index=n)
            self.assertEqual(_marker_frame(rgb, width), 3 + n, f'{label} {kind}: frame {n} reads the wrong source frame')

    def test_raw_multicam_angle_exports_with_its_raw_settings(self):
        multicam = {'id': 'mc1', 'name': 'MC', 'type': 'video', 'duration': FRAMES / FPS, 'width': W, 'height': H,
                    'multicam': {'audioMode': 'camera1', 'audioAngle': 0, 'angles': [
                        {'mediaId': 'camb', 'offset': 0, 'in': 0, 'duration': FRAMES / FPS},
                        {'mediaId': 'raw1', 'offset': 0, 'in': 0, 'duration': FRAMES / FPS}]}}
        # ProRes: the sound follows the RAW angle (its sidecar WAV); H.264: camera 1's sound.
        for (kind, options, suffix), audio_mode in zip(DELIVERIES, ('follow', 'camera1')):
            multicam['multicam']['audioMode'] = audio_mode
            project = self._project([self.raw_item, self.other_item, multicam],
                                    [self._clip('mc1', **{'in': 0.125, 'multicamAngle': 1})])
            out = self._render(project, f'multicam-{kind}', options, suffix)
            self._assert_matches_reference(out, kind, options, suffix, 'multicam angle')
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                                    'stream=codec_type', '-of', 'csv=p=0', str(out)], capture_output=True, text=True)
            self.assertEqual(probe.stdout.strip(), 'audio', f'multicam {audio_mode}: no sound delivered')

    def test_raw_clip_inside_a_nested_sequence_exports_with_its_raw_settings(self):
        child = self._project([self.raw_item], [self._clip('raw1', **{'in': 0.125})], name='Child')
        nested = {'id': 'n1', 'name': 'Nest', 'type': 'video', 'duration': 0.375, 'width': W, 'height': H,
                  'nestedSequence': 'seq-child', 'nestedProject': child}
        for kind, options, suffix in DELIVERIES:
            project = self._project([nested], [self._clip('n1', rawSettings=None)])
            project['clips'][0].pop('rawSettings')
            out = self._render(project, f'nested-{kind}', options, suffix)
            self._assert_matches_reference(out, kind, options, suffix, 'nested sequence')

    def test_subclip_of_raw_media_exports_from_its_original_with_its_raw_settings(self):
        subclip = {**{key: value for key, value in self.raw_item.items() if key.startswith('raw')},
                   'id': 'sub1', 'name': 'B001 Subclip', 'src': self.src, 'type': 'video', 'duration': 0.375,
                   'parentMediaId': 'raw1', 'subclipIn': 0.125, 'subclipOut': 0.5, 'subclipLimits': True}
        self.assertIn('rawEstimated', subclip)
        for kind, options, suffix in DELIVERIES:
            project = self._project([self.raw_item, subclip], [self._clip('sub1')])
            out = self._render(project, f'subclip-{kind}', options, suffix)
            self._assert_matches_reference(out, kind, options, suffix, 'subclip')

    def _ground_truth(self, frame):
        """Source ``frame`` developed straight from its DNG by the reference decoder, with SETTINGS."""
        settings = raw_decode.normalize_raw_settings(SETTINGS, 'cinemadng', self.raw_item.get('rawDefaults'))
        dng = self.first.with_name(f'B001_{frame + 1:06d}.dng')
        width, height, rgb48 = raw_bayer.decode_dng_frame(dng, settings)
        samples = array.array('H')
        samples.frombytes(rgb48)
        rgb = [value * 255 / 65535 for value in samples]
        return [_sample_patch_rgb(width, height, rgb, col, row) for row in range(2) for col in range(3)]

    def test_h264_raw_export_is_not_conformed_twice(self):
        prores = self._reference('prores', DELIVERIES[0][1], '.mov')
        h264 = self._reference('h264', DELIVERIES[1][1], '.mp4')
        self.assertEqual(_tags(h264).get('color_space'), 'bt709')
        # Each delivery is held to the reference decode of the same source frame (export frame 2 = source
        # frame 5, as the clip starts at frame 3): a wrong intermediate tag shifts both deliveries together
        # (~18 codes), which the H.264-vs-ProRes comparison below barely sees.
        truth = self._ground_truth(5)
        for kind, out, limit in (('ProRes', prores, 3.0), ('H.264', h264, 4.0)):
            delta = _delta(_exact_patches(out), truth)
            self.assertLessEqual(delta, limit, f'{kind} RAW export decodes {delta:.2f} codes off the reference develop')
        delta = _delta(_patches(h264), _patches(prores))
        # A second Rec. 601 -> 709 conform of the already-709 intermediate moves these patches ~10 codes.
        self.assertLessEqual(delta, 3.0, f'H.264 RAW export decodes {delta:.2f} codes off its ProRes master')


if __name__ == '__main__':
    unittest.main()
