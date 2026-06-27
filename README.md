# Trim Timelapse Silence — for DaVinci Resolve and Final Cut Pro

Long timelapse, screen, and lecture recordings are full of dead air with no dialogue.
This project analyzes the audio of your clips, finds the boundaries between **spoken**
and **silent** regions, and **splits** the timeline at every boundary — adding real edit
points without removing any media ("split, not cut"). The silent segments are marked so
you can review them and ripple‑delete the dead air to compress the recording.

It ships in two forms that share the **same** ffmpeg‑based silence detection:

- **DaVinci Resolve plugin** (`trim_timelapse_silence.py`) — runs inside Resolve and
  builds a new `"<name> - Split"` timeline. Covered first, below.
- **Final Cut Pro tool** (`fcp_trim_timelapse_silence.py`) — a command‑line companion
  that rewrites an exported FCPXML. See [Final Cut Pro](#final-cut-pro).

Your original timeline/project is never modified.

## How it works (Resolve)

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

## Final Cut Pro

Final Cut Pro has **no in‑app scripting API** — no razor tool, clip color, or audio
sample access of the kind the Resolve plugin relies on. Its automation happens through
the **FCPXML** interchange format instead, so the Final Cut Pro version is a small
command‑line tool that you run on an exported project. It reuses the exact same silence
detection as the Resolve plugin, then splits the **primary storyline** at every
speech/silence boundary and tags each silent segment with a **"Silence" keyword** and a
**to‑do marker**.

### Workflow

1. In Final Cut Pro, select your project (or timeline) and choose
   **File ▸ Export XML…**, saving e.g. `MyTimelapse.fcpxml`.
2. Run the tool:
   ```sh
   python3 fcp_trim_timelapse_silence.py MyTimelapse.fcpxml
   ```
   It writes `MyTimelapse - Split.fcpxml` next to the input.
3. Back in Final Cut Pro, choose **File ▸ Import ▸ XML…** and pick the `- Split` file.
   This creates a **new** project (your original is untouched).
4. Open the **Timeline Index** (`⌘⇧2`) ▸ **Tags** to jump between the "Silence"
   keywords/markers, then select the silent ranges and **ripple‑delete** (`⇧⌫`) to
   compress the recording.

Prefer the tool to do the cutting for you? Pass `--remove` to drop the silent segments
and ripple the storyline automatically, producing an already‑compressed project.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--threshold-db` | `-40` | Audio below this level (dB) counts as silence. |
| `--min-silence` | `1.0` | Ignore silences shorter than this many seconds. |
| `--padding` | `0.2` | Seconds of silence kept around speech. |
| `--remove` | off | Drop silent segments and ripple the storyline (instead of tagging them). |
| `--no-keyword` / `--no-marker` | both on | Omit the "Silence" keyword / to‑do marker. |
| `--ffmpeg PATH` | auto | Override if ffmpeg isn't auto‑detected. |
| `--dry-run` | off | Analyze and report only; write nothing. |

### Scope & caveats (Final Cut Pro)

- Operates on the **primary storyline** of the first sequence. Each storyline
  `asset-clip` is split at the silence boundaries; whole‑clip adjustments and filters are
  copied onto every resulting piece.
- Clips that carry **connected clips/titles** (or compound/synchronized clips with no
  single source media) are passed through **unsplit** rather than risk producing invalid
  geometry, and a note is logged. Single‑clip timelapse recordings — the target use case
  — split cleanly.
- Cuts are snapped to the sequence's frame grid; source and sequence frame rates are
  assumed consistent (extreme mismatches may drift by a frame).
- Source files must contain an audio stream; clips without audio are kept whole.

## Development & testing

Pure logic (ffmpeg parsing, segment & frame math, FCPXML time/split math) is unit‑tested
without Resolve, Final Cut Pro, or ffmpeg:

```sh
python3 tests/test_segments.py   # Resolve core + shared silence logic
python3 tests/test_fcpxml.py     # Final Cut Pro FCPXML rewrite
```

Validate audio analysis against a real file (needs ffmpeg, no NLE required):

```sh
python3 trim_timelapse_silence.py --analyze /path/to/clip.mov
```
