"""Regression checks over pixels and streams returned by the production renderer."""
import json
from io import BytesIO
from pathlib import Path
import statistics
import subprocess
import tempfile
import unittest
import wave

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

    def test_hdr_mp4_tone_maps_and_sets_rec709_signaling(self):
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source = Path(directory) / 'hlg.mov'
            fixture = ('color=gray:s=160x90:r=25:d=1,'
                       'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv')
            to_hlg = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt2020,'
                      'zscale=t=arib-std-b67:m=bt2020nc:r=limited:npl=203,format=yuv422p10le')
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',fixture,
                            '-vf',to_hlg,
                            '-c:v','prores_ks','-profile:v','3','-threads','1','-pix_fmt','yuv422p10le',
                            '-color_primaries','bt2020','-color_trc','arib-std-b67','-colorspace','bt2020nc',
                            '-movflags','+write_colr',str(source)], check=True, timeout=15)
            self.project['media'] = [{'id':'hdr','src':'/media/'+str(source.relative_to(MEDIA_ROOT))}]
            self.project['clips'] = [{**self.clip('hdr','hdr',0,1), 'in':0}]
            render_to_file(self.project, self.destination)
            video = next(stream for stream in self.streams() if stream['codec_type']=='video')
            self.assertEqual(tuple(video[field] for field in ('color_primaries','color_transfer','color_space')),
                             ('bt709','bt709','bt709'))
            self.assertEqual(video['nb_frames'], '25')
            self.assertAlmostEqual(statistics.mean(self.frame(.4)), 128, delta=6)

    def test_slow_audio_preview_keeps_pitch_and_samples_across_windows(self):
        from media_proxy import audition_audio
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source = Path(directory) / 'tone.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                            'sine=frequency=750:sample_rate=48000:duration=2',str(source)],check=True,timeout=10)
            src = '/media/'+str(source.relative_to(MEDIA_ROOT))
            for index in (0,1):
                with wave.open(BytesIO(audition_audio(src,index,.1))) as audio:
                    self.assertEqual((audio.getnchannels(),audio.getframerate()),(2,48000))
                    samples = memoryview(audio.readframes(48000*5)).cast('h')[0::2]
                self.assertEqual(len(samples),192960)
                for start in (48000,len(samples)-24000):
                    part=samples[start:start+24000]
                    frequency=2*sum(a<0<=b for a,b in zip(part,part[1:]))
                    self.assertAlmostEqual(frequency,750,delta=6)

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
        self.project['clips'][0]['keyframes']={'opacity':[{'time':0,'value':0},{'time':.8,'value':100}]}
        render_to_file(self.project,self.destination)
        self.assertLess(max(self.frame(0)),16)
        self.assertAlmostEqual(max(self.frame(.4)),128,delta=18)
        self.assertGreater(max(self.frame(.8)),235)

    def test_overlapping_titles_render_in_track_order(self):
        front = {'type':'title','track':4,'start':0,'duration':1,'title':'TITLE',
                 'titleStyle':{'fontSize':18,'color':'#ffffff'}}
        behind = {**front,'track':3,'titleStyle':{**front['titleStyle'],'color':'#2040df'}}
        self.project['clips'] = [front, behind]
        render_to_file(self.project, self.destination)
        self.assertGreater(max(self.frame(.4)[0::3]),230)

    def test_video_covers_lower_title_only_where_picture_is_opaque(self):
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source = Path(directory) / 'blue.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                            'color=blue:s=160x90:r=25:d=1','-c:v','libx264','-threads','1',str(source)],check=True,timeout=10)
            self.project['media'] = [{'id':'blue','src':'/media/'+str(source.relative_to(MEDIA_ROOT))}]
            title = {'type':'title','track':1,'start':0,'duration':1,'title':'TITLE',
                     'titleStyle':{'fontSize':30,'color':'#00ff00'}}
            video = {**self.clip('blue','blue',0,1),'track':2,'in':0}
            self.project['clips'] = [video,title]
            render_to_file(self.project,self.destination)
            self.assertLess(max(self.frame(.44)[1::3]),15)
            video['transform'] = {'scale':25}
            render_to_file(self.project,self.destination)
            self.assertGreater(max(self.frame(.44)[1::3]),200)

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
        # The 3:2 source fits to 134 pixels before rotation in this 16:9 sequence.
        self.assertGreater(visible_width(1.16),130)

    def test_narrow_upper_picture_preserves_lower_layer_at_edges(self):
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            for color, size in [('red','160x90'),('blue','120x90')]:
                file=Path(directory)/f'{color}.mp4'
                subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                    f'color={color}:s={size}:r=25:d=3','-c:v','libx264','-threads','1',str(file)],
                    timeout=10,check=True)
                self.project['media'].append(dict(id=color,src='/media/'+str(file.relative_to(MEDIA_ROOT))))
            self.project['clips'] = [dict(type='video',mediaId='red',track=1,start=0,duration=1),
                                     dict(type='video',mediaId='blue',track=2,start=0,duration=1)]
            render_to_file(self.project,self.destination)
            frame=self.frame(.4)
            edge,center=(45*160+5)*3,(45*160+80)*3
            self.assertGreater(frame[edge],235)
            self.assertGreater(frame[center+2],235)
            self.project['clips'][1].update(transition='fade',transitionDuration=.48,transform={'scale':35})
            render_to_file(self.project,self.destination)
            halfway=self.frame(.24)
            self.assertGreater(halfway[edge],235)
            self.assertLess(halfway[center],8)
            self.assertAlmostEqual(halfway[center+2],125,delta=8)
            self.assertGreater(self.frame(.48)[edge],235)
            self.project['clips'][1].update(track=1,start=1,transition='dissolve')
            render_to_file(self.project,self.destination)
            self.assertAlmostEqual(self.frame(1.24)[edge],125,delta=8)
            self.assertLess(self.frame(1.44)[edge],35)
            self.assertLess(self.frame(1.48)[edge],8)
            self.project['clips'][1].update(transition='fade',transitionDuration=.8,transitionBefore=.4,
                                            transform={}, **{'in':.4})
            render_to_file(self.project,self.destination)
            self.assertAlmostEqual(self.frame(.8)[center],125,delta=8)
            self.assertLess(max(self.frame(1)[center:center+3]),8)
            self.assertAlmostEqual(self.frame(1.2)[center+2],125,delta=8)

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

    def test_camera_dissolve_preserves_audible_tail_through_native_save(self):
        from array import array
        from premiere import MEDIA_ROOT, open_project, save_project
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source=Path(directory)/'camera.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=red:s=160x90:r=25:d=3',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3',
                '-c:v','libx264','-threads','1','-c:a','aac','-shortest',str(source)],check=True,timeout=10)
            self.project['media']=[dict(id='camera',name=source.name,src='/media/'+str(source.relative_to(MEDIA_ROOT)))]
            outgoing=self.clip('out','camera',0,1,fadeOut=.5)
            incoming=self.clip('in','camera',1,1,volume=0,transition='dissolve',transitionDuration=.48)
            self.project['clips']=[outgoing,incoming]
            def rms(project):
                output=Path(directory)/'mix.wav'
                render_to_file(project,output,options={'codec':'wav'})
                samples=array('f',subprocess.check_output(['ffmpeg','-nostdin','-v','error','-ss','1.1',
                    '-i',str(output),'-t','0.1','-f','f32le','pipe:1'],timeout=10))
                return (sum(sample*sample for sample in samples)/len(samples))**.5
            self.assertLess(rms(self.project),.0001)
            self.assertLess(rms(open_project(save_project(self.project))),.0001)
            outgoing['fadeOut']=0
            before=rms(self.project)
            self.assertGreater(before,.03)
            native=open_project(save_project(self.project))
            self.assertEqual([c['transition'] for c in native['clips'] if c['type']=='audio'],['none','dissolve'])
            self.assertAlmostEqual(rms(native),before,places=4)
            render_to_file(self.project,self.destination)
            stream=next(stream for stream in self.streams() if stream['codec_type']=='audio')
            self.assertEqual((stream['codec_name'],stream['channel_layout']),('aac','stereo'))
            self.project['trackMix']={'1':{'mute':True}}
            muted=open_project(save_project(self.project))
            self.assertLess(rms(muted),.0001)
            self.project['trackMix']={}
            incoming['start']=1.04
            self.assertLess(rms(self.project),.0001)

    def test_prores_preserves_ungraded_ten_bit_luma(self):
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source=Path(directory)/'gradient.mov'
            neutral=Path(directory)/'neutral.mov'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                'nullsrc=s=256x64:r=25,format=yuv422p10le,geq=lum=100+3*X:cb=512:cr=512',
                '-t','1','-c:v','prores_ks','-profile:v','3','-threads','1',str(neutral)],check=True,timeout=10)
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(neutral),'-c:v','copy','-color_primaries','bt2020',
                '-color_trc','arib-std-b67','-colorspace','bt2020nc','-movflags','+write_colr',str(source)],check=True,timeout=10)
            self.project.update(width=256,height=64,media=[dict(id='ramp',src='/media/'+str(source.relative_to(MEDIA_ROOT)))],
                clips=[dict(type='video',mediaId='ramp',track=1,start=0,duration=1)])
            output=Path(directory)/'master.mov'
            render_to_file(self.project,output,options={'codec':'prores'})
            for file in (source,output):
                raw=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-i',str(file),'-frames:v','1',
                    '-pix_fmt','yuv422p10le','-f','rawvideo','pipe:1'],timeout=10)
                row=raw[32*256*2:33*256*2]
                self.assertEqual(len({row[i:i+2] for i in range(0,len(row),2)}),256)
                metadata=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries',
                    'stream=color_primaries,color_transfer,color_space','-of','json',str(file)],timeout=10))['streams'][0]
                self.assertEqual(metadata,dict(color_primaries='bt2020',color_transfer='arib-std-b67',color_space='bt2020nc'))

    def test_mix_includes_second_discrete_camera_stream(self):
        from array import array
        from premiere import MEDIA_ROOT, open_project, save_project
        from media_proxy import describe
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source=Path(directory)/'camera.mov'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=red:s=160x90:r=25:d=1',
                '-f','lavfi','-i','anullsrc=r=48000:cl=mono:d=1','-f','lavfi','-i','sine=1000:sample_rate=48000:duration=1',
                '-map','0:v','-map','1:a','-map','2:a','-c:v','prores_ks','-threads','1','-c:a','pcm_s24le',str(source)],
                check=True,timeout=10)
            src='/media/'+str(source.relative_to(MEDIA_ROOT))
            metadata=describe(src)
            self.assertEqual(metadata['audioStreams'],2)
            self.project['media']=[dict(id='av',src=src,**metadata)]
            self.project['clips']=[dict(type='video',mediaId='av',track=1,start=0,duration=1)]
            output=Path(directory)/'mix.wav'
            render_to_file(self.project,output,options={'codec':'wav'})
            samples=array('f',subprocess.check_output(['ffmpeg','-nostdin','-v','error','-ss','0.2',
                '-i',str(output),'-t','0.5','-f','f32le','pipe:1'],timeout=10))
            self.assertGreater((sum(sample*sample for sample in samples)/len(samples))**.5,.05)
            reopened=open_project(save_project(self.project))
            self.assertEqual({clip['type'] for clip in reopened['clips']},{'video','audio'})
            self.assertEqual(len({clip['linkGroup'] for clip in reopened['clips']}),1)
            self.assertTrue(any(item['name'].endswith('-audio.wav') for item in reopened['media']))
            native=Path(directory)/'native.wav'
            render_to_file(reopened,native,options={'codec':'wav'})
            self.assertEqual(native.read_bytes(),output.read_bytes())

    def test_export_range_retains_frames_inside_long_dissolve(self):
        self.project['clips'] = [self.clip('a','ridge',0,4),
            dict(id='b',type='video',mediaId='valley',track=1,start=4,duration=4,
                 transition='dissolve',transitionDuration=4.8,transitionBefore=2.4,**{'in':2.4})]
        render_to_file(self.project,self.destination)
        expected=self.frame(4)
        render_to_file(self.project,self.destination,options={'range':{'start':4,'end':7}})
        self.assertEqual(next(s['nb_frames'] for s in self.streams() if s['codec_type']=='video'),'75')
        delivered=self.frame(0)
        self.assertLess(statistics.mean(abs(a-b) for a,b in zip(expected,delivered)),5)

    def test_fractional_video_origin_and_delayed_audio(self):
        from array import array
        from premiere import MEDIA_ROOT
        MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=MEDIA_ROOT) as directory:
            source=Path(directory)/'fractional.mov'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                "color=black:s=160x90:r=24000/1001:d=5,geq=lum='if(eq(mod(N,24),0),235,16)':cb=128:cr=128",
                '-itsoffset','0.5','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4.5',
                '-c:v','libx264','-threads','1','-c:a','pcm_s16le',str(source)],check=True,timeout=10)
            fps=24000/1001
            self.project.update(fps=fps,media=[dict(id='av',src='/media/'+str(source.relative_to(MEDIA_ROOT)))])
            self.project['clips']=[dict(type='video',mediaId='av',track=1,start=7/fps,duration=60/fps,**{'in':43/fps})]
            render_to_file(self.project,self.destination)
            received=[]
            for frame in (11,12,13):
                received.append(statistics.mean(self.frame(frame/fps)))
            self.assertLess(received[0],16)
            self.assertGreater(received[1],220)
            self.assertLess(received[2],16)
            self.project['clips'][0]['in']=43.5/fps
            render_to_file(self.project,self.destination)
            self.assertLess(statistics.mean(self.frame(11/fps)),16)
            self.assertGreater(statistics.mean(self.frame(12/fps)),220)
            self.project['clips'][0].update(start=0,duration=2,**{'in':0})
            movie=Path(directory)/'delayed.mov'
            render_to_file(self.project,movie,options={'codec':'prores'})
            pcm=array('f',subprocess.check_output(['ffmpeg','-nostdin','-v','error','-ss','0.15',
                '-i',str(movie),'-t','0.60','-vn','-ac','1','-f','f32le','pipe:1'],timeout=10))
            self.assertLess(max(abs(sample) for sample in pcm[:9600]),.0001)
            self.assertGreater(max(abs(sample) for sample in pcm[-4800:]),.04)

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
