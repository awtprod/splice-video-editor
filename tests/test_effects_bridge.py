import tempfile
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
import py_premiere
import premiere_effects
from premiere import save_project


class ExtendedEffectsRoundTrip(unittest.TestCase):
    def test_custom_title_fill_and_stroke_roundtrip(self):
        project = {'name':'Title', 'fps':25, 'media':[], 'clips':[{
            'type':'title','track':2,'title':'Card','start':0,'duration':2,
        }]}
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'original.prproj'
            saved = Path(directory) / 'saved.prproj'
            original.write_bytes(save_project(project))
            app = py_premiere.parse(str(original))
            clip = app.project.sequences[0].video_tracks[1].clips[0]
            premiere_effects.apply_extended(clip, {'titleStyle':{
                'color':'#e43522','strokeColor':'#1546dd','strokeWidth':3,
            }})
            app.project.save(str(saved))
            reopened = py_premiere.parse(str(saved))
            style = premiere_effects.read_extended(reopened.project.sequences[0].video_tracks[1].clips[0])['titleStyle']
            self.assertEqual((style['color'],style['strokeColor'],style['strokeWidth']),
                             ('#e43522','#1546dd',3))

    def test_title_style_uses_native_premiere_parameters(self):
        project = {'name':'Title', 'fps':25, 'media':[], 'clips':[{
            'type':'title','track':2,'title':'Card','start':0,'duration':2,
        }]}
        with tempfile.NamedTemporaryFile(suffix='.prproj') as file:
            file.write(save_project(project))
            file.flush()
            app = py_premiere.parse(file.name)
            clip = app.project.sequences[0].video_tracks[1].clips[0]
            premiere_effects.apply_extended(clip, {'titleStyle':{
                'x':42,'y':68,'fontFamily':'DM Sans','fontSize':10,
            }})
            saved_path = file.name + '.prproj'
            app.project.save(saved_path)
            reopened = py_premiere.parse(saved_path)
            clip = reopened.project.sequences[0].video_tracks[1].clips[0]
            style = premiere_effects.read_extended(clip)['titleStyle']
            self.assertEqual(style['x'], 42)
            self.assertEqual(style['y'], 68)
            self.assertEqual(style['fontSize'], 10)

    def test_unsupported_audio_edits_are_rejected(self):
        project = {'name':'Title', 'fps':25, 'media':[], 'clips':[{
            'type':'title','track':2,'title':'Card','start':0,'duration':2,
        }]}
        with tempfile.NamedTemporaryFile(suffix='.prproj') as file:
            file.write(save_project(project))
            file.flush()
            clip = py_premiere.parse(file.name).project.sequences[0].video_tracks[1].clips[0]
            with self.assertRaisesRegex(ValueError, 'audio pan'):
                premiere_effects.apply_extended(clip, {'pan':35})


if __name__ == '__main__':
    unittest.main()


class NativeMotionAndFade(unittest.TestCase):
    def test_lumetri_white_balance_roundtrip(self):
        app = py_premiere.new()
        sequence = app.project.add_sequence('Balance', '1080p25')
        item = app.project.import_files(['assets/ridge.mp4'])[0]
        clip = sequence.video_tracks[0].add_clip(item, py_premiere.Time.from_seconds(0))
        premiere_effects.apply(clip, {'temperature':35,'tint':-15}, 100)
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'balance.prproj')
            app.project.save(output)
            reopened = py_premiere.parse(output).project.sequences[0].video_tracks[0].clips[0]
            color, _ = premiere_effects.read(reopened)
            self.assertEqual((color['temperature'],color['tint']),(35,-15))

    def test_native_scalar_animation_roundtrip(self):
        app = py_premiere.new()
        sequence = app.project.add_sequence('Animate', '1080p25')
        item = app.project.import_files(['assets/ridge.mp4'])[0]
        clip = sequence.video_tracks[0].add_clip(item, py_premiere.Time.from_seconds(1))
        clip.end = py_premiere.Time.from_seconds(5)
        properties = {'opacity':80,'keyframes':{
            'opacity':[{'time':0,'value':0},{'time':2,'value':80}],
            'scale':[{'time':0,'value':100},{'time':2,'value':150}],
            'rotation':[{'time':0,'value':0},{'time':2,'value':15}],
        }}
        premiere_effects.apply_extended(clip, properties)
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'animate.prproj')
            app.project.save(output)
            reopened = py_premiere.parse(output).project.sequences[0].video_tracks[0].clips[0]
            effects = premiere_effects.read_extended(reopened)
            self.assertEqual(effects['opacity'],80)
            self.assertEqual(effects['transform']['scale'],100)
            self.assertEqual(effects['keyframes'],properties['keyframes'])

    def test_adobe_opacity_curves_unchanged_on_native_save(self):
        fixture = Path(__file__).resolve().parent / 'fixtures' / '09_keyframes.prproj'
        app = py_premiere.parse(str(fixture))
        clip = app.project.sequences[0].video_tracks[0].clips[0]
        opacity = next(param for component in clip.components if component.match_name=='AE.ADBE Opacity'
                       for param in component.properties if param.display_name=='Opacity')
        before = opacity._element.findtext('Keyframes')
        premiere_effects.apply_extended(clip,premiere_effects.read_extended(clip))
        self.assertEqual(opacity._element.findtext('Keyframes'),before)

    def test_new_video_motion_roundtrip(self):
        app = py_premiere.new()
        sequence = app.project.add_sequence('Motion', '1080p25')
        item = app.project.import_files(['assets/ridge.mp4'])[0]
        clip = sequence.video_tracks[0].add_clip(item, py_premiere.Time.from_seconds(0))
        premiere_effects.apply_extended(clip, {'transform':{'x':13,'y':-9,'scale':120,'rotation':7}})
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'motion.prproj')
            app.project.save(output)
            reopened = py_premiere.parse(output).project.sequences[0].video_tracks[0].clips[0]
            self.assertEqual(premiere_effects.read_extended(reopened)['transform'],
                {'x':13,'y':-9,'scale':120,'rotation':7})

    def test_new_audio_fades_roundtrip(self):
        app = py_premiere.new()
        sequence = app.project.add_sequence('Sound', '1080p25')
        item = app.project.import_files(['assets/score.wav'])[0]
        clip = sequence.audio_tracks[0].add_clip(item, py_premiere.Time.from_seconds(0))
        clip.end = py_premiere.Time.from_seconds(4)
        premiere_effects.apply_extended(clip, {'volume':70,'fadeIn':.4,'fadeOut':.5})
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'fade.prproj')
            app.project.save(output)
            reopened = py_premiere.parse(output).project.sequences[0].audio_tracks[0].clips[0]
            self.assertEqual(premiere_effects.read_extended(reopened),
                {'volume':70,'fadeIn':.4,'fadeOut':.5})
