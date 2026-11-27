"""Export speed benchmark (not a unit test): generated 1080p media cut into a long timeline.

    SPLICE_DATA_DIR=<dir> SPLICE_TEMP_DIR=<dir> python3 tests/delivery_bench.py <minutes> <cuts> <codec> <out.json> [repo]

Renders the timeline through render_movie.render_to_file of the repository given (default: this
checkout) and writes wall time, CPU time of this process and every FFmpeg it started, and the peak
resident memory of the largest child (ru_maxrss of RUSAGE_CHILDREN) as JSON. Sources are ten
60-second 1920x1080 24 fps H.264 files with a 1 kHz tone, generated once under the data folder.
"""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import threading
import time


def _tree_rss_mb(root):
    """Resident memory of this process and every descendant, from /proc (Linux)."""
    parents, rss = {}, {}
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/stat') as handle:
                fields = handle.read().rsplit(')', 1)[1].split()
            parents[int(entry)] = int(fields[1])
            rss[int(entry)] = int(fields[21]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, IndexError, ValueError):
            continue
    total, stack = 0, [root]
    while stack:
        pid = stack.pop()
        total += rss.get(pid, 0)
        stack.extend(child for child, parent in parents.items() if parent == pid)
    return total / 2**20


def main():
    minutes, cuts, codec, out = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
    repo = Path(sys.argv[5] if len(sys.argv) > 5 else Path(__file__).resolve().parent.parent).resolve()
    sys.path.insert(0, str(repo))
    from premiere import MEDIA_ROOT
    import render_movie
    bench = MEDIA_ROOT / 'bench'
    bench.mkdir(parents=True, exist_ok=True)
    sources = []
    for number in range(10):
        path = bench / f'source-{number}.mp4'
        if not path.exists():
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            f'testsrc2=s=1920x1080:r=24:d=60,hue=h={number*36}', '-f', 'lavfi', '-i',
                            f'sine=f={300+number*100}:sample_rate=48000:d=60', '-c:v', 'libx264', '-preset', 'ultrafast',
                            '-g', '48', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path.with_suffix('.tmp.mp4'))],
                           check=True)
            path.with_suffix('.tmp.mp4').rename(path)
        sources.append(path)
    total = minutes * 60
    length = total / cuts
    clips = []
    for index in range(cuts):
        source_in = (index * 7.3) % (60 - length - 1)
        common = dict(mediaId=f'm{index % 10}', start=round(index * length, 6), duration=round(length, 6), **{'in': round(source_in, 6)})
        clips.append(dict(common, type='video', track=1, id=f'v{index}'))
        clips.append(dict(common, type='audio', track=1, id=f'a{index}'))
    project = dict(name='bench', width=1920, height=1080, fps=24,
                   media=[dict(id=f'm{n}', name=p.name, src='/media/bench/' + p.name) for n, p in enumerate(sources)],
                   clips=clips)
    target = Path(os.environ['SPLICE_TEMP_DIR']) / f'bench-output.{"mov" if codec != "h264" else "mp4"}'
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    own = resource.getrusage(resource.RUSAGE_SELF)
    peak, done = [0.0], threading.Event()

    def sample():
        while not done.wait(.5):
            peak[0] = max(peak[0], _tree_rss_mb(os.getpid()))
    threading.Thread(target=sample, daemon=True).start()
    start = time.monotonic()
    render_movie.render_to_file(project, target, options={'codec': codec})
    wall = time.monotonic() - start
    done.set()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    mine = resource.getrusage(resource.RUSAGE_SELF)
    frames = subprocess.run(['ffprobe', '-v', 'error', '-count_packets', '-select_streams', 'v:0', '-show_entries',
                             'stream=nb_read_packets', '-of', 'csv=p=0', str(target)], capture_output=True, text=True).stdout.strip()
    result = dict(repo=str(repo), minutes=minutes, cuts=cuts, codec=codec, wall_s=round(wall, 1),
                  cpu_s=round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime +
                              mine.ru_utime + mine.ru_stime - own.ru_utime - own.ru_stime, 1),
                  peak_child_rss_mb=round(after.ru_maxrss / 1024), peak_tree_rss_mb=round(peak[0]), frames=frames, bytes=target.stat().st_size)
    target.unlink()
    out.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
