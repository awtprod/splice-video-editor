"""Colour across source tags, found validating clip effects:

- a graded clip with a crop, mask or key shows the colour of the same clip graded without them, for
  untagged, Rec. 601, Rec. 709, 16-bit and HDR (PQ, HLG) sources, in 8-, 10- and 16-bit deliveries
  (FFmpeg converted the source to the grade's output matrix before the grade, which then read it
  with the source's own matrix);
- a graded clip's crop and mask still cut the picture;
- blend modes and image sequences read an untagged source as Rec. 601, as the monitor does (both
  relabelled the picture as Rec. 709);
- a graded clip with alpha (ProRes 4444, a PNG) keeps its 16-bit alpha with or without effects.
"""
import array
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_effects_regressions import Fixture, W, H, FPS, TAGS, ffmpeg  # noqa: E402
from render_movie import render_to_file  # noqa: E402

GRADE = {'exposure': 0.3, 'saturation': 120}
GRADIENT = f"nullsrc=s={W}x{H}:r={FPS}:d=0.2,format=gbrp,geq=r='40+180*X/W':g='200-150*Y/H':b='60+120*(X+Y)/(W+H)'"
BT601 = ['-colorspace', 'smpte170m', '-color_primaries', 'smpte170m', '-color_trc', 'smpte170m']


def hdr_source(transfer):
    return ['-f', 'lavfi', '-i', GRADIENT, '-vf', 'scale=out_color_matrix=bt2020:out_range=tv,format=yuv444p10le,'
            f'setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc:range=tv',
            '-c:v', 'prores_ks', '-profile:v', '4']


SOURCES = {
    'untagged.mp4': ['-f', 'lavfi', '-i', GRADIENT, '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-qp', '0'],
    'bt601.mp4': ['-f', 'lavfi', '-i', GRADIENT, '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-qp', '0', *BT601],
    'bt709.mp4': ['-f', 'lavfi', '-i', GRADIENT, '-vf', 'scale=out_color_matrix=bt709', '-pix_fmt', 'yuv420p', '-c:v', 'libx264',
                  '-qp', '0', *TAGS],
    'deep16.mkv': ['-f', 'lavfi', '-i', f"nullsrc=s={W}x{H}:r={FPS}:d=0.2,format=yuv444p16le,"
                   "geq=lum='4096+300*X+40*Y':cb='20000+90*X':cr='45000-120*Y'", '-c:v', 'ffv1'],
    'pq.mov': hdr_source('smpte2084'),
    'hlg.mov': hdr_source('arib-std-b67'),
}
# Two pictures, each as an untagged (Rec. 601) and a Rec. 709-tagged file.
PICTURES = {'top': ('40+180*X/W', '200-150*Y/H', '60+120*(X+Y)/(W+H)'), 'bot': ('220-150*Y/H', '50+120*X/W', '180-100*X/W')}


def rgb48(path, frame=2):
    """One frame as 16-bit R'G'B' (a file, or an image-sequence folder)."""
    if path.is_dir():
        path = sorted(p for p in path.iterdir() if p.suffix in ('.tif', '.png'))[frame]
        select = 'format=rgb48le'
    else:
        select = f'select=eq(n\\,{frame}),format=rgb48le'
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf', select, '-frames:v', '1', '-f', 'rawvideo', '-'],
                         capture_output=True, check=True, timeout=120).stdout
    return array.array('H', raw)


def alpha16(path, frame=2):
    if path.is_dir():
        path = sorted(p for p in path.iterdir() if p.suffix == '.png')[frame]
        raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf', 'format=rgba64le', '-frames:v', '1',
                              '-f', 'rawvideo', '-'], capture_output=True, check=True, timeout=120).stdout
        return array.array('H', raw)[3::4]
    raw = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-vf', f'select=eq(n\\,{frame}),format=yuva444p16le',
                          '-frames:v', '1', '-f', 'rawvideo', '-'], capture_output=True, check=True, timeout=120).stdout
    return array.array('H', raw)[3*W*H:]


def difference(a, b, x0=0, x1=W, y0=0, y1=H):
    """(max, mean) |a-b| over a region, in 8-bit codes."""
    d = [abs(a[(y*W+x)*3+c]-b[(y*W+x)*3+c])/257 for y in range(y0, y1) for x in range(x0, x1) for c in range(3)]
    return max(d), sum(d)/len(d)


def clip(media, track=1, **extra):
    return {'id': f'{media}-{track}', 'type': 'video', 'mediaId': media, 'track': track, 'start': 0, 'duration': .2, 'in': 0,
            'embeddedAudio': False, 'transition': 'none', **extra}


class ChainOutputTags(unittest.TestCase):
    """The alpha branch of a graded clip with effects is tagged with what the grade leaves."""
    def test_the_last_colour_step_decides(self):
        from render_movie import _chain_output_tags
        lut = "lut1d=file='/tmp/a,b.cube':interp=linear"
        cases = {
            f',setparams=colorspace=smpte170m:range=tv,zscale,format=gbrpf32le,{lut},zscale=m=709:r=limited': ('bt709', 'tv'),
            ',zscale=p=bt2020,zscale=t=smpte2084:m=bt2020nc:r=limited:npl=203.0,format=yuv444p10le': ('bt2020nc', 'tv'),
            ',zscale=m=170m:r=limited,format=yuva444p16le[g];[g][a]blend=c3_opacity=0': ('smpte170m', 'tv'),
            # Keyframed grades mix their branches with blend, then leave through one zscale.
            ",split=2[k0][k1];[k0]null,exposure=exposure=1[m0];[k1]null[m1];[m0][m1]blend=all_expr='if(lt(T,1),A,B)';[m]null,"
            'zscale=m=2020_ncl:r=full': ('bt2020nc', 'pc'),
            # Ending on R'G'B' (or never tagged), the Y'CbCr after it is FFmpeg's to choose.
            ',zscale=m=709:r=limited,format=yuv444p10le,' + lut: None,
            ',format=gbrp16le,lutrgb=r=val*1.1': None,
            ',null': None,
            ',zscale=m=input:r=limited': None,
        }
        for chain, expected in cases.items():
            with self.subTest(chain=chain):
                self.assertEqual(_chain_output_tags(chain), expected)


class ColorTags(Fixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name, arguments in SOURCES.items():
            ffmpeg(*arguments, cls.media/name)
        for name, (r, g, b) in PICTURES.items():
            graph = f"nullsrc=s={W}x{H}:r={FPS}:d=0.2,format=gbrp,geq=r='{r}':g='{g}':b='{b}'"
            ffmpeg('-f', 'lavfi', '-i', graph, '-vf', 'scale=out_color_matrix=bt601:out_range=tv,format=yuv444p', '-c:v', 'libx264',
                   '-qp', '0', cls.media/f'{name}601.mp4')
            ffmpeg('-f', 'lavfi', '-i', graph, '-vf', 'scale=out_color_matrix=bt709:out_range=tv,format=yuv444p', '-c:v', 'libx264',
                   '-qp', '0', *TAGS, cls.media/f'{name}709.mp4')
        # A ProRes 4444 clip and a 16-bit PNG whose alpha ramps across the frame.
        ffmpeg('-f', 'lavfi', '-i', f"nullsrc=s={W}x{H}:r={FPS}:d=0.2,format=yuva444p16le,geq=lum='9000+200*X':cb=28000:cr=38000:"
               "a='409*X'", '-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le', cls.media/'alpha.mov')
        ffmpeg('-f', 'lavfi', '-i', f"nullsrc=s={W}x{H},format=rgba64le,geq=r='4096+300*X':g=30000:b=20000:a='409*X'",
               '-frames:v', '1', '-pix_fmt', 'rgba64be', cls.media/'alpha.png')
        names = [*SOURCES, *(f'{p}{t}.mp4' for p in PICTURES for t in ('601', '709')), 'alpha.mov']
        cls.items = [{'id': name, 'name': name, 'src': f'/media/{name}', 'type': 'video', 'duration': .2} for name in names]
        cls.items.append({'id': 'alpha.png', 'name': 'alpha.png', 'src': '/media/alpha.png', 'type': 'image', 'duration': 1})

    def render(self, clips, name, options):
        out = self.out(name if options['codec'] in ('tiff', 'png') else name+'.mov')
        render_to_file({'name': 't', 'width': W, 'height': H, 'fps': FPS, 'media': self.items, 'clips': clips}, out, options=options)
        return out

    def test_a_graded_clips_effects_keep_its_colour_on_every_source(self):
        deliveries = {'H.264 8-bit': {'codec': 'h264', 'rateControl': 'crf', 'crf': 4}, 'ProRes HQ 10-bit': {'codec': 'prores', 'profile': 'hq'},
                      'TIFF 16-bit': {'codec': 'tiff'}}
        crop = {'crop': {'left': 5}}
        for source in SOURCES:
            for label, options in deliveries.items():
                if label.startswith('H.264') and source not in ('untagged.mp4', 'bt601.mp4', 'deep16.mkv'):
                    continue
                with self.subTest(source=source, delivery=label):
                    name = f'{source}-{label.split()[0]}'
                    alone = rgb48(self.render([clip(source, color=GRADE)], name+'-grade', options))
                    cropped = rgb48(self.render([clip(source, color=GRADE, effects=crop)], name+'-crop', options))
                    # Away from the crop (it is 5 % of the width).
                    worst, mean = difference(alone, cropped, x0=W//8, x1=W-8, y0=8, y1=H-8)
                    # H.264's own noise is about 2 codes; before the fix untagged and Rec. 601 sources were 6 (mean) and 31 (max) off.
                    limit = (12, 3) if label.startswith('H.264') else (2, .6)
                    self.assertLessEqual(worst, limit[0], f'max {worst:.2f} codes, mean {mean:.3f}')
                    self.assertLessEqual(mean, limit[1], f'max {worst:.2f} codes, mean {mean:.3f}')

    def test_a_graded_clips_crop_and_mask_still_cut_the_picture(self):
        effects = {'crop': {'crop': {'left': 25}}, 'mask': {'masks': [{'shape': 'rectangle', 'x': 75, 'y': 50, 'width': 50, 'height': 100}]}}
        for label, fx in effects.items():
            for codec, options in (('ProRes', {'codec': 'prores', 'profile': 'hq'}), ('TIFF', {'codec': 'tiff'}),
                                   ('ProRes 4444 alpha', {'codec': 'prores', 'profile': '4444', 'alpha': True})):
                with self.subTest(effect=label, delivery=codec):
                    under = clip('bot709.mp4')
                    name = f'cut-{label}-{codec.split()[0]}{len(codec)}'
                    layered = rgb48(self.render([under, clip('top709.mp4', 2, color=GRADE, effects=fx)], name, options))
                    background = rgb48(self.render([under], name+'-under', options))
                    graded = rgb48(self.render([clip('top709.mp4', color=GRADE)], name+'-top', options))
                    # Left quarter: the backdrop shows through; right half: the graded layer.
                    self.assertLessEqual(difference(layered, background, x0=4, x1=W//4-4)[1], 1, 'the cut part shows the clip beneath')
                    self.assertLessEqual(difference(layered, graded, x0=W//2+4, x1=W-4)[1], 1, 'the kept part shows the graded clip')

    def test_blend_modes_and_image_sequences_read_an_untagged_source_as_rec601(self):
        cases = {'screen': lambda t: [clip(f'bot{t}.mp4'), clip(f'top{t}.mp4', 2, effects={'blend': 'screen'})],
                 'softLight and mask': lambda t: [clip(f'bot{t}.mp4'), clip(f'top{t}.mp4', 2, effects={
                     'blend': 'softLight', 'masks': [{'shape': 'ellipse', 'x': 50, 'y': 50, 'width': 60, 'height': 70, 'feather': 8}]})],
                 'two plain clips': lambda t: [clip(f'bot{t}.mp4'), clip(f'top{t}.mp4', 2, opacity=70)],
                 'a graded clip': lambda t: [clip(f'bot{t}.mp4'), clip(f'top{t}.mp4', 2, color=GRADE, opacity=70)]}
        for label, clips in cases.items():
            for codec, options in (('ProRes', {'codec': 'prores', 'profile': 'hq'}), ('TIFF', {'codec': 'tiff'}),
                                   ('ProRes 4444 alpha', {'codec': 'prores', 'profile': '4444', 'alpha': True})):
                with self.subTest(case=label, delivery=codec):
                    name = f'tags-{label.replace(" ", "-")}-{codec.split()[0]}{len(codec)}'
                    untagged = rgb48(self.render(clips('601'), name+'-601', options))
                    tagged = rgb48(self.render(clips('709'), name+'-709', options))
                    # The same picture: 601 and 709 encodings differ by rounding (before: 20-25 codes, mean 2.6-4.7).
                    worst, mean = difference(untagged, tagged)
                    self.assertLessEqual(worst, 6, f'max {worst:.2f} codes, mean {mean:.3f}')
                    self.assertLessEqual(mean, 1.2, f'max {worst:.2f} codes, mean {mean:.3f}')

    def test_a_graded_clip_with_alpha_keeps_its_alpha(self):
        options = {'codec': 'png', 'profile': '16', 'alpha': True}
        for source in ('alpha.mov', 'alpha.png'):
            ungraded = alpha16(self.render([clip(source)], f'alpha-{source}-plain', options))
            self.assertGreater(len(set(ungraded)), 120, 'the source alpha ramps')
            for label, extra in (('graded', {'color': GRADE}), ('graded and cropped', {'color': GRADE, 'effects': {'crop': {'top': 10}}})):
                with self.subTest(source=source, clip=label):
                    out = self.render([clip(source, **extra)], f'alpha-{source}-{label.replace(" ", "-")}', options)
                    alpha = alpha16(out)
                    rows = slice(W*(H//5), None)   # below the crop
                    worst = max(abs(a-b) for a, b in zip(alpha[rows], ungraded[rows]))
                    # Before: a graded video was opaque (65535 off); a graded still kept 8 bits (up to 320 off).
                    self.assertLessEqual(worst, 16, f'alpha differs by up to {worst} of 65535')


if __name__ == '__main__':
    unittest.main()
