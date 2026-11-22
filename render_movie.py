"""Render a Splice timeline to a deterministic MP4 with FFmpeg."""
import json
import math
from fractions import Fraction
import subprocess
import tempfile
from pathlib import Path

from premiere import _media_path



def _number(value, label, low=0, high=7200):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Invalid {label}')
    return float(value)


def _probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries', 'stream=codec_type:format=duration', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(result.stdout)
    return {stream['codec_type'] for stream in data.get('streams', [])}, float(data.get('format', {}).get('duration', 0))


def _css_color_filter(color):
    brightness=_number(color.get('brightness',100),'brightness',0,300)/100
    contrast=_number(color.get('contrast',100),'contrast',0,300)/100
    saturation=_number(color.get('saturation',100),'saturation',0,300)/100
    exposure=_number(color.get('exposure',0),'exposure',-4,4)
    temperature=_number(color.get('temperature',0),'temperature',-100,100)/100
    tint=_number(color.get('tint',0),'tint',-100,100)/100
    gain=2**exposure
    gains=(gain*(1+.35*temperature-.1*tint),gain*(1+.2*tint),gain*(1-.35*temperature-.1*tint))
    light=(.213,.715,.072)
    matrix=[[saturation*(r==c)+(1-saturation)*light[c] for c in range(3)] for r in range(3)]
    channels='rgb'
    brightness_filter=':'.join(f"{c}=val*{brightness:.6f}" for c in channels)
    contrast_filter=':'.join(f"{c}=(val-127.5)*{contrast:.6f}+127.5" for c in channels)
    mixer=':'.join(f"{channels[r]}{channels[c]}={matrix[r][c]:.6f}" for r in range(3) for c in range(3))
    grade_filter=':'.join(f'{c}=val*{g:.7f}' for c,g in zip(channels,gains))
    return f'format=rgb24,lutrgb={brightness_filter},lutrgb={contrast_filter},colorchannelmixer={mixer},lutrgb={grade_filter},format=yuva420p'


def _run_controlled(command, cancelled, timeout):
    import os, signal, time
    process=subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline=time.monotonic()+timeout
        while process.poll() is None:
            if cancelled and cancelled.is_set():
                os.killpg(process.pid,signal.SIGTERM)
                raise InterruptedError('Export cancelled')
            if time.monotonic()>deadline:
                os.killpg(process.pid,signal.SIGKILL)
                raise TimeoutError('Export timed out')
            time.sleep(.2)
        error=process.stderr.read().decode('utf-8','replace')
        if process.returncode: raise ValueError('FFmpeg render failed: '+error[-1500:])
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL)
        process.wait()
        process.stderr.close()


def _transform(clip, width, height):
    t = clip.get('transform') or {}
    scale = _number(t.get('scale',100),'scale',10,400)/100
    x = _number(t.get('x',0),'horizontal position',-100,100)/100
    y = _number(t.get('y',0),'vertical position',-100,100)/100
    angle = _number(t.get('rotation',0),'rotation',-180,180)
    filters = f',scale=trunc(iw*{scale:.5f}/2)*2:trunc(ih*{scale:.5f}/2)*2'
    if angle:
        filters += f',rotate={angle:.5f}*PI/180:ow=rotw(iw):oh=roth(ih):c=none'
    position = f'x=(main_w-overlay_w)/2+{width*x:.3f}:y=(main_h-overlay_h)/2+{height*y:.3f}'
    return filters, position


def _render_window(project, output_path, *, options=None, cancelled=None):
    with tempfile.TemporaryDirectory(prefix='splice-lut-') as lut_directory:
        return _render_window_inner(project, output_path, lut_directory, options=options, cancelled=cancelled)


def _render_window_inner(project, output_path, lut_directory, *, options=None, cancelled=None):
    if not isinstance(project, dict):
        raise ValueError('Invalid project')
    width = int(_number(project.get('width'), 'width', 16, 3840))
    height = int(_number(project.get('height'), 'height', 16, 2160))
    fps = Fraction(_number(project.get('fps'), 'frame rate', 1, 60)).limit_denominator(1001)
    if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
    clips = project.get('clips')
    media = project.get('media')
    if not isinstance(clips, list) or not isinstance(media, list) or len(clips) > 200 or len(media) > 200:
        raise ValueError('Invalid project timeline')
    if any(not isinstance(c, dict) for c in clips) or any(not isinstance(m, dict) or not isinstance(m.get('id'), str) for m in media):
        raise ValueError('Invalid project entries')
    media_by_id = {item.get('id'): item for item in media if isinstance(item, dict)}
    if len(media_by_id) != len(media):
        raise ValueError('Invalid project media')
    duration = max((_number(c.get('start'), 'start') + _number(c.get('duration'), 'duration', .001) for c in clips), default=0)
    if duration <= 0 or duration > 7200:
        raise ValueError('Timeline duration must be between 0 and 2 hours')
    render_options = project.get('renderOptions') or {}
    video_only = (options or {}).get('_videoOnly',False)
    if not isinstance(render_options, dict) or not isinstance(render_options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = render_options.get('mutedTracks', {})
    inputs = []
    paths = {}
    kinds = {}
    for clip in clips:
        if clip.get('type') not in ('video', 'audio', 'title'):
            raise ValueError('Unsupported timeline item')
        _number(clip.get('in', 0), 'source in')
        if clip['type'] == 'title':
            continue
        item = media_by_id.get(clip.get('mediaId'))
        if not item:
            raise ValueError('A clip references missing media')
        if item.get('nestedSequence') is not None or clip.get('nestedSequence') is not None:
            raise ValueError('Nested Premiere sequences are not yet supported for export')
        if item['id'] not in paths:
            path = _media_path(item.get('src', ''))
            paths[item['id']] = path
            kinds[item['id']], item['_actualDuration'] = _probe(path)
            inputs.extend(['-threads', '2', '-protocol_whitelist', 'file', '-i', str(path)])
        if float(clip.get('in', 0)) + float(clip.get('duration', 0)) > item['_actualDuration'] + .05:
            raise ValueError(f"{item.get('name', 'Media')} ends before this clip's out point")
        if clip['type'] not in kinds[item['id']]:
            raise ValueError(f"{item.get('name', 'Media')} lacks a {clip['type']} stream")
    indexes = {identity: index for index, identity in enumerate(paths)}
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_complex_threads', '3', '-y', *inputs,
               '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}']
    base_index = len(indexes)
    filters = [f'[{base_index}:v]format=yuv420p[base0]']
    active = 'base0'
    video_clips = sorted((c for c in clips if c['type'] == 'video'), key=lambda c: (c.get('track', 0), c['start']))
    if not muted.get('video'):
        for number, clip in enumerate(video_clips):
            item = media_by_id[clip['mediaId']]
            index = indexes[item['id']]
            start = float(clip['start']); length = float(clip['duration']); source_in = float(clip.get('in', 0))
            color = clip.get('color') or {}
            transform,position=_transform(clip,width,height)
            lut_filter=''
            if color.get('lut'):
                from lut import cube_text
                file=Path(lut_directory)/f'clip-{number}.cube'
                file.write_text(cube_text(color['lut'], color.get('lutIntensity',100)))
                lut_filter=f',lut3d=file={file}:interp=trilinear,format=yuva420p'
            opacity=_number(clip.get('opacity',100),'opacity',0,100)/100
            chain = f'[{index}:v]trim=start={source_in:.6f}:duration={length:.6f},setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,{_css_color_filter(color)}{lut_filter}{transform},format=yuva420p,colorchannelmixer=aa={opacity:.6f}'
            previous = next((c for c in reversed(video_clips[:number]) if c['start'] + c['duration'] >= start - .05), None)
            overlap_transition = previous is not None and clip.get('transition') in ('dissolve','wipe')
            if overlap_transition:
                prior_index = indexes[previous['mediaId']]
                tail = min(float(clip.get('transitionDuration', .5)), float(previous['duration']), float(clip['duration']), media_by_id[previous['mediaId']]['_actualDuration'] - (float(previous.get('in', 0)) + float(previous['duration'])))
                if tail < 1 / float(fps):
                    raise ValueError('Cross dissolve needs more footage after the outgoing clip')
                prior_source = float(previous.get('in', 0)) + float(previous['duration'])
                prior_transform,prior_position=_transform(previous,width,height)
                filters.append(f'[{prior_index}:v]trim=start={prior_source:.6f}:duration={tail:.6f},setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuva420p{prior_transform},setpts=PTS+{start:.6f}/TB[tail{number}]')
                filters.append(f'[{active}][tail{number}]overlay={prior_position}:eof_action=pass:shortest=0[under{number}]')
                active = f'under{number}'
                if clip.get('transition') == 'dissolve':
                    chain += f',fade=t=in:st=0:d={tail:.6f}:alpha=1'
                else:
                    chain += f",format=yuva444p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':alpha='if(lt(X,W*min(1,T/{tail:.6f})),{255*opacity:.3f},0)',format=yuva420p"
            if clip.get('transition') == 'fade':
                fade_duration=min(float(clip.get('transitionDuration',.5)),length)
                chain += f',fade=t=in:st=0:d={fade_duration:.6f}'
            chain += f',setpts=PTS+{start:.6f}/TB[clip{number}]'
            filters.append(chain)
            filters.append(f'[{active}][clip{number}]overlay={position}:eof_action=pass:shortest=0[base{number+1}]')
            active = f'base{number+1}'
    with tempfile.TemporaryDirectory(prefix='splice-render-') as work:
        for number, clip in enumerate(c for c in clips if c['type'] == 'title' and c.get('title') and not muted.get('title')):
            text_file = Path(work) / f'title-{number}.txt'
            text_file.write_text(str(clip['title'])[:500])
            begin = float(clip['start']); end = begin + float(clip['duration'])
            style=clip.get('titleStyle') or {}
            font={'DM Sans':'dm-sans.ttf','IBM Plex Mono':'plex-mono.ttf','DejaVu Sans':'dm-sans.ttf'}.get(style.get('fontFamily','DM Sans'))
            if font is None: raise ValueError('Unsupported title typeface')
            font_path=Path(__file__).resolve().parent/'assets'/'fonts'/font
            fontsize=round(height*_number(style.get('fontSize',8),'title size',2,20)/100)
            x=_number(style.get('x',50),'title horizontal',0,100)/100
            y=_number(style.get('y',78),'title vertical',0,100)/100
            color=str(style.get('color','#ffffff'))
            if len(color)!=7 or not color.startswith('#') or any(c not in '0123456789abcdefABCDEF' for c in color[1:]):
                raise ValueError('Invalid title color')
            align=style.get('align','center')
            if align not in ('left','center','right'): raise ValueError('Invalid title alignment')
            xpos={'left':f'w*{x:.5f}','center':f'w*{x:.5f}-text_w/2','right':f'w*{x:.5f}-text_w'}[align]
            stroke=str(style.get('strokeColor','#000000'))
            if len(stroke)!=7 or not stroke.startswith('#') or any(c not in '0123456789abcdefABCDEF' for c in stroke[1:]):
                raise ValueError('Invalid title outline color')
            border=int(_number(style.get('strokeWidth',0),'title outline',0,8)*height/180)
            filters.append(f'[{active}]drawtext=fontfile={font_path}:textfile={text_file}:expansion=none:fontsize={fontsize}:fontcolor=0x{color[1:]}:borderw={border}:bordercolor=0x{stroke[1:]}:shadowcolor=black:shadowx=3:shadowy=3:x={xpos}:y=h*{y:.5f}-text_h/2:enable=between(t\\,{begin:.6f}\\,{end:.6f})[title{number}]')
            active = f'title{number}'
        audio_labels = []
        track_mix=project.get('trackMix') or {}
        solo=any(v.get('solo') for v in track_mix.values() if isinstance(v,dict))
        if not muted.get('audio') and not video_only:
            for number, clip in enumerate(c for c in clips if c['type'] in ('audio', 'video')):
                item = media_by_id[clip['mediaId']]
                mix = track_mix.get(str(clip.get('track', -1))) or {}
                if mix.get('mute') or (solo and not mix.get('solo')): continue
                if 'audio' not in kinds[item['id']] or (clip['type'] == 'video' and clip.get('embeddedAudio') is False):
                    continue
                index = indexes[item['id']]
                source_in = float(clip.get('in', 0)); length = float(clip['duration']); delay = round(float(clip['start']) * 1000)
                gain = _number(clip.get('volume', 100), 'volume', 0, 200) * _number(mix.get('gain',100), 'track gain', 0, 200) / 10000
                pan = max(-1,min(1,(_number(clip.get('pan', 0), 'pan', -100, 100) + _number(mix.get('pan',0),'track pan',-100,100))/100))
                left = min(1,1-pan); right=min(1,1+pan)
                fade_in = min(length/2,_number(clip.get('fadeIn',0),'fade in',0,7200))
                fade_out = min(length/2,_number(clip.get('fadeOut',0),'fade out',0,7200))
                chain = f'[{index}:a]atrim=start={source_in:.6f}:duration={length:.6f},asetpts=PTS-STARTPTS,volume={gain:.4f},aformat=channel_layouts=stereo,pan=stereo|c0={left:.3f}*c0|c1={right:.3f}*c1'
                if fade_in: chain += f',afade=t=in:st=0:d={fade_in:.6f}'
                if fade_out: chain += f',afade=t=out:st={length-fade_out:.6f}:d={fade_out:.6f}'
                filters.append(chain + f',adelay={delay}|{delay}[audio{number}]')
                audio_labels.append(f'[audio{number}]')
        if audio_labels:
            filters.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,alimiter=limit=0.95[aout]")
        elif not video_only:
            command.extend(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo'])
            filters.append(f'[{base_index+1}:a]atrim=duration={duration:.6f}[aout]')
        output = Path(output_path)
        command.extend(['-filter_complex', ';'.join(filters), '-map', f'[{active}]'])
        if not video_only: command.extend(['-map', '[aout]'])
        command.extend([
                        '-t', f'{duration:.6f}', '-r', str(fps), '-c:v', 'libx264', '-threads', '4', '-preset', 'veryfast', '-crf', str(int(_number((options or {}).get('crf', 20), 'quality', 14, 32))), '-pix_fmt', 'yuv420p',
                        *([] if video_only else ['-c:a', 'aac', '-b:a', str(int(_number((options or {}).get('audioBitrate',192), 'audio bitrate', 64, 320)))+'k']), '-movflags', '+faststart', str(output)])
        _run_controlled(command,cancelled,min(3600,max(120,duration*8)))
        return output.stat().st_size


def render_to_file(project, output_path, *, options=None, progress=None, cancelled=None):
    """Render bounded timeline windows, then concatenate the encoded segments."""
    import copy
    import os
    total = max((float(c['start']) + float(c['duration']) for c in project['clips']), default=0)
    fps = float(project['fps'])
    if total <= 0 or total > 7200: raise ValueError('Invalid timeline duration')
    # Bound the number of simultaneous filter chains. Avoid splitting inside a transition.
    boundaries = [0.0]
    current = 0.0
    while current + 6 < total:
        proposed = current + 6
        for clip in project['clips']:
            if clip.get('transition') in ('dissolve','wipe','fade'):
                start = float(clip['start']); end = start + float(clip.get('transitionDuration', .5))
                if start - .001 <= proposed <= end + .001:
                    proposed = end + 1/fps
        next_frame = min(total, math.ceil(proposed * fps) / fps)
        if next_frame <= current: raise ValueError('Could not segment timeline')
        boundaries.append(next_frame)
        current = next_frame
    boundaries.append(total)
    with tempfile.TemporaryDirectory(prefix='splice-segments-') as directory:
        segments = []
        for index,(begin,end) in enumerate(zip(boundaries,boundaries[1:])):
            if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
            segment = copy.deepcopy(project)
            segment['clips'] = []
            for original in project['clips']:
                a = float(original['start']); b = a + float(original['duration'])
                overlap_start=max(begin,a); overlap_end=min(end,b)
                if overlap_end - overlap_start < .5/fps: continue
                clip=copy.deepcopy(original)
                clip['start']=overlap_start-begin
                clip['duration']=overlap_end-overlap_start
                clip['in']=float(clip.get('in',0)) + overlap_start-a
                if overlap_start > a+.5/fps: clip['transition']='none'
                segment['clips'].append(clip)
            # Gaps still need a black base track to keep exact segment duration.
            segment['clips'].append({'type':'title','start':0,'duration':end-begin,'in':0,'title':''})
            target=Path(directory)/f'{index:04d}.mp4'
            _render_window(segment,target,options={**(options or {}),'_videoOnly':True},cancelled=cancelled)
            segments.append(target)
            if progress: progress((index+1)/len(boundaries[:-1])*.95)
        manifest=Path(directory)/'segments.txt'
        manifest.write_text(''.join(f"file '{segment}'\n" for segment in segments))
        video=Path(directory)/'video.mp4'
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(manifest),'-an','-c:v','copy',str(video)]
        _run_controlled(command,cancelled,120)
        audio=Path(directory)/'audio.mp4'
        audio_project=copy.deepcopy(project)
        audio_project['width']=16; audio_project['height']=16
        audio_project['renderOptions']={'mutedTracks':{**(project.get('renderOptions') or {}).get('mutedTracks',{}),'video':True,'title':True}}
        _render_window(audio_project,audio,options=options,cancelled=cancelled)
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(video),'-i',str(audio),'-map','0:v:0','-map','1:a:0','-c','copy','-t',f'{total:.6f}','-movflags','+faststart',str(output_path)]
        _run_controlled(command,cancelled,120)
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
        if progress: progress(1.0)
        return Path(output_path).stat().st_size
