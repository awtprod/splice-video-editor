# Splice

Splice is a local video editor for Linux. It opens with **Alpine Passage**, a 32-second sequence with four moving landscape shots, an opening title, dissolves, and an ambient score.

## Launch

Use Python 3.10 or newer, Chromium or Google Chrome, and FFmpeg with ffprobe, H.264, AAC, and the drawtext filter. HDR-to-SDR conversion uses FFmpeg's zscale and tonemap filters. Extract the entire Linux archive and run this command inside its Splice directory.

```sh
./splice
```

The launcher opens a dedicated application window and stops its loopback server when the window closes. All fonts, sample media, and project-format code are included. To use a regular browser, run `python3 server.py` and open the local address it prints. The desktop and browser modes have separate browser storage.

## Edit

Use **File → New project** to choose a name, frame size, and frame rate. The media pool accepts files from **Import media**. Splice probes camera originals with FFmpeg and builds local proxies for formats such as ProRes MOV. Choose **Build seek proxy** in the source monitor for faster scrubbing and reverse playback of long-GOP footage. Premiere save and final export use the original. In the source monitor, set In and Out and insert, overwrite, or append to a target track. A bin path organizes the pool. Click an offline item to relink it individually. **Relink from folder** matches moved sources by directory suffix and asks for confirmation when the path matches only a filename or a single folder and filename.

Drag a clip body to move it, drag an edge to trim, Shift-drag a shared edge to roll, or Alt-drag the body to slip. The **Snap** and **Ripple** switches control trims and ripple deletes. Track headers control locking and ripple synchronization. Linked camera audio follows the picture through supported timeline edits. Use **S** to split at the playhead. Ctrl-click selects multiple clips. The ruler supports scrubbing and markers. The File menu includes the keyboard reference.

Select a clip to adjust its speed, playback direction, transform, color, transition, level, or pan. Changing speed adjusts timeline duration to retain the source range to the nearest available timeline frame. The Color workspace includes exposure, white balance, .cube lookup tables, and scopes. Titles support a typeface, position, size, fill, outline, and opacity keyframes. The Audio workspace includes clip fades and crossfades, track level, mute, solo, pan, and metering.

**Save** writes an editable Premiere project. The native file picker can update a file you select. Browsers without filesystem access download a copy. For newly placed media with discrete audio streams, choose a stream or default stereo routing in the Source Monitor. Premiere save retains the camera picture and creates linked 24-bit PCM audio. Include that generated audio with the originals when moving the project. **File → Save working project** retains Splice-specific state. Edits are also checkpointed to a local session. **File → Recent projects & recovery** opens earlier projects and versions.

**Export video** offers H.264/AAC MP4, ProRes 422 HQ with 24-bit PCM in MOV, and a separate 48 kHz 24-bit WAV mix. Choose the entire sequence or a custom range. MP4 delivery includes selectable video quality and audio bitrate. Splice previews newly imported HLG and PQ on an SDR monitor and tone-maps those sources to Rec. 709 for MP4 delivery. ProRes can retain one HDR color space across the sequence. Export supports progress, cancellation, and in-app MP4 and WAV review. The Captions panel imports, edits, and exports SRT. MP4/MOV can carry a selectable subtitle stream. The renderer processes video in segments to bound memory. The sequence frame size and frame rate determine output dimensions and timing.

## Premiere compatibility

Splice reads and writes the gzip-compressed Premiere project graph. Open a multi-sequence project and use the sequence selector to edit its sequences. The bridge retains the original graph when saving existing projects. Its Linux test suite compares timing with Adobe-authored reference data and verifies that unchanged native objects survive round trips in projects containing multicam, nested sequences, proxies, titles, wipes, audio automation, and retiming.

A preserved native object can have no live editing or rendering equivalent in Splice. Support depends on the feature and its parameters. When a Premiere save reports an unsupported setting, the working project format retains the edit. Open **File → Project compatibility** to inspect flagged effects and sources. The Adobe-authored regression corpus and reference model files are attributed in tests/test_adobe_corpus.py. The bundled Alpine Passage project was opened, converted, edited, and saved in Adobe Premiere Pro 27.0.0 on macOS. Splice reopened Adobe’s saved copy, relinked the bundled clips, and retained the Adobe-added marker. Install the bundled DM Sans font on the Premiere workstation for the original title appearance.

## Included materials

The Premiere bridge vendors the MIT-licensed py-premiere library at commit ce75c9935cbf75f8b7492eb7c92ffed66d32677a. The bundled Adobe-authored test projects and isolated native effect templates come from the same MIT-licensed upstream corpus. Splice adds standard timecode-format mappings. [py-premiere](https://github.com/Pouf/py-premiere) · [Adobe time display constants](https://github.com/Adobe-CEP/Samples/blob/master/PProPanel/jsx/PPRO/Premiere.jsx)

DM Sans and IBM Plex Mono are distributed under the SIL Open Font License. Licenses accompany the fonts. The sample footage animates four landscape photographs, and the included score was synthesized for this project. [Ridge](https://images.unsplash.com/photo-1464822759023-fed622ff2c3b) · [Valley](https://images.unsplash.com/photo-1472396961693-142e6e269027) · [Forest](https://images.unsplash.com/photo-1441974231531-c6227db76b6e) · [Lake](https://images.unsplash.com/photo-1476514525535-07fb3b4ae5f1)

## Verify locally

From the extracted application directory, run the Python suite with `python3 -m unittest discover -s tests`. For the two-pass browser and render verification, install Node 20 or later and run `npm ci && npm test`. Set `SPLICE_CHROME` if Chromium is installed outside the standard paths. The test uses an isolated local media store, imports real footage, trims a clip, styles a title, reloads the session, saves and reopens a native project, and checks the exported video and audio streams. It also tests linked edits, locks, folder and camera-original relinking, short-source rejection, and recovery after both a Chromium renderer crash and a full browser and server restart during a save.
