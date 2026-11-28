# Editing

This page covers the timeline: placing media, trimming, ripple, linked picture and sound, marks, clip enable, track state and timecode. The in-app **File → Keyboard and mouse** list is generated from the same keymap the editor uses, so it is always the authoritative shortcut reference. The tables below are a copy of it.

## Projects and sequences

- **File → New project…** asks for a name, frame size and frame rate. New sequences start at 01:00:00:00. Change the start timecode in **File → Sequence settings…**.
- **↥ Open** (Ctrl/Cmd+O) opens a Premiere project (`.prproj`) or a Splice working project (`.splice.json`). A Premiere project with several sequences shows a sequence selector.
- **▣ Save** (Ctrl/Cmd+S) writes an editable Premiere project. **File → Save working project…** (Ctrl/Cmd+Shift+S) writes a `.splice.json` file that keeps everything Splice-specific: grades, multicam angles, nested edits, subclip limits, effects and editorial metadata. Save a working project if you want all of your work back.
- Edits are checkpointed to local browser storage as you work. **File → Recent projects & recovery** opens earlier projects and versions. Splice keeps the 30 most recently edited projects, plus any edited in the last 30 days, up to 200 in total, plus the open project. Older drafts and their versions are removed automatically. Removing a draft never deletes media.
- The minimum supported window is 1024 × 640. Smaller windows scroll.

## Media

- **Import media** copies files into Splice's data folder. Use it when the browser runs on a different machine from the Splice server.
- **File → Link media in place…** browses the computer the server runs on and links files, or a whole card or folder, without copying them. Keep the drive connected. If a linked file is moved or changed, the clip goes offline; relink it from the same dialog (**Relink offline from folder** matches by folder and file name). Linking in place works in the desktop app and with `python3 server.py` on the same machine.
- Splice probes media with FFmpeg and builds local proxies for formats the browser cannot play directly, such as ProRes MOV. Choose **Build seek proxy** in the source monitor for faster scrubbing and reverse playback of long-GOP footage. Export always reads the original media, never a proxy.
- Any format the installed FFmpeg can decode is accepted. Text files are refused. Camera RAW has its own page: [Camera RAW](camera-raw.md).
- **File → Media cache…** shows the disk used by imported copies and proxies:
  - *Delete unused proxies* frees proxies that no project in this browser uses. They are rebuilt on demand.
  - Imported copies may be your only copy of a file, so they are never deleted in bulk. Select the ones you no longer need and confirm. The check covers this browser's projects only. Other Splice windows, the desktop app and project files saved to disk are not checked.
  - *Forget unused links…* forgets linked originals that no project here uses. The files stay where they are.
  - Folders made by older Splice versions may show as imported copies until Splice has checked them (at most 2 GiB per opening). The bulk delete never removes them in the meantime.
- The media pool **List** view sorts by column. Search also matches bin, path, reel, timecode and camera. Bins, subclips and metadata are covered in [Multicam, nesting, subclips and bins](multicam-nesting-bins.md).

## Source monitor and three-point editing

The source monitor opens as a panel to the left of the program monitor. On narrow windows the Inspector makes room, and the timeline gets a little shorter while the panel is open. The panel stays open so you can mark and place repeatedly.

- **I / O** mark In and Out on the frame under the playhead. **Both frames are included**: In on frame 50 and Out on frame 80 is 31 frames, and the footer shows the Out frame. This matches Premiere, Avid and Resolve. Source marks work the same way.
- **,** inserts and **.** overwrites from the source monitor, using the three- and four-point rules. A four-point edit whose source and timeline lengths differ always fits to fill; there is no option to trim the source instead.
- After an insert or overwrite the playhead moves to the end of the new clip, so repeated mark-and-insert builds a scene in order. An edit placed at the timeline In and/or Out clears both marks, as in Premiere. Undo brings them back.
- Drag the source picture onto a timeline track to overwrite the marked range there. Hold Ctrl/Cmd to insert instead.
- **Targeting:** picture goes to the track you drop on or choose; sound goes to the audio track with the same number (V2 → A2), unless the **Sound** picker says otherwise ("With picture", A1…An, or "In picture clip"). Dropping on an audio track places sound only.
- **Locked tracks refuse the edit.** The edit never skips a locked track and lands partially; the locked target is named.
- **;** lifts and **'** extracts the In/Out range on every unlocked track. A lift also removes any transition it cuts through. There is no per-track targeting for lift and extract beyond the source monitor's picture and sound targets.

## Timeline editing

| Keys | Action |
|---|---|
| Space | Play / pause |
| J · K · L | Play reverse · stop · forward (repeat to speed up) |
| ← · → | Step one frame |
| Shift + ← · → | Step five frames |
| ↑ · ↓ | Go to previous · next edit point |
| I · O | Mark timeline In · Out on the frame under the playhead (both frames are included) |
| Shift + I · O | Go to the timeline In frame · Out frame |
| X | Mark In and Out around the selected clip |
| Ctrl/Cmd + Shift + X | Clear timeline In and Out |
| ; · ' | Lift · extract the In/Out range on unlocked tracks |
| , · . | Insert · overwrite from the source monitor (three-point edit) |
| S · Ctrl/Cmd + K | Add edit to the selected clip at the playhead |
| Ctrl/Cmd + Shift + K | Add edit on all unlocked tracks |
| Q · W | Ripple trim clip start · end to the playhead |
| Ctrl/Cmd + ← · → | Trim the selected edit one frame (click an edge to select it) |
| Ctrl/Cmd + Shift + ← · → | Trim the selected edit five frames |
| Alt + ← · → | Nudge selected clips one frame |
| Alt + Shift + ← · → | Nudge selected clips five frames |
| Ctrl/Cmd + Alt + ← · → | Slip the selected clip one frame |
| Alt + , · . | Slide the selected clip one frame |
| A · Shift + A | Select forward · backward on the selected clip's track |
| Delete · Backspace | Delete the selection (ripple when Ripple is on) |
| Shift + Delete · Backspace | Ripple delete the selection |
| Shift + E | Enable · disable the selected clips |
| Ctrl/Cmd + L | Link · unlink clips (separates a camera clip's sound) |
| Ctrl/Cmd + Shift + L | Toggle linked selection |
| = · − · \\ | Zoom in · out · fit the whole timeline |
| M · [ · ] | Add marker · previous · next marker |
| N · R | Toggle snapping · ripple mode |
| Ctrl/Cmd + C · V | Copy · paste clips at the playhead |
| Ctrl/Cmd + Z · Shift + Z | Undo · redo |

| Mouse | Action |
|---|---|
| Drag clip edge | Trim (both sides of a linked clip) |
| Alt + drag clip edge | Trim only this side of a linked clip: J-cut or L-cut |
| Shift + drag clip edge | Roll the cut between adjacent clips |
| Alt + Shift + drag clip edge | Roll only this side's cut: J-cut or L-cut against the picture |
| Alt + drag clip | Slip the source under the edit |
| Alt + Shift + drag clip | Slide the clip between its neighbours |
| Ctrl/Cmd + click | Select multiple clips |
| Ctrl/Cmd or Alt + scroll | Zoom the timeline around the pointer |
| Click the program timecode | Type a timecode to move the playhead (+/− for relative) |
| Click the sync number | Move or slip an out-of-sync clip back into sync |

Notes:

- Delete and ripple delete do nothing while a text field has focus. Ripple delete works on scattered selections too.
- The **Snap** and **Ripple** switches above the timeline control trims and deletes. Track headers control locking and ripple synchronization.

## Linked picture and sound, J- and L-cuts

- A linked camera clip (picture plus sound) moves and trims as one. **Alt+drag** an edge to trim one side, or **Alt+Shift+drag** an edge to roll one side's cut, for J- and L-cuts.
- **Ctrl/Cmd+L** links or unlinks the selection. On a camera clip whose sound is still embedded in the picture clip, it moves the sound onto its own audio clip.
- The **Linked** toggle (Ctrl/Cmd+Shift+L) turns linked selection off.
- A red number on a linked clip is its offset in frames from its partner. Click it to move or slip the clip back into sync; the action is all or nothing. The badge only appears when picture and sound come from the same source. Its menu closes with Escape or when you scroll.
- After picture-only overwrites, a shot can be in several linked pieces. Trims move only the pieces at the same edge; deleting or disabling a piece affects only that piece; ripple delete keeps the sound in sync.

## Clip enable / disable

Park alternates the way you would in any NLE:

- **Shift+E**, **Edit → Enable / disable clip**, or right-click a clip.
- With several clips selected, the command treats them as one: if the lead clip (the one you right-clicked, otherwise the first selected) is enabled, all become disabled; otherwise all become enabled. Linked partners follow when linked selection is on. One undo step.
- A disabled clip shows **OFF** (when its name fits) and plays and exports nothing: no picture, no sound, including sound embedded in a picture clip.
- A transition next to a disabled clip still plays for the enabled clip, against whatever lies beneath (black on V1). A dip still dips to black. In an audio crossfade, the enabled side fades against silence. The same rule applies in the monitor and in exports.
- Disabling a clip on a locked track is refused, like every other edit.

## Track state

- Eye buttons hide a track's pictures and titles in the monitor and in exports. **Camera sound in those clips keeps playing**, as in Premiere. A title lane and the video track with the same number share one state. Hidden tracks are saved to and read from Premiere's track mute.
- Lock, mute and solo are on the track headers.

## Speed

- Select a clip to change its speed or direction in the Inspector. Speed runs from 10% to 1600%. Changing speed changes the clip's timeline duration so that it keeps its source range, to the nearest timeline frame.
- **Reset (↺)** resets every clip adjustment, speed included, or nothing if the speed cannot return to 100%.
- Variable speed (speed ramps) is in [Effects](effects.md).

## Timecode

- Timecode is drop-frame at 29.97 and 59.94 fps.
- Premiere, Resolve, FCPXML, FCP 7 XML, EDL and OTIO timelines open at their own start timecode and drop-frame setting. A Resolve project that does not say opens as non-drop, Resolve's default. Importers do not support a start before 00:00:00:00 (a negative start becomes 0).
- **Click the program timecode** to type a position. A `.` stands for `00`, and a short entry replaces the playhead's timecode from the right: `1000` → hh:mm:10:00. `+` and `−` move relative to the playhead.

## Titles, captions and markers

- Titles have a typeface, position, size, fill, outline and opacity keyframes.
- The **Captions** panel imports, edits and exports SRT. Exports can carry a subtitle stream or SRT/WebVTT sidecars; see [Delivery](delivery.md).
- **M** adds a marker; **[** and **]** move between markers. The ruler supports scrubbing and markers.

## Status messages

Status messages appear in the bar under the header. Click one to dismiss it.

## Known limits

- Four-point edits always fit to fill.
- Lift and extract act on every unlocked track.
- The source panel's settings area (scrub, shortcuts, seek proxy, bin path) still scrolls in short windows.
- Empty lanes between tracks in use are made short, not hidden.
- Playback at "fit" zoom on a 5,000-clip timeline can show a few long frames (up to about 56 ms), depending on machine load. The cause has not been found.
