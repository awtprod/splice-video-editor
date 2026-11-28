# Colour finishing

Splice grades in the **Color** workspace. The program monitor and the export share the same grade maths, and the test suite measures them against each other (see [How close the monitor is to the export](#how-close-the-monitor-is-to-the-export)). This is a primary-correction toolset. There are no secondaries or qualifiers, no tracking, no video noise reduction and no ACES. Power windows exist only as masks on an adjustment clip, which is part of the unmerged effects work ([Effects](effects.md)).

## Grading a clip

Select a clip and open **Color**. The controls apply in this order:

1. **Input LUT** (technical, set per source under **Source**): a `.cube` such as camera log to Rec. 709. It applies to every clip of that source, before the grade.
2. **Primaries:** exposure, temperature, tint, brightness, contrast (around an adjustable **Contrast pivot**) and saturation.
3. **Wheels:** lift, gamma, gain and offset.
4. **Curves:** master, red, green and blue.
5. **Hue vs saturation.**
6. **Clip LUT** (creative look), applied after the grade, with **LUT intensity**.

Other controls:

- **Copy** / **Paste** a grade between clips, **Apply to source** to grade every clip from the same media, **Reset**, **Reset curve**, and bypass.
- **Export .cube** writes the grade as a 33-point 3D LUT, for applications that cannot read Splice's grade. Check the loss report before relying on it.
- Six controls can be keyframed: exposure, temperature, tint, brightness, contrast and saturation. Reset clears all six in one undo step.
- **Adjustment clips** (**Edit → New adjustment clip**) grade every track beneath them for their duration. Use one for a look shared across several shots or tracks. Adjustment clips can be stacked.

## LUTs

- Import `.cube` files up to 65 × 65 × 65 (3D) or 65,536 entries (1D), up to 16 MB of text, blank lines included.
- `DOMAIN_MIN` / `DOMAIN_MAX` and `LUT_3D_INPUT_RANGE` / `LUT_1D_INPUT_RANGE` are honoured. `TITLE` is ignored; the file name is shown.
- A 1D LUT is applied as its full curve, in the monitor and in exports.

## Colour management

- The SDR working signal is full-range, video-encoded RGB. The monitor reads the decoded Y′CbCr planes where the browser provides them, and honours their range and matrix. Where the browser cannot provide them, it uses the browser's own RGB conversion and says so.
- **Untagged sources default to Rec. 601.** Use the **Untagged matrix** control to interpret one as Rec. 709.
- **HLG and PQ** sources are previewed through tone-mapped SDR proxies. HDR delivery keeps its own output path (see [Delivery](delivery.md)). SDR-only finishing operations on an HDR master require Rec. 709 delivery.
- SDR exports are tagged Rec. 709 (primaries, transfer, matrix, video range).

## Program monitor: viewing aids

These are monitor settings. They are not saved in the project and never appear in an export.

- **Quality:** Auto, Full, Half or Quarter. Auto steps down during playback when frames take longer than the frame period.
- **Views:** Normal, Luma only, Red / Green / Blue channel, False colour, Legal range warning, Zebra (clipping).
- **Guides:** Title/action safe, 2.39:1, 1.85:1, 4:3, 1:1, 9:16.
- **Bypass** the grade, or drag a **split** to compare graded and ungraded.

The monitor draws with **WebGL2** where the browser has it, and falls back to a Canvas 2D path otherwise, or while a lost WebGL context is being restored. Both use the same grade tables as the export.

## Scopes

- The Color section of the Inspector has four scopes, chosen by tab: **Waveform**, **RGB parade**, **Vectorscope** and **Histogram**. The histogram has a **Show luma curve** option. A height slider resizes the scope.
- **Program monitor › Scopes** docks a large scope beside the monitor. The dock closes when the source monitor needs the room in windows narrower than 1100 px, and it is not remembered across reloads.
- The percent axis runs 0–100%. The right-hand column gives 8-bit and 10-bit code values, and amber dashed lines mark legal range, 16–235 (64–940). Luma uses the Rec. 709 weights. The vectorscope marks 75% colour-bar targets and the skin-tone line.
- **What the scopes measure:** the graded picture of the program monitor, as 8-bit. They are not a measurement of the exported file. They sample at most 960 × 540 pixels, evenly spaced, so a one-pixel detail between samples does not show. During playback the monitor may draw at reduced quality, and the scopes read that.

## How close the monitor is to the export

The browser test suite draws synthetic patches in the monitor, exports them, decodes the exports with FFmpeg and compares patch means in 8-bit RGB codes. On the merged colour work (PR #13 and its follow-ups) the limit was 2 codes against 10-bit ProRes 422 HQ and 4 against H.264 for SDR cases (5 and 6 for HLG/PQ sources). The worst measured differences were about 2 codes against ProRes and about 2.5 against H.264. These are measurements on patch interiors in headless Chromium, not on fine chroma detail across hard edges, and not on your GPU and browser.

## Interchange

Grades and LUTs do not travel in FCPXML, FCP 7 XML, EDL or OTIO, and are not written into Resolve projects. The turnover and Resolve reports list them per clip. Export a `.cube` where a look needs to reach another application. See [Interchange](interchange.md).

## Known limits

- No secondaries, qualifiers, tracking, video noise reduction or ACES.
- Scopes read the 8-bit preview, not the export.
- An HDR grade is previewed on an SDR monitor through a tone-mapped proxy. There is no HDR display path.
- A clip with a steep LUT and a custom LUT domain can export up to about 2 codes away from the preview.
