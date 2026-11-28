# Windows: install and troubleshooting

The Windows installer, `Splice-Setup-x64.exe`, is a single EXE that contains everything Splice needs: Python, a GPL build of FFmpeg and ffprobe, and Electron (Chromium). You do not need to install Python or FFmpeg yourself.

## Getting the installer

**No published release contains this installer yet.** The only release so far, v0.1.0, predates the installer and most of the features in these docs; it ships portable ZIP builds that need Python and FFmpeg installed separately.

Until a release includes it, you can:

- **Build it yourself** on Windows with Python 3.11 or later: `python desktop/windows/build.py artifacts/installer`. The build downloads the pinned Python, FFmpeg, Electron and NSIS archives and checks each one against its SHA-256. `--stage-only` stages the files without building the EXE, on any OS.
- **Take it from CI.** The repository's *Windows installer* workflow builds, installs, runs and uninstalls the EXE on GitHub's `windows-latest` runner, and uploads it as the `Splice-Windows-x64` artifact for same-repository pull requests and manual runs. Downloading an artifact needs a GitHub sign-in.

## Installing

1. Run `Splice-Setup-x64.exe`.
2. **Windows SmartScreen will warn you**, because the installer and `Splice.exe` are not code-signed. Choose **More info → Run anyway** only if you trust where your copy came from.
3. The installer is per-user and needs no administrator rights. It installs to `%LOCALAPPDATA%\Programs\Splice\Application` and adds **Splice** to the Start menu.
4. Open Splice from Start.

Upgrading replaces the previous version's program files completely. If Splice is running, the installer (and the uninstaller) asks you to close every Splice window and click Retry. An export in progress stops when Splice closes.

## What is installed, and where your data goes

| What | Where |
|---|---|
| Program files, bundled Python, FFmpeg and Electron | `%LOCALAPPDATA%\Programs\Splice\Application` |
| Your data: imported media copies, proxies, render queue, recovery drafts, settings | `%LOCALAPPDATA%\Splice` |
| Render-queue default output folder | `%LOCALAPPDATA%\Splice\Exports` |
| RAW decoder settings | `%LOCALAPPDATA%\Splice\raw-decoders.json` |
| Versions, checksums and licences of the bundled software | `Third-party.txt` in the install folder; FFmpeg's `LICENSE.txt` and `BUILD-CONFIGURATION.txt` in `resources\ffmpeg` |

`%LOCALAPPDATA%` is a hidden folder for most users. Choose a visible output folder (for example in Videos) in the export dialog.

**Uninstalling keeps `%LOCALAPPDATA%\Splice`**, so your projects, caches and exports survive. Delete that folder yourself to remove them. Uninstall from Windows Settings → Apps, like any other program.

## How the app runs

- Splice is a local web application in its own window. `Splice.exe` starts Splice's Python server and opens the editor in an Electron window.
- The server listens only on `127.0.0.1:47834` (this computer, not the network). It answers only the window that opened it.
- The app uses **only its bundled FFmpeg**: other FFmpeg builds on the computer are never picked up, even one on your `PATH`. The server runs FFmpeg and ffprobe from the install folder by their full paths, and the bundled FFmpeg folder comes first on its `PATH`. At startup it checks that this FFmpeg has every filter, encoder, decoder and muxer Splice uses.
- The rest of your `PATH` is kept, after the bundled FFmpeg folder, so camera RAW decoders you put on `PATH` are found (see below).

## Troubleshooting

**"Splice could not start" / "Splice stopped"**: the message shows the last lines the server printed. Common causes:

- **Splice could not listen on 127.0.0.1:47834.** Another program, or another Splice window, holds that port. Close it and start Splice again.
- **Splice cannot start: its FFmpeg (…) is missing …** or **FFmpeg did not answer**: the bundled FFmpeg is damaged or was removed, for example by antivirus software. Reinstall Splice.
- **The editor server did not start within 40 seconds.** Usually a very slow first start (antivirus scanning the new files). Try again; if it keeps happening, reinstall.

**SmartScreen blocked the installer.** See [Installing](#installing). The files are unsigned; this is expected until the project has a signing certificate.

**Exports are hard to find.** The default folder is `%LOCALAPPDATA%\Splice\Exports`. Use *reveal* in the render queue, or choose another folder in the export dialog.

**Camera RAW decoders are not found.** Splice finds a decoder (`braw-decode`, `REDline`, `art-cmd`, `dcraw_emu`, …) in this order: the path set in **File → RAW Decoders…**, the decoder's environment variable, your `PATH`, then the vendor's standard install folder (for example REDCINE-X PRO's or ARRI Reference Tool's default location). If a decoder on `PATH` is not found, check that its folder is in your user or system `PATH` (quotes around a folder are fine) and restart Splice, which reads `PATH` when it starts. Or set its full path in the dialog. See [Camera RAW](camera-raw.md#setting-up-vendor-decoders).

**An export was cancelled but files remain.** A cancelled job only shows as cancelled once its processes have exited and its scratch files are gone (see [Delivery](delivery.md#cancelling)). If Splice crashed instead, the leftovers are removed the next time it starts.

## What has been verified on Windows

Windows has been verified **only through the GitHub `windows-latest` CI job**, not on a range of real Windows desktops. That job builds the EXE, checks the bundled runtimes, installs it, round-trips a project, renders a set of export formats, cancels and retries a render, restores a draft, checks that another program cannot take over Splice's port, and uninstalls it. It does not exercise interactive dialogs (the "close Splice and Retry" prompt is checked in its silent, cancel path) or GPU hardware encoders.

## The portable ZIP

`python3 package_desktop.py artifacts` also builds `Splice-Windows.zip`. Extract it and double-click `Splice.cmd`. The portable build does **not** include Python or FFmpeg: install Python 3.10 or newer (with the `py` launcher or on `PATH`), put a folder containing `ffmpeg.exe` and `ffprobe.exe` on `PATH`, and have Chrome, Chromium or Edge installed. It does not run the FFmpeg feature check at startup; run `python runtime_check.py` yourself to check your FFmpeg. It uses port 47834 too, so do not run it alongside the installed app.
