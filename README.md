# Splice

Splice is a local, single-user video editor for Windows, Linux and macOS. It cuts, grades, mixes and delivers on your own machine, from camera originals it links in place rather than copies, and it exchanges work with Premiere Pro, DaVinci Resolve and other NLEs: Premiere `.prproj`, FCPXML, Final Cut Pro 7 XML, EDL, OpenTimelineIO, and Resolve projects and disk libraries.

The editor runs in a Chromium window (Electron on Windows, Chrome, Chromium or Edge elsewhere) and talks to a Python server on `127.0.0.1` that uses FFmpeg for decoding and rendering. It opens with **Alpine Passage**, a 32-second sample sequence.

> **Status: pre-release.** The features described here are on the `dev` branch. No published release contains them yet, Splice's own licence has not been decided (see [License](#license)), and several features are verified only by Splice's own tests. Read [Limitations and known issues](#limitations-and-known-issues) before relying on it for paid work.

## Contents

- [Install](#install) · [Requirements](#requirements) · [Quick start](#quick-start)
- [Features](#features)
- [Interchange and round-trip](#interchange-and-round-trip)
- [Can it replace commercial software?](#can-it-replace-commercial-software)
- [Limitations and known issues](#limitations-and-known-issues)
- [License](#license) · [Included materials](#included-materials)
- [Documentation](#documentation) · [Contributing and tests](#contributing-and-tests)

## Install

### Windows: the installer (`Splice-Setup-x64.exe`)

A single per-user EXE that bundles Python, FFmpeg and Electron, so nothing else needs installing. It installs to `%LOCALAPPDATA%\Programs\Splice\Application` without administrator rights and adds Splice to the Start menu.

- **It is not code-signed, so Windows SmartScreen will warn** before it runs.
- **No published release contains it yet.** Build it with `python desktop/windows/build.py artifacts/installer` on Windows, or take the `Splice-Windows-x64` artifact from the repository's *Windows installer* workflow.
- It has been verified only through GitHub's `windows-latest` CI runner.
- Your data lives in `%LOCALAPPDATA%\Splice` and is kept when you uninstall.

Full instructions and troubleshooting: [docs/windows.md](docs/windows.md).

### Linux and macOS: run from source

1. Install Python 3.10 or newer, FFmpeg with ffprobe (see [Requirements](#requirements)), and Chrome, Chromium or Microsoft Edge.
2. Clone this repository and check out `dev`.
3. Start Splice:
   - **In its own window:** `./splice` on Linux, or `python3 splice_app.py`. It opens a dedicated browser window and stops the server when the window closes.
   - **In a browser tab:** `python3 server.py`, then open the one-time link it prints (`http://127.0.0.1:8765/?launch=…`). The plain address is refused and the link works once. Lost the session? Press Enter in the server's terminal, or run `kill -USR1 <pid>`, for a new link.

No Python packages are needed. On macOS, `python3 package_desktop.py artifacts` builds an unsigned portable `Splice.app` that finds Homebrew's Python and FFmpeg in `/opt/homebrew/bin` or `/usr/local/bin`. **macOS has no CI and the features on `dev` have not been tested there.**

The existing v0.1.0 release (portable ZIPs for Windows, macOS and Linux) predates almost everything in this README.

## Requirements

- **Python 3.10 or newer** (development and the Windows build use 3.14; older versions are not covered by CI). Standard library only.
- **A Chromium-based browser**: Chrome, Chromium or Edge. The monitor uses WebGL2 when available, with a Canvas fallback.
- **FFmpeg and ffprobe** with the features Splice builds its filter graphs from. Check yours with:

  ```sh
  python3 runtime_check.py           # required features
  python3 runtime_check.py --strict  # also the optional ones
  ```

  `runtime_check.py` is the authoritative list. In summary:
  - **Encoders:** libx264 / libx264rgb, libx265 (8-, 10- and 12-bit), prores_ks, dnxhd, ffv1, libvpx-vp9, PNG, TIFF, DPX, EXR, AAC, PCM and mov_text.
  - **Filters:** among others zscale and tonemap (colour and HDR conversion), drawtext, lut1d and lut3d, geq, signalstats, ebur128, astats, acompressor, agate, biquad, atempo and join.
  - **Bitstream filters:** `h264_metadata`, `prores_metadata`. **Muxers and demuxers:** MP4, MOV, MXF, WAV, WebM, `ipod` (.m4a), Matroska, image2, lavfi, concat and the raw formats.
  - **Optional:** `vidstabdetect` / `vidstabtransform` (stabilization), `libsvtav1` (AV1 export), and the `prores_raw` decoder (Apple ProRes RAW; FFmpeg 8 or later).

  Ubuntu's packaged FFmpeg 8.0.1 passes the strict check. Other builds may lack zscale (libzimg) or vid.stab; run the check.
- **The Windows installer bundles** gyan.dev's FFmpeg 9.1 "full_build-shared" (GPL v3, with vid.stab, Rubber Band, zimg, x264, x265, SVT-AV1 and the `prores_raw` decoder), embeddable Python 3.14.10 and Electron 45.5.0, each pinned by SHA-256. The app uses only its bundled FFmpeg and refuses to start, with a message, if that FFmpeg lacks a required feature. Versions and licences: [`desktop/windows/Third-party.txt`](desktop/windows/Third-party.txt).

## Quick start

1. **File → New project…**: name, frame size and frame rate.
2. **File → Link media in place…** to link camera originals where they are (nothing is copied), or **Import media** to copy files in.
3. Open a clip in the source monitor, mark **I** and **O**, then **,** to insert or **.** to overwrite at the playhead. Trim on the timeline by dragging edges; **Q** / **W** ripple trim to the playhead.
4. Grade in the **Color** workspace and mix in the **Audio** workspace.
5. **▣ Save** (Ctrl/Cmd+S) writes a Premiere project. **File → Save working project…** (Ctrl/Cmd+Shift+S) keeps everything Splice-specific. Edits are also checkpointed locally; **File → Recent projects & recovery** brings back earlier versions.
6. **↗ Export video**: pick a preset, then render or add to the **render queue**. **File → Export timeline…** hands the cut to another application.

**File → Keyboard and mouse** lists every shortcut.

## Features

| Area | What you get | Docs |
|---|---|---|
| Editing | Three- and four-point editing, ripple, roll, slip, slide, J/L cuts on linked picture and sound, clip enable (Shift+E), lift and extract, inclusive In/Out, drop-frame timecode, titles, SRT captions, markers | [Editing](docs/editing.md) |
| Organising | Nested sequences, multicam (up to 9 angles, live multi-angle monitor), subclips, bins and sub-bins, editorial metadata and colour labels | [Multicam, nesting, subclips and bins](docs/multicam-nesting-bins.md) |
| Colour | Lift/gamma/gain/offset, curves, hue vs saturation, input and look LUTs (up to 65-point 3D, 65,536-entry 1D), adjustment clips, grade keyframes, waveform/parade/vectorscope/histogram, WebGL2 monitor measured against the export | [Colour finishing](docs/color.md) |
| Audio | EQ, compressor, gate, de-noise; volume and pan automation; waveform and timecode sync; BS.1770 loudness targets; 4× oversampled true-peak limiter; 5.1; stems and isos | [Audio finishing](docs/audio.md) |
| Delivery | Presets for web, HDR10, festival and broadcast masters, archive and proxies; ProRes, DNxHR, H.264, HEVC, AV1, image sequences; render queue; feature-length timelines | [Delivery and the render queue](docs/delivery.md) |
| Camera RAW | CinemaDNG and ProRes RAW built in; BRAW, R3D, ARRIRAW and others through vendor decoders (**verified only with mock decoders**) | [Camera RAW](docs/camera-raw.md) |
| Effects | Speed ramps, bezier easing, crop and masks, chroma key, blend modes, stabilization | [Effects](docs/effects.md) |
| Interchange | Premiere, FCPXML, FCP 7 XML, EDL, OTIO, Resolve read and write, with loss reports | [Interchange and Resolve round-trip](docs/interchange.md) |
| Windows | Installer, data locations, troubleshooting | [Windows](docs/windows.md) |

## Interchange and round-trip

| Route | Read | Write |
|---|---|---|
| Premiere Pro `.prproj` | ✅ | ✅ keeps native objects Splice cannot edit |
| FCPXML | 1.9–1.11 | 1.10 (Resolve, Final Cut Pro) |
| Final Cut Pro 7 XML | ✅ | ✅ (the route into Premiere, also Resolve and Avid) |
| CMX3600 EDL | ✅ | ✅ one per picture track, plus sound EDLs |
| OpenTimelineIO | ✅ | ✅ |
| DaVinci Resolve | `.drp` files and disk libraries (read-only, from a private copy) | New projects in a disk library; no `.drp` export |

- **Nothing is dropped silently.** Every route reports, clip by clip, what it could not carry. A **turnover package** zips the exports with that report and, optionally, a burned-in reference movie.
- Grades, LUTs, effects, nested sequences and unflattened multicam clips do not travel through FCPXML, FCP 7 XML, EDL or OTIO. Flatten and unnest first, and read the report.
- **Resolve write** never modifies or replaces an existing project, builds the new project in a staging folder, checks it with Splice's own reader before moving it in, and leaves nothing behind if anything fails. Projects written by the current writer **had not yet been confirmed in Resolve** when it merged.
- Network (PostgreSQL) Resolve libraries and Blackmagic Cloud are not supported.

Details, per-format tables and what was checked in real Premiere and Resolve: [docs/interchange.md](docs/interchange.md).

## Can it replace commercial software?

Not yet as your only NLE. Splice can cut, grade, mix and deliver a short, documentary or feature-length timeline, and hand it to Premiere or Resolve with a written account of what did not travel. It is behind on verification in the other applications, vendor camera RAW, speed, colour beyond primaries, and packaging (no release, unsigned builds). It is missing motion tracking, advanced titling, DCP/IMF, Dolby Vision, CEA-608/708 captions, plug-ins and collaboration. The full, candid comparison: [docs/can-it-replace-commercial-nles.md](docs/can-it-replace-commercial-nles.md).

## Limitations and known issues

**Release and platforms**

- No published release contains these features. The Windows installer and `Splice.exe` are unsigned, so SmartScreen warns.
- Windows is verified only by the GitHub `windows-latest` CI job. macOS has no CI and the current features are untested there. Day-to-day development and the test suites run on Linux.
- Splice is single-user and local: the UI is a Chromium page talking to a server on `127.0.0.1`. The session cookie is also sent to other servers on `127.0.0.1` (browsers do not separate cookies by port); with random link ids and a token header, the cookie alone reads only media whose URL is already known.

**Verified only partly**

- **Vendor camera RAW** (BRAW, R3D, ARRIRAW, Canon, Nikon, Sony): tested only with mock decoders and header fixtures. No real camera file and no real vendor tool (`REDline`, `art-cmd`, `dcraw_emu`, `braw-decode`) has been run. No real ProRes RAW or lossless-JPEG DNG sample either. [Camera RAW](docs/camera-raw.md)
- **Resolve library write:** checked by Splice's reader and a row-by-row comparison with real Resolve projects; projects from the current writer had not yet been opened in Resolve when it merged.
- **Interchange in the receiving apps:** Premiere and Resolve imports were checked on earlier rounds of the interchange work; later position/scale changes, mixed-rate FCPXML in Resolve, and OTIO in Resolve have not been rechecked.
- **Hardware encoders** (NVENC, Quick Sync, VAAPI, VideoToolbox, AMF): detected and offered, but no successful hardware encode has been verified.
- **Audio** tests use generated media; they do not certify compliance with a broadcast delivery specification.

**Missing or limited**

- Colour: no secondaries or qualifiers, tracking, video noise reduction or ACES; HDR is previewed tone-mapped on an SDR display; scopes read the 8-bit preview.
- Audio: live monitoring is stereo only; stems are taken before master dynamics and may not null against a limited mix.
- Delivery: no DCP, IMF, Dolby Vision or embedded CEA-608/708 captions. MP4 AAC can overshoot the true-peak ceiling after encoding by default (reported; opt-in protection available).
- The built-in CinemaDNG developer is pure Python: about 0.6 s per 1080p frame at half debayer, 2.4 s at full.
- Lift and extract act on every unlocked track; four-point edits always fit to fill.
- Minimum window size is 1024 × 640.
- `%LOCALAPPDATA%\Splice\Exports`, the Windows default output folder, is hidden from most users.

Each page in [docs](#documentation) ends with its own known limits.

## License

**Splice's licence has not been decided yet.** The repository has no LICENSE file, so no licence to copy, modify or redistribute Splice's own code is granted at the moment, even though the repository is public. This section will change when the maintainers choose a licence.

Third-party components keep their own licences:

- The Windows installer bundles FFmpeg under the **GNU GPL v3**, Python under the PSF License and Electron under the MIT License. Splice runs FFmpeg and Python as separate programs and does not link to them. [`desktop/windows/Third-party.txt`](desktop/windows/Third-party.txt) gives the versions, checksums, licences and how to obtain FFmpeg's corresponding source (including `python desktop/windows/build.py OUT --source-archive`, and its known gap: the build's library sources are not published as archives).
- See also [Included materials](#included-materials).

## Included materials

The Premiere bridge vendors the MIT-licensed py-premiere library at commit ce75c9935cbf75f8b7492eb7c92ffed66d32677a. The bundled Adobe-authored test projects and isolated native effect templates come from the same MIT-licensed upstream corpus; they are attributed in `tests/test_adobe_corpus.py`. Splice adds standard timecode-format mappings. [py-premiere](https://github.com/Pouf/py-premiere) · [Adobe time display constants](https://github.com/Adobe-CEP/Samples/blob/master/PProPanel/jsx/PPRO/Premiere.jsx)

DM Sans and IBM Plex Mono are distributed under the SIL Open Font License; their licences accompany the fonts. Install the bundled DM Sans on a Premiere workstation for the sample title's original appearance. The sample footage animates four landscape photographs, and the score was synthesized for this project. [Ridge](https://images.unsplash.com/photo-1464822759023-fed622ff2c3b) · [Valley](https://images.unsplash.com/photo-1472396961693-142e6e269027) · [Forest](https://images.unsplash.com/photo-1441974231531-c6227db76b6e) · [Lake](https://images.unsplash.com/photo-1476514525535-07fb3b4ae5f1)

## Documentation

- [Editing](docs/editing.md): timeline, trims, ripple, linked sound, clip enable, timecode
- [Multicam, nested sequences, subclips and bins](docs/multicam-nesting-bins.md)
- [Colour finishing](docs/color.md): grading, LUTs, HDR, scopes, WebGL2 preview
- [Audio finishing](docs/audio.md): effects, automation, sync, LUFS, true peak, 5.1, stems
- [Delivery and the render queue](docs/delivery.md): formats, presets, cancel behaviour, limits
- [Camera RAW](docs/camera-raw.md): built-in formats, vendor decoders, what is verified
- [Effects](docs/effects.md)
- [Interchange and the Resolve round-trip](docs/interchange.md), including the loss reports
- [Windows: install and troubleshooting](docs/windows.md)
- [Can it replace Premiere, Resolve or Final Cut Pro?](docs/can-it-replace-commercial-nles.md)
- [Contributing: running from source and testing](docs/contributing.md)

## Contributing and tests

Run the Python suite in batches and the browser suites with Chrome or Chromium; use a dedicated `TMPDIR`. Instructions, environment variables and build commands: [docs/contributing.md](docs/contributing.md).
