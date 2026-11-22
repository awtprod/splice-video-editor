"""Regression checks over pixels and streams returned by the production renderer."""
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
import unittest

from render_movie import render_to_file


class MovieDelivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / 'output.mp4'
        self.project = dict(name='Delivery QA', width=160, height=90, fps=25,
                            media=[{'id':name,'name':name,'src':'assets/'+name+'.mp4'} for name in ('ridge','valley')],
                            clips=[])

    @staticmethod
    def clip(identity, source, start, duration, **settings):
        return dict(id=identity, mediaId=source, type='video', track=1,
                    start=start, duration=duration, **{'in':1}, **settings)

    def frame(self, time):
        return subprocess.check_output(['ffmpeg','-nostdin','-v','error','-threads','1',
            '-ss',str(time),'-i',str(self.destination),'-frames:v','1','-pix_fmt','rgb24',
            '-f','rawvideo','pipe:1'], timeout=15)

    def streams(self):
        result = subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(self.destination)],timeout=10)
        return json.loads(result)['streams']

    def test_graded_outgoing_handle_keeps_its_grade(self):
        self.project['clips']=[self.clip('out','ridge',0,1,color={'brightness':0}),
                               self.clip('in','valley',1,1,transition='dissolve',transitionDuration=.48)]
        render_to_file(self.project,self.destination)
        before, atcut, middle, after = [statistics.mean(self.frame(t)) for t in (.96,1,1.24,1.56)]
        self.assertLess(abs(before-atcut),6, 'The outgoing handle flashes on the cut')
        self.assertLess(atcut+10,middle)
        self.assertLess(middle+10,after)

    def test_directional_wipe_matches_both_sides_of_cut(self):
        self.project['clips']=[self.clip('out','ridge',0,1,color={'brightness':0}),
                               self.clip('in','valley',1,1,transition='wipe',transitionDuration=.48)]
        render_to_file(self.project,self.destination)
        frame=self.frame(1.24)
        left,right=[],[]
        for y in range(10,80):
            row=frame[y*160*3:(y+1)*160*3]
            left.extend(row[10*3:60*3]);right.extend(row[100*3:150*3])
        self.assertGreater(statistics.mean(left),30)
        self.assertLess(statistics.mean(right),7)

    def test_dissolve_adjacent_to_segment_boundary_keeps_outgoing_picture(self):
        for cut in (11.8, 12.0):
            with self.subTest(cut=cut):
                self.project['clips'] = [
                    self.clip('out', 'ridge', 6, cut-6),
                    self.clip('in', 'valley', cut, 2, transition='dissolve', transitionDuration=.48),
                ]
                render_to_file(self.project, self.destination)
                before = statistics.mean(self.frame(cut-.04))
                self.assertGreater(before, 35)
                self.assertAlmostEqual(statistics.mean(self.frame(cut)), before, delta=7)
                self.assertGreater(statistics.mean(self.frame(cut+.24)), 35)

    def test_dissolve_requires_the_full_outgoing_handle(self):
        self.project['clips'] = [
            self.clip('out', 'ridge', 0, 7.76),
            self.clip('in', 'valley', 7.76, 1, transition='dissolve', transitionDuration=.48),
        ]
        with self.assertRaisesRegex(ValueError, 'needs 0.48s of unused outgoing footage'):
            render_to_file(self.project, self.destination)

    def test_final_render_uses_original_still(self):
        self.project['media']=[dict(id='still',src='/assets/ridge.jpg')]
        self.project['clips']=[self.clip('still','still',0,1)]
        render_to_file(self.project,self.destination)
        self.assertEqual(next(s['nb_frames'] for s in self.streams() if s['codec_type']=='video'),'25')
        self.assertGreater(statistics.mean(self.frame(.8)),30)

    def test_title_lines_center_independently_and_end_on_out_frame(self):
        self.project.update(width=640,height=360,clips=[
            {'type':'title','start':0,'duration':1,'title':'WIDE FIRST LINE\nI',
             'titleStyle':{'align':'center','x':50,'y':50,'fontSize':10}},
            {'type':'title','start':0,'duration':2,'title':''},
        ])
        render_to_file(self.project,self.destination)
        inside=self.frame(.96)
        coords=[x for y in range(184,235) for x in range(640) if inside[(y*640+x)*3]>180]
        self.assertTrue(coords)
        self.assertAlmostEqual(statistics.mean(coords),320,delta=6)
        self.assertLess(max(self.frame(1.0)),16)

    def test_growing_rotated_clip_keeps_picture_at_the_edges(self):
        self.project['media']=[dict(id='still',src='/assets/ridge.jpg')]
        self.project['clips']=[self.clip('still','still',0,1.2,keyframes={
            'scale':[{'time':0,'value':40},{'time':1.2,'value':100}],
            'rotation':[{'time':0,'value':0},{'time':1.2,'value':8}],
        })]
        render_to_file(self.project,self.destination)
        def visible_width(time):
            frame = self.frame(time)
            columns = [pixel % 160 for pixel in range(160*90)
                       if max(frame[pixel*3:pixel*3+3]) > 35]
            return max(columns) - min(columns)
        self.assertLess(visible_width(0),70)
        self.assertGreater(visible_width(1.16),140)

    def test_export_pan_routes_both_stereo_channels(self):
        from array import array
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source=Path(directory)/'stereo.wav'
            subprocess.run(['ffmpeg','-nostdin','-y','-v','error','-f','lavfi',
                            '-i','aevalsrc=0.12*sin(2*PI*440*t)|0.12*sin(2*PI*990*t):s=48000:d=1',
                            str(source)],check=True,timeout=10)
            self.project['media']=[dict(id='score',src='/media/'+str(source.relative_to(MEDIA_ROOT)))]
            for pan in (-100,0,100):
                self.project['clips']=[dict(type='audio',start=0,duration=1,mediaId='score',track=-1,pan=pan)]
                movie=Path(directory)/'mix.wav'
                render_to_file(self.project,movie,options={'codec':'wav'})
                pcm=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-ss','0.25','-i',str(movie),
                    '-t','0.25','-f','f32le','pipe:1'],timeout=10)
                samples=array('f',pcm)
                channels=[samples[i::2] for i in range(2)]
                rms=[(sum(n*n for n in channel)/len(channel))**.5 for channel in channels]
                if pan==0:
                    self.assertAlmostEqual(rms[0],rms[1],places=3)
                else:
                    self.assertLess(rms[1 if pan<0 else 0],.001)
                    self.assertGreater(rms[0 if pan<0 else 1],.11)

    def test_250_cuts_frame_count_and_selectable_subtitles(self):
        self.project['clips']=[self.clip(str(i),'ridge',i/25,.04) for i in range(250)]
        self.project['captions']=[dict(start=6.2,duration=1,text='Across the segment boundary')]
        render_to_file(self.project,self.destination)
        streams=self.streams()
        self.assertEqual(next(s['nb_frames'] for s in streams if s['codec_type']=='video'),'250')
        self.assertAlmostEqual(float(next(s['duration'] for s in streams if s['codec_type']=='audio')),10,places=2)
        self.assertTrue(any(s['codec_name']=='mov_text' for s in streams))


if __name__=='__main__':
    unittest.main()
