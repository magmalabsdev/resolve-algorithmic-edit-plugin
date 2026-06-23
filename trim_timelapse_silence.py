#!/usr/bin/env python3
"""
Trim Timelapse Silence - a DaVinci Resolve plugin.

Finds the boundaries between spoken and silent regions of the clips on a video
track by analyzing audio levels (via ffmpeg's `silencedetect` filter) and rebuilds
the timeline so every speech/silence boundary becomes a real edit point. Nothing is
removed -- this is a "split, not cut" operation -- and the silent clips are tinted
orange so you can review and ripple-delete the dead air to compress a long timelapse.

The original timeline is never modified; results land in a new "<name> - Split" timeline.

Run it from DaVinci Resolve: Workspace > Scripts > Edit > "Trim Timelapse Silence".

It can also be exercised from the command line without Resolve, to validate the audio
analysis against a real file:

    python3 trim_timelapse_silence.py --analyze /path/to/clip.mov

Requires ffmpeg on PATH (or in /opt/homebrew/bin or /usr/local/bin):  brew install ffmpeg
"""

import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Defaults (also the initial values shown in the settings dialog)
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLD_DB = -40.0   # audio below this level is considered silence
DEFAULT_MIN_SILENCE = 1.0      # ignore silences shorter than this (seconds)
DEFAULT_PADDING = 0.2          # keep this much silence as breathing room around speech
SILENT_CLIP_COLOR = "Orange"   # DaVinci Resolve clip color applied to silent segments

# ===========================================================================
# Pure logic (no Resolve / ffmpeg dependency) -- unit tested in tests/
# ===========================================================================

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_AUDIO_STREAM_RE = re.compile(r"Stream #\d+:\d+.*: Audio:")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def parse_silencedetect(stderr_text):
    """Parse ffmpeg silencedetect stderr into a list of (start, end) silent intervals
    in seconds, measured from the start of the file. An unterminated trailing silence
    (a silence_start with no matching silence_end) is returned with end == inf so the
    caller can clamp it to the clip's used range."""
    intervals = []
    pending_start = None
    for line in stderr_text.splitlines():
        m = _SILENCE_START_RE.search(line)
        if m:
            pending_start = max(0.0, float(m.group(1)))
            continue
        m = _SILENCE_END_RE.search(line)
        if m and pending_start is not None:
            intervals.append((pending_start, float(m.group(1))))
            pending_start = None
    if pending_start is not None:
        intervals.append((pending_start, float("inf")))
    return intervals


def has_audio_stream(stderr_text):
    """True if ffmpeg reported at least one audio stream for the input."""
    return bool(_AUDIO_STREAM_RE.search(stderr_text))


def parse_duration(stderr_text):
    """Return the input duration in seconds from ffmpeg stderr, or None if absent."""
    m = _DURATION_RE.search(stderr_text)
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def compute_segments(used_start, used_end, silence_intervals, padding):
    """Split the used range [used_start, used_end] (seconds, in source-media time) into
    an ordered list of contiguous, non-overlapping segments tagged as silent or speech.

    Each silence interval is clipped to the used range and then shrunk by `padding` on
    both sides so a little silence is kept around speech; silences too short to survive
    padding are treated as speech. Returns a list of dicts: {"start", "end", "silent"}.
    """
    padded = []
    for raw_start, raw_end in silence_intervals:
        clip_start = max(raw_start, used_start)
        clip_end = min(raw_end, used_end)
        if clip_end <= clip_start:
            continue
        # Only pad an edge that is a real speech->silence transition inside the clip.
        # If the silence runs to (or past) the clip's used edge, there is no adjacent
        # speech to protect there, so leave that edge flush -- a fully silent trimmed
        # clip then stays one silent segment instead of growing speech slivers.
        pad_start = clip_start + padding if raw_start > used_start else clip_start
        pad_end = clip_end - padding if raw_end < used_end else clip_end
        if pad_end <= pad_start:
            continue  # too short to bother splitting after padding
        padded.append((pad_start, pad_end))

    padded.sort()

    segments = []
    cursor = used_start
    for pad_start, pad_end in padded:
        if pad_start > cursor:
            segments.append({"start": cursor, "end": pad_start, "silent": False})
        segments.append({"start": pad_start, "end": pad_end, "silent": True})
        cursor = pad_end
    if cursor < used_end:
        segments.append({"start": cursor, "end": used_end, "silent": False})
    if not segments:
        segments.append({"start": used_start, "end": used_end, "silent": False})
    return segments


# --- interval algebra (operates on lists of [start, end) intervals) --------
# Silence is tracked as half-open timeline-frame intervals so it can be combined
# across multiple analysis tracks before the timeline is split.

def merge_intervals(intervals):
    """Return sorted, disjoint intervals; touching intervals (a.end == b.start) merge."""
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def intersect_intervals(a, b):
    """Intersection of two lists of sorted, disjoint intervals."""
    result = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            result.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return result


def complement_intervals(intervals, lo, hi):
    """Gaps within [lo, hi) not covered by the (sorted, disjoint) intervals."""
    result = []
    cursor = lo
    for start, end in intervals:
        start = max(start, lo)
        end = min(end, hi)
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < hi:
        result.append((cursor, hi))
    return result


def point_in_intervals(x, intervals):
    """True if x falls within any of the (sorted, disjoint) intervals."""
    for start, end in intervals:
        if start <= x < end:
            return True
        if start > x:
            break
    return False


def interior_boundaries(intervals, lo, hi):
    """Sorted, unique interval edges that fall strictly inside (lo, hi) -- the cut
    points at which clips should be split."""
    cuts = set()
    for start, end in intervals:
        if lo < start < hi:
            cuts.add(start)
        if lo < end < hi:
            cuts.add(end)
    return sorted(cuts)


def interpolate(x, in_lo, in_hi, out_lo, out_hi):
    """Linearly map x from [in_lo, in_hi] onto [out_lo, out_hi]. Returns out_lo for a
    degenerate input range. Used to convert between source-seconds, source-frames and
    timeline-frames without relying on a separately reported frame rate."""
    if in_hi <= in_lo:
        return out_lo
    return out_lo + (x - in_lo) / (in_hi - in_lo) * (out_hi - out_lo)


def item_silence_on_timeline(segments, used_start, used_end, tl_start, tl_end):
    """Map a clip's silent source-second segments to rounded timeline-frame intervals."""
    intervals = []
    for seg in segments:
        if not seg["silent"]:
            continue
        a = round(interpolate(seg["start"], used_start, used_end, tl_start, tl_end))
        b = round(interpolate(seg["end"], used_start, used_end, tl_start, tl_end))
        if b > a:
            intervals.append((a, b))
    return intervals


def split_item(tl_start, tl_end, src_start, src_end, cut_points, silence_intervals):
    """Split one timeline item (in relative timeline frames) at the cut points that fall
    inside it. Returns a list of sub-segments: {"record", "startFrame", "endFrame",
    "silent"}, with `endFrame` inclusive (Resolve's AppendToTimeline convention) and the
    final sub-segment ending exactly on the item's source end so its tail is preserved."""
    bounds = [tl_start] + [c for c in cut_points if tl_start < c < tl_end] + [tl_end]
    subs = []
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1]
        start_frame = round(interpolate(a, tl_start, tl_end, src_start, src_end))
        if b == tl_end:
            end_frame = src_end
        else:
            end_frame = round(interpolate(b, tl_start, tl_end, src_start, src_end)) - 1
        start_frame = max(src_start, min(start_frame, src_end))
        end_frame = max(start_frame, min(end_frame, src_end))
        subs.append({
            "record": a,
            "startFrame": int(start_frame),
            "endFrame": int(end_frame),
            "silent": point_in_intervals((a + b) / 2.0, silence_intervals),
        })
    return subs


def parse_fps(value):
    """Best-effort float() of a frame-rate string/number; None if unparseable."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


# ===========================================================================
# ffmpeg integration
# ===========================================================================

def find_ffmpeg(explicit_path=None):
    """Locate an ffmpeg executable. Checks an explicit path, PATH, then the common
    Homebrew locations. Returns the path or None."""
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    candidates += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run_silencedetect(ffmpeg, path, threshold_db, min_silence):
    """Run ffmpeg silencedetect on `path`. Returns (intervals, has_audio, stderr)."""
    cmd = [
        ffmpeg, "-hide_banner", "-nostats",
        "-i", path,
        "-af", "silencedetect=noise={}dB:d={}".format(threshold_db, min_silence),
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    stderr = proc.stderr or ""
    audio = has_audio_stream(stderr)
    intervals = parse_silencedetect(stderr) if audio else []
    return intervals, audio, stderr


# ===========================================================================
# Resolve orchestration
# ===========================================================================

class Settings(object):
    def __init__(self):
        self.threshold_db = DEFAULT_THRESHOLD_DB
        self.min_silence = DEFAULT_MIN_SILENCE
        self.padding = DEFAULT_PADDING
        # Tracks to analyze for silence as a list of ("video"|"audio", index) tuples.
        # Multiple tracks are combined: a region is silent only where every selected
        # track is silent, so audio is kept whenever anyone is speaking.
        self.analyze_tracks = [("video", 1)]
        self.color_silent = True
        self.add_markers = False
        self.ffmpeg_path = ""


def _track_label(track_type, index):
    return ("V" if track_type == "video" else "A") + str(index)


def _used_seconds(item):
    """Return the clip's used source range in seconds, or None if unavailable."""
    used_start = item.GetSourceStartTime()
    used_end = item.GetSourceEndTime()
    if used_start is None or used_end is None or used_end <= used_start:
        return None
    return used_start, used_end


def analyze_track(timeline, track_type, index, orig_start, content_end,
                  settings, ffmpeg, cache):
    """Return merged silent timeline-frame intervals (relative to the timeline start)
    for one track. Clip gaps (no media = no audio) and clips with no audio stream are
    handled, and silence detected within clips is mapped onto the timeline."""
    detected = []
    coverage = []
    for item in timeline.GetItemListInTrack(track_type, index):
        tl_start = item.GetStart() - orig_start
        tl_end = item.GetEnd() - orig_start
        coverage.append((tl_start, tl_end))
        mp_item = item.GetMediaPoolItem()
        used = _used_seconds(item)
        if not mp_item or used is None:
            continue  # leave this clip as speech (don't add to silence)
        path = mp_item.GetClipProperty("File Path")
        if path in cache:
            intervals, audio = cache[path]
        else:
            intervals, audio, _ = run_silencedetect(
                ffmpeg, path, settings.threshold_db, settings.min_silence)
            cache[path] = (intervals, audio)
        if not audio:
            continue  # no audio stream -> treat the whole clip as speech
        segments = compute_segments(used[0], used[1], intervals, settings.padding)
        detected.extend(item_silence_on_timeline(
            segments, used[0], used[1], tl_start, tl_end))
    gaps = complement_intervals(merge_intervals(coverage), 0, content_end)
    return merge_intervals(detected + gaps)


def ensure_track_count(timeline, track_type, target, source_timeline=None):
    """Add tracks so `timeline` has at least `target` tracks of the given type, matching
    audio sub-types from `source_timeline` where possible."""
    while timeline.GetTrackCount(track_type) < target:
        next_index = timeline.GetTrackCount(track_type) + 1
        if track_type == "audio" and source_timeline is not None:
            subtype = source_timeline.GetTrackSubType("audio", next_index) or "stereo"
            timeline.AddTrack("audio", subtype)
        else:
            timeline.AddTrack(track_type)


def process_timeline(resolve, settings, log):
    """Detect silence on the selected analysis track(s), combine it, and build a new
    split timeline that reproduces every track cut at the silence boundaries.
    `log` is a callable taking one string. Returns True on success."""
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    if not ffmpeg:
        log("ERROR: ffmpeg not found. Install it with:  brew install ffmpeg")
        log("Or set the ffmpeg path field to your ffmpeg binary.")
        return False
    log("Using ffmpeg: " + ffmpeg)

    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        log("ERROR: No project is open.")
        return False
    timeline = project.GetCurrentTimeline()
    if not timeline:
        log("ERROR: No timeline is open.")
        return False
    if not settings.analyze_tracks:
        log("ERROR: Select at least one track to analyze.")
        return False

    timeline_fps = parse_fps(project.GetSetting("timelineFrameRate")) or 24.0
    orig_start = timeline.GetStartFrame()
    v_count = timeline.GetTrackCount("video")
    a_count = timeline.GetTrackCount("audio")

    # Overall content range across every track, in timeline frames relative to start.
    content_end = 0
    for track_type, count in (("video", v_count), ("audio", a_count)):
        for index in range(1, count + 1):
            for item in timeline.GetItemListInTrack(track_type, index):
                content_end = max(content_end, item.GetEnd() - orig_start)
    if content_end <= 0:
        log("ERROR: Timeline has no clips.")
        return False

    media_pool = project.GetMediaPool()
    cache = {}  # file path -> (silence intervals, has_audio)

    # Detect silence per selected track, then combine: a region is silent only where
    # every analyzed track is silent (so audio is kept whenever anyone is speaking).
    log("Analyzing track(s): " + ", ".join(
        _track_label(t, i) for t, i in settings.analyze_tracks) + " ...")
    combined = None
    for track_type, index in settings.analyze_tracks:
        limit = v_count if track_type == "video" else a_count
        if index < 1 or index > limit:
            log("  - skipping {} (no such track).".format(_track_label(track_type, index)))
            continue
        track_silence = analyze_track(
            timeline, track_type, index, orig_start, content_end, settings, ffmpeg, cache)
        log("  - {}: {} silent region(s).".format(
            _track_label(track_type, index), len(track_silence)))
        if combined is None:
            combined = track_silence
        else:
            combined = merge_intervals(intersect_intervals(combined, track_silence))
    if combined is None:
        log("ERROR: No valid analysis tracks selected.")
        return False

    total_silence_frames = sum(end - start for start, end in combined)
    cut_points = interior_boundaries(combined, 0, content_end)

    new_name = timeline.GetName() + " - Split"
    new_timeline = media_pool.CreateEmptyTimeline(new_name)
    if not new_timeline:
        log("ERROR: Could not create timeline '{}'.".format(new_name))
        return False
    project.SetCurrentTimeline(new_timeline)
    ensure_track_count(new_timeline, "video", v_count)
    ensure_track_count(new_timeline, "audio", a_count, timeline)
    offset = new_timeline.GetStartFrame()

    # Reproduce every track, splitting each clip at the combined silence boundaries.
    clip_dicts = []
    for track_type, count, media_type in (("video", v_count, 1), ("audio", a_count, 2)):
        for index in range(1, count + 1):
            for item in timeline.GetItemListInTrack(track_type, index):
                mp_item = item.GetMediaPoolItem()
                if not mp_item:
                    continue
                subs = split_item(
                    item.GetStart() - orig_start, item.GetEnd() - orig_start,
                    item.GetSourceStartFrame(), item.GetSourceEndFrame(),
                    cut_points, combined)
                for sub in subs:
                    clip_dicts.append({
                        "mediaPoolItem": mp_item,
                        "startFrame": sub["startFrame"],
                        "endFrame": sub["endFrame"],
                        "recordFrame": sub["record"] + offset,
                        "trackIndex": index,
                        "mediaType": media_type,
                    })
    if not clip_dicts:
        log("Nothing to rebuild.")
        return False
    if not media_pool.AppendToTimeline(clip_dicts):
        log("ERROR: AppendToTimeline failed.")
        return False

    # Mark silent clips by testing each rebuilt clip's timeline midpoint against the
    # combined silence -- robust regardless of AppendToTimeline's return shape.
    marked = 0
    if settings.color_silent or settings.add_markers:
        for track_type, count in (("video", v_count), ("audio", a_count)):
            for index in range(1, count + 1):
                for ti in new_timeline.GetItemListInTrack(track_type, index):
                    mid = (ti.GetStart() + ti.GetEnd()) / 2.0 - offset
                    if not point_in_intervals(mid, combined):
                        continue
                    if settings.color_silent:
                        ti.SetClipColor(SILENT_CLIP_COLOR)
                    if settings.add_markers:
                        try:
                            ti.AddMarker(0, "Blue", "Silence", "Low audio level", 1)
                        except Exception:
                            pass
                    marked += 1

    log("")
    log("Done. Created timeline: '{}'".format(new_name))
    log("Split into {} clip(s) across {} video / {} audio track(s); {} marked silent."
        .format(len(clip_dicts), v_count, a_count, marked))
    log("Detected ~{:.1f}s of removable silence."
        .format(total_silence_frames / timeline_fps))
    if settings.color_silent:
        log("Tip: select the {} clips, then ripple-delete to remove the dead air."
            .format(SILENT_CLIP_COLOR.lower()))
    return True


# ===========================================================================
# Bootstrap + UI
# ===========================================================================

def get_resolve():
    """Return the Resolve scripting object, or None if it cannot be reached."""
    try:
        import DaVinciResolveScript as bmd
    except ImportError:
        module_path = ("/Library/Application Support/Blackmagic Design/"
                       "DaVinci Resolve/Developer/Scripting/Modules/")
        sys.path.append(module_path)
        try:
            import DaVinciResolveScript as bmd
        except ImportError:
            return None, None
    return bmd.scriptapp("Resolve"), bmd


def _track_checkbox_specs(timeline):
    """Build (checkbox-id, label, track_type, index) specs for every track on the
    timeline. Video tracks are analyzed via their embedded audio."""
    specs = []
    if timeline:
        for i in range(1, timeline.GetTrackCount("video") + 1):
            specs.append(("trk_video_%d" % i, "V%d  (embedded audio)" % i, "video", i))
        for i in range(1, timeline.GetTrackCount("audio") + 1):
            name = timeline.GetTrackName("audio", i) or ""
            label = ("A%d  %s" % (i, name)).rstrip()
            specs.append(("trk_audio_%d" % i, label, "audio", i))
    return specs


def launch_ui(resolve, bmd):
    """Build and run the UIManager settings dialog."""
    fusion = resolve.Fusion()
    ui = fusion.UIManager
    dispatcher = bmd.UIDispatcher(ui)

    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    track_specs = _track_checkbox_specs(timeline)
    detected_ffmpeg = find_ffmpeg() or ""

    # One checkbox per track; default to the first video track (else the first track).
    track_checkboxes = []
    for offset, (cid, label, _ttype, _idx) in enumerate(track_specs):
        track_checkboxes.append(ui.CheckBox({
            "ID": cid, "Text": label, "Checked": offset == 0, "Weight": 0}))
    if not track_checkboxes:
        track_checkboxes.append(ui.Label({"Text": "(no timeline open)"}))

    win = dispatcher.AddWindow({
        "ID": "TrimWin",
        "WindowTitle": "Trim Timelapse Silence",
        "Geometry": [200, 200, 500, 600],
    }, [
        ui.VGroup([
            ui.Label({"Text": "Split the timeline at speech/silence boundaries.",
                      "WordWrap": True, "Weight": 0}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Silence threshold (dB):", "MinimumSize": [180, 0]}),
                ui.LineEdit({"ID": "Threshold", "Text": str(DEFAULT_THRESHOLD_DB)}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Min silence length (s):", "MinimumSize": [180, 0]}),
                ui.LineEdit({"ID": "MinSilence", "Text": str(DEFAULT_MIN_SILENCE)}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Padding around speech (s):", "MinimumSize": [180, 0]}),
                ui.LineEdit({"ID": "Padding", "Text": str(DEFAULT_PADDING)}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "ffmpeg path:", "MinimumSize": [180, 0]}),
                ui.LineEdit({"ID": "Ffmpeg", "Text": detected_ffmpeg,
                             "PlaceholderText": "auto-detected"}),
            ]),
            ui.Label({"Text": "Analyze these track(s) for silence "
                              "(multiple = silent only where all are silent):",
                      "WordWrap": True, "Weight": 0}),
            ui.VGroup({"Weight": 0}, track_checkboxes),
            ui.CheckBox({"ID": "Color", "Text": "Color silent clips orange",
                         "Checked": True, "Weight": 0}),
            ui.CheckBox({"ID": "Markers", "Text": "Add a marker on each silent clip",
                         "Checked": False, "Weight": 0}),
            ui.Label({"Text": "Log:", "Weight": 0}),
            ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1}),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "Run", "Text": "Run"}),
                ui.Button({"ID": "Close", "Text": "Close"}),
            ]),
        ]),
    ])

    items = win.GetItems()

    def log(message):
        items["Log"].PlainText = items["Log"].PlainText + message + "\n"
        print(message)

    def on_close(ev):
        dispatcher.ExitLoop()

    def on_run(ev):
        items["Log"].PlainText = ""
        settings = Settings()
        settings.threshold_db = parse_fps(items["Threshold"].Text)
        settings.min_silence = parse_fps(items["MinSilence"].Text)
        settings.padding = parse_fps(items["Padding"].Text)
        if settings.threshold_db is None or settings.min_silence is None \
                or settings.padding is None:
            log("ERROR: Threshold, min silence and padding must be numbers.")
            return
        settings.analyze_tracks = [
            (ttype, idx) for cid, _label, ttype, idx in track_specs
            if items[cid].Checked]
        if not settings.analyze_tracks:
            log("ERROR: Select at least one track to analyze.")
            return
        settings.color_silent = items["Color"].Checked
        settings.add_markers = items["Markers"].Checked
        settings.ffmpeg_path = items["Ffmpeg"].Text.strip()
        items["Run"].Enabled = False
        try:
            process_timeline(resolve, settings, log)
        except Exception as exc:  # surface unexpected errors in the log
            log("ERROR: " + str(exc))
        finally:
            items["Run"].Enabled = True

    win.On.TrimWin.Close = on_close
    win.On.Close.Clicked = on_close
    win.On.Run.Clicked = on_run

    win.Show()
    dispatcher.RunLoop()
    win.Hide()


def cli_analyze(path):
    """Command-line debug mode: print detected segments for a single file."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg not found. Install it with:  brew install ffmpeg")
        return 1
    intervals, audio, stderr = run_silencedetect(
        ffmpeg, path, DEFAULT_THRESHOLD_DB, DEFAULT_MIN_SILENCE)
    if not audio:
        print("No audio stream detected in: " + path)
        return 1
    duration = parse_duration(stderr) or (max(e for _, e in intervals) if intervals else 0)
    segments = compute_segments(0.0, duration, intervals, DEFAULT_PADDING)
    print("File: {}  (duration {:.2f}s)".format(path, duration))
    print("Detected {} silence interval(s) at {}dB / >= {}s:".format(
        len(intervals), DEFAULT_THRESHOLD_DB, DEFAULT_MIN_SILENCE))
    for start, end in intervals:
        print("  silence {:.2f} -> {:.2f}".format(start, end))
    print("Segments after {}s padding:".format(DEFAULT_PADDING))
    for seg in segments:
        label = "SILENCE" if seg["silent"] else "speech "
        print("  [{}] {:.2f} -> {:.2f}  ({:.2f}s)".format(
            label, seg["start"], seg["end"], seg["end"] - seg["start"]))
    return 0


def main():
    if "--analyze" in sys.argv:
        idx = sys.argv.index("--analyze")
        if idx + 1 >= len(sys.argv):
            print("Usage: trim_timelapse_silence.py --analyze <file>")
            return 1
        return cli_analyze(sys.argv[idx + 1])

    resolve, bmd = get_resolve()
    if not resolve:
        print("Could not connect to DaVinci Resolve. Run this from within Resolve "
              "(Workspace > Scripts), or use --analyze <file> for offline testing.")
        return 1
    launch_ui(resolve, bmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
