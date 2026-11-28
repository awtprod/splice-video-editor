# Can Splice replace Premiere, Resolve or Final Cut Pro?

A short, candid answer for independent filmmakers, based on what has actually been built and tested so far. Details and sources are on the other pages in [docs](../README.md#documentation).

## The short answer

**Not yet as your only tool, and not on a deadline.** Splice can already cut a short, a documentary or a feature-length timeline, finish primary colour and dialogue, and deliver ProRes, DNxHR, H.264 and HDR10 masters. Its strongest point is that it hands work to and from Premiere, Resolve and other NLEs without dropping things silently. But it has had no public release with these features yet, several important paths have been verified only by its own tests rather than in the other applications, and some tools that commercial NLEs take for granted are missing.

A realistic way to use it today: **cut in Splice, and finish or check in Resolve or Premiere**, using the interchange routes, with your camera originals kept where they are.

## Where Splice is ready (with stated limits)

- **Editorial.** Three- and four-point editing, ripple and roll, slip and slide, J/L cuts on linked picture and sound, clip enable/disable, lift and extract, inclusive In/Out marks, drop-frame timecode, a full keyboard workflow. Feature scale was designed and tested in: up to 20,000 timeline items and 12-hour timelines. [Editing](editing.md)
- **Organising.** Bins and sub-bins, subclips, scene/shot/take/reel/camera metadata, colour labels, nested sequences, multicam with up to 9 angles and a live multi-angle monitor. [Multicam, nesting, subclips and bins](multicam-nesting-bins.md)
- **Premiere round trip.** Splice opens and saves `.prproj` files and keeps the native objects it cannot edit. Its tests compare timing with Adobe-authored reference projects. [Interchange](interchange.md)
- **Turnover.** FCPXML, FCP 7 XML, EDL and OTIO export and import, with a report of everything each format cannot carry and a turnover package. Premiere imported Splice's FCP 7 XML and OTIO, and Resolve imported its FCPXML, with the cuts frame-exact in the files checked.
- **Opening Resolve projects.** `.drp` files and disk libraries open read-only, with a per-clip list of what was left out.
- **Primary colour.** Lift/gamma/gain/offset, curves, hue vs saturation, LUTs up to 65-point, adjustment clips, four scopes, and a monitor measured to stay within about 2 codes of a ProRes export on the test patches. [Colour finishing](color.md)
- **Dialogue finishing and delivery loudness.** EQ, compressor, gate and de-noise that match between preview and export; BS.1770 loudness targets; a 4× oversampled true-peak limiter; 5.1; stems. [Audio finishing](audio.md)
- **Masters.** ProRes (all profiles, including 4444 XQ with alpha), DNxHR in MXF, H.264, HEVC Main 10 with HDR10 metadata, AV1, image sequences, a render queue that survives restarts, and cancels that clean up after themselves. [Delivery](delivery.md)
- **Your media stays put.** Link camera originals in place; Splice copies nothing unless you ask it to.

## Where Splice is behind

- **No release yet.** The Windows installer exists and passes CI, but no published release contains it or most of the features described here. Builds are unsigned, so Windows SmartScreen warns. macOS gets an unsigned portable build and has no CI. [Windows](windows.md)
- **Verification in the other applications is incomplete.** Projects written by the current Resolve library writer had not yet been opened in Resolve when it merged. The latest FCP 7 XML position and scale changes have not been re-checked in Premiere. Resolve has not been tried with mixed-frame-rate FCPXML, and an early OTIO file failed in Resolve. [Interchange](interchange.md#known-limits)
- **Camera RAW beyond CinemaDNG is unproven.** BRAW, R3D, ARRIRAW, Canon, Nikon and Sony RAW import with sound and metadata, but their pictures need the vendor's decoder, and that path has been tested only with mock decoders and header fixtures, never with a real camera file. The built-in CinemaDNG developer is pure Python and slow (about 0.6–2.4 s per 1080p frame). [Camera RAW](camera-raw.md)
- **Colour is primaries only.** No secondaries or qualifiers, no tracking, no video noise reduction, no ACES, no HDR display path (HDR is previewed tone-mapped on SDR). Scopes read the 8-bit preview, not the export.
- **Audio monitoring is stereo only**, even when you deliver 5.1.
- **Speed.** It is a browser-based editor with a Python and FFmpeg render engine. Hardware encoders are detected but no successful hardware encode has been verified. Exports take real time: on the test machine a 10-minute 1080p H.264 timeline took about two and a half minutes.
- **Runs as a local web app.** The editor UI is a Chromium window talking to a local server. It is designed for one person on one machine; there is no shared-project or multi-user workflow.

## What is missing

- Titling beyond basic text titles; motion graphics; compositing beyond keys, masks and blend modes (once merged).
- Motion tracking (masks are positioned by hand and keyframed).
- DCP and IMF packaging (Splice makes source masters for a separate packaging tool), Dolby Vision, embedded CEA-608/708 closed captions.
- Automatic transcription or other AI-assisted tools.
- Third-party plug-ins (OpenFX, VST and the like), external video output to a reference monitor, and control surfaces.
- Collaboration: shared projects, bin locking, cloud libraries. Resolve network (PostgreSQL) libraries and Blackmagic Cloud are not supported.
- Writing `.drp` files (Splice writes Resolve projects into a disk library instead).

## The licence

Splice's own licence has **not been decided yet**; see [License](../README.md#license). If you need certainty about what you may do with the code, wait for that decision.
