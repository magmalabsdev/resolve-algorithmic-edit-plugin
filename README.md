# Trim Timelapse Silence — a DaVinci Resolve plugin

Long timelapse, screen, and lecture recordings are full of dead air with no dialogue.
This plugin analyzes the audio of the clips on a video track, finds the boundaries
between **spoken** and **silent** regions, and **splits** the timeline at every boundary
— adding real edit points without removing any media ("split, not cut"). The silent
clips are tinted orange so you can review them and ripple‑delete the dead air to
compress the recording.

The result is written to a **new** `"<name> - Split"` timeline; your original timeline
is never modified.

## How it works

DaVinci Resolve's scripting API exposes neither a razor/blade tool nor audio sample
data, so the plugin:

1. Reads the clips on the track(s) you choose to analyze and finds their source media
   files. You can analyze a **video track's embedded audio**, a **dedicated audio
   track**, or **several audio tracks at once**.
2. Runs ffmpeg's [`silencedetect`](https://ffmpeg.org/ffmpeg-filters.html#silencedetect)
   filter on each file to find silent intervals by audio level.
3. Maps each silence onto the timeline and pads it (to keep a little air around speech).
   When you select multiple tracks they are combined so a moment is treated as silent
   **only where every selected track is silent** — audio is kept whenever anyone is
   speaking — and gaps with no clip count as silence.
4. Rebuilds a new timeline via `MediaPool.AppendToTimeline`, reproducing **every** video
   and audio track split into separate clips at the silence boundaries (so the picture
   and all audio stay in sync when you delete dead air).
5. Colors the silent clips orange (optionally adds a marker to each).

## Prerequisites

- **DaVinci Resolve** (tested against the 20.2 scripting API; Studio recommended —
  free‑edition scripting support may vary).
- **ffmpeg** on your `PATH` (or in `/opt/homebrew/bin` or `/usr/local/bin`):
  ```sh
  brew install ffmpeg
  ```

## Install

```sh
./install.sh
```

This symlinks the script into
`~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit/`.
Restart Resolve if it's already running, then open it from
**Workspace ▸ Scripts ▸ Edit ▸ Trim Timelapse Silence**.

To uninstall: `./install.sh --remove`.

## Usage

1. Open the timeline containing your timelapse recording.
2. Run **Workspace ▸ Scripts ▸ Edit ▸ Trim Timelapse Silence**.
3. Adjust settings if needed and click **Run**.
4. Review the new `"<name> - Split"` timeline. To remove the dead air, select the orange
   (silent) clips and ripple‑delete them.

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| Silence threshold (dB) | `-40` | Audio below this level counts as silence. Quieter rooms → use a lower (more negative) value. |
| Min silence length (s) | `1.0` | Silences shorter than this are ignored (not split). |
| Padding around speech (s) | `0.2` | Silence kept on each side of speech, so onsets/tails aren't clipped. |
| Analyze tracks | first video | Check one or more tracks to analyze — a video track (its embedded audio) and/or audio tracks. Multiple checked tracks are combined (silent only where all are silent). |
| ffmpeg path | auto | Override if ffmpeg isn't auto‑detected. |
| Color silent clips orange | on | Mark silent clips for easy selection. |
| Add a marker on each silent clip | off | Also drop a marker on silent clips. |

Whichever track(s) you analyze, the rebuild always reproduces **all** video and audio
tracks split at the same boundaries, so deleting the silent regions keeps everything in
sync.

## Scope & caveats

Handles a single video track with embedded audio, dedicated audio tracks (e.g. a separate
mic recording), and multiple video/audio tracks, at native speed.

- Transitions, compound clips, and pre‑existing retimes are not fully reconstructed by
  the rebuild.
- The rebuilt timeline contains fresh clips — grades / Fusion / effects from the original
  are not copied over (the original timeline is left intact, so nothing is lost).
- Audio is reproduced per track; the V↔A link from the original is not recreated, and a
  clip's embedded audio is only carried over if it appears on an audio track (Resolve's
  normal editing puts it there). New audio tracks match the source track's channel layout
  where possible.
- Source files must contain an audio stream; clips without audio are kept whole.
- Placement assumes source and timeline frame rates are consistent; extreme mismatches
  may drift by a frame.

## Development & testing

Pure logic (ffmpeg parsing, segment & frame math) is unit‑tested without Resolve or
ffmpeg:

```sh
python3 tests/test_segments.py
```

Validate audio analysis against a real file (needs ffmpeg, no Resolve required):

```sh
python3 trim_timelapse_silence.py --analyze /path/to/clip.mov
```
