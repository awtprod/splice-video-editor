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
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file', '-show_entries', 'stream=codec_type', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=15, check=True)
    return {stream['codec_type'] for stream in json.loads(result.stdout).get('streams', [])}


def render_to_file(project, output_path):
    if not isinstance(project, dict):
        raise ValueError('Invalid project')
    width = int(_number(project.get('width'), 'width', 16, 3840))
    height = int(_number(project.get('height'), 'height', 16, 2160))
    fps = Fraction(_number(project.get('fps'), 'frame rate', 1, 60)).limit_denominator(1001)
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
    options = project.get('renderOptions') or {}
    if not isinstance(options, dict) or not isinstance(options.get('mutedTracks', {}), dict):
        raise ValueError('Invalid render options')
    muted = options.get('mutedTracks', {})
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
        if item['id'] not in paths:
            path = _media_path(item.get('src', ''))
            paths[item['id']] = path
            kinds[item['id']] = _probe(path)
            inputs.extend(['-threads', '2', '-protocol_whitelist', 'file', '-i', str(path)])
        if clip['type'] not in kinds[item['id']]:
            raise ValueError(f"{item.get('name', 'Media')} lacks a {clip['type']} stream")
    indexes = {identity: index for index, identity in enumerate(paths)}
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-filter_complex_threads', '3', '-y', *inputs,
               '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}']
    base_index = len(indexes)
    filters = [f'[{base_index}:v]format=yuv420p[base0]']
    active = 'base0'
    video_clips = sorted((c for c in clips if c['type'] == 'video'), key=lambda c: (c['start'], c.get('track', 0)))
    if not muted.get('video'):
        for number, clip in enumerate(video_clips):
            item = media_by_id[clip['mediaId']]
            index = indexes[item['id']]
            start = float(clip['start']); length = float(clip['duration']); source_in = float(clip.get('in', 0))
            color = clip.get('color') or {}
            brightness = _number(color.get('brightness', 100), 'brightness', 0, 300) / 100
            contrast = _number(color.get('contrast', 100), 'contrast', 0, 300) / 100
            saturation = _number(color.get('saturation', 100), 'saturation', 0, 300) / 100
            chain = f'[{index}:v]trim=start={source_in:.6f}:duration={length:.6f},setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,eq=brightness={brightness-1:.3f}:contrast={contrast:.3f}:saturation={saturation:.3f},format=yuva420p'
            previous = next((c for c in reversed(video_clips[:number]) if c['start'] + c['duration'] >= start - .05), None)
            dissolve = previous is not None and clip.get('transition') == 'dissolve'
            if dissolve:
                prior_index = indexes[previous['mediaId']]
                tail = min(.6, float(previous['duration']))
                prior_source = float(previous.get('in', 0)) + float(previous['duration']) - tail
                filters.append(f'[{prior_index}:v]trim=start={prior_source:.6f}:duration={tail:.6f},setpts=PTS-STARTPTS,fps={fps},scale={width}:{height},format=yuva420p,setpts=PTS+{start:.6f}/TB[tail{number}]')
                filters.append(f'[{active}][tail{number}]overlay=eof_action=pass:shortest=0[under{number}]')
                active = f'under{number}'
                chain += f',fade=t=in:st=0:d={tail:.6f}:alpha=1'
            chain += f',setpts=PTS+{start:.6f}/TB[clip{number}]'
            filters.append(chain)
            filters.append(f'[{active}][clip{number}]overlay=eof_action=pass:shortest=0[base{number+1}]')
            active = f'base{number+1}'
    with tempfile.TemporaryDirectory(prefix='splice-render-') as work:
        for number, clip in enumerate(c for c in clips if c['type'] == 'title' and c.get('title') and not muted.get('title')):
            text_file = Path(work) / f'title-{number}.txt'
            text_file.write_text(str(clip['title'])[:500])
            begin = float(clip['start']); end = begin + float(clip['duration'])
            filters.append(f'[{active}]drawtext=font=sans-serif:textfile={text_file}:expansion=none:fontsize={round(height*.08)}:fontcolor=white:shadowcolor=black:shadowx=3:shadowy=3:x=(w-text_w)/2:y=h*0.78:enable=between(t\\,{begin:.6f}\\,{end:.6f})[title{number}]')
            active = f'title{number}'
        audio_labels = []
        if not muted.get('audio'):
            for number, clip in enumerate(c for c in clips if c['type'] in ('audio', 'video')):
                item = media_by_id[clip['mediaId']]
                if 'audio' not in kinds[item['id']] or (clip['type'] == 'video' and clip.get('embeddedAudio') is False):
                    continue
                index = indexes[item['id']]
                source_in = float(clip.get('in', 0)); length = float(clip['duration']); delay = round(float(clip['start']) * 1000)
                gain = _number(clip.get('volume', 100), 'volume', 0, 200) / 100
                filters.append(f'[{index}:a]atrim=start={source_in:.6f}:duration={length:.6f},asetpts=PTS-STARTPTS,volume={gain:.4f},adelay={delay}|{delay}[audio{number}]')
                audio_labels.append(f'[audio{number}]')
        if audio_labels:
            filters.append(f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=longest:normalize=0,alimiter=limit=0.95[aout]")
        else:
            command.extend(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo'])
            filters.append(f'[{base_index+1}:a]atrim=duration={duration:.6f}[aout]')
        output = Path(output_path)
        command.extend(['-filter_complex', ';'.join(filters), '-map', f'[{active}]', '-map', '[aout]',
                        '-t', f'{duration:.6f}', '-r', str(fps), '-c:v', 'libx264', '-threads', '4', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(output)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=min(3600, max(120, duration * 8)))
        if result.returncode:
            raise ValueError('FFmpeg render failed: ' + result.stderr[-1500:])
        return output.stat().st_size
