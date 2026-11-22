"""Render a Splice timeline to a deterministic MP4 with FFmpeg."""
import json
import math
from fractions import Fraction
import subprocess
import tempfile
from pathlib import Path

from premiere import MEDIA_ROOT, STILL_IMAGES, _media_path



def _number(value, label, low=0, high=7200):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Invalid {label}')
    return float(value)


def _probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries', 'stream=codec_type,channels:format=duration', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=15, check=True)
    data = json.loads(result.stdout)
    streams=data.get('streams', [])
    channels=next((stream.get('channels',2) for stream in streams if stream['codec_type']=='audio'),0)
    return {stream['codec_type'] for stream in streams}, float(data.get('format', {}).get('duration', 0)), channels


def _stereo_pan(pan, channels):
    # Mirror the Web Audio StereoPannerNode used for playback.
    if channels == 1:
        angle = (pan + 1) * math.pi / 4
        return f'pan=stereo|c0={math.cos(angle):.8f}*c0|c1={math.sin(angle):.8f}*c0'
    if pan <= 0:
        angle = (pan + 1) * math.pi / 2
        matrix = f'c0=c0+{math.cos(angle):.8f}*c1|c1={math.sin(angle):.8f}*c1'
    else:
        angle = pan * math.pi / 2
        matrix = f'c0={math.cos(angle):.8f}*c0|c1=c1+{math.sin(angle):.8f}*c0'
    return 'aformat=channel_layouts=stereo,pan=stereo|' + matrix


def _font_path(family):
    bundled={'DM Sans':'dm-sans.ttf','IBM Plex Mono':'plex-mono.ttf'}
    if not isinstance(family,str) or not family or len(family)>100 or any(c in family for c in '\r\n\x00'):
        raise ValueError('Invalid title typeface')
    if family in bundled:
        return Path(__file__).resolve().parent/'assets'/'fonts'/bundled[family]
    match=subprocess.run(['fc-match','-f','%{family}|%{file}',family],capture_output=True,text=True,timeout=5,check=True).stdout
    resolved_family,separator,path=match.partition('|')
    resolved=Path(path).resolve()
    if not separator or not resolved.is_file() or not resolved.is_relative_to('/usr/share/fonts'):
        raise ValueError('Unable to resolve title typeface')
    if family.casefold() not in resolved_family.casefold():
        import warnings
        warnings.warn(f'Title font {family!r} is unavailable; using {resolved_family!r}',stacklevel=2)
    return resolved


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


def _fit_picture(width, height, color, lut):
    return (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,'
            f'{_css_color_filter(color)}{lut},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0')


def _run_controlled(command, cancelled, timeout):
    import time
    process=subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+timeout
        while process.poll() is None:
            if cancelled and cancelled.is_set():
                process.terminate()
                raise InterruptedError('Export cancelled')
            if time.monotonic()>deadline:
                process.kill()
                raise TimeoutError('Export timed out')
            time.sleep(.2)
        error=process.stderr.read().decode('utf-8','replace')
        if process.returncode: raise ValueError('FFmpeg render failed: '+error[-1500:])
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stderr.close()


def _transform(clip, width, height, local_offset=0):
    t = clip.get('transform') or {}
    scale = _number(t.get('scale',100),'scale',10,400)/100
    x = _number(t.get('x',0),'horizontal position',-100,100)/100
    y = _number(t.get('y',0),'vertical position',-100,100)/100
    angle = _number(t.get('rotation',0),'rotation',-180,180)
    local_clock=f'(t-{local_offset:.6f})' if local_offset else 't'
    scale_expr=_keyframe_expr(clip,'scale',scale*100,10,400,local_clock)
    x_expr=_keyframe_expr(clip,'x',x*100,-100,100,f'(t-{float(clip["start"]):.6f})')
    y_expr=_keyframe_expr(clip,'y',y*100,-100,100,f'(t-{float(clip["start"]):.6f})')
    angle_expr=_keyframe_expr(clip,'rotation',angle,-180,180,local_clock)
    scale_keys = (clip.get('keyframes') or {}).get('scale')
    angle_keys = (clip.get('keyframes') or {}).get('rotation')
    if scale_keys:
        filters=f",scale=w='trunc(iw*({scale_expr})/200)*2':h='trunc(ih*({scale_expr})/200)*2':eval=frame"
    else:
        filters=f',scale=trunc(iw*{scale:.5f}/2)*2:trunc(ih*{scale:.5f}/2)*2'
    if angle or angle_keys:
        # FFmpeg configures rotate's output dimensions from the first input
        # frame. Reserve space for the largest animated scale up front.
        if scale_keys:
            peak = max(float(point['value']) for point in scale_keys)/100
            if angle_keys:
                canvas_width = canvas_height = math.hypot(width,height)*peak
            else:
                sin, cos = abs(math.sin(math.radians(angle))), abs(math.cos(math.radians(angle)))
                canvas_width = (width*cos+height*sin)*peak
                canvas_height = (width*sin+height*cos)*peak
            out_width, out_height = 2*math.ceil(canvas_width/2), 2*math.ceil(canvas_height/2)
            filters += f",rotate=a='({angle_expr})*PI/180':ow={out_width}:oh={out_height}:c=none"
        elif angle_keys:
            filters += f",rotate=a='({angle_expr})*PI/180':ow=hypot(iw\\,ih):oh=ow:c=none"
        else:
            filters += f',rotate={angle:.5f}*PI/180:ow=rotw(iw):oh=roth(ih):c=none'
    position = f"x='(main_w-overlay_w)/2+{width/100:.3f}*({x_expr})':y='(main_h-overlay_h)/2+{height/100:.3f}*({y_expr})'"
    return filters, position


def _keyframe_expr(clip, key, base, low, high, clock):
    points=(clip.get('keyframes') or {}).get(key) or []
    if not points: return f'{base:.6f}'
    if not isinstance(points,list) or len(points)>100: raise ValueError(f'Invalid {key} keyframes')
    ordered=[]
    for point in points:
        if not isinstance(point,dict): raise ValueError(f'Invalid {key} keyframe')
        at=_number(point.get('time'),f'{key} keyframe time',0,float(clip['duration'])+.001)
        value=_number(point.get('value'),f'{key} keyframe value',low,high)
        ordered.append((at,value))
    ordered.sort()
    if any(b[0]<=a[0] for a,b in zip(ordered,ordered[1:])): raise ValueError(f'Duplicate {key} keyframes')
    expr=f'{ordered[-1][1]:.6f}'
    for (at,value),(next_at,next_value) in reversed(list(zip(ordered,ordered[1:]))):
        ramp=f'{value:.6f}+({next_value-value:.6f})*({clock}-{at:.6f})/{next_at-at:.6f}'
        expr=f'if(lte({clock},{next_at:.6f}),{ramp},{expr})'
    return f'if(lte({clock},{ordered[0][0]:.6f}),{ordered[0][1]:.6f},{expr})'


def _slice_keyframes(keyframes, offset, length):
    if not keyframes: return keyframes
    if not isinstance(keyframes,dict) or len(keyframes)>6: raise ValueError('Invalid keyframes')
    result={}
    for key,points in keyframes.items():
        bounds={'opacity':(0,100),'x':(-100,100),'y':(-100,100),'scale':(10,400),'rotation':(-180,180),'volume':(0,200)}
        if key not in bounds or not isinstance(points,list) or len(points)>100: raise ValueError('Invalid keyframes')
        if not points: continue
        if any(not isinstance(point,dict) for point in points): raise ValueError('Invalid keyframes')
        ordered=sorted(({'time':_number(point.get('time'),'keyframe time',0,7200),
                         'value':_number(point.get('value'),'keyframe value',*bounds[key])} for point in points),key=lambda point:point['time'])
        if any(right['time']<=left['time'] for left,right in zip(ordered,ordered[1:])): raise ValueError('Duplicate keyframes')
        def value_at(at):
            if at<=ordered[0]['time']: return ordered[0]['value']
            for left,right in zip(ordered,ordered[1:]):
                if at<=right['time']:
                    fraction=(at-left['time'])/(right['time']-left['time'])
                    return left['value']+(right['value']-left['value'])*fraction
            return ordered[-1]['value']
        sliced=[{'time':0,'value':value_at(offset)}]
        sliced.extend({'time':point['time']-offset,'value':point['value']} for point in ordered if offset<point['time']<offset+length)
        sliced.append({'time':length,'value':value_at(offset+length)})
        result[key]=sliced
    return result


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
    if not isinstance(clips, list) or not isinstance(media, list) or len(clips) > 1000 or len(media) > 500:
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
    codec=(options or {}).get('codec','h264')
    if codec not in ('h264','prores'): raise ValueError('Unsupported export codec')
    if not isinstance(render_options, dict) or not isinstance(render_options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = render_options.get('mutedTracks', {})
    paths = {}
    kinds = {}
    for clip in clips:
        if clip.get('type') not in ('video', 'audio', 'title'):
            raise ValueError('Unsupported timeline item')
        if clip.get('nativeTimeRemap'):
            raise ValueError('Keyframed Premiere time remapping is not supported for export')
        if clip.get('transition') == 'native':
            raise ValueError('This Premiere transition is not supported for export')
        before=_number(clip.get('transitionBefore',0),'transition lead',0,2)
        if before and clip.get('transition') not in ('dissolve','wipe','fade'):
            raise ValueError('Unsupported centered Premiere transition')
        if before>float(clip.get('start',0))+.001:
            raise ValueError('Transition begins before the sequence')
        _number(clip.get('in', 0), 'source in')
        if clip['type'] == 'title':
            continue
        item = media_by_id.get(clip.get('mediaId'))
        if not item:
            raise ValueError('A clip references missing media')
        generator=item.get('nativeGenerator')
        if generator not in (None,1112293707,1414680150) or item.get('nativeUnsupported'):
            raise ValueError(f"Premiere generator {item.get('name','')} is not supported for export")
        if item.get('nestedSequence') is not None or clip.get('nestedSequence') is not None:
            raise ValueError('Nested Premiere sequences are not yet supported for export')
        if generator in (1112293707,1414680150):
            kinds[item['id']]={'video'}
            _number(item.get('duration'),'generator duration',0,7200)
            item['_actualDuration']=7200.0
            if clip['type']!='video': raise ValueError('Premiere generator has no audio stream')
            continue
        if item['id'] not in paths:
            path = _media_path(item.get('src', ''))
            paths[item['id']] = path
            if path.suffix.lower() in STILL_IMAGES:
                kinds[item['id']], item['_actualDuration'], item['_audioChannels'] = {'video'}, 7200, 0
            else:
                kinds[item['id']], item['_actualDuration'], item['_audioChannels'] = _probe(path)
        speed=_number(clip.get('speed',1),'speed',.1,16)
        before=float(clip.get('transitionBefore',0) or 0)
        if before and not clip.get('reverse') and float(clip.get('in',0))-before*speed<-.01:
            raise ValueError('Incoming clip lacks pre-cut footage for its transition')
        required=float(clip.get('in',0))+(float(clip.get('duration',0))+(before if clip.get('reverse') else 0))*speed
        if required > item['_actualDuration'] + .05:
            raise ValueError(f"{item.get('name', 'Media')} ends before this clip's out point")
        if clip['type'] not in kinds[item['id']]:
            raise ValueError(f"{item.get('name', 'Media')} lacks a {clip['type']} stream")
    indexes = {identity: index for index, identity in enumerate(paths)}
    source_offsets={identity:min((float(c.get('in',0))-
        (float(c.get('transitionBefore',0) or 0)*_number(c.get('speed',1),'speed',.1,16) if not c.get('reverse') else 0)
        for c in clips if c.get('mediaId')==identity),default=0) for identity in paths}
    inputs=[]
    for identity,path in paths.items():
        offset=max(0,source_offsets[identity]);source_offsets[identity]=offset
        if path.suffix.lower() in STILL_IMAGES:
            inputs.extend(['-protocol_whitelist','file','-f','image2','-loop','1','-framerate',str(fps),'-i',str(path)])
            source_offsets[identity] = 0
        else:
            inputs.extend(['-ss',f'{offset:.6f}','-threads','2','-protocol_whitelist','file','-i',str(path)])
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_complex_threads', '3', '-y', *inputs,
               '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}']
    base_index = len(indexes)
    filters = [f'[{base_index}:v]format=yuv420p[base0]']
    active = 'base0'
    video_clips = sorted((c for c in clips if c['type'] == 'video'), key=lambda c: (c.get('track', 0), c['start']))
    if not muted.get('video'):
        for number, clip in enumerate(video_clips):
            item = media_by_id[clip['mediaId']]
            if item.get('nativeGenerator')==1414680150: continue
            index = indexes.get(item['id'])
            start = float(clip['start']); length = float(clip['duration']); source_in = float(clip.get('in', 0))
            speed=_number(clip.get('speed',1),'speed',.1,16)
            reverse=',reverse' if clip.get('reverse') else ''
            before=_number(clip.get('transitionBefore',0),'transition lead',0,2)
            transition_duration=min(float(clip.get('transitionDuration',.5)),length+before)
            render_start=start-before
            render_length=length+before
            render_source=(source_in if clip.get('reverse') else source_in-before*speed)-source_offsets.get(item['id'],0)
            color = clip.get('color') or {}
            transform,position=_transform(clip,width,height,before)
            lut_filter=''
            if color.get('lut'):
                from lut import cube_text
                file=Path(lut_directory)/f'clip-{number}.cube'
                file.write_text(cube_text(color['lut'], color.get('lutIntensity',100)))
                lut_filter=f',lut3d=file={file}:interp=trilinear,format=yuva420p'
            opacity=_number(clip.get('opacity',100),'opacity',0,100)/100
            opacity_keys=(clip.get('keyframes') or {}).get('opacity')
            prefix=(f'color=c=black:s={width}x{height}:r={fps}:d={render_length:.6f},setpts=PTS-STARTPTS' if index is None else
                    f'[{index}:v]trim=start={render_source:.6f}:duration={render_length*speed:.6f}{reverse},setpts=(PTS-STARTPTS)/{speed:.6f},fps={fps}')
            fit = _fit_picture(width,height,color,lut_filter)
            chain = f'{prefix},{fit}{transform},format=yuva420p,colorchannelmixer=aa={1 if opacity_keys else opacity:.6f}'
            if opacity_keys:
                opacity_expr=_keyframe_expr(clip,'opacity',opacity*100,0,100,f'(T-{before:.6f})')
                chain += f",format=yuva444p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*({opacity_expr})/100',format=yuva420p"
            previous = next((c for c in reversed(video_clips[:number]) if c.get('track') == clip.get('track') and
                             media_by_id[c['mediaId']].get('nativeGenerator') != 1414680150 and c['start'] + c['duration'] >= start - .05), None)
            overlap_transition = previous is not None and clip.get('transition') in ('dissolve','wipe')
            if overlap_transition:
                prior_index = indexes.get(previous['mediaId'])
                prior_speed=_number(previous.get('speed',1),'speed',.1,16)
                prior_available=(float(previous.get('in',0))/prior_speed if previous.get('reverse') else
                                 (media_by_id[previous['mediaId']]['_actualDuration']-(float(previous.get('in',0))+float(previous['duration'])*prior_speed))/prior_speed)
                frame_duration = 1 / float(fps)
                tail = max(0, transition_duration - before)
                if tail >= frame_duration - 1e-6 and prior_available + 1e-6 < tail:
                    raise ValueError(f'Transition at {start:.2f}s needs {tail:.2f}s of unused outgoing footage')
                prior_source = float(previous.get('in',0))-tail*prior_speed if previous.get('reverse') else float(previous.get('in',0))+float(previous['duration'])*prior_speed
                prior_source-=source_offsets.get(previous['mediaId'],0)
                prior_reverse=',reverse' if previous.get('reverse') else ''
                prior_tail={**previous,'keyframes':{},'transform':dict(previous.get('transform') or {})}
                for key in ('x','y','scale','rotation','opacity'):
                    points=(previous.get('keyframes') or {}).get(key) or []
                    if points:
                        terminal=sorted(points,key=lambda point:point['time'])[-1]['value']
                        if key=='opacity': prior_tail['opacity']=terminal
                        else: prior_tail['transform'][key]=terminal
                prior_transform,prior_position=_transform(prior_tail,width,height)
                prior_color=previous.get('color') or {}
                prior_lut=''
                if prior_color.get('lut'):
                    from lut import cube_text
                    prior_file=Path(lut_directory)/f'tail-{number}.cube'
                    prior_file.write_text(cube_text(prior_color['lut'],prior_color.get('lutIntensity',100)))
                    prior_lut=f',lut3d=file={prior_file}:interp=trilinear,format=yuva420p'
                prior_opacity=_number(prior_tail.get('opacity',100),'opacity',0,100)/100
                if tail + 1e-6 >= frame_duration:
                    prior_prefix=(f'color=c=black:s={width}x{height}:r={fps}:d={tail:.6f},setpts=PTS-STARTPTS' if prior_index is None else
                                  f'[{prior_index}:v]trim=start={prior_source:.6f}:duration={tail*prior_speed:.6f}{prior_reverse},setpts=(PTS-STARTPTS)/{prior_speed:.6f},fps={fps}')
                    prior_fit = _fit_picture(width,height,prior_color,prior_lut)
                    filters.append(f'{prior_prefix},{prior_fit}{prior_transform},format=yuva420p,colorchannelmixer=aa={prior_opacity:.6f},setpts=PTS+{start:.6f}/TB[tail{number}]')
                    filters.append(f'[{active}][tail{number}]overlay={prior_position}:eof_action=pass:shortest=0[under{number}]')
                    active = f'under{number}'
                if clip.get('transition') == 'dissolve':
                    chain += f',fade=t=in:st=0:d={transition_duration:.6f}:alpha=1'
                else:
                    chain += f",format=yuva444p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*if(lt(X,W*min(1,T/{transition_duration:.6f})),1,0)',format=yuva420p"
            elif clip.get('transition') in ('dissolve','fade'):
                chain += f',fade=t=in:st=0:d={transition_duration:.6f}:alpha=1'
            elif clip.get('transition') == 'wipe':
                chain += f",format=yuva444p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*if(lt(X,W*min(1,T/{transition_duration:.6f})),1,0)',format=yuva420p"
            if clip.get('transition') == 'fade':
                first = math.ceil(render_start*fps-1e-6)
                after = math.ceil((render_start+transition_duration)*fps-1e-6)
                filters.append(f"[{active}]drawbox=color=black:t=fill:enable='gte(n,{first})*lt(n,{after})'[black{number}]")
                active = f'black{number}'
            chain += f',setpts=PTS+{render_start:.6f}/TB[clip{number}]'
            filters.append(chain)
            filters.append(f'[{active}][clip{number}]overlay={position}:eof_action=pass:shortest=0[base{number+1}]')
            active = f'base{number+1}'
    with tempfile.TemporaryDirectory(prefix='splice-render-') as work:
        for number, clip in enumerate(c for c in clips if c['type'] == 'title' and c.get('title') and not muted.get('title')):
            begin = float(clip['start']); end = begin + float(clip['duration'])
            first_frame = math.ceil(begin * fps - 1e-6)
            out_frame = math.ceil(end * fps - 1e-6)
            style=clip.get('titleStyle') or {}
            font_path=_font_path(style.get('fontFamily','DM Sans'))
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
            lines = str(clip['title'])[:500].split('\n')[:20]
            for row, line in enumerate(lines):
                if not line.strip():
                    continue
                text_file = Path(work) / f'title-{number}-{row}.txt'
                text_file.write_text(line.rstrip('\r'), encoding='utf-8')
                offset = (row - (len(lines) - 1) / 2) * fontsize * 1.2
                label = f'title{number}line{row}'
                filters.append(
                    f'[{active}]drawtext=fontfile={font_path}:textfile={text_file}:expansion=none:'
                    f'fontsize={fontsize}:fontcolor=0x{color[1:]}:borderw={border}:bordercolor=0x{stroke[1:]}:'
                    f'shadowcolor=black:shadowx=3:shadowy=3:x={xpos}:y=h*{y:.5f}+({offset:.3f})-text_h/2:'
                    f'enable=gte(n\\,{first_frame})*lt(n\\,{out_frame})[{label}]')
                active = label
        audio_labels = []
        track_mix=project.get('trackMix') or {}
        solo=any(v.get('solo') for v in track_mix.values() if isinstance(v,dict))
        if not muted.get('audio') and not video_only:
            for number, clip in enumerate(c for c in clips if c['type'] in ('audio', 'video')):
                item = media_by_id[clip['mediaId']]
                if item.get('nativeGenerator') in (1112293707,1414680150): continue
                mix = track_mix.get(str(clip.get('track', -1))) or {}
                if mix.get('mute') or (solo and not mix.get('solo')): continue
                if 'audio' not in kinds[item['id']] or (clip['type'] == 'video' and clip.get('embeddedAudio') is False):
                    continue
                index = indexes[item['id']]
                source_in = float(clip.get('in', 0)); length = float(clip['duration']); start=float(clip['start'])
                speed=_number(clip.get('speed',1),'speed',.1,16)
                before=_number(clip.get('transitionBefore',0),'transition lead',0,2)
                render_source=(source_in if clip.get('reverse') else source_in-before*speed)-source_offsets.get(item['id'],0)
                render_length=length+before
                delay=round((start-before)*1000)
                volume_keys=(clip.get('keyframes') or {}).get('volume')
                gain = (100 if volume_keys else _number(clip.get('volume',100),'volume',0,200)) * _number(mix.get('gain',100),'track gain',0,200) / 10000
                pan = max(-1,min(1,(_number(clip.get('pan', 0), 'pan', -100, 100) + _number(mix.get('pan',0),'track pan',-100,100))/100))
                panner = _stereo_pan(pan,item['_audioChannels'])
                fade_in = min(length,_number(clip.get('fadeIn',0),'fade in',0,7200))
                fade_out = min(length,_number(clip.get('fadeOut',0),'fade out',0,7200))
                chain = f'[{index}:a]atrim=start={render_source:.6f}:duration={render_length*speed:.6f}'
                if clip.get('reverse'): chain += ',areverse'
                chain += ',asetpts=PTS-STARTPTS'
                remaining=speed
                while remaining>2:
                    chain += ',atempo=2';remaining/=2
                while remaining<.5:
                    chain += ',atempo=0.5';remaining*=2
                chain += f',atempo={remaining:.6f},volume={gain:.4f},{panner}'
                envelope=[]
                if volume_keys:
                    volume_expr=_keyframe_expr(clip,'volume',float(clip.get('volume',100)),0,200,f'(t-{before:.6f})')
                    envelope.append(f'({volume_expr})/100')
                if clip.get('transition') in ('dissolve','wipe','fade'):
                    chain += f',afade=t=in:st=0:d={min(float(clip.get("transitionDuration",.5)),render_length):.6f}'
                next_clip=next((candidate for candidate in clips if candidate is not clip and candidate.get('type')==clip['type'] and candidate.get('track')==clip.get('track') and candidate.get('transition') in ('dissolve','wipe') and abs(float(candidate['start'])-(start+length))<.05),None)
                if next_clip:
                    next_before=_number(next_clip.get('transitionBefore',0),'transition lead',0,2)
                    next_duration=_number(next_clip.get('transitionDuration',.5),'transition duration',1/float(fps),2)
                    fade_start=before+length-next_before
                    chain += f",volume='if(lt(t,{fade_start:.6f}),1,max(0,1-(t-{fade_start:.6f})/{next_duration:.6f}))':eval=frame"
                if fade_in or fade_out:
                    head = f'max(0,(t-{before:.9f})/{fade_in:.9f})' if fade_in else '1'
                    tail = f'max(0,({render_length:.9f}-t)/{fade_out:.9f})' if fade_out else '1'
                    envelope.append(f'min(1,min({head},{tail}))')
                if envelope:
                    gain_expr='*'.join(envelope)
                    chain += f",aeval=exprs='val(ch)*{gain_expr}':channel_layout=stereo"
                filters.append(chain + f',adelay={delay}|{delay}[audio{number}]')
                audio_labels.append(f'[audio{number}]')
                if next_clip:
                    after=max(0,next_duration-next_before)
                    if after>=1/float(fps):
                        available=(source_in/speed if clip.get('reverse') else (item['_actualDuration']-source_in-length*speed)/speed)
                        if available+1e-3<after: raise ValueError('Audio crossfade needs more outgoing source footage')
                        tail_source=source_in-after*speed if clip.get('reverse') else source_in+length*speed
                        tail_source-=source_offsets.get(item['id'],0)
                        tail_chain=f'[{index}:a]atrim=start={tail_source:.6f}:duration={after*speed:.6f}'
                        if clip.get('reverse'): tail_chain+=',areverse'
                        tail_chain+=',asetpts=PTS-STARTPTS'
                        remaining=speed
                        while remaining>2: tail_chain+=',atempo=2';remaining/=2
                        while remaining<.5: tail_chain+=',atempo=0.5';remaining*=2
                        tail_chain+=f",atempo={remaining:.6f},volume={gain:.4f},{panner}"
                        if volume_keys:
                            terminal=sorted(volume_keys,key=lambda point:point['time'])[-1]['value']
                            tail_chain+=f",volume={_number(terminal,'volume keyframe',0,200)/100:.6f}"
                        tail_chain+=f",volume='max(0,1-({next_before:.6f}+t)/{next_duration:.6f})':eval=frame,adelay={round((start+length)*1000)}|{round((start+length)*1000)}[tailaudio{number}]"
                        filters.append(tail_chain)
                        audio_labels.append(f'[tailaudio{number}]')
        if audio_labels:
            filters.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,alimiter=limit=0.95:level=0:latency=1,apad,atrim=duration={duration:.6f}[aout]")
        elif not video_only:
            command.extend(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo'])
            filters.append(f'[{base_index+1}:a]atrim=duration={duration:.6f}[aout]')
        output = Path(output_path)
        command.extend(['-filter_complex', ';'.join(filters), '-map', f'[{active}]'])
        if not video_only: command.extend(['-map', '[aout]'])
        if codec=='prores':
            command.extend(['-t',f'{duration:.6f}','-r',str(fps),'-c:v','prores_ks','-profile:v','3','-pix_fmt','yuv422p10le','-threads','4'])
            if not video_only: command.extend(['-c:a','pcm_s24le','-ar','48000'])
        else:
            command.extend(['-t',f'{duration:.6f}','-r',str(fps),'-c:v','libx264','-threads','4','-preset','veryfast','-crf',str(int(_number((options or {}).get('crf',20),'quality',14,32))),'-pix_fmt','yuv420p'])
            if not video_only: command.extend(['-c:a','aac','-b:a',str(int(_number((options or {}).get('audioBitrate',192),'audio bitrate',64,320)))+'k'])
        command.extend(['-movflags','+faststart',str(output)])
        _run_controlled(command,cancelled,min(3600,max(120,duration*8)))
        return output.stat().st_size


def render_to_file(project, output_path, *, options=None, progress=None, cancelled=None, _nested_depth=0):
    """Resolve imported nested Premiere sequences, then render the parent timeline."""
    import copy
    if not isinstance(project,dict) or not isinstance(project.get('media'),list): raise ValueError('Invalid project')
    if _nested_depth>4: raise ValueError('Nested Premiere sequences exceed four levels')
    if not any(isinstance(item,dict) and item.get('nestedProject') for item in project['media']):
        return _render_prepared(project,output_path,options=options,progress=progress,cancelled=cancelled)
    MEDIA_ROOT.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='splice-nested-',dir=MEDIA_ROOT) as directory:
        prepared=copy.deepcopy(project)
        for index,item in enumerate(prepared['media']):
            child=item.get('nestedProject')
            if not child: continue
            if _nested_depth>=4: raise ValueError('Nested Premiere sequences exceed four levels')
            suffix='.mov' if (options or {}).get('codec')=='prores' else '.mp4'
            target=Path(directory)/f'nested-{index}{suffix}'
            child_options={**(options or {}),'codec':'prores' if suffix=='.mov' else 'h264'}
            render_to_file(child,target,options=child_options,cancelled=cancelled,_nested_depth=_nested_depth+1)
            item['src']='/media/'+str(target.relative_to(MEDIA_ROOT))
            item['duration']=max((float(c['start'])+float(c['duration']) for c in child['clips']),default=0)
            item.pop('nestedSequence',None)
            item.pop('nestedProject',None)
            for clip in prepared['clips']:
                if clip.get('mediaId')==item['id']: clip.pop('nestedSequence',None)
        return _render_prepared(prepared,output_path,options=options,progress=progress,cancelled=cancelled)


def _render_prepared(project, output_path, *, options=None, progress=None, cancelled=None):
    """Render bounded timeline windows, then concatenate the encoded segments."""
    import copy
    import os
    total = max((float(c['start']) + float(c['duration']) for c in project['clips']), default=0)
    fps = float(project['fps'])
    if total <= 0 or total > 7200: raise ValueError('Invalid timeline duration')
    total=math.ceil((total-1e-9)*fps)/fps
    codec=(options or {}).get('codec','h264')
    if codec not in ('h264','prores','wav'): raise ValueError('Unsupported export codec')
    suffix='.mov' if codec=='prores' else '.mp4'
    captions=project.get('captions') or []
    if not isinstance(captions,list) or len(captions)>10000: raise ValueError('Invalid captions')
    validated_captions=[]
    for caption in captions:
        if not isinstance(caption,dict) or not isinstance(caption.get('text'),str) or len(caption['text'])>5000:
            raise ValueError('Invalid caption')
        start=_number(caption.get('start'),'caption start',0,total)
        length=_number(caption.get('duration'),'caption duration',.001,total)
        if start+length>total+.001: raise ValueError('Caption extends past timeline')
        validated_captions.append((start,start+length,caption['text']))
    validated_captions.sort(key=lambda item:item[0])
    def srt_time(seconds):
        ms=round(seconds*1000)
        return f'{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}'
    if codec=='wav':
        with tempfile.TemporaryDirectory(prefix='splice-audio-') as directory:
            audio_project=copy.deepcopy(project)
            audio_project['width']=16;audio_project['height']=16
            audio_project['renderOptions']={'mutedTracks':{**(project.get('renderOptions') or {}).get('mutedTracks',{}),'video':True,'title':True}}
            audio_project['clips'].append({'type':'title','start':0,'duration':total,'in':0,'title':''})
            intermediate=Path(directory)/'audio.mov'
            _render_window(audio_project,intermediate,options={**(options or {}),'codec':'prores'},cancelled=cancelled)
            _run_controlled(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(intermediate),'-map','0:a:0','-c:a','pcm_s24le','-ar','48000','-t',f'{total:.6f}',str(output_path)],cancelled,120)
            if progress: progress(1.0)
            return Path(output_path).stat().st_size
    # Keep transitions and the preceding frame in a single window. Work in
    # frame numbers so snapping cannot move a boundary back into an effect.
    starts = []
    protected = []
    total_frames = round(total * fps)
    for clip in project['clips']:
        start = float(clip['start']) - float(clip.get('transitionBefore', 0) or 0)
        frame = math.ceil(start * fps - 1e-6)
        starts.append(frame)
        if clip.get('transition') in ('dissolve', 'wipe', 'fade'):
            end = start + float(clip.get('transitionDuration', .5))
            protected.append((frame - 1, math.ceil(end * fps - 1e-6)))
    starts.sort()
    protected.sort()
    frames = [0]
    while frames[-1] < total_frames:
        current = frames[-1]
        proposed = min(current + round(6 * fps), total_frames)
        eligible = [frame for frame in starts if current < frame < proposed]
        if len(eligible) > 24:
            proposed = eligible[24]
        for first, after in protected:
            if first <= proposed < after:
                proposed = after
        frames.append(min(total_frames, proposed))
    boundaries = [frame / fps for frame in frames]
    with tempfile.TemporaryDirectory(prefix='splice-segments-') as directory:
        segments = []
        for index,(begin,end) in enumerate(zip(boundaries,boundaries[1:])):
            if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
            segment = copy.deepcopy(project)
            segment['clips'] = []
            for original in project['clips']:
                cut=float(original['start']);before=float(original.get('transitionBefore',0) or 0)
                a = cut-before; b = cut + float(original['duration'])
                overlap_start=max(begin,a); overlap_end=min(end,b)
                if overlap_end - overlap_start < .5/fps: continue
                clip=copy.deepcopy(original)
                speed=_number(clip.get('speed',1),'speed',.1,16)
                if before and overlap_start <= cut+.5/fps:
                    clip['start']=cut-begin
                    clip['duration']=overlap_end-cut
                else:
                    clip['start']=overlap_start-begin
                    clip['duration']=overlap_end-overlap_start
                    clip['in']=float(clip.get('in',0)) + ((b-overlap_end) if clip.get('reverse') else (overlap_start-cut))*speed
                    clip['transitionBefore']=0
                    if overlap_start > cut+.5/fps: clip['transition']='none'
                    if clip.get('keyframes'):
                        clip['keyframes']=_slice_keyframes(clip['keyframes'],overlap_start-cut,clip['duration'])
                segment['clips'].append(clip)
            # Gaps still need a black base track to keep exact segment duration.
            segment['clips'].append({'type':'title','start':0,'duration':end-begin,'in':0,'title':''})
            target=Path(directory)/f'{index:04d}{suffix}'
            _render_window(segment,target,options={**(options or {}),'_videoOnly':True},cancelled=cancelled)
            segments.append(target)
            if progress: progress((index+1)/len(boundaries[:-1])*.95)
        manifest=Path(directory)/'segments.txt'
        manifest.write_text(''.join(f"file '{segment}'\n" for segment in segments))
        video=Path(directory)/f'video{suffix}'
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(manifest),'-an','-c:v','copy',str(video)]
        _run_controlled(command,cancelled,120)
        audio=Path(directory)/f'audio{suffix}'
        audio_project=copy.deepcopy(project)
        audio_project['width']=16; audio_project['height']=16
        audio_project['renderOptions']={'mutedTracks':{**(project.get('renderOptions') or {}).get('mutedTracks',{}),'video':True,'title':True}}
        audio_project['clips'].append({'type':'title','start':0,'duration':total,'in':0,'title':''})
        _render_window(audio_project,audio,options=options,cancelled=cancelled)
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(video),'-i',str(audio)]
        if validated_captions and (options or {}).get('captions',True):
            subtitle=Path(directory)/'captions.srt'
            subtitle.write_text(''.join(f'{index}\n{srt_time(start)} --> {srt_time(end)}\n{text.replace(chr(13)," ").replace("-->","→").strip()}\n\n' for index,(start,end,text) in enumerate(validated_captions,1)))
            command.extend(['-i',str(subtitle)])
        command.extend(['-map','0:v:0','-map','1:a:0'])
        if validated_captions and (options or {}).get('captions',True): command.extend(['-map','2:s:0'])
        command.extend(['-c','copy'])
        if validated_captions and (options or {}).get('captions',True):
            command.extend(['-c:s','mov_text','-metadata:s:s:0','language=eng','-disposition:s:0','default'])
        command.extend(['-t',f'{total:.6f}','-movflags','+faststart',str(output_path)])
        _run_controlled(command,cancelled,120)
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
        if progress: progress(1.0)
        return Path(output_path).stat().st_size
