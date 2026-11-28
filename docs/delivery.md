# Delivery and the render queue

Export renders from the original media (never from proxies), with the grade, audio processing and (once merged) effects applied the same way as in the monitor.

## Export video

Open **↗ Export video**. Choose a preset or set the format yourself, review the frame-size and colour notices, then **Render** now or **add the job to the render queue**. Presets can be saved, imported and exported (up to 200 of your own).

### Built-in presets

| Preset | Container and picture | Colour and audio |
|---|---|---|
| YouTube / Vimeo 1080p (SDR) and UHD (SDR) | MP4 (faststart), H.264 High, 8-bit 4:2:0, 1920×1080 or 3840×2160 | Rec. 709, AAC 320 kb/s |
| YouTube UHD HDR10 | MP4, HEVC Main 10, 10-bit 4:2:0, 3840×2160 | Rec. 2100 PQ, P3-D65 mastering display, measured MaxCLL/MaxFALL, AAC 320 kb/s |
| Web AV1 (small file) | MP4, AV1 Main, 10-bit 4:2:0 | Source colour, AAC 192 kb/s |
| Festival / DCP source (ProRes 422 HQ, ProRes 4444, or 16-bit TIFF sequence) | MOV, or numbered 16-bit RGB TIFF files | Source colour, 24-bit PCM in the MOV or a companion WAV |
| Broadcast master (ProRes 422 HQ, or DNxHR HQX MXF) | MOV ProRes 422 HQ, or MXF OP1a DNxHR HQX, 10-bit 4:2:2 | Source colour, 24-bit PCM, sequence timecode |
| Archive master (ProRes 4444 XQ) | MOV, 10-bit 4:4:4 with alpha | Source colour, 24-bit PCM |
| Editorial proxy (ProRes Proxy or DNxHR LB, half size) | MOV ProRes Proxy 10-bit 4:2:2, or MXF DNxHR LB 8-bit 4:2:2 | Source colour where supported, 16-bit PCM, timecode and clip-name burn-ins |
| Social vertical 1080 × 1920 (crop or pad) | MP4, H.264 High, 8-bit 4:2:0 | SDR, AAC 256 kb/s |

The "Festival / DCP source" presets make **source masters for a separate packaging tool**. Splice does not make DCPs or IMF packages.

### Custom formats

- **Video:** H.264 (CRF, bitrate or two-pass), HEVC Main or Main 10, AV1 (needs an FFmpeg with `libsvtav1`), all six ProRes profiles, all five DNxHR profiles, in MP4, MOV or MXF as each codec allows.
- **Image sequences:** PNG (8- or 16-bit), TIFF 16-bit, DPX 10-bit, OpenEXR half float.
- **Audio only:** WAV. **Stems:** see [Audio finishing](audio.md#delivery-loudness-limiter-channels-and-stems).
- **Captions:** an embedded subtitle stream (MP4/MOV; its language comes from **Subtitle language**, ISO 639-2, default `und`) or SRT/WebVTT sidecar files.
- **Metadata and overlays:** start timecode, reel, title and comment metadata, timecode and clip-name burn-ins, a watermark and safe-margin guides.
- **Hardware encoders:** NVIDIA NVENC, Intel Quick Sync, VAAPI, Apple VideoToolbox and AMD AMF for H.264 and HEVC, offered only when FFmpeg reports them. **Successful hardware encodes have not been verified**: no test machine had a working device and driver. Only the refusal and the fall-back to software were tested.

### HDR

- Choose **PQ** and a compatible 10-bit encoder for HDR10. Splice uses libx265 for PQ HEVC so that HDR10 static metadata (mastering display, MaxCLL/MaxFALL) is written.
- HLG export keeps HLG source material as HLG.
- Dolby Vision is not supported.

### Turnover

The turnover package can include a rendered reference movie with sequence timecode and clip name burned in. See [Interchange](interchange.md).

## The render queue

**File → Render queue…** renders jobs one at a time against a snapshot of the timeline taken when you queued them, so you can keep editing.

- Pause the whole queue; hold, release, move up or down, cancel, retry or remove a job; reveal a finished file.
- The queue survives a page reload and a restart. Jobs that had not started stay queued. A job that was rendering when Splice stopped is marked *interrupted* and can be retried.
- The default output folder is `Exports` inside Splice's data folder (on Windows, `%LOCALAPPDATA%\Splice\Exports`, which most users never see). Choose another folder in the export dialog, or set `SPLICE_EXPORT_DIR`.
- Existing files are not replaced without confirmation.

### Cancelling

- Cancelling stops the render's processes and removes its scratch files.
- **A job only shows as cancelled (or finished) once nothing of it is left**: every FFmpeg process it started has exited (Splice waits up to 10 seconds, then reports a warning) and its scratch folder, including any nested-sequence or RAW intermediates, has been removed. The next job cannot start before that.
- If Splice crashes mid-export, the leftovers are removed the next time it starts.

## Limits and scratch space

- Up to 20,000 timeline items, 5,000 distinct media files used by the timeline, 12-hour timelines, and frame sizes up to 8192 × 4320 (even width and height). Each limit has an error that names it.
- Range exports read only the media inside the range.
- **Exports have no time limit.** An export stops only when you cancel it, or when FFmpeg makes no progress for 5 minutes. Change that with `SPLICE_EXPORT_STALL_SECONDS` (1 to 86400).
- Scratch files go to `SPLICE_TEMP_DIR`, or by default to a `render-work` folder in Splice's data folder. Allow about the size of the finished file, plus 384 KB per second of program for the audio mix (twice that if a speed-changed clip runs the whole program). Splice checks free space before it starts.

## Correctness

- Export pictures match the monitor, including variable-frame-rate, mixed-frame-rate and reversed footage, sources with coarse time bases (phones, MKV, AVCHD/MPEG-TS), and MPEG-TS/AVCHD files with long GOPs (FFmpeg starts a seek in those at the next keyframe; Splice checks and seeks earlier).
- Image sequences read an untagged source as Rec. 601, as the monitor and the video deliveries do.
- Disabled clips are left out, picture and sound. Hidden video and title tracks are left out; a hidden video track's camera sound still plays.
- SDR exports are tagged Rec. 709.
- Long H.264 timelines are rendered in bounded windows. On the delivery PR's test machine a 10-minute, 150-cut 1080p24 H.264 timeline took about 150 s with about 1.3 GiB peak memory, and a 100-minute timeline about 30 minutes. These are single measurements on one shared Linux machine.

## Known limits

- No DCP, IMF, Dolby Vision, or embedded CEA-608/708 closed captions.
- Hardware encoders are unverified (see above). Native Windows and macOS encodes are verified only by the Windows CI job (see [Windows](windows.md)); macOS is not tested.
- The export dialog can overflow horizontally by a few pixels, and a long queue list does not keep its footer in view.
- MP4 audio: see the AAC note in [Audio finishing](audio.md#delivery-loudness-limiter-channels-and-stems).
- On Windows, a process started by FFmpeg that outlives FFmpeg itself cannot be stopped by cancel. FFmpeg starts no such processes itself.
