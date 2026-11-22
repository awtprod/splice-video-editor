# Splice

Splice is a local video editor for Linux. It opens with **Alpine Passage**, a 32-second sequence with four moving landscape shots, an opening title, dissolves, and an ambient score.

## Launch

Use Python 3.10 or newer, Chromium or Google Chrome, and FFmpeg with ffprobe, H.264, AAC, and the drawtext filter. Extract the entire Linux archive and run this command inside its Splice directory.

```sh
./splice
```

The launcher opens a dedicated application window and stops its loopback server when the window closes. All fonts, sample media, and project-format code are included. To use a regular browser, run `python3 server.py` and open the local address it prints. The desktop and browser modes have separate browser storage.

## Edit

Use **File → New project** to choose a name, frame size, and frame rate. The media pool accepts files from **Import media**. Click a source to open its monitor, set In and Out, and insert, overwrite, or append to a target track. A bin path in the source monitor organizes the pool. A click on an offline item opens its individual relink picker.

Drag a clip body to move it, drag an edge to trim, Shift-drag a shared edge to roll, or Alt-drag the body to slip. The **Snap** and **Ripple** switches control trims. Use **S** to split at the playhead. Ctrl-click selects multiple clips. The ruler supports scrubbing and markers. The File menu includes the keyboard reference.

Select a clip to adjust its transform, color, transition, level, or pan. The Color workspace includes exposure, white balance, .cube lookup tables, and scopes. Titles support a typeface, position, size, fill, and outline. The Audio workspace includes clip fades, track level, mute, solo, pan, and metering.

**Save** writes an editable Premiere project. The native file picker can update a file you select. Browsers without filesystem access download a copy. **File → Save working project** retains Splice-specific state. Edits are also checkpointed to a local session. **File → Recent projects & recovery** opens earlier projects and versions. Recovering media across computers requires the original sources.

**Export video** renders a local H.264/AAC MP4 with selectable video quality and audio bitrate, progress, cancellation, and a rendered-video monitor. The renderer processes video in segments to bound memory. The sequence frame size and frame rate determine the output dimensions and timing.

## Premiere compatibility

Splice reads and writes the gzip-compressed Premiere project graph. Open a multi-sequence project and use the sequence selector to edit its sequences. The bridge retains the original graph when saving existing projects. Its Linux test suite compares timing with Adobe-authored reference data and verifies that unchanged native objects survive round trips in projects containing multicam, nested sequences, proxies, titles, wipes, audio automation, and retiming.

A preserved native object can have no live editing or rendering equivalent in Splice. Support depends on the feature and its parameters. When a Premiere save reports an unsupported setting, the working project format retains the edit. Direct verification of Splice-authored output inside Adobe Premiere desktop is still pending.

## Included materials

The Premiere bridge vendors the MIT-licensed py-premiere library at commit ce75c9935cbf75f8b7492eb7c92ffed66d32677a. The bundled Adobe-authored test projects and isolated native effect templates come from the same MIT-licensed upstream corpus. Splice adds standard timecode-format mappings. [py-premiere](https://github.com/Pouf/py-premiere) · [Adobe time display constants](https://github.com/Adobe-CEP/Samples/blob/master/PProPanel/jsx/PPRO/Premiere.jsx)

DM Sans and IBM Plex Mono are distributed under the SIL Open Font License. Licenses accompany the fonts. The sample footage animates four landscape photographs, and the included score was synthesized for this project. [Ridge](https://images.unsplash.com/photo-1464822759023-fed622ff2c3b) · [Valley](https://images.unsplash.com/photo-1472396961693-142e6e269027) · [Forest](https://images.unsplash.com/photo-1441974231531-c6227db76b6e) · [Lake](https://images.unsplash.com/photo-1476514525535-07fb3b4ae5f1)

## Verify locally

From the extracted application directory, run the Python suite with `python3 -m unittest discover -s tests`. For the two-pass browser and render verification, install Node 20 or later and run `npm ci && npm test`. Set `SPLICE_CHROME` if Chromium is installed outside the standard paths. The test uses an isolated local media store, imports real footage, trims a clip, styles a title, reloads the session, saves and reopens a native project, and checks the exported video and audio streams.
