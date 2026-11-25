"""Write sample timeline exports for checking in DaVinci Resolve, Premiere Pro and Final Cut Pro.

Usage: python3 tests/interchange_evidence.py OUTPUT_DIR

Writes the bundled "Alpine Passage" project, a synthetic 23.976 timeline cut from generated
timecoded media, and a 29.97 drop-frame timeline, each as FCPXML, EDLs (picture and sound),
OTIO and a report, with the media beside them and a NOTES.txt saying what an importer should show.
"""
import shutil
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange.api import export_timeline  # noqa: E402
from interchange_fixtures import DF, alpine_project, make_media, synthetic_project  # noqa: E402

NOTES = """Splice timeline interchange evidence
=====================================

Media for every timeline is in media/ (generated test patterns and tones, plus the four
bundled Splice clips and score). The file URLs point at this folder on the machine that wrote
it; after copying elsewhere, relink to media/ (Resolve: Relink Clips / Change Source Folder;
Premiere: Link Media; Final Cut Pro: Relink Files).

Each folder holds TIMELINE.fcpxml, one EDL per picture track (-V1.edl ...), sound EDLs
(-A1.edl ...; -V1-sound.edl is the camera sound under V1), TIMELINE.otio and REPORT.txt,
which lists everything each format could not carry.

1. alpine-passage/ (25 fps, 1920x1080, starts 01:00:00:00, 32 s)
   V1: The Ridge, Into the Valley, Forest Light, The Lake, 8 s each, joined by three
       12-frame cross dissolves starting on the cut. Every clip's source in is frame 13
       (Splice's 0.5 s rounds to 0.52 s, reported).
   V4: title "ALPINE PASSAGE" from 01:00:00:20 for 3 s (FCPXML: Basic Title; not in EDLs).
   A1: Ambient score for 32 s at -5.19 dB (55 %).
   Sources have no embedded timecode, so every asset starts at 00:00:00:00.

2. synthetic-23976/ (23.976 fps, 1920x1080, starts 01:00:00:00, 264 frames = 11.011 s)
   Sources: A001_C001_tc1h.mov (23.976, TC 01:00:00:00, stereo), A001_C002_tc2h.mov
   (23.976, TC 02:00:00:00, stereo), B001_C001_df.mov (29.97 drop frame, TC 00:59:59;28),
   plain_25.mp4 (25 fps, no TC), room_tone.wav (Broadcast WAV, time reference 01:00:00:00
   at 23.976). In Resolve's media pool the start timecodes must read exactly those values;
   a clip that imports offline means the asset start did not match.
   V1  01:00:00:00-01:00:02:00  A001_C001 from 01:00:00:00
       01:00:02:00-01:00:04:00  A001_C002 from 02:00:01:00, 12-frame dissolve starting
                                6 frames before the cut (01:00:01:18-01:00:02:06)
       01:00:04:00-01:00:05:00  gap
       01:00:05:00-01:00:07:00  A001_C001 at 50 % from 01:00:01:00
       01:00:07:00-01:00:08:00  A001_C002 at 200 % from 02:00:00:00
       01:00:08:00-01:00:09:00  A001_C001 reversed, source 01:00:00:12-01:00:01:12
   V2  01:00:01:00-01:00:03:00  B001_C001 (29.97 DF source, from 01:00:00;04 source TC),
                                50 % opacity rising from 20 %, moved right and up, 80 % scale
       01:00:03:08-01:00:04:04  plain_25 DISABLED (FCPXML enabled="0"; left out of EDLs)
   V3  01:00:09:00-01:00:11:00  A001_C001 picture from 01:00:02:00 with its sound starting
                                12 frames earlier on A1 (J-cut; FCPXML audioStart)
   V4  title "SYNTHETIC TURNOVER" for the first 2 s
   A2  room_tone 01:00:00:00-01:00:05:00 at 70 % with 12-frame fade in, 24-frame fade out
       and volume keys 70 % -> 35 %; then room_tone again to 01:00:08:08 with a 12-frame
       audio crossfade at the cut
   Markers: "Picture lock note" at 01:00:01:06, "Music in" at 01:00:05:10 (EDL locators;
   FCPXML clip markers).

3. synthetic-2997df/ (29.97 fps drop frame, starts 00:00:00;00)
   V1  B001_C001_df.mov from 00:59:59;28 (source) placed at 00:00:59;28, 6 frames long,
       so the record timecode crosses the minute boundary: 00:00:59;28, 00:00:59;29,
       00:01:00;02 (;00 and ;01 are skipped).

Known importer behaviour to check (see each REPORT.txt):
- Resolve ignores FCPXML srcEnable; audio-only uses of A/V files may also bring picture.
- Resolve imports FCPXML fades as volume keyframes and few or no FCPXML markers.
- Premiere Pro does not import FCPXML natively; use the EDLs or OTIO there.
"""


def drop_project(paths):
    rate = DF
    return {'name': 'Synthetic 2997 DF', 'width': 1920, 'height': 1080, 'fps': float(rate),
            'media': [{'id': 'drop', 'name': paths['drop'].name, 'src': '', 'type': 'video', 'duration': 12,
                       'path': paths['drop'].name, 'sourcePath': str(paths['drop'])}],
            'clips': [{'id': 'df', 'mediaId': 'drop', 'type': 'video', 'track': 1, 'start': float(Fraction(1798) / rate),
                       'duration': float(Fraction(1804) / rate) - float(Fraction(1798) / rate), 'in': 0, 'volume': 100}]}


def write(folder: Path, project: dict, options: dict):
    folder.mkdir(parents=True, exist_ok=True)
    result = export_timeline(project, {'formats': ['fcpxml', 'edl', 'otio'], 'audioEdl': True, **options})
    for file in result['files']:
        (folder / file['name']).write_text(file['content'])
    (folder / 'REPORT.txt').write_text(result['report'])
    return [file['name'] for file in result['files']]


def main(output: Path):
    media = output / 'media'
    paths = make_media(media)
    alpine = alpine_project(ROOT)
    for item in alpine['media']:
        source = ROOT / item['src']
        target = media / 'alpine' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        item['sourcePath'] = str(target)
    written = {
        'alpine-passage': write(output / 'alpine-passage', alpine, {}),
        'synthetic-23976': write(output / 'synthetic-23976', synthetic_project(paths), {}),
        'synthetic-2997df': write(output / 'synthetic-2997df', drop_project(paths), {'dropFrame': True, 'startTimecode': '00:00:00;00'}),
    }
    (output / 'NOTES.txt').write_text(NOTES)
    for folder, names in written.items():
        print(folder, ' '.join(names))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).resolve())
