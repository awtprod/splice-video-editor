# Audio finishing

Splice can take dialogue from cut to a delivered mix: clip and track effects, volume and pan automation, double-system sync, loudness measurement and normalization, a true-peak limiter, 5.1, and stems. It is an editor's mixing toolset, not a DAW. Live preview is stereo only, even when you deliver 5.1.

## The Audio workspace

- Clip fades and crossfades, clip level and pan, track level, mute, solo and pan, and metering of the preview bus.
- Per track: a **Stem** name (Dialogue, Music, Effects or your own), **Channels** (stereo or mono), its **5.1** placement (L/R, Centre, LFE, Ls/Rs), **Effects** and **Automation**.
- **Analyze program** measures the program through the export path: integrated loudness, loudness range, maximum momentary and short-term loudness, and true peak (4× oversampled, BS.1770).

## Effects

Clip effects are set in the Inspector. Track and master effects are set in the Audio workspace. The same processing runs in the browser preview and in the export; the test suite compares them sample by sample.

- **EQ:** parametric bands (high-pass, low-pass, low shelf, high shelf, bell) with a response curve. Presets: *Dialogue clean-up*, *De-ess band cut*, *Music under dialogue* (presets replace the bands).
- **Compressor:** threshold, ratio, attack, release, knee, make-up.
- **Gate / expander:** threshold, range, ratio, attack, release.
- **De-noise:** reduction and noise floor.

## Automation

Track volume and pan can be automated. Each track's **Automation** button shows its lanes.

## Syncing double-system sound

**Edit → Sync picture to sound…** lines up separately recorded sound with camera clips, by waveform or by timecode.

- Waveform sync compares the camera's scratch track with the recorder file. You can sync a batch; files with no match are reported as unmatched.
- Long takes are checked for clock drift. A take whose devices drift apart is refused rather than synced wrongly, and a take whose shared passage is too short to measure is left for you to confirm.
- A very repetitive signal (a steady tone, for example) can have several equally good offsets. Splice then refuses rather than guess.

## Delivery: loudness, limiter, channels and stems

In **Export video**, the **Audio** section has:

- **Loudness:** Off (the default; normalization is opt-in), −14 LUFS (streaming), −16 LUFS (podcast, mobile), −23 LUFS (EBU R128 broadcast), −24 LUFS (ATSC A/85 broadcast), −27 LUFS (cinema, dialogue-led), or a custom target from −36 to −8 LUFS. Loudness is measured in two passes.
- **Method:** *Gain only* (no change to dynamics; stops at the true-peak ceiling and reports any shortfall) or *Gain, then true-peak limiting* (reaches the target).
- **Limiter** with a **Ceiling** from −24 to 0 dBTP, on by default at −1 dBTP. It measures peaks on a 4× oversampled copy of the program.
- **AAC in MP4.** AAC encoding can push the decoded peak above the ceiling (up to about 2 dB on the test programs). By default Splice keeps the program's loudness, reports the decoded peak, and warns when it is over. **Keep decoded AAC under the ceiling** lowers the whole program in 0.5 dB steps until the decoded peak fits, and reports how much. A range export can land on a different step from the full export.
- **Channels:** Stereo, 5.1 (L R C LFE Ls Rs, SMPTE order), or a 5.1 mix delivered as a stereo downmix.
- **Deliver:** the program with the picture, or **stems and mix as WAV files** (ZIP, no picture). Stems, and optional per-track iso WAVs, are full-length, sample-aligned 32-bit float WAVs taken before the master compressor and limiter. The finished mix is a 24-bit WAV.

Because stems are taken before master dynamics, the stems may not sum exactly to the finished mix when the master compressor or limiter is working. The report says so.

## Turnover

The turnover report lists audio settings that another application will have to rebuild (effects, automation, routing). Deliver it alongside the interchange files. See [Interchange](interchange.md).

## How it was verified

From the audio-finishing PR (#14), on generated test media in Chromium:

- All five presets and a custom −20.5 LUFS target landed within 0.001 LU of the target.
- At a −2 dBTP ceiling, an independent 4× SoX measurement read −2.05 dBTP for WAV and ProRes MOV.
- Browser EQ differed from the rendered impulse response by at most 0.000005 dB; compressor, gate and de-noise matched the export to within about 6e−8.
- Waveform sync was within 2 samples over offsets from −9.6 s to +9.95 s at 48 kHz.
- Three stem groups summed to the mix with a residual about 117 dB below peak.

These tests do not establish compliance with a broadcast delivery specification, listening-panel quality, or compatibility with a particular recorder, surround monitor controller or other NLE.

## Known limits

- Live preview is stereo. 5.1 can be exported and measured, not monitored.
- Stems are pre-master-dynamics, so they may not null against a limited mix (reported).
- Default AAC can overshoot the ceiling after encoding (measured, reported and warned about).
- Waveform sync refuses repetitive signals with ambiguous offsets.
- Sound from an MPEG-TS file whose audio stream starts later than its picture can play early, by the gap between the two streams, when a clip's In point is under about 1.25 s. Found during the effects review (PR #18) and not fixed yet.
