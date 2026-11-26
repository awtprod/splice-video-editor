"""Write sample timeline exports for checking in DaVinci Resolve, Premiere Pro and Final Cut Pro.

Usage: python3 tests/interchange_evidence.py OUTPUT_DIR

Starts the server, links the media in place and exports through /api/interchange/export. Writes the bundled "Alpine Passage" project, a synthetic 23.976 timeline cut from generated
timecoded media, and a 29.97 drop-frame timeline, each as FCPXML, Final Cut Pro 7 XML, EDLs
(picture and sound), OTIO and a report, with the media beside them and a NOTES.txt saying what an
importer should show.
"""
import shutil
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from interchange_fixtures import DF, at, alpine_project, make_media, synthetic_project  # noqa: E402

NOTES = """Splice timeline interchange evidence
=====================================

Media for every timeline is in media/ (generated test patterns and tones, plus the four
bundled Splice clips and score). The timelines were exported by a running Splice server with
every source linked in place (File > Link media in place, no copy), so each file names the
original's absolute path in media/ on the machine that wrote it, never a Splice URL or proxy;
REPORT.txt lists each reference under "Media the timeline files reference". After copying
elsewhere, relink to media/ (Resolve: Relink Clips / Change Source Folder; Premiere: Link
Media; Final Cut Pro: Relink Files), or use synthetic-23976-path-mapped/ (below).

Each folder holds TIMELINE.fcpxml (FCPXML 1.10: Resolve, Final Cut Pro), TIMELINE.xml
(Final Cut Pro 7 XML, xmeml 5: Premiere Pro, Resolve, Avid), one EDL per picture track
(-V1.edl ...), sound EDLs (-A1.edl ...; -V1-sound.edl is the camera sound under V1),
TIMELINE.otio (OpenTimelineIO) and REPORT.txt, which lists everything each format could not
carry, clip by clip.

Which file to give which application:
- DaVinci Resolve: TIMELINE.fcpxml (or TIMELINE.xml). Not the EDLs for these sources: Resolve
  conforms an EDL by source timecode only, and sources without embedded timecode (the Alpine
  clips, plain_25.mp4) or with overlapping timecode cannot be told apart; REPORT.txt names them
  under "CONFORM WARNING". Resolve refuses the sound-only EDLs (-A1.edl ...).
- Premiere Pro: TIMELINE.xml (File > Import). Premiere does not read FCPXML 1.x. The .otio
  imports too, but clip gain, opacity and scale live only in its metadata there. Premiere's
  own "EDL information" and "New Sequence" dialogs on an EDL import are normal.

1. alpine-passage/ (25 fps, 1920x1080, starts 01:00:00:00, 32 s)
   V1: The Ridge, Into the Valley, Forest Light, The Lake, 8 s each, joined by three
       12-frame cross dissolves starting on the cut. Every clip's source in is frame 13
       (Splice's 0.5 s rounds to 0.52 s, reported).
   V4: title "ALPINE PASSAGE" from 01:00:00:20 for 3 s (FCPXML: Basic Title named after its
       text, which Resolve still calls "Text" and puts on V2 because it drops the empty V2/V3;
       XML: Text generator; OTIO: placeholder clip "ALPINE PASSAGE"; not in EDLs).
   A1: Ambient score for 32 s at -5.19 dB (55 %).
   The clips are 960 x 540 and Splice fits them to the frame, so the XML gives each Scale 200:
   in Premiere they fill the 1920 x 1080 frame, centred (Position 960, 540).
   Sources have no embedded timecode, so every asset starts at 00:00:00:00 and an EDL conform
   in Resolve links all four events to one file (the report says so).

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
   V2  01:00:01:00-01:00:03:00  B001_C001 (29.97 DF source, from 01:00:00;04 source TC; the
                                FCPXML clip carries tcFormat="DF"), 50 % opacity rising from
                                20 %, moved right and up, 80 % scale. In the 23.976 EDL the
                                source timecode counts at 23.976: 01:00:00:03-01:00:02:03
                                (start label 00:59:59;28 read as 00:59:59:22, plus 5 frames).
       01:00:03:08-01:00:04:04  plain_25 DISABLED (FCPXML enabled="0"; left out of EDLs)
       01:00:05:00-01:00:07:00  A001_C002 from 02:00:02:00 at half the fitted size, moved
                                25 % of the width left and 20 % of the height down
   V3  01:00:09:00-01:00:11:00  A001_C001 picture from 01:00:02:00 with its sound starting
                                12 frames earlier on A1 (J-cut; FCPXML audioStart; XML: linked
                                picture and two sound clipitems)
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

4. synthetic-23976-path-mapped/: timeline 2 exported with the path mapping option
   "<this folder>/media => X:\\Turnover\\media", for a Windows machine that sees the media
   there: FCPXML and OTIO name file:///X:/Turnover/media/..., FCP 7 XML
   file://localhost/X%3a/Turnover/media/..., the EDLs "* SOURCE FILE: X:\\Turnover\\media\\...".
   REPORT.txt records the rule and, per source, the local path and the path written.

Position and scale (FCP 7 XML, Premiere Pro Effect Controls > Motion, 1920 x 1080 sequence):
the generated clips are 64 x 36 pixels and Splice fits them to the frame (30x), so Premiere's
Scale is 30 x Splice's scale and the Basic Motion centre is the offset in media widths/heights.
   V2 B001_C001 at 01:00:01:00: Scale 2400 (a 1536 x 864 picture), Position 1152, 486
       (192 px right of centre, 54 px up; XML centre 3, -1.5).
   V2 A001_C002 at 01:00:05:00: Scale 1500 (a 960 x 540 picture), Position 480, 756
       (480 px left of centre, 216 px down; XML centre -7.5, 6), so the picture spans
       x 0-960, y 486-1026.
   Every other picture clip: Scale 3000 (fills the frame), Position 960, 540.

Known importer behaviour to check (see each REPORT.txt):
- Resolve ignores FCPXML srcEnable; audio-only uses of A/V files may also bring picture.
- Resolve imports FCPXML fades as volume keyframes and few or no FCPXML markers.
- Resolve drops empty video tracks from FCPXML (V4 lands on V2 when V2 and V3 are empty).
- EDL: every FCM line sets the source frame code mode of the events after it; the first FCM is
  the record mode. M2 timecode uses colons only. Reel names longer than eight characters are
  mapped in a "* REEL" comment inside the first event that uses them.
- XML: stereo sound is two mono tracks per Splice track (Premiere recombines them); a clip next
  to a transition has start or end -1; pproTicksIn/Out give the exact source position, but
  Premiere uses the whole-frame <in>: REPORT.txt lists each clip whose in point that rounds
  (B001 at 01:00:01:00 starts 0.25 source frames later in Premiere).
- XML: Premiere imports the reversed V1 clip at 01:00:08:00 unlinked from its sound (its own XML
  export writes that pair unlinked too); every other link holds. Titles arrive as Premiere
  graphics named after their text.
- XML, path-mapped folder: the media is not at X:\\Turnover\\media here, so Premiere shows its
  Link Media dialog; that is expected (REPORT.txt says so).
- OTIO: written exactly as OpenTimelineIO 0.18 writes it (Clip.2, media_references); the
  top-level stack carries the timeline name, which Premiere uses for the sequence.
"""


MAPPED_TO = 'X:\\Turnover\\media'


def evidence_synthetic(paths):
    """The synthetic timeline plus one clip that checks position and scale on media smaller than
    the sequence: A001_C002 (64 x 36) on V2 at half the fitted size, moved left and down."""
    project = synthetic_project(paths)
    project['clips'].append({'id': 'v2-moved', 'mediaId': 'camera_b', 'type': 'video', 'track': 2, 'start': at(120),
                             'duration': at(168) - at(120), 'in': at(48), 'title': '', 'volume': 100,
                             'color': {'brightness': 100, 'contrast': 100, 'saturation': 100}, 'transition': 'none',
                             'embeddedAudio': False, 'transform': {'x': -25, 'y': 20, 'scale': 50, 'rotation': 0}})
    return project


def drop_project(paths):
    rate = DF
    return {'name': 'Synthetic 2997 DF', 'width': 1920, 'height': 1080, 'fps': float(rate),
            'media': [{'id': 'drop', 'name': paths['drop'].name, 'src': '', 'type': 'video', 'duration': 12,
                       'path': paths['drop'].name, 'sourcePath': str(paths['drop'])}],
            'clips': [{'id': 'df', 'mediaId': 'drop', 'type': 'video', 'track': 1, 'start': float(Fraction(1798) / rate),
                       'duration': float(Fraction(1804) / rate) - float(Fraction(1798) / rate), 'in': 0, 'volume': 100}]}


class Splice:
    """``python3 server.py`` with its own data directory, driven like the app (launch link,
    session cookie, page token). The evidence media is linked in place, as camera originals are."""

    def __init__(self, data: Path):
        from test_media_server import ServerProcess
        self.process = ServerProcess(data)

    def call(self, method, route, body=None):
        status, payload = self.process.json(method, route, body)
        if status != 200:
            raise RuntimeError(f'{route}: {status} {payload}')
        return payload

    def link(self, project: dict) -> dict:
        """Each media item as media-store.js stores a linked original: describe() spread, then src."""
        from urllib.parse import quote
        items = [m for m in project['media'] if m.get('sourcePath')]
        linked = self.call('POST', '/api/media/link', {'paths': [m['sourcePath'] for m in items]})['files']
        for item, result in zip(items, linked):
            if 'src' not in result:
                raise RuntimeError(f'{item["sourcePath"]}: {result.get("error")}')
            info = self.call('GET', '/api/media/info?src=' + quote(result['src'], safe=''))
            keep = {k: item[k] for k in ('id', 'name', 'type')}
            item.clear()
            item.update(info, **keep, src=result['src'], originalSrc=result['src'], path=result['sourcePath'],
                        linked=True, sourceFingerprint=result['fingerprint'], offline=False)
        return project

    def close(self):
        self.process.close()


def write(splice, folder: Path, project: dict, options: dict):
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file():
            old.unlink()
    result = splice.call('POST', '/api/interchange/export', {'project': project, 'options': {
        'formats': ['fcpxml', 'xmeml', 'edl', 'otio'], 'audioEdl': True, **options}})
    for file in result['files']:
        (folder / file['name']).write_text(file['content'])
    (folder / 'REPORT.txt').write_text(result['report'])
    return [file['name'] for file in result['files']]


def main(output: Path):
    import tempfile
    media = output / 'media'
    paths = make_media(media)
    alpine = alpine_project(ROOT)
    for item in alpine['media']:
        source = ROOT / item['src']
        target = media / 'alpine' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        item['sourcePath'] = str(target)
    with tempfile.TemporaryDirectory(prefix='splice-evidence-') as data:
        splice = Splice(Path(data))
        try:
            synthetic = splice.link(evidence_synthetic(paths))
            written = {
                'alpine-passage': write(splice, output / 'alpine-passage', splice.link(alpine), {}),
                'synthetic-23976': write(splice, output / 'synthetic-23976', synthetic, {}),
                'synthetic-2997df': write(splice, output / 'synthetic-2997df', splice.link(drop_project(paths)),
                                          {'dropFrame': True, 'startTimecode': '00:00:00;00'}),
                'synthetic-23976-path-mapped': write(splice, output / 'synthetic-23976-path-mapped', synthetic,
                                                     {'pathMap': [{'from': str(media), 'to': MAPPED_TO}]}),
            }
        finally:
            splice.close()
    (output / 'NOTES.txt').write_text(NOTES)
    for folder, names in written.items():
        print(folder, ' '.join(names))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).resolve())
