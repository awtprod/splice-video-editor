# Splice

Splice is a local video editor for Linux. It opens with **Alpine Passage**, a 32-second sequence with four moving landscape shots, an opening title, dissolves, and an ambient score.

## Launch

Use Python 3.10 or newer, Chromium or Google Chrome, and FFmpeg with ffprobe, H.264, AAC, and the drawtext filter. Extract the entire Linux archive and run this command inside its Splice directory.

```sh
./splice
```

The launcher opens a dedicated application window and stops its loopback server when the window closes. All fonts, sample media, and project-format code are included. To use a regular browser, run `python3 server.py` and open the local address it prints. The desktop and browser modes have separate browser storage.

## Edit

- Choose **New project** for a blank timeline. **Import media** adds files to the bin. Double-click footage to append it, or drag a bin item to its timeline lane.
- Click the ruler to seek. Space plays or pauses. Select a clip and press **S** to split it. Drag to move it, or edit its start, source in, and duration in the inspector. Delete removes the selection. Ctrl+Z and Ctrl+Shift+Z undo and redo edits.
- Use **Titles** to add a text overlay at the playhead. **Effects** applies a dissolve to the selected shot. **Color** exposes brightness, contrast, and saturation. **Audio** exposes clip gain. Overlapping audio clips mix during playback and export.
- **Open** accepts a Premiere project or a Splice working copy. Use **Relink offline media** to choose referenced files that have moved.
- **Save** or Ctrl+S downloads a new Premiere project. Ctrl+Shift+S saves a Splice working copy. Keep your source media with your project. Moving the project to another computer can require relinking.
- **Export video** renders a H.264/AAC MP4 locally. The renderer supports timelines up to two hours, 200 clips, 3840×2160, and 60 fps. A source file copied into Splice's local media directory can be up to 1 GiB.

## Premiere compatibility

Splice reads and writes the gzip-compressed Premiere project object graph. When you open an existing project, Splice edits the first sequence and carries the original graph forward into the saved copy. The editor handles file-backed clips, timing, supported text graphics, static color and gain, and basic dissolves. Newly imported A/V clips produce linked camera-audio items on save.

The timeline groups clips into video, title, and audio lanes. Nested sequences, multicam switching, third-party effects, and animated Premiere effects have no editing controls here. Preview and movie export use Splice's supported effects. Static color settings map to Lumetri parameters, and a new dissolve maps to Premiere's Cross Dissolve. Cuts with insufficient source handles can require trimming before saving a transition. Keep a Splice working copy for media that your browser can decode but the Premiere writer cannot import.

Compatibility checks included opening a Premiere-authored project, saving edits, and reopening the resulting project on Linux. Opening the output in Adobe Premiere Pro on Windows or macOS remains unverified.

## Included materials

The Premiere bridge vendors the MIT-licensed py-premiere library at commit ce75c9935cbf75f8b7492eb7c92ffed66d32677a. The two isolated effect templates come from its audio volume and Lumetri fixture projects. Splice adds standard timecode-format mappings. [py-premiere](https://github.com/Pouf/py-premiere) · [Adobe time display constants](https://github.com/Adobe-CEP/Samples/blob/master/PProPanel/jsx/PPRO/Premiere.jsx)

DM Sans and IBM Plex Mono are distributed under the SIL Open Font License. Licenses accompany the fonts. The sample footage animates four landscape photographs, and the included score was synthesized for this project. [Ridge](https://images.unsplash.com/photo-1464822759023-fed622ff2c3b) · [Valley](https://images.unsplash.com/photo-1472396961693-142e6e269027) · [Forest](https://images.unsplash.com/photo-1441974231531-c6227db76b6e) · [Lake](https://images.unsplash.com/photo-1476514525535-07fb3b4ae5f1)

The round-trip tests use the upstream Premiere-authored audio-volume fixture. Run them from the application directory with `python3 -m unittest discover -s tests`.
