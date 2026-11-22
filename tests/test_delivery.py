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
