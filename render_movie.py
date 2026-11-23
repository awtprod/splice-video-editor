"""Render a Splice timeline to a deterministic MP4 with FFmpeg."""
import json
import math
import struct
from fractions import Fraction
from functools import lru_cache
import subprocess
import tempfile
from pathlib import Path

from premiere import MEDIA_ROOT, STILL_IMAGES, _media_path
from media_proxy import AVC_REC709, HDR_LINEAR_REC709, HDR_TONE_MAP, HDR_TRANSFERS, HDR_TO_REC709, REC709, tempo_filters
from splice_platform import HIDE_CONSOLE, font_roots, installed_font


PQ = ('bt2020','smpte2084','bt2020nc')


def _render_source(item):
    src = item.get('src')
    if not src:
        raise ValueError(f"Relink {item.get('name') or 'the offline source'} before exporting.")
    return _media_path(src)



def _number(value, label, low=0, high=7200):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Invalid {label}')
    return float(value)


def _probe(path):
    from fractions import Fraction
    from media_proxy import MAX_AUDIO_STREAMS
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries',
                            'stream=codec_type,channels,avg_frame_rate,r_frame_rate,color_primaries,color_transfer,color_space:format=duration', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=15, check=True, creationflags=HIDE_CONSOLE)
    data = json.loads(result.stdout)
    streams=data.get('streams', [])
    audio=[stream.get('channels',2) for stream in streams if stream['codec_type']=='audio']
    if len(audio)>MAX_AUDIO_STREAMS:
        raise ValueError(f'Media has more than {MAX_AUDIO_STREAMS} audio streams')
    video = next((stream for stream in streams if stream['codec_type']=='video'),{})
    average = video.get('avg_frame_rate')
    rate = float(Fraction(average)) if average and average == video.get('r_frame_rate') and average != '0/0' else 0
    profile = tuple(video.get(key) for key in ('color_primaries','color_transfer','color_space'))
    return {stream['codec_type'] for stream in streams}, float(data.get('format', {}).get('duration', 0)), audio, rate, profile


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
    candidate=installed_font(family)
    if candidate:
        return candidate[1]
    from shutil import which
    resolved_family, resolved = 'DM Sans', Path(__file__).resolve().parent/'assets'/'fonts'/'dm-sans.ttf'
    fontconfig = which('fc-match')
    if fontconfig:
        match=subprocess.run([fontconfig,'-f','%{family}|%{file}',family],capture_output=True,text=True,timeout=5,check=True).stdout
        name,separator,path=match.partition('|')
        if separator:
            path=Path(path).resolve()
            if path.is_file() and any(path.is_relative_to(root.resolve()) for root in font_roots()):
                resolved_family,resolved=name,path
    if family.casefold() not in resolved_family.casefold():
        import warnings
        warnings.warn(f'Title font {family!r} is unavailable; using {resolved_family!r}',stacklevel=2)
    return resolved


def _filter_path(path):
    # FFmpeg parses option values, then the filter graph itself.
    value=path.as_posix()
    option=''.join('\\'+char if char in "\\':" else char for char in value)
    return ''.join('\\'+char if char in "\\'[],;" else char for char in option)


def _grade_values(color):
    brightness=_number(color.get('brightness',100),'brightness',0,300)/100
    contrast=_number(color.get('contrast',100),'contrast',0,300)/100
    saturation=_number(color.get('saturation',100),'saturation',0,300)/100
    exposure=_number(color.get('exposure',0),'exposure',-4,4)
    temperature=_number(color.get('temperature',0),'temperature',-100,100)/100
    tint=_number(color.get('tint',0),'tint',-100,100)/100
    gain=2**exposure
    gains=(gain*(1+.35*temperature-.1*tint),gain*(1+.2*tint),gain*(1-.35*temperature-.1*tint))
    return brightness, contrast, saturation, gains


def _saturation_matrix(saturation):
    return [[saturation*(r==c)+(1-saturation)*weight for c,weight in enumerate((.213,.715,.072))] for r in range(3)]


def _channel_mixer(matrix):
    return 'colorchannelmixer='+':'.join(f'{r}{c}={matrix[i][j]:.6f}' for i,r in enumerate('rgb') for j,c in enumerate('rgb'))


def _css_color_filter(color, high_depth, still=False):
    brightness,contrast,saturation,gains=_grade_values(color)
    if (brightness,contrast,saturation,gains)==(1,1,1,(1,1,1)):
        return ''
    channels='rgb'
    brightness_filter=':'.join(f"{c}=val*{brightness:.6f}" for c in channels)
    contrast_filter=':'.join(f"{c}=(val-maxval/2)*{contrast:.6f}+maxval/2" for c in channels)
    mixer=_channel_mixer(_saturation_matrix(saturation))
    grade_filter=':'.join(f'{c}=val*{g:.7f}' for c,g in zip(channels,gains))
    rgb = ('gbrap10le' if still else 'gbrp10le') if high_depth else ('rgba' if still else 'rgb24')
    return f',format={rgb},lutrgb={brightness_filter},lutrgb={contrast_filter},{mixer},lutrgb={grade_filter}'


def _linear_gain(gains):
    chain=''
    # Keep the channel coefficients within ±2 without clipping float pixels.
    while max(gains)>2:
        factor=min(8,max(gains))
        chain+=f',exposure=exposure={math.log2(factor):.12f}'
        gains=tuple(value/factor for value in gains)
    if gains!=(1,1,1):
        chain+=',colorchannelmixer='+':'.join(f'{c}{c}={value:.9f}' for c,value in zip('rgb',gains))
    return chain


def _brightness_contrast(brightness, contrast, pivot=.5):
    slope=brightness*contrast
    offset=(1-contrast)*pivot
    if not offset:
        return _linear_gain((slope,)*3)
    # FFmpeg uses (pixel - black) / abs(2**-exposure - black).
    # Combine both operations when their settings are in range.
    if slope:
        black=-offset/slope
        white=(1-offset)/slope
        if -1<=black<=1 and .125<=white<=8:
            return f',exposure=exposure={-math.log2(white):.12f}:black={black:.12f}'
    # At zero or very low slope, apply the offset with a unit denominator.
    return _linear_gain((slope,)*3)+f',exposure=exposure={-math.log2(1-offset):.12f}:black={-offset:.12f}'


def _hdr_grade(color, lut, high_depth, profile, target=None):
    brightness,contrast,saturation,gains=_grade_values(color)
    if (brightness,contrast,saturation,gains)==(1,1,1,(1,1,1)) and (not lut or not high_depth):
        if not high_depth:
            return ','+HDR_TO_REC709+lut
        if not target or target==profile:
            return ''
    display_grade=(brightness,contrast,saturation)!=(1,1,1)
    scale=1
    if display_grade and high_depth and not lut:
        # Scale the working signal below reference white for zscale's fast
        # gamma tables. PQ's 10,000-nit peak remains below 8 in gamma 2.4
        # after the Rec.709 matrix, before the grading controls are applied.
        scale=8*max(1,brightness*contrast*max(1,saturation))
    peak=203*scale**2.4
    chain=','+HDR_LINEAR_REC709.format(peak=peak)
    if display_grade:
        # Brightness, contrast, and saturation retain their display-referred
        # controls. Exposure and white balance below use linear light.
        gamma=f'agamma={int(scale!=1)}'
        chain+=f',zscale=t=bt709:{gamma},format=gbrpf32le'
        chain+=_brightness_contrast(brightness,contrast,pivot=.5/scale)
        if saturation!=1:
            matrix=_saturation_matrix(saturation)
            # colorchannelmixer limits a coefficient to ±2. Preserve the
            # extra gain for imported projects with a stronger saturation.
            matrix_gain=max(1,max(abs(value) for row in matrix for value in row)/2)
            chain+=','+_channel_mixer([[value/matrix_gain for value in row] for row in matrix])+_linear_gain((matrix_gain,)*3)
        chain+=f',zscale=t=linear:{gamma},format=gbrpf32le'
    chain+=_linear_gain(gains)
    if not high_depth:
        return chain+','+HDR_TONE_MAP+lut
    if lut:
        # The approximate transfer tables can clip extended-range RGB.
        chain+=',zscale=t=bt709:agamma=0,format=gbrpf32le'+lut+',zscale=t=linear:agamma=0,format=gbrpf32le'
    primaries,transfer,matrix=target or profile
    return chain+f',zscale=p={primaries},zscale=t={transfer}:m={matrix}:r=limited:npl={peak:.9f},format=yuv444p10le'


def _hdr_still_filter(profile):
    primaries, transfer, matrix = profile
    # zscale applies the requested legal range to every plane. Keep the
    # converted picture in legal range and restore full-range opacity.
    limited = (f':m={matrix}:r=limited,format=yuva444p10le,'
               "geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='clip((p(X,Y)-64)*1023/876,0,1023)'")
    chain = (',format=gbrapf32le,setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=gbr:range=pc,'
             f'zscale=t=linear:p={primaries},format=gbrapf32le')
    if transfer != 'arib-std-b67' or primaries != 'bt2020':
        return chain+f',zscale=t={transfer}:npl=203'+limited
    # Match the reference-display inverse OOTF used for solid title swatches.
    luma = 'max(.000001,.203*(.2627*r(X,Y)+.6780*g(X,Y)+.0593*b(X,Y)))'
    channels = [
        f"{c}='st(0,.203*max(0,{c}(X,Y))*pow({luma},-1/6));"
        "if(lte(ld(0),1/12),sqrt(3*ld(0)),.17883277*log(12*ld(0)-.28466892)+.55991073)'"
        for c in 'rgb'
    ]
    return (chain+',geq='+':'.join(channels)+":a='alpha(X,Y)',"
            f'setparams=color_primaries={primaries}:color_trc={transfer}:colorspace=gbr:range=pc,'
            'zscale=t='+transfer+limited)


def _fit_picture(width, height, color, lut, alpha_format, high_depth, profile=None, rec709=False,
                 *, still_frames=0, hdr_profile=None, conform_pq=False):
    if profile and profile[1] in HDR_TRANSFERS:
        grade = _hdr_grade(color,lut,high_depth and not rec709,profile,PQ if conform_pq else None)
    else:
        convert = (',zscale=p=bt709:t=bt709:m=bt709:r=limited,format=yuv444p10le'
                   if rec709 and profile and profile!=REC709
                   and all(value not in (None,'unknown','unspecified') for value in profile) else '')
        grade = convert+_css_color_filter(color,high_depth,bool(still_frames))+lut
        if still_frames and hdr_profile:
            grade += _hdr_still_filter(hdr_profile)
        elif conform_pq and profile:
            primaries,transfer,matrix=(value if value not in (None,'unknown','unspecified') else default
                                      for value,default in zip(profile,REC709))
            grade += (f',zscale=pin={primaries}:tin={transfer}:min={matrix}:p=bt2020:t=linear,format=gbrpf32le,'
                      'zscale=t=smpte2084:m=bt2020nc:r=limited:npl=203,format=yuv444p10le')
    repeat = f',loop=loop={still_frames-1}:size=1:start=0,setpts=N' if still_frames>1 else ''
    return (f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2'
            f'{grade},format={alpha_format},'
            f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0{repeat}')


def _picture_input(index, source, length, speed, width, height, fps, reverse, still_frames):
    if still_frames:
        return f'[{index}:v]trim=end_frame=1,setpts=PTS-STARTPTS'
    if index is None:
        return f'color=c=black:s={width}x{height}:r={fps}:d={length:.6f},setpts=PTS-STARTPTS'
    return (f'[{index}:v]trim=start={source:.6f}:duration={length*speed:.6f}{reverse},'
            f'setpts=(PTS-STARTPTS)/{speed:.6f},fps={fps}')


def _lut_filter(color, destination, *, preserve_headroom=False):
    lut = color.get('lut')
    if not lut:
        return ''
    intensity = _number(color.get('lutIntensity',100),'LUT intensity',0,100)
    if not intensity:
        return ''
    from lut import cube_text
    domain_min = lut.get('domainMin', [0,0,0]) if isinstance(lut, dict) else [0,0,0]
    domain_max = lut.get('domainMax', [1,1,1]) if isinstance(lut, dict) else [1,1,1]
    custom_domain = domain_min != [0,0,0] or domain_max != [1,1,1]
    mix_after_lookup = custom_domain or preserve_headroom
    destination.write_text(cube_text(lut,100 if mix_after_lookup else intensity,normalized_domain=custom_domain))
    apply_lut = f'lut3d=file={_filter_path(destination)}:interp=trilinear'
    if not mix_after_lookup:
        return ','+apply_lut
    # Apply the input offset and scale explicitly. FFmpeg's .cube reader
    # ignores the offset and caps the input scale at one.
    if preserve_headroom:
        # Keep both branches in float RGB. Adding an alpha plane or using
        # integer RGB here clips HDR values before the LUT can read them.
        working_format = 'gbrpf32le'
        normalize = 'format='+working_format
        if domain_min == [0,0,0] and min(domain_max) >= .5:
            normalize += _linear_gain(tuple(1/maximum for maximum in domain_max))
        else:
            channels = ':'.join(f"{channel}='clip(({channel}(X,Y)-({low!r}))/({high-low!r}),0,1)'"
                                for channel, low, high in zip('rgb',domain_min,domain_max))
            normalize += ',geq='+channels
    else:
        working_format = 'gbrapf32le'
        channels = ':'.join(f"{channel}='clip((val-maxval*({low!r}))/({high-low!r}),0,maxval)'"
                            for channel, low, high in zip('rgb',domain_min,domain_max))
        normalize = f'format=gbrap16le,lutrgb={channels},format={working_format}'
    graded = f'{normalize},{apply_lut}'
    if intensity == 100:
        return ','+graded
    label = '_lut_'+destination.stem.replace('-','_')
    return (f',format={working_format},split[{label}_original][{label}_input];'
            f'[{label}_input]{graded}[{label}_graded];'
            f'[{label}_graded][{label}_original]blend=all_mode=normal:all_opacity={intensity/100:.12g}')


def _mask_alpha(expression, alpha_format, full_alpha):
    return f",format={full_alpha},geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*({expression})',format={alpha_format}"


def _video_sample_time(point, item, fps):
    # Browser playback displays the last decoded frame at an in-between timestamp.
    if abs(item.get('_videoFPS',0)-fps)<.001:
        return math.floor(point*fps+1e-6)/fps
    return point


def _trim_audio(filters, label, index, streams, start, length, reverse):
    # A stream can start later than the video's source clock. Preserve the gap.
    clock = f'PTS-({start:.9f})/TB'
    trim = f'atrim=start={start:.9f}:end={start+length:.9f},asetpts={clock},aresample=48000:first_pts=0'
    if len(streams)>1:
        from media_proxy import stereo_mix_filters
        sources = [f'[{index}:a:{number}]{trim}' for number in streams]
        filters.extend(stereo_mix_filters(sources,label,list(streams.values()),length))
        result = f'[{label}]'
    else:
        result = f'[{index}:a:{next(iter(streams))}]{trim},'
    return result + ('areverse,' if reverse else '')


def _audio_transition_gain(clip, expression):
    return f'sin(PI*({expression})/2)' if clip.get('transitionMatchName')=='Constant Power' else expression


def _run_controlled(command, cancelled, timeout):
    import time
    # FFmpeg can fill a Windows stderr pipe before it exits. Keep the child
    # writable while polling it and read only the diagnostic tail on failure.
    with tempfile.TemporaryFile() as stderr:
        process=subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr, creationflags=HIDE_CONSOLE)
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
            if process.returncode:
                size=stderr.seek(0,2)
                stderr.seek(max(0,size-1500))
                raise ValueError('FFmpeg render failed: '+stderr.read(1500).decode('utf-8','replace'))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _horizontal_position(clip, width, overlay_width, clock):
    x = _number((clip.get('transform') or {}).get('x',0),'horizontal position',-100,100)
    keyframes = _keyframe_expr(clip,'x',x,-100,100,clock)
    return f'({width}-{overlay_width})/2+{width/100:.3f}*({keyframes})'


def _transform(clip, width, height, local_offset=0):
    t = clip.get('transform') or {}
    scale = _number(t.get('scale',100),'scale',10,400)/100
    y = _number(t.get('y',0),'vertical position',-100,100)/100
    angle = _number(t.get('rotation',0),'rotation',-180,180)
    local_clock=f'(t-{local_offset:.6f})' if local_offset else 't'
    scale_expr=_keyframe_expr(clip,'scale',scale*100,10,400,local_clock)
    horizontal=_horizontal_position(clip,width,'overlay_w',f'(t-{float(clip["start"]):.6f})')
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
    position = f"x='{horizontal}':y='(main_h-overlay_h)/2+{height/100:.3f}*({y_expr})'"
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


@lru_cache(maxsize=256)
def _hdr_graphic_color(color, primaries, transfer):
    if color == '000000':
        return color
    # Convert the authored RGB swatch to the master color space before
    # drawtext blends its glyph mask. BT.2408 places graphics white at 203 nits.
    swatch = (f'color=0x{color}:s=2x2,format=gbrp,'
              'setparams=color_primaries=bt709:color_trc=bt709:colorspace=gbr:range=pc,'
              f'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p={primaries}')
    hlg = transfer=='arib-std-b67' and primaries=='bt2020'
    if not hlg:
        swatch += f',zscale=t={transfer}:m=gbr:r=full:npl=203,format=gbrp'
    pixel_format = 'gbrpf32le' if hlg else 'rgb24'
    result = subprocess.check_output(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',swatch,
                                      '-frames:v','1','-pix_fmt',pixel_format,'-f','rawvideo','pipe:1'],
                                     timeout=5,creationflags=HIDE_CONSOLE)
    if len(result) != (48 if hlg else 12):
        raise ValueError('Could not convert the HDR title color')
    if hlg:
        samples = struct.unpack('<12f',result)
        return _hlg_graphic_rgb(samples[8],samples[0],samples[4])
    return result[:3].hex()


def _hlg_graphic_rgb(red, green, blue):
    # BT.2100 inverse reference OOTF for a 1,000-nit display. Use the same
    # luminance-derived gain for all channels before applying the HLG OETF.
    display = [max(0,channel)*.203 for channel in (red,green,blue)]
    luminance = sum(channel*weight for channel,weight in zip(display,(.2627,.6780,.0593)))
    if not luminance:
        return '000000'
    inverse_gain = luminance**(1/1.2-1)
    a,b,c = .17883277,.28466892,.55991073
    encoded = []
    for channel in display:
        scene = channel*inverse_gain
        value = math.sqrt(3*scene) if scene<=1/12 else a*math.log(12*scene-b)+c
        encoded.append(round(255*min(1,value)))
    return bytes(encoded).hex()


def _draw_title(filters, active, number, clip, height, fps, directory, hdr_profile):
    begin = float(clip['start']); end = begin + float(clip['duration'])
    first_frame = math.ceil(begin * fps - 1e-6)
    out_frame = math.ceil(end * fps - 1e-6)
    style=clip.get('titleStyle') or {}
    font_path=_font_path(style.get('fontFamily','DM Sans'))
    fontsize=round(height*_number(style.get('fontSize',8),'title size',2,100)/100)
    x=_number(style.get('x',50),'title horizontal',0,100)/100
    y=_number(style.get('y',78),'title vertical',0,100)/100
    opacity = _number(clip.get('opacity',100),'title opacity',0,100)
    alpha = _keyframe_expr(clip,'opacity',opacity,0,100,f'(t-{begin:.9f})')
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
    fill, outline = color[1:], stroke[1:]
    if hdr_profile:
        primaries, transfer, _ = hdr_profile
        fill = _hdr_graphic_color(fill,primaries,transfer)
        if border:
            outline = _hdr_graphic_color(outline,primaries,transfer)
    lines = str(clip['title'])[:500].split('\n')[:20]
    for row, line in enumerate(lines):
        if not line.strip():
            continue
        text_file = Path(directory) / f'title-{number}-{row}.txt'
        text_file.write_text(line.rstrip('\r'), encoding='utf-8')
        offset = (row - (len(lines) - 1) / 2) * fontsize * 1.2
        label = f'title{number}line{row}'
        filters.append(
            f'[{active}]drawtext=fontfile={_filter_path(font_path)}:textfile={_filter_path(text_file)}:expansion=none:'
            f'fontsize={fontsize}:fontcolor=0x{fill}:borderw={border}:bordercolor=0x{outline}:'
            f'shadowcolor=black:shadowx=3:shadowy=3:x={xpos}:y=h*{y:.5f}+({offset:.3f})-text_h/2:'
            f"alpha='({alpha})/100':"
            f'enable=gte(n\\,{first_frame})*lt(n\\,{out_frame})[{label}]')
        active = label
    return active


def _render_window(project, output_path, *, options=None, cancelled=None):
    with tempfile.TemporaryDirectory(prefix='splice-lut-') as lut_directory:
        return _render_window_inner(project, output_path, lut_directory, options=options, cancelled=cancelled)


def _picture_transitions(clips, media_by_id, fps):
    by_end = {}
    predecessors, followers = {}, {}
    for number, clip in enumerate(clips):
        if media_by_id.get(clip.get('mediaId'), {}).get('nativeGenerator') == 1414680150:
            continue
        track = clip.get('track',0)
        start = float(clip['start'])
        prior = by_end.get((track,round(start*fps)))
        if prior is not None and clip.get('transition') in ('dissolve','wipe','fade'):
            predecessors[number] = prior
            followers[prior] = number
        by_end[track,round((start+float(clip['duration']))*fps)] = number
    return predecessors, followers


def _picture_range_media(clips, media_by_id, fps, first, stop):
    _, followers = _picture_transitions(clips, media_by_id, fps)
    active = set()
    for number, clip in enumerate(clips):
        cut = float(clip['start'])
        lead = float(clip.get('transitionBefore',0) or 0) if clip.get('transition') in ('dissolve','wipe') else 0
        end = cut+float(clip['duration'])
        following = clips[followers[number]] if number in followers else None
        if following and following.get('transition') in ('dissolve','wipe'):
            before = float(following.get('transitionBefore',0) or 0)
            tail = min(float(following.get('transitionDuration',.5))-before,float(following['duration']))
            if tail >= 1/fps-1e-6:
                end = max(end,float(following['start'])+tail)
        if math.ceil((cut-lead)*fps-1e-6)<stop and math.ceil(end*fps-1e-6)>first:
            active.add(clip.get('mediaId'))
    return active


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
    high_depth = codec=='prores'
    rec709 = (options or {}).get('colorSpace')=='rec709'
    conform_pq = (options or {}).get('colorSpace')=='pq'
    base_format = 'yuv422p10le' if high_depth else 'yuv420p'
    alpha_format = 'yuva422p10le' if high_depth else 'yuva420p'
    full_alpha = 'yuva444p10le' if high_depth else 'yuva444p'
    dip_rgb = 'gbrap10le' if high_depth else 'gbrap'
    overlay = 'eof_action=pass:shortest=0:format=' + ('yuv422p10' if high_depth else 'yuv420')
    if not isinstance(render_options, dict) or not isinstance(render_options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = render_options.get('mutedTracks', {})
    paths = {}
    kinds = {}
    probes = (options or {}).get('_probes',{})
    for clip in clips:
        if clip.get('type') not in ('video', 'audio', 'title'):
            raise ValueError('Unsupported timeline item')
        if clip.get('nativeTimeRemap'):
            raise ValueError('Keyframed Premiere time remapping is not supported for export')
        if clip.get('transition') == 'native':
            raise ValueError('This Premiere transition is not supported for export')
        before=_number(clip.get('transitionBefore',0),'transition lead')
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
            path = _render_source(item)
            paths[item['id']] = path
            if path.suffix.lower() in STILL_IMAGES:
                kinds[item['id']], item['_actualDuration'], item['_audioStreams'] = {'video'}, math.inf, []
                item['_renderStill'] = True
            else:
                probe = probes.get(path) or _probe(path)
                kinds[item['id']], item['_actualDuration'], item['_audioStreams'], item['_videoFPS'], profile = probe
                item['_sourceProfile'] = profile
        speed=_number(clip.get('speed',1),'speed',.1,16)
        before=float(clip.get('transitionBefore',0) or 0)
        if before and not item.get('_renderStill') and not clip.get('reverse') and float(clip.get('in',0))-before*speed<-.01:
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
        first_source = _video_sample_time(source_offsets[identity],media_by_id[identity],fps)
        offset=math.floor(max(0,first_source)*1_000_000)/1_000_000
        source_offsets[identity]=offset
        if path.suffix.lower() in STILL_IMAGES:
            inputs.extend(['-protocol_whitelist','file','-f','image2','-loop','1','-framerate',str(fps),'-i',str(path)])
            source_offsets[identity] = 0
        else:
            inputs.extend(['-ss',f'{offset:.6f}','-threads','2','-protocol_whitelist','file','-i',str(path)])
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_complex_threads', '3', '-y', *inputs,
               '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}']
    base_index = len(indexes)
    profile = (options or {}).get('_color_profile')
    hdr_profile = profile if high_depth and profile and profile[1] in HDR_TRANSFERS else None
    picture_params = (f',setparams=color_primaries={profile[0]}:color_trc={profile[1]}:colorspace={profile[2]}:range=tv'
                      if hdr_profile else '')
    filters = [f'[{base_index}:v]format={base_format}{picture_params}[base0]']
    active = 'base0'
    video_clips = [] if muted.get('video') else sorted((c for c in clips if c['type']=='video'),
                                                       key=lambda c: (c.get('track',0),c['start']))
    titles = [] if muted.get('title') else [c for c in clips if c['type']=='title' and c.get('title')]
    picture_layers = [(clip.get('track',0),False,number,clip) for number,clip in enumerate(video_clips)]
    picture_layers.extend((clip.get('track',4),True,number,clip) for number,clip in enumerate(titles))
    picture_layers.sort(key=lambda row: (row[0],row[1],row[3]['start']))
    if picture_layers:
        predecessors, followers = _picture_transitions(video_clips,media_by_id,fps)
        for _, is_title, number, clip in picture_layers:
            if is_title:
                active = _draw_title(filters,active,number,clip,height,fps,lut_directory,hdr_profile)
                continue
            item = media_by_id[clip['mediaId']]
            if item.get('nativeGenerator')==1414680150: continue
            index = indexes.get(item['id'])
            start = float(clip['start']); length = float(clip['duration']); source_in = float(clip.get('in', 0))
            speed=_number(clip.get('speed',1),'speed',.1,16)
            reverse=',reverse' if clip.get('reverse') else ''
            before=_number(clip.get('transitionBefore',0),'transition lead')
            transition_duration=min(float(clip.get('transitionDuration',.5)),length+before)
            incoming_dip = clip.get('transition') == 'fade'
            dip_head = transition_duration-before if incoming_dip else 0
            if incoming_dip:
                before = 0
            render_start=start-before
            render_length=length+before
            render_source=source_in if clip.get('reverse') else source_in-before*speed
            if not reverse and speed==1:
                render_source=_video_sample_time(render_source,item,fps)
            render_source=max(0,render_source-source_offsets.get(item['id'],0)-.000001)
            color = clip.get('color') or {}
            transform,position=_transform(clip,width,height,before)
            lut_filter=_lut_filter(color,Path(lut_directory)/f'clip-{number}.cube',preserve_headroom=bool(
                hdr_profile and (item.get('_sourceProfile') or REC709)[1] in HDR_TRANSFERS))
            opacity=_number(clip.get('opacity',100),'opacity',0,100)/100
            opacity_keys=(clip.get('keyframes') or {}).get('opacity')
            still_frames = max(1,math.ceil(render_length*fps-1e-6)) if item.get('_renderStill') else 0
            prefix = _picture_input(index,render_source,render_length,speed,width,height,fps,reverse,still_frames)
            fit = _fit_picture(width,height,color,lut_filter,alpha_format,high_depth,item.get('_sourceProfile'),rec709,
                               still_frames=still_frames,hdr_profile=hdr_profile,conform_pq=conform_pq)
            chain = f'{prefix},{fit}{transform}'
            if opacity_keys:
                opacity_expr=_keyframe_expr(clip,'opacity',opacity*100,0,100,f'(T-{before:.6f})')
                chain += _mask_alpha(f'({opacity_expr})/100',alpha_format,full_alpha)
            elif opacity!=1:
                chain += _mask_alpha(f'{opacity:.6f}',alpha_format,full_alpha)
            prior_number = predecessors.get(number)
            previous = video_clips[prior_number] if prior_number is not None else None
            next_number = followers.get(number)
            following = video_clips[next_number] if next_number is not None else None
            if following and following.get('transition') == 'dissolve':
                # Use the same lower layers under both sides of the dissolve.
                filters.append(f'[{active}]split[with{number}][without{next_number}]')
                active = f'with{number}'
            if dip_head>0:
                chain += f',format={dip_rgb},fade=t=in:st=0:d={dip_head:.6f}'
            if following and following.get('transition') == 'fade':
                dip_tail = float(following.get('transitionBefore',0) or 0)
                if dip_tail>0:
                    chain += f',format={dip_rgb},fade=t=out:st={max(0,render_length-dip_tail):.6f}:d={dip_tail:.6f}'
            overlap_transition = previous is not None and clip.get('transition') in ('dissolve','wipe')
            if overlap_transition:
                prior_item = media_by_id[previous['mediaId']]
                prior_index = indexes.get(previous['mediaId'])
                prior_speed=_number(previous.get('speed',1),'speed',.1,16)
                prior_available=(float(previous.get('in',0))/prior_speed if previous.get('reverse') else
                                 (prior_item['_actualDuration']-(float(previous.get('in',0))+float(previous['duration'])*prior_speed))/prior_speed)
                frame_duration = 1 / float(fps)
                tail = max(0, transition_duration - before)
                if tail >= frame_duration - 1e-6 and prior_available + 1e-6 < tail:
                    raise ValueError(f'Transition at {start:.2f}s needs {tail:.2f}s of unused outgoing footage')
                prior_source = float(previous.get('in',0))-tail*prior_speed if previous.get('reverse') else float(previous.get('in',0))+float(previous['duration'])*prior_speed
                prior_reverse=',reverse' if previous.get('reverse') else ''
                if not prior_reverse and prior_speed==1:
                    prior_source=_video_sample_time(prior_source,prior_item,fps)
                prior_source=max(0,prior_source-source_offsets.get(previous['mediaId'],0)-.000001)
                prior_tail={**previous,'keyframes':{},'transform':dict(previous.get('transform') or {})}
                for key in ('x','y','scale','rotation','opacity'):
                    points=(previous.get('keyframes') or {}).get(key) or []
                    if points:
                        terminal=sorted(points,key=lambda point:point['time'])[-1]['value']
                        if key=='opacity': prior_tail['opacity']=terminal
                        else: prior_tail['transform'][key]=terminal
                prior_transform,prior_position=_transform(prior_tail,width,height)
                prior_color=previous.get('color') or {}
                prior_lut=_lut_filter(prior_color,Path(lut_directory)/f'tail-{number}.cube',preserve_headroom=bool(
                    hdr_profile and (prior_item.get('_sourceProfile') or REC709)[1] in HDR_TRANSFERS))
                prior_opacity=_number(prior_tail.get('opacity',100),'opacity',0,100)/100
                if tail + 1e-6 >= frame_duration:
                    still_tail = max(1,math.ceil(tail*fps-1e-6)) if prior_item.get('_renderStill') else 0
                    prior_prefix = _picture_input(prior_index,prior_source,tail,prior_speed,width,height,fps,prior_reverse,still_tail)
                    prior_fit = _fit_picture(width,height,prior_color,prior_lut,alpha_format,high_depth,
                                             prior_item.get('_sourceProfile'),rec709,still_frames=still_tail,
                                             hdr_profile=hdr_profile,conform_pq=conform_pq)
                    prior_alpha = _mask_alpha(f'{prior_opacity:.6f}',alpha_format,full_alpha) if prior_opacity!=1 else ''
                    cut_frame = math.ceil(start*fps-1e-6)
                    filters.append(f'{prior_prefix},{prior_fit}{prior_transform}{prior_alpha},setpts=PTS+{cut_frame}[tail{number}]')
                    filters.append(f'[{active}][tail{number}]overlay={prior_position}:{overlay}[under{number}]')
                    active = f'under{number}'
            elif clip.get('transition') == 'dissolve':
                chain += f',fade=t=in:st=0:d={transition_duration:.6f}:alpha=1'
            if clip.get('transition') == 'wipe':
                left = _horizontal_position(clip,width,'W',f'(T-{before:.6f})')
                mask = f'if(gte(T,{transition_duration:.6f}),1,lt(X+({left}),{width}*T/{transition_duration:.6f}))'
                chain += _mask_alpha(mask,alpha_format,full_alpha)
            first_frame = math.ceil(render_start*fps-1e-6)
            chain += f',setpts=PTS+{first_frame}[clip{number}]'
            filters.append(chain)
            if overlap_transition and clip.get('transition') == 'dissolve':
                filters.append(f'[without{number}][clip{number}]overlay={position}:{overlay}[incoming{number}]')
                end = render_start+transition_duration
                progress = f'(T-{render_start:.9f})/{transition_duration:.9f}'
                blend = f'if(lte(T,{render_start:.9f}),A,if(gte(T,{end:.9f}),B,A*(1-{progress})+B*{progress}))'
                filters.append(f"[{active}][incoming{number}]blend=all_expr='{blend}'[base{number+1}]")
            else:
                filters.append(f'[{active}][clip{number}]overlay={position}:{overlay}[base{number+1}]')
            active = f'base{number+1}'
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
            before=_number(clip.get('transitionBefore',0),'transition lead')
            render_source=(source_in if clip.get('reverse') else source_in-before*speed)-source_offsets.get(item['id'],0)
            render_length=length+before
            delay=round((start-before)*48000)
            volume_keys=(clip.get('keyframes') or {}).get('volume')
            gain = (100 if volume_keys else _number(clip.get('volume',100),'volume',0,200)) * _number(mix.get('gain',100),'track gain',0,200) / 10000
            pan = max(-1,min(1,(_number(clip.get('pan', 0), 'pan', -100, 100) + _number(mix.get('pan',0),'track pan',-100,100))/100))
            streams = dict(enumerate(item['_audioStreams']))
            selection = clip.get('audioStream',item.get('audioStream'))
            if selection is not None:
                if type(selection) is not int or selection not in streams:
                    raise ValueError('The selected source audio stream is unavailable')
                streams = {selection:streams[selection]}
            panner = _stereo_pan(pan,2 if len(streams)>1 else next(iter(streams.values())))
            fade_in = min(length,_number(clip.get('fadeIn',0),'fade in',0,7200))
            fade_out = min(length,_number(clip.get('fadeOut',0),'fade out',0,7200))
            chain = _trim_audio(filters,f'mix{number}',index,streams,render_source,render_length*speed,clip.get('reverse'))
            processing=','.join([*tempo_filters(speed),f'volume={gain:.4f}',panner])
            chain += processing
            envelope=[]
            if volume_keys:
                volume_expr=_keyframe_expr(clip,'volume',float(clip.get('volume',100)),0,200,f'(t-{before:.6f})')
                envelope.append(f'({volume_expr})/100')
            if clip.get('transition') in ('dissolve','wipe','fade'):
                incoming=min(_number(clip.get('transitionDuration',.5),'transition duration',1/float(fps)),render_length)
                envelope.append(_audio_transition_gain(clip,f'min(1,t/{incoming:.9f})'))
            next_clip=next((candidate for candidate in clips if candidate is not clip and candidate.get('type')==clip['type'] and candidate.get('track')==clip.get('track') and candidate.get('transition') in ('dissolve','wipe') and round(float(candidate['start'])*fps)==round((start+length)*fps)),None)
            if next_clip:
                next_before=_number(next_clip.get('transitionBefore',0),'transition lead')
                next_duration=_number(next_clip.get('transitionDuration',.5),'transition duration',1/float(fps))
                next_duration=min(next_duration,next_before+float(next_clip['duration']))
                fade_start=before+length-next_before
                envelope.append(_audio_transition_gain(next_clip,f'max(0,min(1,1-(t-{fade_start:.9f})/{next_duration:.9f}))'))
            if fade_in or fade_out:
                head = f'max(0,(t-{before:.9f})/{fade_in:.9f})' if fade_in else '1'
                tail = f'max(0,({render_length:.9f}-t)/{fade_out:.9f})' if fade_out else '1'
                envelope.append(f'min(1,min({head},{tail}))')
            if envelope:
                gain_expr='*'.join(envelope)
                chain += f",aeval=exprs='val(ch)*{gain_expr}':channel_layout=stereo"
            filters.append(chain + f',adelay={delay}S:all=1[audio{number}]')
            audio_labels.append(f'[audio{number}]')
            if next_clip and not fade_out:
                after=max(0,next_duration-next_before)
                if after>=1/float(fps):
                    available=(source_in/speed if clip.get('reverse') else (item['_actualDuration']-source_in-length*speed)/speed)
                    if available+1e-3<after: raise ValueError('Audio crossfade needs more outgoing source footage')
                    tail_source=source_in-after*speed if clip.get('reverse') else source_in+length*speed
                    tail_source-=source_offsets.get(item['id'],0)
                    tail_chain=_trim_audio(filters,f'tailmix{number}',index,streams,tail_source,after*speed,clip.get('reverse'))
                    tail_chain+=processing
                    if volume_keys:
                        terminal=sorted(volume_keys,key=lambda point:point['time'])[-1]['value']
                        tail_chain+=f",volume={_number(terminal,'volume keyframe',0,200)/100:.6f}"
                    tail_gain=_audio_transition_gain(next_clip,f'max(0,1-({next_before:.9f}+t)/{next_duration:.9f})')
                    tail_delay=round((start+length)*48000)
                    tail_chain+=f",aeval=exprs='val(ch)*{tail_gain}':channel_layout=stereo,adelay={tail_delay}S:all=1[tailaudio{number}]"
                    filters.append(tail_chain)
                    audio_labels.append(f'[tailaudio{number}]')
    if audio_labels:
        filters.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,aformat=channel_layouts=stereo,alimiter=limit=0.95:level=0:latency=1,apad,atrim=duration={duration:.6f}[aout]")
    elif not video_only:
        command.extend(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo'])
        filters.append(f'[{base_index+1}:a]atrim=duration={duration:.6f}[aout]')
    audio_output = 'aout'
    window = (options or {}).get('_output_frames')
    if window is not None:
        first, stop = window
        filters.append(f'[{active}]trim=start_frame={first}:end_frame={stop},setpts=PTS-STARTPTS[vwindow]')
        active = 'vwindow'
        if not video_only:
            first_sample,stop_sample = (round(frame*48000/fps) for frame in (first,stop))
            filters.append(f'[aout]atrim=start_sample={first_sample}:end_sample={stop_sample},asetpts=PTS-STARTPTS[awindow]')
            audio_output = 'awindow'
        duration = (stop-first)/fps
    output = Path(output_path)
    command.extend(['-filter_complex', ';'.join(filters), '-map', f'[{active}]'])
    if not video_only: command.extend(['-map', f'[{audio_output}]'])
    if codec=='prores':
        command.extend(['-t',f'{duration:.6f}','-r',str(fps),'-c:v','prores_ks','-profile:v','3','-pix_fmt','yuv422p10le','-threads','4'])
        if not video_only: command.extend(['-c:a','pcm_s24le','-ar','48000'])
    else:
        command.extend(['-t',f'{duration:.6f}','-r',str(fps),'-c:v','libx264','-threads','4','-preset','veryfast','-crf',str(int(_number((options or {}).get('crf',20),'quality',14,32))),'-pix_fmt','yuv420p'])
        if not video_only: command.extend(['-c:a','aac','-b:a',str(int(_number((options or {}).get('audioBitrate',192),'audio bitrate',64,320)))+'k'])
    profile = (options or {}).get('_color_profile')
    if profile:
        command.extend(['-color_primaries',profile[0],'-color_trc',profile[1],'-colorspace',profile[2]])
        if high_depth and profile[1] in HDR_TRANSFERS:
            command.extend(['-bsf:v',f'prores_metadata=color_primaries={profile[0]}:color_trc={profile[1]}:colorspace={profile[2]}'])
    command.extend(['-movflags','+faststart+write_colr' if profile else '+faststart',str(output)])
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
            child_options.pop('range',None)
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
    options={**(options or {}),'_output_frames':None}
    codec=options.get('codec','h264')
    if codec not in ('h264','prores','wav'): raise ValueError('Unsupported export codec')
    color_space=options.get('colorSpace','source')
    if color_space!='source' and (codec!='prores' or color_space not in ('rec709','pq')):
        raise ValueError('Invalid export color space')
    suffix='.mov' if codec=='prores' else '.mp4'
    total_frames = round(total*fps)
    first,stop = 0,total_frames
    requested = options.get('range')
    if requested is not None:
        if not isinstance(requested,dict):raise ValueError('Invalid export range')
        first = round(_number(requested.get('start'),'range start',0,total+.000001)*fps)
        stop = round(_number(requested.get('end'),'range end',0,total+.000001)*fps)
        if stop<=first or stop>total_frames:raise ValueError('The export range must contain frames inside the sequence')
    out_start,out_end = first/fps,stop/fps
    out_duration = (stop-first)/fps
    probes = {}
    profiles = set()
    if codec!='wav' and not ((project.get('renderOptions') or {}).get('mutedTracks') or {}).get('video'):
        by_id = {item['id']:item for item in project['media']}
        video_clips = sorted((clip for clip in project['clips'] if clip.get('type')=='video'),
                             key=lambda clip:(clip.get('track',0),float(clip['start'])))
        vid_ids = _picture_range_media(video_clips,by_id,fps,first,stop)
        for item in project['media']:
            if item.get('id') not in vid_ids or item.get('nativeGenerator') is not None:continue
            path = _render_source(item)
            if path.suffix.lower() not in STILL_IMAGES:
                if path not in probes:probes[path]=_probe(path)
                profiles.add(probes[path][-1])
    hdr = any(profile[1] in HDR_TRANSFERS for profile in profiles)
    if hdr and codec=='prores' and len(profiles)!=1 and color_space=='source':
        raise ValueError('A ProRes HDR master needs one HDR color space. Select Rec. 2100 PQ or Rec. 709 to combine HDR and SDR sources.')
    profile = next(iter(profiles)) if len(profiles)==1 else None
    if profile and not all(value and value not in ('unknown','unspecified') for value in profile):profile=None
    if color_space=='rec709' or hdr and codec=='h264': profile=REC709
    if color_space=='pq': profile=PQ
    options.update(_probes=probes,_color_profile=profile)
    audio_options={**options,'_output_frames':(first,stop) if (first,stop)!=(0,total_frames) else None}
    captions=project.get('captions') or []
    if not isinstance(captions,list) or len(captions)>10000: raise ValueError('Invalid captions')
    validated_captions=[]
    for caption in captions:
        if not isinstance(caption,dict) or not isinstance(caption.get('text'),str) or len(caption['text'])>5000:
            raise ValueError('Invalid caption')
        start=_number(caption.get('start'),'caption start',0,total)
        length=_number(caption.get('duration'),'caption duration',.001,total)
        if start+length>total+.001: raise ValueError('Caption extends past timeline')
        begin,end = max(start,out_start),min(start+length,out_end)
        if end>begin:validated_captions.append((begin-out_start,end-out_start,caption['text']))
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
            _render_window(audio_project,intermediate,options={**audio_options,'codec':'prores'},cancelled=cancelled)
            _run_controlled(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(intermediate),'-map','0:a:0','-c:a','pcm_s24le','-ar','48000','-t',f'{out_duration:.6f}',str(output_path)],cancelled,120)
            if progress: progress(1.0)
            return Path(output_path).stat().st_size
    # Keep transitions and the preceding frame in a single window. Work in
    # frame numbers so snapping cannot move a boundary back into an effect.
    starts = []
    protected = []
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
        for protect_from, protect_until in protected:
            if protect_from <= proposed < protect_until:
                proposed = protect_until
        frames.append(min(total_frames, proposed))
    windows = [(a,b) for a,b in zip(frames,frames[1:]) if b>first and a<stop]
    with tempfile.TemporaryDirectory(prefix='splice-segments-') as directory:
        segments = []
        for index,(a_frame,b_frame) in enumerate(windows):
            if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
            begin,end = a_frame/fps,b_frame/fps
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
            output_window = (max(first,a_frame)-a_frame,min(stop,b_frame)-a_frame)
            window_options={**options,'_videoOnly':True,'_output_frames':output_window if output_window!=(0,b_frame-a_frame) else None}
            _render_window(segment,target,options=window_options,cancelled=cancelled)
            segments.append(target)
            if progress: progress((index+1)/len(windows)*.95)
        manifest=Path(directory)/'segments.txt'
        manifest.write_text(''.join(f"file '{segment.name}'\n" for segment in segments),encoding='utf-8')
        video=Path(directory)/f'video{suffix}'
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(manifest),'-an','-c:v','copy',str(video)]
        _run_controlled(command,cancelled,120)
        audio=Path(directory)/f'audio{suffix}'
        audio_project=copy.deepcopy(project)
        audio_project['width']=16; audio_project['height']=16
        audio_project['renderOptions']={'mutedTracks':{**(project.get('renderOptions') or {}).get('mutedTracks',{}),'video':True,'title':True}}
        audio_project['clips'].append({'type':'title','start':0,'duration':total,'in':0,'title':''})
        _render_window(audio_project,audio,options=audio_options,cancelled=cancelled)
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-i',str(video),'-i',str(audio)]
        if validated_captions and (options or {}).get('captions',True):
            subtitle=Path(directory)/'captions.srt'
            subtitle.write_text(''.join(f'{index}\n{srt_time(start)} --> {srt_time(end)}\n{text.replace(chr(13)," ").replace("-->","→").strip()}\n\n' for index,(start,end,text) in enumerate(validated_captions,1)),encoding='utf-8')
            command.extend(['-i',str(subtitle)])
        command.extend(['-map','0:v:0','-map','1:a:0'])
        if validated_captions and (options or {}).get('captions',True): command.extend(['-map','2:s:0'])
        command.extend(['-c','copy'])
        if validated_captions and (options or {}).get('captions',True):
            command.extend(['-c:s','mov_text','-metadata:s:s:0','language=eng','-disposition:s:0','default'])
        if profile:
            command.extend(['-color_primaries',profile[0],'-color_trc',profile[1],'-colorspace',profile[2]])
            if codec=='h264' and profile==REC709:
                command.extend(['-bsf:v',AVC_REC709])
        command.extend(['-t',f'{out_duration:.6f}','-movflags','+faststart+write_colr' if profile else '+faststart',str(output_path)])
        _run_controlled(command,cancelled,120)
        if cancelled and cancelled.is_set(): raise InterruptedError('Export cancelled')
        if progress: progress(1.0)
        return Path(output_path).stat().st_size
