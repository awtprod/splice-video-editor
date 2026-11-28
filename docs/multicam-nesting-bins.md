# Multicam, nested sequences, subclips and bins

All of these are saved in a Splice working project (**File → Save working project…**). Other applications do not receive them as such. See [Interchange](#interchange) below before you hand a cut over.

## Nested sequences

- Select one or more timeline clips and choose **Edit → Nest…** (or right-click). Splice creates a named sequence in the media pool and replaces the selection with one nested clip, marked `NEST`.
- Double-click a nested clip, or click **Open nested sequence** in the Inspector, to edit inside it. A breadcrumb bar (`Sequence > Nest 1 > …`) takes you back up.
- Nests can contain nests. Splice refuses projects that nest sequences more than four levels deep.
- Exports render nested sequences directly: outer trims, inner edits, disabled clips inside nests and the nest's own mix all carry through.
- **Edit → Unnest / Decompose in place** puts a nest's clips back onto the parent timeline, keeping trims, offsets, the enabled state, the child mix and track visibility. When the result cannot be reproduced exactly, Unnest refuses with "Cannot unnest losslessly: …" and changes nothing.

## Multicam

- **Edit → Create multicam clip…** takes 2 to 9 media items (from the media pool or the timeline). Sync the angles by **In points / Start**, **source timecode** or **Marker**. The dialog previews the computed angle offsets before it creates the clip, and refuses inputs it cannot sync.
- Sound: **Camera 1**, **Follow active angle**, **All angles**, or one fixed angle.
- **Edit → Multicam monitor** (or the **Multicam** button on the program monitor) shows a live 2×2 grid (up to 4 angles) or 3×3 grid (up to 9 angles). The active angle is outlined red while playing or cutting and yellow when parked.
- **1–9**, or a click on an angle tile, cuts to that angle at the playhead. **Shift+1–9** switches the clip's active angle without adding a cut. Every switch can be undone.
- **Edit → Flatten multicam** replaces multicam clips with plain clips on each angle's source media, with the same in points, offsets and durations. An unflattened multicam clip exports the same picture and sound as its flattened version.
- Saving a Premiere project is refused while multicam clips are on the timeline: flatten them first, or save a Splice working project to keep the editable angles.

## Subclips

- Set In and Out in the source monitor and choose **Make Subclip…** or press **Ctrl/Cmd+U**.
- A subclip is a reusable media-pool entry with its own range, adjusted display timecode and optional bin. The source monitor and timeline trims stop at its limits; turn on **Remove subclip limits** to trim past them.
- Deleting media from the project lists the subclips, multicam clips and timeline clips that depend on it and asks before removing them too.
- The Inspector shows a subclip's own timecode and frame count.

## Bins and editorial metadata

- The media pool has a collapsible bin tree and breadcrumbs. Create bins and sub-bins (`Scene 1 / Cam A`), rename them (the change cascades to sub-bins and items), and drag items onto bins.
- Deleting a bin moves its items to the parent bin, or to **Unfiled**, after you confirm. It never deletes media.
- Editorial metadata: **Scene**, **Shot**, **Take**, **Reel**, **Camera** and **Log Note**, plus a colour label (eight colours, or none). Edit them in the source monitor, the **Media metadata** dialog or the Inspector. The media pool list view shows them as sortable, searchable columns, and labels colour the pool items and timeline clips.

## Interchange

- **Before sending camera edits to Premiere, use Flatten multicam.**
- **Before exporting a timeline (FCPXML, FCP 7 XML, EDL, OTIO), use Unnest / Decompose in place.** The turnover report lists what was left out, including nested sequences, unflattened multicam clips, subclip limits and the editorial fields (camera, scene, take, log note, label and bin).
- Saving to a Resolve library reports nested sequences and multicam clips that Splice created; see [Interchange and Resolve round-trip](interchange.md).

## Known limits

- Up to 9 angles per multicam clip.
- Nesting depth is limited to four levels.
- With the effects work (PR #18, not merged), a speed ramp on a multicam clip and stabilization of a nested or multicam clip are refused; see [Effects](effects.md).
