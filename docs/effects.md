# Effects

Effects live in **Inspector → Effects**. All of them are undoable, saved with the working project, and rendered the same way in the monitor and in exports (ProRes 422 HQ, ProRes 4444 with alpha, H.264 and 16-bit image sequences). The monitor and the exporter use twin implementations of the same maths, and the test suite compares them.

## Colour

- An effect never changes a clip's colour. A graded clip with a crop, mask or key shows the same colour as the same clip graded without them. This holds for untagged, Rec. 601, Rec. 709, 16-bit and HDR (PQ, HLG) sources, in 8-bit, 10-bit and 16-bit deliveries.
- An untagged source is read as Rec. 601, as in the monitor. That includes blend modes and image-sequence (TIFF, PNG, DPX, EXR) exports.
- A clip with alpha (ProRes 4444, PNG) keeps its alpha at 16 bits when it is graded, with or without effects.

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
- Every stabilized frame is the frame the plain clip shows at that time. That includes variable-frame-rate phone video, and MPEG-TS and AVCHD (`.ts`, `.mts`, `.m2ts`) files with long GOPs (x264's default is 250 frames).
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

- **Keep pitch at 0.25× and slower, right after a transient** (a click or hit). Low sustained tones beside it are heavily distorted for a short time: the distortion is nearly as loud as the tone (about −4 dB below it on a 110 Hz tone). The tone's level is unaffected. Measured with a tone plus a 1 ms click, from 12 ms after the click:
  - at 0.1×: about 70 ms on a 110 Hz tone, and about 200 ms on a 60 Hz tone;
  - at 0.25×: about 30 ms.

  The hit's own decay masks part of it in real material.
- **A split whose pieces have different sound settings** (volume, effect, mode, a fade, automation, or a transition at the cut) keeps a hard cut, as any cut between two different sounds does. Pieces with the same settings play as one sound.
- **MPEG-TS whose sound starts after its picture.** For clip In points under about 1.25 s, that sound can play early by the gap between the streams.
- **Stabilization analyses made by earlier test builds** report "outdated: analyze the clip again".
- **HDR (PQ, HLG) 4:2:0 or 4:2:2 sources tone-mapped to an image sequence.** A graded clip with a crop, mask or key can differ from the same clip without them by up to 20 8-bit codes at isolated pixels (mean under 1). Effects upsample chroma before the tone map, and the steep PQ and HLG curves magnify the difference. 4:4:4 sources match exactly.
- **16-bit RGBA stills in ProRes 422 HQ** keep 87 levels of a 4096-step ramp. This is the same on `dev` and is not caused by effects.
- No blend modes on adjustment clips. No motion tracking: masks are positioned by hand and keyframed.
