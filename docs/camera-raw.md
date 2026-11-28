# Camera RAW

Splice imports camera RAW as RAW: the clip keeps its original file, its real metadata and its own decode settings, and export develops the original at full resolution. What you can do depends on the format:

- **Built in (no install):** CinemaDNG and Apple ProRes RAW.
- **Other cinema RAW:** imports with its sound, timecode and reel without any install. To see and export its picture you need the vendor's decoder (or a bridge program), or you attach a proxy.

> **How much of this has been tested with real camera files?** Not much yet. The built-in CinemaDNG developer was tested with synthetic DNG files. The ProRes RAW path was tested on a Bayer stand-in, because no ProRes RAW sample was available. The vendor decoders (`REDline`, `art-cmd`, `dcraw_emu`, `braw-decode`) were **not run**: their support was tested against mock decoders and against header fixtures laid out as FFmpeg and LibRaw read them. No real `.braw`, `.r3d`, `.ari` or camera `.dng` file has been run through Splice. Treat vendor RAW support as unproven until you have tried your own footage.

## What each format gets

| Format | Without a decoder | Picture | How it was verified |
|---|---|---|---|
| CinemaDNG, uncompressed 8–16-bit | — | Built in (`raw_bayer.py`) | Synthetic 12- and 16-bit sequences: exposure, ISO, white balance and tint direction, highlight recovery, frame accuracy, sidecar WAV, preview against export |
| CinemaDNG, lossless JPEG | — | FFmpeg's TIFF decoder, then the built-in colour pipeline | Synthetic frames through the FFmpeg path. **No real lossless-JPEG DNG** was run (FFmpeg cannot write one) |
| Apple ProRes RAW | Metadata through ffprobe | FFmpeg 8 or later (`prores_raw` decoder) | Pipeline checked on a Bayer stand-in. **No real ProRes RAW sample.** FFmpeg does not expose the camera colour matrix, so primaries stay camera-native |
| Blackmagic RAW `.braw` | Size, frame rate, duration, timecode, reel, camera, sound, `.sidecar` look | `braw-decode` or a contract bridge | Mock decoder only |
| RED `.r3d` | Size, frame rate, frame count, `.RMD` look, timecode and reel | `REDline` (through TIFF) or a contract bridge | Header fixtures only; REDline not run |
| ARRIRAW `.ari` / `.arx` | Size, camera model, frame count for sequences | `art-cmd` or a contract bridge | Header fixtures only; art-cmd not run |
| Canon Cinema RAW Light `.crm`, Nikon N-RAW `.nev` | QuickTime metadata and sound | Contract bridge | Synthetic containers |
| Sony X-OCN, ARRIRAW in MXF | ffprobe metadata | Contract bridge | Detection only. XDCAM and DNxHD MXF are verified to stay ordinary media |
| Still RAW (`.cr2` `.cr3` `.nef` `.arw` `.raf` `.orf` `.rw2`), other compressed DNG | Refused with guidance | LibRaw `dcraw_emu` | Adapter only; dcraw_emu not installed on the test machine |

## Importing

- Link CinemaDNG sequences with **File → Link media in place…**. A sequence (`A001_000001.dng`, …) is listed once, by its first frame, and plays as one clip with its frame rate, timecode and sidecar WAV. A browser upload through **Import media** copies one frame only.
- Other RAW files import through **Link media in place…** or **Import media** like any other media.
- If a RAW clip has no decoder, it plays a slate with its real sound, and the Inspector offers **Configure RAW Decoders…** and **Attach Proxy…**.

## Metadata Splice could not read: "estimated"

Splice never invents metadata silently. If a file does not record a value Splice needs (timecode, frame rate, duration, frame size, ISO, white balance, tint or camera model), Splice uses a placeholder and:

- the Inspector says **"Not in the file: …"** and marks the placeholder controls **estimated**;
- the turnover report and the Resolve report list the placeholders.

## The Camera RAW Inspector

Shown automatically for RAW clips, in the Edit and Color workspaces:

- Format and decoder badges.
- **Decode using** Camera Metadata or Clip.
- ISO, Exposure (−4 to +4), White Balance (2000–15000 K), Tint (−100 to +100), Color space and Gamma (choices depend on the format), Highlight recovery.
- **Preview debayer:** Full, Half or Quarter. This affects the preview only.
- **Reset to Camera Metadata.**

Every change is an ordinary clip edit: undoable, and saved with the project. When a decoder cannot apply a setting, that control is turned off for the clip, and an export lists the settings it could not apply (for example "art-cmd can't apply exposure, gamma, highlight recovery — exported with camera values"). `art-cmd`'s exposure and highlight controls and `dcraw_emu`'s Kelvin and tint are not documented by their vendors, so Splice turns those off rather than guess.

## Export

Before grading, LUTs and delivery, each enabled RAW clip is developed from the original at full resolution in 16-bit RGB with its own settings, into a lossless intermediate. Only the frames the cut uses (plus transition handles) are developed. Cancelling stops every decoder process.

If you attached a proxy, export uses the proxy's picture. The original RAW path is still what interchange and Resolve receive.

RAW clips can be multicam angles, can sit inside nested sequences, and can be subclipped.

## Speed

The built-in CinemaDNG developer is pure Python. On the build machine it took about 0.6 s per 1080p frame at half debayer and 2.4 s at full. Long 4K rolls take a long time to proxy and export.

## Setting up vendor decoders

Splice ships no vendor code. Install the vendor's free tool yourself and point Splice at it in **File → RAW Decoders…**, or with an environment variable:

| Decoder | Environment variable | What Splice looks for |
|---|---|---|
| Blackmagic RAW | `SPLICE_BRAW_SDK` | `braw-decode`, or another program that speaks the contract below. The Blackmagic RAW SDK library on its own is reported but not used: Splice needs a command-line program. |
| RED R3D | `SPLICE_REDLINE` | `REDline`, which comes with REDCINE-X PRO |
| ARRIRAW | `SPLICE_ARRI_CMD` | `art-cmd`, which comes with ARRI Reference Tool |
| Still RAW and compressed DNG | `SPLICE_LIBRAW` | LibRaw's `dcraw_emu` (for example `apt install libraw-bin` or `brew install libraw`) |
| Canon Cinema RAW Light | `SPLICE_CRM_CMD` | A contract bridge. Canon publishes no command-line decoder. |
| Nikon N-RAW | `SPLICE_NRAW_CMD` | A contract bridge. Nikon publishes no command-line decoder. |
| Sony X-OCN | `SPLICE_XOCN_CMD` | A contract bridge. Sony's RAW Viewer is a desktop app. |

`SPLICE_RAW_DECODERS` can set several at once as a JSON object, keyed by decoder id (`braw`, `r3d`, `arriraw`, `libraw`, `canon_crm`, `nraw`, `xocn`), for example:

```sh
export SPLICE_RAW_DECODERS='{"r3d": "/usr/local/bin/REDline", "libraw": "/usr/bin/dcraw_emu"}'
```

Splice also searches `PATH` and the vendors' standard install folders. Paths saved in the dialog go to `raw-decoders.json` in Splice's data folder (`%LOCALAPPDATA%\Splice\raw-decoders.json` on Windows). A saved path must be absolute, must exist, must be executable, and must not contain shell metacharacters. Decoders are run with an argument list, never through a shell.

### The Splice RAW decoder contract

Splice drives `REDline`, `art-cmd` and `dcraw_emu` with their own options. Any other program can be used if it accepts:

```
DECODER --input FILE --pix-fmt rgb48le --width W --height H --start-frame N --frames N
        --decode-using camera|clip --iso ISO --exposure EV --kelvin K --tint T
        --colorspace NAME --gamma NAME --highlight-recovery 0|1
        --quality full|half|quarter|eighth --output -
```

and writes exactly `--frames` frames of W × H 16-bit little-endian RGB to standard output. The contract is documented at the top of [`raw_decode.py`](../raw_decode.py).

## Interchange

FCPXML, FCP 7 XML, EDL and OTIO keep the original RAW path, reel and timecode. Other applications decode the original with their own camera settings, so Splice's RAW overrides do not travel; they are listed in the turnover report and the Resolve report. A frame sequence is referenced by its first frame. Attached proxies are not written to Resolve.

## Known limits

- **Vendor decoders and real camera files are untested** (see the note at the top).
- RAW clips cannot be stabilized yet: export a copy and stabilize that.
- The pure-Python CinemaDNG developer is slow.
- ProRes RAW primaries stay camera-native.
- A CinemaDNG sequence with missing frame numbers plays later frames early; the Inspector says how many are missing.
