# Effects

> **Status: not merged yet.** Everything on this page comes from [PR #18](https://github.com/awtprod/splice-video-editor/pull/18), which was open and under review when this page was written (head `f58c84f`). None of it is on the `dev` branch until that PR merges, and details may change in review. If PR #18 changes, update this page to match.

Effects live in **Inspector → Effects**. All of them are undoable, saved with the working project, and rendered the same way in the monitor and in exports (ProRes 422 HQ, ProRes 4444 with alpha, and H.264). The monitor and the exporter use twin implementations of the same maths, and the test suite compares them.

## Speed ramps

- Presets: **Smooth Slow-Mo**, **Fast-to-Slow**, **Speed Up** and **Freeze Middle**, or your own speed keys with easing. Speed runs from 0% (a freeze) to 1600%.
- Timeline time follows the curve everywhere: monitor playback, trims, splits, rolls and slides, long-export windows and the export itself. The test suite checks the exported frames against the expected source frames exactly.
- Tick marks on the timeline clip show the speed keys.
- **Ramp sound** (applies to a linked picture-and-sound pair together):
  - **Keep pitch** stretches the sound in short grains. It plays to the clip's end and keeps clicks and hits on the curve.
  - **Varispeed** resamples along the curve; pitch follows speed.
  - **Mute.**
  - A freeze is silent in every mode.

## Keyframe easing

Any animated property (opacity, transform, volume, grade keyframes, crop, masks and speed) can use linear, hold, ease in, ease out, ease in-and-out, or a custom bezier curve. The Inspector has an easing selector and a draggable curve editor with numeric handles. In exports, eased segments become FFmpeg expressions.

## Crop and masks

- Keyframeable left/right/top/bottom crop with feather.
- Up to **8 masks** per clip: rectangle, ellipse or polygon (up to 16 points). Each has position, size, rotation, feather, expansion, opacity and invert; masks combine as a union.
- Drag the handles on the monitor: the centre moves, corners size, the stem rotates, polygon points move. The handles are drawn over the picture, never into it.
- **On an adjustment clip, masks limit its grade: a power window.**

## Chroma key

- Pick the key colour from a swatch or with the monitor eyedropper, which samples the clip on its own.
- Similarity, smoothness, spill suppression, choke, matte feather and invert.
- **View alpha matte** is a monitor-only toggle.
- Keying runs on the source's Y′CbCr before the grade, and the matte is carried through the grade. ProRes 4444 exports keep real alpha.

## Blend modes

Normal, multiply, screen, overlay, add, darken, lighten, soft light, hard light and difference. They respect opacity, masks, keys, grades and adjustment clips beneath them. Adjustment clips themselves can only use normal.

## Stabilization

- Two-pass FFmpeg `vidstabdetect` / `vidstabtransform`. **Analyze** runs in the background and stops if you reset stabilization or delete the clip. Controls: smoothing, max shift, zoom, auto zoom and tripod.
- The monitor shows the stabilized picture through an H.264 proxy made from the same analysis and transform. Export uses a lossless intermediate from the same analysis.
- The clip keeps its own sound from the original media, so its crossfades and dips are unchanged.
- **Stabilization needs an FFmpeg built with libvidstab** (`--enable-libvidstab`). The bundled Windows FFmpeg has it. Without it, the Inspector says so and names the build flag. `python3 runtime_check.py` lists it among the optional features.

## Refused, with a message

These are refused at load, during an edit and on paste, with the same message the export would give:

- A speed ramp on a multicam clip ("Speed ramps on multicam clips are not supported. Flatten the multicam clip first.").
- A speed ramp on a reversed clip ("Speed ramps play forward. Turn off Reverse to ramp this clip.").
- Stabilizing a nested or multicam clip (stabilize the clips inside it instead), a generated clip, or a camera RAW clip ("Splice cannot stabilize a camera RAW clip yet. Stabilize an exported copy of the clip instead.").
- Keyframes for more than 8 masks, and more than 100 keyframes for one property.

## Nests, multicam, unnest

- Effects on a nested clip apply to its composite; effects inside the nest render inside it. An effect on a multicam clip applies to the camera it shows.
- **Unnest** copies a nest's crop, masks, key and blend mode onto its clips when the nest holds plain pictures. It refuses ("Cannot unnest losslessly") when the nest has a speed ramp, stabilization, or children with effects of their own.

## Interchange

FCPXML, FCP 7 XML, EDL and OTIO do not carry these effects. The export report lists each one per clip. A speed ramp is written as its average speed, and eased keyframes as linear between the keys. Saving to a Resolve library reports effects as not written.

## Known issues

From PR #18's own list:

- **Slow motion right after a transient.** Until the curve passes the transient's own grain (about 8 ms of source, so 80 ms of output at 0.1×), the stretched sound beside it is a little rough in low tones.
- **A split whose pieces have different sound settings** (volume, effect, mode, a fade, automation, or a transition at the cut) keeps a hard cut, as any cut between two different sounds does. Pieces with the same settings play as one sound.
- **MPEG-TS whose sound starts after its picture.** For clip In points under about 1.25 s, that sound can play early by the gap between the streams.
- **Stabilization analyses made before this PR's fixes** report "outdated: analyze the clip again".
- **16-bit RGBA stills in ProRes 422 HQ** keep 87 levels of a 4096-step ramp. This is the same on `dev` and is not caused by effects.
- **The Easing select shows "Ease in and"** (truncated) at 1440 × 900 and 1180 × 780; the full label is in the menu.
- No blend modes on adjustment clips. No motion tracking: masks are positioned by hand and keyframed.
