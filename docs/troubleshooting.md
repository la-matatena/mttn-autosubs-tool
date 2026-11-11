# Troubleshoot MTTN AutoSubs Tool and DaVinci Resolve 20

Use this guide to resolve macOS Gatekeeper permissions, missing Homebrew binaries, DaVinci Resolve 20 audio bounce failures, and GPU VRAM issues on your Mac.

## Allow macOS Gatekeeper to run the launcher scripts

When you extract `MTTN_AutoSubs_Tool.zip` downloaded from a browser, macOS assigns a `com.apple.quarantine` extended attribute that can block double-clicking `Iniciar AutoSubs ES.command`.

1.  Open **Terminal** and change to your extracted folder:

    ```bash
    cd AUTOSUBS_DIR
    ```

    Replace the following:

    -   `AUTOSUBS_DIR`: the directory path that contains the extracted `AutoSubs_ES/` (or cloned `mttn-autosubs-tool/`) files.

2.  Restore execute bits and remove the macOS quarantine attribute:

    ```bash
    chmod +x install_imac.sh "Iniciar AutoSubs ES.command"
    xattr -d com.apple.quarantine "Iniciar AutoSubs ES.command" install_imac.sh 2>/dev/null || true
    ```

## Fix missing `whisper-cli` or `ffmpeg` binaries

If the top status badges or `/api/transcribe` report missing `whisper-cli` or `ffmpeg` executables (or MP4 burn-in reports missing `libass`), confirm that Homebrew is on your `PATH` and reinstall the formulas.

1.  Install or verify the formulas with Homebrew:

    ```bash
    brew install whisper-cpp ffmpeg ffmpeg-full
    ```

2.  Confirm that both executables are callable from Terminal:

    ```bash
    which whisper-cli ffmpeg
    ```

`hardware_imac2017.py` checks `/usr/local/bin` and `/usr/local/opt/whisper-cpp/bin` (Intel Macs), `/opt/homebrew/bin` (Apple Silicon Macs), `ffmpeg-full` keg paths, and your shell `PATH`. If Homebrew placed `whisper-cli` elsewhere, check its directory in `echo $PATH` before launching `autosubs_server.py`.

## Resolve missing timeline audio after running Script 1

If **Workspace** > **Scripts** > **1 - MTTN AutoSubs - Exportar Audio** completes in DaVinci Resolve 20 without loading a waveform in MTTN AutoSubs Tool:

1.  In DaVinci Resolve 20, click **Workspace** > **Console** and inspect messages prefixed with `[AutoSubs ES]`.
2.  Confirm that `~/Library/Application Support/AutoSubsES/bridge/inbox/timeline_audio.wav` exists and is non-empty on disk.
3.  If the WAV file exists on disk, return to MTTN AutoSubs Tool, open **1. Audio de origen**, and click **Leer buzón**.
4.  If your timeline has no active project, unmuted audio tracks, or selectable render range, enable at least one audio track in Resolve and re-run **1 - MTTN AutoSubs - Exportar Audio**.

## Import SRT subtitles manually when `AppendToTimeline` is skipped

In some DaVinci Resolve 20 Free project configurations, `mediaPool:AppendToTimeline` ignores subtitle items or returns an empty list `{}` even though **Script 2** exported valid `.srt` files.

1.  Open **Workspace** > **Console** in DaVinci Resolve 20 and check whether Script 2 printed the fallback path to `autosubs_es_resolve_tc.srt`.
2.  In DaVinci Resolve 20, click **File** > **Import** > **Subtitle** (or **Archivo** > **Importar** > **Subtítulo**).
3.  Select `~/Library/Application Support/AutoSubsES/bridge/outbox/autosubs_es_resolve_tc.srt` (this file is pre-aligned to your timeline's `01:00:00:00` starting timecode).
4.  Drag the imported subtitle clip onto your subtitle track (`ST1`).

## Enable `Text+` insertion from the Media Pool

If **2 - MTTN AutoSubs - Insertar Subtitulos** falls back to FCPXML import with the message `No hay ninguna plantilla Text+ en tu Media Pool`, DaVinci Resolve has not indexed one of the Fusion `.setting` presets inside the current project's Media Pool.

1.  Open the **Edit** page in DaVinci Resolve 20 and open the **Effects** panel.
2.  Navigate to **Toolbox** > **Titles** > **MTTN AutoSubs** (or **Effects** > **Titles** > **MTTN AutoSubs**).
3.  Drag your chosen preset (`AutoSubs_ES_Cine_Netflix`, `AutoSubs_ES_Amarillo_Clasico`, `AutoSubs_ES_Reels_Impacto`, `AutoSubs_ES_Caja_Editorial`, or `AutoSubs_ES_Anitype_Animado`) into the **Media Pool** (or into **Power Bins** so every future project inherits it).
4.  Return to MTTN AutoSubs Tool, click **Enviar a DaVinci Resolve 20**, and run **Workspace** > **Scripts** > **2 - MTTN AutoSubs - Insertar Subtitulos** inside Resolve.

## Fix missing Spanish accents or invisible custom fonts in Resolve 20

Custom display fonts uploaded in AutoSubs ES (such as the five-font `Anitype Journal` pack) require both Fusion font enumeration and valid Spanish glyphs in `cmap` tables:

-   **Font name missing in Resolve `Text+`**: Confirm **Instalar en macOS (`~/Library/Fonts`)** was enabled when uploading the `.otf`, `.ttf`, or `.zip` pack, then quit and reopen DaVinci Resolve 20. Resolve caches available macOS font descriptors only at application startup.
-   **Accented letters or `¿` / `¡` disappear in `Text+`**: Fusion does not borrow missing characters from fallback system fonts. If the AutoSubs ES glyph banner reports missing Spanish characters, enable **Adaptar glifos ASCII** under **4. Estilo y tipografía**. AutoSubs ES preserves accented spelling in your cue list while outputting clean Basic ASCII text (`á→a`, `ñ→n`, removing `¿` and `¡`) to Resolve, FCPXML, and `.srt` files.
-   **Animation stays static after pushing to timeline**: Confirm **Método en timeline** uses **Clips Text+ GPU-Light (1 nodo)** with `AutoSubs_ES_Anitype_Animado` dragged into your **Media Pool**, or switch to **FCPXML 1.9 Universal** to slice flipbook variants across frame holds on Basic Titles.

## Prevent GPU memory errors on 2 GB VRAM Macs

To avoid `GPU Memory Full` errors or sluggish 4K playback on a 2017 iMac with 2 GB AMD Radeon Polaris VRAM:

-   Open **⚙ Esta Mac** (**Rendimiento de esta Mac**) from the top bar or **Ajustar…** in **2. Motor Whisper**, and keep the **Automático** or **Seguro** profile active (`--no-gpu` / `-ng` on Intel Polaris GPUs, `3` threads when Resolve is running) so transcription consumes zero GPU VRAM.
-   Choose **Pista Subtítulos Nativa (0 MB VRAM)** during rough cuts on 4K timelines, and switch to single-node **Clips Text+ GPU-Light (1 nodo)** only when burned-in vector styling is needed.
-   Use **Timeline Proxy Resolution** (`Half` or `Quarter`) inside DaVinci Resolve **Playback** settings while scrubbing 4K footage with `Text+` overlays.

## Change the local server port when port 55120 is busy

If another local service already binds TCP port `55120`, launch the server on an alternate port:

```bash
python3 -B autosubs_server.py --port PORT_NUMBER
```

Replace the following:

-   `PORT_NUMBER`: a free local TCP port number such as `55121`.

When running on a non-default port, Script 1's background `curl` ping to port `55120` skips auto-refreshing the UI, so click **Leer buzón** inside **1. Audio de origen** after each timeline audio bounce.

## Run the automated self-test suite

Verify the Spanish NLP formatter, all subtitle exporters, Retina 4K template headers, Lua bridge guards, local HTTP server, and `.zip` bundle builder in one pass without modifying your Resolve folders:

```bash
python3 -B autosubs_es_test.py -v
```

Confirm that all unit and HTTP integration tests report `OK`.

## What's next

-   Return to the [MTTN AutoSubs Tool overview](../README.md) or step-by-step [user guide](user-guide.md).
-   Review hardware tuning rationale in the [architecture and 2017 iMac tuning guide](imac-2017-architecture.md).
