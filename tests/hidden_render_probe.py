"""Regression check: the renderer leaves hidden picture tracks out of an export.

A tiny probe: V1 is a black-graded ridge clip, V2 an ungraded valley clip on a hidden track. The
renderer honours ``trackState["video:2"].hidden`` when the frame comes out dark (V1). Prints
``honors`` and exits 0; if V2's bright picture is on top it prints ``ignores`` and exits 1 (a hard
failure: the renderer always honours hidden tracks). Run by tests/editing-round1.cjs.
"""
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from render_movie import render_to_file  # noqa: E402


def main():
    clip = lambda identity, media, track, **extra: dict(id=identity, mediaId=media, type='video', track=track, start=0,
                                                        duration=.4, **{'in': 1}, color=extra.pop('color', {}),
                                                        volume=100, transition='none', **extra)
    project = dict(name='Hidden probe', width=64, height=36, fps=25,
                   media=[{'id': name, 'name': name, 'src': f'assets/{name}.mp4'} for name in ('ridge', 'valley')],
                   clips=[clip('dark', 'ridge', 1, color={'brightness': 0}), clip('top', 'valley', 2)],
                   trackState={'video:2': {'hidden': True}})
    with tempfile.TemporaryDirectory(prefix='splice-hidden-probe-') as directory:
        output = Path(directory) / 'probe.mp4'
        render_to_file(project, output)
        frame = subprocess.check_output(['ffmpeg', '-nostdin', '-v', 'error', '-ss', '0.2', '-i', str(output),
                                         '-frames:v', '1', '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1'], timeout=30)
    honors = statistics.mean(frame) < 24
    print('honors' if honors else 'ignores')
    if not honors:
        print('The export shows the hidden V2 track: the renderer must leave hidden picture tracks out.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
