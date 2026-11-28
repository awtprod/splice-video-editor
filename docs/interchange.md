# Interchange and the Resolve round-trip

Splice exchanges cuts with other applications in five ways:

| Route | Read | Write |
|---|---|---|
| Premiere Pro project (`.prproj`) | Yes (**↥ Open**) | Yes (**▣ Save**, **File → Save Premiere as…**) |
| FCPXML | 1.9–1.11 | 1.10 |
| Final Cut Pro 7 XML (XMEML) | Yes | Yes |
| CMX3600 EDL | Yes | Yes, one per picture track, plus optional sound EDLs |
| OpenTimelineIO (`.otio`) | Yes | Yes |
| DaVinci Resolve | `.drp` files and disk-library projects | New projects in a disk library; no `.drp` export |

Whatever the route, **nothing is dropped silently**. Every route produces a report that lists what it could not carry, clip by clip: **File → Project compatibility…** after an import, the export report and turnover package after an export, and **File → Resolve compatibility details…** for Resolve.

## Premiere Pro projects

- Splice reads and writes Premiere's gzip-compressed project graph. Open a multi-sequence project and pick a sequence in the sequence selector.
- When it saves an existing project, Splice keeps the original graph and changes only what you edited. Native objects that Splice cannot edit or render survive the round trip; **File → Project compatibility…** lists the effects and sources it flags.
- The Linux test suite compares timing with Adobe-authored reference data and checks that unchanged native objects survive round trips in projects with multicam, nested sequences, proxies, titles, wipes, audio automation and retiming. The bundled sample project was opened, edited and saved in Premiere Pro 27.0 on macOS, and Splice reopened Adobe's saved copy.
- For newly placed media with several audio streams, choose a stream or the default stereo routing in the source monitor. The Premiere save keeps the camera picture and creates linked 24-bit PCM audio; move that generated audio with the originals.
- Multicam clips must be flattened before a Premiere save (Splice refuses otherwise). When a Premiere save reports a setting it cannot write, save a Splice working project to keep the edit.

## Exporting a timeline

**File → Export timeline…** writes:

- **FCPXML 1.10** for DaVinci Resolve and Final Cut Pro.
- **Final Cut Pro 7 XML** for Premiere Pro, Resolve and Avid. **For Premiere, use this one**: Premiere does not read FCPXML 1.x.
- **CMX3600 EDLs**, one per picture track, with optional sound EDLs. Picture EDLs carry picture only.
- **OpenTimelineIO.**
- A **turnover package** that zips them together with a report of everything each format could not carry. It can include a rendered reference movie with timecode and clip name burned in.

What each format carries:

| | FCPXML 1.10 | FCP 7 XML | CMX3600 EDL | OTIO |
|---|---|---|---|---|
| Tracks, gaps, record/source ranges | ✅ | ✅ | ✅ one EDL per track | ✅ |
| Source start timecode, reel | ✅ | ✅ | ✅ | ✅ |
| Drop-frame | ✅ | ✅ | ✅ | ✅ |
| Mixed frame rates | ✅ | ✅ | ⚠️ counted at the list's rate; in points rounded (reported) | ✅ |
| Dissolves and audio crossfades, with alignment | ✅ | ✅ | ✅ dissolve start; the cut point moves to it (reported) | ✅ |
| Dip to black, wipe | ⚠️ cross dissolve (reported) | ⚠️ cross dissolve (reported) | wipe; dip as dissolve (reported) | ⚠️ dissolve + kind in metadata |
| Linked picture and sound, J/L cuts | ✅ | ✅ | ✅ separate sound EDLs | ⚠️ separate tracks, link in metadata |
| Constant speed, reverse | ✅ | ✅ | ✅ `M2` | ✅ |
| Disabled clips | ✅ | ✅ | ❌ omitted (reported) | ✅ |
| Clip gain, fades, volume keys | ✅ | ✅ | ❌ reported | ⚠️ metadata only |
| Transform, opacity and their keyframes | ✅ | ⚠️ position approximate | ❌ reported | ⚠️ metadata only |
| Titles | ⚠️ basic title | ⚠️ text generator | ❌ reported | ⚠️ placeholder named after its text |
| Markers | ⚠️ clip markers | ✅ | ✅ locators | ✅ |
| Grades, LUTs, pan, track mixer, captions, nested sequences, effects | ❌ reported per clip | ❌ | ❌ | ❌ |

Before exporting:

- **Unnest / Decompose in place** nested sequences and **Flatten multicam** clips. The report lists what is left out otherwise, including subclip limits and editorial metadata (camera, scene, take, log note, label, bin). See [Multicam, nesting, subclips and bins](multicam-nesting-bins.md).
- Effects (speed ramps, masks, keys, blend modes, stabilization) are not carried; a speed ramp is written as its average speed. See [Effects](effects.md).
- Camera RAW clips keep the original RAW path, reel and timecode; Splice's RAW settings are listed, not carried. See [Camera RAW](camera-raw.md).

Details that matter in practice:

- **Timecode:** each source starts at its embedded timecode. If a file cannot be read, the export stops and lists it, unless you choose to write 00:00:00:00 and flag the affected clips. Probing is limited to 60 s per export, and the report lists any sources left unprobed. The export starts from the sequence's own start timecode and its In/Out range, Out frame included.
- **EDL timecode** counts at the EDL's frame rate. Sources at other rates are converted and their in points rounded to that rate's frames (reported).
- **EDL dissolves:** an EDL cannot say where inside a dissolve the edit point sat, so a dissolve read back from an EDL starts at the cut. The picture is the same; the cut point may move.
- **Premiere positions:** FCP 7 XML writes scale and position the way Premiere reads them. Scale 100 is the media's own size, so media that Splice fits to the frame is written enlarged (960×540 media in a 1080p sequence is Scale 200). Pixel aspect ratios that FCP 7 XML cannot name are written as square and listed.
- **Path mapping:** one `from folder => to folder` rule per line, both absolute paths. When several rules match, the longest "from" folder wins. The report flags mapped names that Windows cannot hold. The receiving application asks you to locate media whose mapped path does not exist there.
- **Premiere** reads the FCP 7 XML. The `.otio` also imports into Premiere, but clip gain, opacity and scale are not applied from it. Premiere shows its own dialogs for every EDL.
- **Resolve** reads the FCPXML or FCP 7 XML. Resolve conforms EDLs by source timecode only, so sources without embedded timecode, or with overlapping timecode, can link to the wrong file (the report names them). Resolve refuses sound-only EDLs and drops empty tracks.

## Importing a timeline

**File → Import timeline…** opens FCPXML 1.9–1.11, FCP 7 XML, a CMX3600 EDL or OTIO as a new project.

- Media that is not in Splice arrives offline, ready to relink. Anything not imported is listed under **Project compatibility**, including every clamped transform with its original value; repeated warnings are counted.
- For an EDL, choose its frame rate. The report names the start timecode it assumed.
- Speed ramps arrive at their average speed (with a warning). Compound clips are flattened. Multicam clips, captions, crops, filters and panners are reported by name, not imported.
- A long import can be cancelled.
- **Check the linked-media list before sharing a project.** The import lists every file it linked from this computer, because the timeline file chose those paths.

## Opening DaVinci Resolve projects

- **File → Open Resolve project (.drp)…**, or **File → Open from Resolve library…** and choose the library folder (the one that holds "Resolve Projects"). Network (PostgreSQL) libraries and Blackmagic Cloud are not supported.
- Splice only reads, from a private copy, and never changes the library. You can open a library on a read-only drive or while Resolve has it open. If Resolve is saving at that moment, Splice asks you to try again.
- For a project with several timelines, pick one in the sequence selector. Each timeline keeps your edits while you switch. If Resolve saved the project in the meantime, the timeline you switch to comes from the newer save.
- Media on this computer is linked in place, with nothing copied. When a project opens, Splice looks for its media for at most 45 s and 2,000 files, and only for media some timeline uses. Media from another computer (for example `E:\…` paths) or a missing file opens offline; use **File → Link media in place → Relink offline from folder**, which matches by folder and file name.
- Splice shows times from zero; the Resolve start timecode is kept with the project. Drop-frame is inferred from a drop-frame start hour; otherwise the timeline opens as non-drop.

What comes in:

| Imported exactly | Imported approximately (listed per clip) | Listed, not imported |
|---|---|---|
| Frame rate, resolution, start timecode | Position Y | Colour page grades, Fusion, Fairlight effects and mixer |
| Tracks with names; clips with track, start, duration, source in, name, media | Zoom (even zoom only) | Generators (as placeholders); compound, multicam and nested timelines (as placeholders; also openable as timelines) |
| Linked picture and sound | Opacity | Clip markers, media pool markers, subtitle tracks |
| Disabled clips, disabled/hidden video tracks, muted audio tracks, locked tracks | Speed and reverse (a speed curve comes in at its average speed) | Rotation, anchor, pitch, yaw, flips, crop; pan and other clip audio settings |
| Clip volume (clamped above +6.02 dB) | Timeline markers (confirmed for Resolve 19 only) | Transitions other than cross dissolves, fade-ins and audio crossfades; ResolveFX/OpenFX; composite modes |
| Position X | Drop-frame | Media pool files that no timeline uses |
| Cross dissolves, video fade-in, audio crossfades (0 dB / +3 dB) | | Anything that failed to decode, with the reason |

**File → Resolve compatibility details…** shows what an import left out, clip by clip.

## Saving to a Resolve library

**File → Save to Resolve library…** writes your cut as **a new project** in a Resolve disk library, for the colorist to open from Resolve's Project Manager. There is no `.drp` export; use FCPXML to reach other editors.

Safety rules (each has a test):

- **An existing project is never modified or replaced.** The name must be new (compared the way Windows compares names) and valid on Windows.
- The library must be one Resolve created. The project is built in a staging folder inside the library and moved in with one rename that never replaces anything.
- If anything fails, nothing is left in the library. A save that is killed half-way is cleaned up by the next save or the next server start.
- Before the move, Splice reads the project back with its own reader and compares every written timeline with what it meant to write. Any difference aborts the save and shows it.

What is written:

| Written | Written only if you allow it (reported) | Reported, not written |
|---|---|---|
| Trims, moves, reorders, deletions; inserted clips from pool media; picture–sound links | New media of kinds Resolve has not been verified to take (**Allow media kinds Resolve has not been verified to take**) | Titles and other Splice-only clips; copies of Resolve generators, titles, compound and multicam clips |
| New media of verified kinds: H.264 MP4 with PCM, 24-bit WAV, ProRes 422 HQ, RGBA PNG, MP3 44.1 kHz | Track lock / hide / mute, Position X, and a project converted from a `.drp` (**Allow changes whose bytes are not proven**) | Speed changes, ramps, reverse, keyframes, Splice grades and LUTs, zoom, Y, rotation, opacity, clip fades, clip pan, effects |
| Disabled picture clips; clip volume in dB | | Picture transitions and fades; sound crossfades outside the tested range |
| Sound crossfades: Cross Fade 0 dB / +3 dB, centred, 2–16 frames, at 29.97 fps | | New tracks; markers; captions; resolution and frame-rate changes; In/Out marks; track gain, pan and solo; renames of clips and media |
| Timeline and track renames; start timecode | | Deleting a clip that carries Resolve work (grade, Fusion, effects, markers): the clip is kept and reported |
| Media paths through the path mapping | | Any field the writer does not know |

The report groups each kind into one line with a count and up to five places; **Show all** lists every place.

### How far the Resolve round trip is verified

- The reader was checked against a collection of real Resolve projects: an untouched re-save through the writer reproduced 84 timelines and 51,521 database rows exactly (apart from the intended new project id and name).
- Projects made by an earlier version of the writer (copies, trims, re-cuts with inserted and reordered linked clips, new media-pool rows, sound crossfades) were opened in Resolve and confirmed. The current writer writes the same rows.
- **Projects written by the current writer itself had not yet been opened in Resolve** when it merged (PR #12). A set of test projects was prepared for that check; until it is reported, treat Resolve write as verified by Splice's own reader and the row comparison, not by Resolve.

## Known limits

- Premiere: reversed linked pairs arrive unlinked (a Premiere limit, reported per clip). Sub-frame in points are read at the whole frame by Premiere (reported). Premiere graphics import into Splice as plain titles (text only).
- FCP 7 XML positions for anamorphic media, and rotation sign, are untested in Premiere. The latest position/scale evidence had not been re-imported into Premiere when the interchange PR merged.
- FCPXML from mixed-rate timelines has not been tried in Resolve. Resolve's OTIO import failed on an early file ("Operation canceled", then a crash); the `.otio` was changed afterwards and has not been rechecked in Resolve.
- No format carries titles exactly; each receiving application makes its own generator or placeholder.
- Resolve: network libraries, Blackmagic Cloud and `.drp` export are not supported. Clip markers are not imported (their unit is not settled).
- A path check on a hung network mount is abandoned when its time runs out, not interrupted.
