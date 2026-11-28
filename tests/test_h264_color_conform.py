"""H.264 and ProRes deliveries of the same timeline must decode to the same colours.

Untagged footage is Rec. 601 by convention. A streamed H.264 delivery labels its frames Rec. 709,
so the composite has to be conformed from Rec. 601 before the encoder (render_movie._delivery_tail);
without that step an untagged picture decodes about 10 codes off its ProRes master. Each timeline
region below is a solid colour, rendered once to ProRes (the reference) and once to H.264, then
decoded with Rec. 709 coefficients (the tags both files carry) and compared region by region.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import delivery
from premiere import MEDIA_ROOT
import render_movie
from render_movie import render_to_file

W, H, FPS = 128, 72, 25
TOLERANCE = 3  # 8-bit codes per channel
VERBOSE = bool(os.environ.get('SPLICE_TEST_VERBOSE'))
BT601_TAGS = ('-colorspace', 'smpte170m', '-color_primaries', 'smpte170m', '-color_trc', 'smpte170m')
BT709_TAGS = ('-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709')
WHEEL = {'grade': {'gain': [1, .6, 1, 1]}}


def ffmpeg(*arguments):
    subprocess.run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *arguments], check=True, timeout=120)


def solid(path, rgb, matrix, tags, seconds):
    """A lossless solid-colour clip whose YUV was made with ``matrix`` and carries ``tags``."""
    ffmpeg('-f', 'lavfi', '-i', f'color=c=0x{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}:s={W}x{H}:r={FPS}:d={seconds}',
           '-vf', f'format=gbrp,scale=out_color_matrix={matrix}:out_range=tv,format=yuv444p',
           '-c:v', 'libx264', '-qp', '0', *tags, str(path))


def region_colours(movie, regions):
    """Mean RGB of the middle frame of each one-second region, decoded as legal-range Rec. 709."""
    stream = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_streams',
                                                 '-of', 'json', str(movie)], timeout=60))['streams'][0]
    assert stream.get('color_space') == 'bt709', stream.get('color_space')
    raw = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(movie), '-vf',
                                   'scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=rgb24',
                                   '-f', 'rawvideo', 'pipe:1'], timeout=120)
    size = W * H * 3
    colours = []
    for second in range(regions):
        frame = raw[(second * FPS + FPS // 2) * size:(second * FPS + FPS // 2 + 1) * size]
        colours.append([sum(frame[channel::3]) / (W * H) for channel in range(3)])
    return colours


class H264MatchesProRes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        cls.media = Path(tempfile.mkdtemp(prefix='h264-conform-', dir=MEDIA_ROOT))
        cls.out = Path(tempfile.mkdtemp(prefix='h264-conform-out-'))
        sources = {'untagged': ((40, 160, 200), 'bt601', ()),
                   'untagged2': ((210, 150, 120), 'bt601', ()),
                   'bt601': ((40, 160, 200), 'bt601', BT601_TAGS),
                   'bt709': ((40, 160, 200), 'bt709', BT709_TAGS)}
        cls.sources = {}
        for name, (rgb, matrix, tags) in sources.items():
            path = cls.media / f'{name}.mov'
            solid(path, rgb, matrix, tags, 8)
            cls.sources[name] = dict(id=name, name=path.name, type='video', duration=8.0, fps=FPS,
                                     src='/media/' + str(path.relative_to(MEDIA_ROOT)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media, ignore_errors=True)
        shutil.rmtree(cls.out, ignore_errors=True)

    def compare(self, name, clips, labels):
        project = dict(name=name, width=W, height=H, fps=FPS, media=list(self.sources.values()), clips=clips)
        prores, h264 = self.out / f'{name}.mov', self.out / f'{name}.mp4'
        render_to_file(project, prores, options=dict(codec='prores'))
        render_to_file(project, h264, options=dict(codec='h264'))
        reference, delivered = region_colours(prores, len(labels)), region_colours(h264, len(labels))
        for label, want, got in zip(labels, reference, delivered):
            if VERBOSE:
                print(f'{name} {label}: ProRes {[round(v, 1) for v in want]} H.264 {[round(v, 1) for v in got]}')
            with self.subTest(region=label):
                self.assertLessEqual(max(abs(a - b) for a, b in zip(want, got)), TOLERANCE,
                                     f'{label}: H.264 {[round(v, 1) for v in got]} vs ProRes {[round(v, 1) for v in want]}')

    @staticmethod
    def clip(identity, media, start, track=1, **extra):
        return dict(id=identity, type='video', mediaId=media, track=track, start=start, duration=1, embeddedAudio=False,
                    **{'in': start}, **extra)

    def test_untagged_and_rec601_sources(self):
        clip = self.clip
        self.compare('single', [clip('u', 'untagged', 0), clip('s', 'bt601', 1), clip('r', 'bt709', 2)],
                     ['untagged', 'BT.601-tagged', 'BT.709-tagged'])

    def test_graded_over_ungraded_untagged_stack(self):
        clip = self.clip
        # Graded pictures are already labelled Rec. 709; the ungraded ones around and under them are not.
        self.compare('stack', [
            clip('b0', 'untagged', 0),
            clip('g1', 'untagged2', 1, color={'brightness': 50}),
            clip('g2', 'untagged2', 2, color=WHEEL),
            clip('b3', 'untagged', 3), clip('g3', 'untagged2', 3, track=2, color={'brightness': 50}, opacity=50),
            clip('b4', 'untagged', 4), clip('g4', 'untagged2', 4, track=2, color=WHEEL, opacity=50),
            clip('b5', 'untagged', 5), clip('g5', 'untagged2', 5, track=2, color=WHEEL),
            clip('b6', 'bt601', 6), clip('g6', 'untagged2', 6, track=2, color={'brightness': 50}, opacity=50),
        ], ['ungraded alone', 'legacy grade alone', 'wheel grade alone', 'legacy grade over ungraded at 50%',
            'wheel grade over ungraded at 50%', 'full-frame wheel grade over ungraded',
            'legacy grade over BT.601-tagged at 50%'])

    def picture_graphs(self, options):
        """The picture filter graphs a one-clip untagged timeline renders with."""
        project = dict(name='graph', width=W, height=H, fps=FPS, media=list(self.sources.values()),
                       clips=[self.clip('u', 'untagged', 0)])
        graphs = []
        def record(graph, directory, name):
            if name == 'picture':
                graphs.append(graph)
            return real(graph, directory, name)
        real = render_movie._graph_args
        target = self.out / ('graph.mp4' if options['codec'] == 'h264' else 'graph.mov')
        with mock.patch.object(render_movie, '_graph_args', record):
            render_to_file(project, target, options=options)
        self.assertTrue(graphs)
        return graphs

    def test_sdr_composite_stays_untagged_until_the_delivery_conform(self):
        # The black base layer of an SDR delivery carries no colour tags, so an untagged composite
        # reaches _delivery_tail untagged and is conformed there exactly once, the way the ProRes
        # master sees it. Only an HDR master labels its base (the PQ or HLG signal it is built in).
        for graph in self.picture_graphs(dict(codec='h264')):
            base = next(step for step in graph.split(';') if step.endswith('[base0]'))
            self.assertNotIn('setparams', base, base)
            self.assertIn('scale=out_color_matrix=bt709:out_range=tv', graph)
        options = dict(codec='prores', colorSpace='pq')
        if not delivery.unavailable_reason(options):
            for graph in self.picture_graphs(options):
                base = next(step for step in graph.split(';') if step.endswith('[base0]'))
                self.assertIn('setparams=color_primaries=bt2020:color_trc=smpte2084', base, base)


if __name__ == '__main__':
    unittest.main()
