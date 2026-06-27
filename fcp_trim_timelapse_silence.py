#!/usr/bin/env python3
"""
Trim Timelapse Silence - a Final Cut Pro companion tool.

Final Cut Pro has no in-app scripting API (no razor tool, clip color, or audio sample
access the way DaVinci Resolve Studio exposes), so automation happens through its
FCPXML interchange format. This tool is the Final Cut Pro counterpart of the Resolve
plugin in this repo: it reads a project exported as FCPXML, analyzes the audio of each
clip on the primary storyline (via ffmpeg's `silencedetect`), and rebuilds the storyline
so every speech/silence boundary becomes a real edit point. Nothing is removed by default
-- this is a "split, not cut" operation -- and each silent segment is tagged with a
"Silence" keyword and a to-do marker so you can find and ripple-delete the dead air.

The audio analysis and segment math are imported wholesale from `trim_timelapse_silence.py`
so the Resolve plugin and this tool detect silence identically.

Workflow
--------
1. In Final Cut Pro, select your project (or timeline) and choose:
      File > Export XML...            -> save e.g.  MyTimelapse.fcpxml
2. Run this tool:
      python3 fcp_trim_timelapse_silence.py MyTimelapse.fcpxml
   It writes  "MyTimelapse - Split.fcpxml"  next to the input.
3. Back in Final Cut Pro:
      File > Import > XML...           -> pick the "- Split" file
   This creates a new project; your original is untouched. Open the Timeline Index
   (Cmd-Shift-2) > Tags to jump between the "Silence" keywords/markers, or select the
   silent ranges and ripple-delete (Shift-Delete) to compress the recording.

   Or pass --remove to have the tool drop the silent segments and ripple the storyline
   for you, producing an already-compressed project.

Requires ffmpeg on PATH (or in /opt/homebrew/bin or /usr/local/bin):  brew install ffmpeg
"""

import argparse
import copy
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from fractions import Fraction

# Reuse the *exact* audio-analysis + segmentation logic from the Resolve plugin so both
# tools agree on what counts as silence. These functions have no Resolve dependency.
import trim_timelapse_silence as core

# Child element tags that carry their own start time (markers/keywords/ratings) rather
# than being whole-clip settings. They are redistributed to the split piece that contains
# their start time.
MARK_TAGS = {"marker", "chapter-marker", "keyword", "rating", "analysis-marker"}


# ===========================================================================
# Pure logic (no ffmpeg / FCP dependency) -- unit tested in tests/test_fcpxml.py
# ===========================================================================

def parse_time(value):
    """Parse an FCPXML time value (e.g. "600/30s", "3s", "0s", None) into a Fraction of
    seconds. FCPXML expresses every time as an exact rational with an `s` suffix."""
    if value is None:
        return Fraction(0)
    text = value.strip()
    if text.endswith("s"):
        text = text[:-1]
    if not text:
        return Fraction(0)
    if "/" in text:
        num, den = text.split("/", 1)
        return Fraction(int(num), int(den))
    return Fraction(text)  # handles "3" and "3.5"


def format_time(frac):
    """Render a Fraction of seconds back as an FCPXML time string. Whole seconds are
    written as "<n>s"; everything else as the exact rational "<num>/<den>s"."""
    frac = Fraction(frac)
    if frac == 0:
        return "0s"
    if frac.denominator == 1:
        return "%ds" % frac.numerator
    return "%d/%ds" % (frac.numerator, frac.denominator)


def snap_seconds(seconds, frame_dur):
    """Snap a time in seconds (float or Fraction) to the nearest whole frame on the given
    frame-duration grid, returning an exact Fraction. Frame alignment keeps every cut on a
    real frame boundary and removes ffmpeg's sub-frame float wobble. With no grid the value
    is returned as a bounded-denominator Fraction."""
    if not frame_dur:
        return Fraction(seconds).limit_denominator(1000000)
    frames = round(float(seconds) / float(frame_dur))
    return frames * frame_dur


def build_subclips(segments, asset_start, clip_start, clip_offset, frame_dur):
    """Turn a clip's speech/silence `segments` (from core.compute_segments, in file seconds)
    into a list of frame-aligned sub-clips.

    Each segment's file-second bounds are converted to the clip's source timebase
    (source_time = asset_start + file_seconds), snapped to the frame grid, and paired with
    the timeline offset that keeps the piece in place (offset = clip_offset + (start -
    clip_start)). Pieces that collapse to zero length after snapping are dropped; because
    adjacent segments share a boundary they snap identically, so the survivors still tile
    the clip with no gaps or overlaps.

    Returns a list of dicts: {"start", "duration", "offset" (Fractions), "silent" (bool)}.
    """
    asset_start_f = float(asset_start)
    subs = []
    for seg in segments:
        start = snap_seconds(asset_start_f + seg["start"], frame_dur)
        end = snap_seconds(asset_start_f + seg["end"], frame_dur)
        if end <= start:
            continue
        subs.append({
            "start": start,
            "duration": end - start,
            "offset": clip_offset + (start - clip_start),
            "silent": bool(seg["silent"]),
        })
    return subs


def file_url_to_path(src):
    """Convert an FCPXML media-rep src (a file:// URL) into a local filesystem path."""
    if not src:
        return None
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme and parsed.scheme != "file":
        return None
    return urllib.parse.unquote(parsed.path)


# ===========================================================================
# FCPXML traversal helpers
# ===========================================================================

def load_document(path):
    """Parse an .fcpxml file (or the Info.fcpxml inside an .fcpxmld bundle). Returns the
    ElementTree root <fcpxml> element."""
    if os.path.isdir(path):  # .fcpxmld bundle
        path = os.path.join(path, "Info.fcpxml")
    tree = ET.parse(path)
    return tree.getroot()


def index_by_id(root, tag):
    """Map id -> element for every `tag` element in the document (assets, formats)."""
    return {el.get("id"): el for el in root.iter(tag) if el.get("id")}


def asset_media_path(asset):
    """Return the local media path for an <asset>, preferring its original media-rep."""
    chosen = None
    for rep in asset.findall("media-rep"):
        if rep.get("kind") == "original-media":
            chosen = rep
            break
    if chosen is None:
        chosen = asset.find("media-rep")
    if chosen is None:
        return None
    return file_url_to_path(chosen.get("src"))


def has_connected_children(clip):
    """True if the clip contains timed connected items (children with their own `offset`),
    e.g. connected clips, titles, or secondary storylines. Such clips are copied through
    unsplit so we never emit invalid connected-item geometry."""
    return any(child.get("offset") is not None for child in clip)


# ===========================================================================
# Rebuild
# ===========================================================================

class Settings(object):
    def __init__(self):
        self.threshold_db = core.DEFAULT_THRESHOLD_DB
        self.min_silence = core.DEFAULT_MIN_SILENCE
        self.padding = core.DEFAULT_PADDING
        self.ffmpeg_path = ""
        self.add_keyword = True
        self.add_marker = True
        self.remove = False      # drop silent segments and ripple the storyline
        self.dry_run = False


def make_subclip_element(orig, sub, frame_dur, settings):
    """Build one <asset-clip> for a split piece: copy the original's attributes (except the
    timing ones we override) and its whole-clip children (filters, adjustments, audio
    config), redistribute any pre-existing markers/keywords to the piece that contains them,
    and tag silent pieces with a "Silence" keyword and to-do marker."""
    el = ET.Element("asset-clip")
    for key, value in orig.attrib.items():
        if key in ("offset", "start", "duration"):
            continue
        el.set(key, value)
    el.set("offset", format_time(sub["offset"]))
    el.set("start", format_time(sub["start"]))
    el.set("duration", format_time(sub["duration"]))

    sub_end = sub["start"] + sub["duration"]
    for child in orig:
        if child.get("offset") is not None:
            continue  # connected item -- not reached (such clips are not split)
        if child.tag in MARK_TAGS:
            at = parse_time(child.get("start", "0s"))
            if sub["start"] <= at < sub_end:
                el.append(copy.deepcopy(child))
            continue
        el.append(copy.deepcopy(child))  # whole-clip setting -> every piece

    if sub["silent"]:
        if settings.add_keyword:
            kw = ET.SubElement(el, "keyword")
            kw.set("start", format_time(sub["start"]))
            kw.set("duration", format_time(sub["duration"]))
            kw.set("value", "Silence")
        if settings.add_marker:
            mk = ET.SubElement(el, "marker")
            mk.set("start", format_time(sub["start"]))
            mk.set("duration", format_time(frame_dur))
            mk.set("value", "Silence")
            mk.set("completed", "0")
    return el


def process_clip(child, assets, frame_dur, settings, ffmpeg, cache, log):
    """Analyze one spine <asset-clip> and return (pieces, silent_seconds) where `pieces` is
    the list of replacement elements (one if unsplit). Returns ([child], 0) unchanged when
    the clip has no analyzable audio or carries connected items."""
    asset = assets.get(child.get("ref"))
    if asset is None or asset.get("hasAudio") != "1":
        return [child], Fraction(0)

    path = asset_media_path(asset)
    if not path:
        log("  - %s: no media path; left whole." % (child.get("name") or child.get("ref")))
        return [child], Fraction(0)

    if path in cache:
        intervals, audio = cache[path]
    else:
        intervals, audio, _ = core.run_silencedetect(
            ffmpeg, path, settings.threshold_db, settings.min_silence)
        cache[path] = (intervals, audio)
    if not audio:
        return [child], Fraction(0)

    asset_start = parse_time(asset.get("start", "0s"))
    clip_start = parse_time(child.get("start", "0s"))
    clip_offset = parse_time(child.get("offset", "0s"))
    clip_dur = parse_time(child.get("duration"))
    file_used_start = float(clip_start - asset_start)
    file_used_end = file_used_start + float(clip_dur)

    segments = core.compute_segments(
        file_used_start, file_used_end, intervals, settings.padding)
    subs = build_subclips(segments, asset_start, clip_start, clip_offset, frame_dur)

    silent_seconds = sum((s["duration"] for s in subs if s["silent"]), Fraction(0))
    name = child.get("name") or child.get("ref")

    if len(subs) <= 1:
        return [child], Fraction(0)  # nothing to split

    if has_connected_children(child):
        log("  - %s: has connected clips; left whole (would need manual cuts)." % name)
        return [child], Fraction(0)

    log("  - %s: split into %d piece(s), %d silent." % (
        name, len(subs), sum(1 for s in subs if s["silent"])))
    pieces = [make_subclip_element(child, sub, frame_dur, settings) for sub in subs]
    return pieces, silent_seconds


def process_document(root, settings, log):
    """Detect silence on the primary storyline and rebuild it, splitting every clip at the
    speech/silence boundaries. Mutates `root` in place. Returns True on success."""
    ffmpeg = core.find_ffmpeg(settings.ffmpeg_path)
    if not ffmpeg:
        log("ERROR: ffmpeg not found. Install it with:  brew install ffmpeg")
        log("Or pass --ffmpeg /path/to/ffmpeg.")
        return False
    log("Using ffmpeg: " + ffmpeg)

    sequence = next(root.iter("sequence"), None)
    if sequence is None:
        log("ERROR: No <sequence> found. Export a project/timeline as FCPXML.")
        return False
    spine = sequence.find("spine")
    if spine is None:
        log("ERROR: Sequence has no <spine>.")
        return False

    formats = index_by_id(root, "format")
    assets = index_by_id(root, "asset")
    seq_format = formats.get(sequence.get("format"))
    frame_dur = parse_time(seq_format.get("frameDuration")) if seq_format is not None else None
    if not frame_dur:
        log("WARNING: sequence frame rate unknown; cuts will not be frame-snapped.")

    cache = {}
    log("Analyzing primary storyline clips...")

    # Build the replacement list of spine children, splitting asset-clips and passing
    # everything else (gaps, titles, transitions) through untouched.
    rebuilt = []        # list of (element, is_silent_piece)
    total_silence = Fraction(0)
    for child in list(spine):
        if child.tag == "asset-clip":
            pieces, silence = process_clip(
                child, assets, frame_dur, settings, ffmpeg, cache, log)
            total_silence += silence
            if len(pieces) > 1:
                for piece in pieces:
                    rebuilt.append((piece, _is_silent(piece)))
            else:
                rebuilt.append((pieces[0], False))
        else:
            rebuilt.append((child, False))

    if settings.remove:
        rebuilt = [(el, s) for el, s in rebuilt if not s]
        _reflow_offsets(rebuilt, spine, frame_dur)

    # Swap in the rebuilt children.
    for child in list(spine):
        spine.remove(child)
    for el, _ in rebuilt:
        spine.append(el)

    _rename_project(root, log)
    _recompute_sequence_duration(sequence, spine)

    seconds = float(total_silence)
    if settings.remove:
        log("")
        log("Removed %.1fs of silence and rippled the storyline." % seconds)
    else:
        log("")
        log("Detected ~%.1fs of removable silence (tagged 'Silence')." % seconds)
        log("Tip: in Final Cut Pro open Timeline Index > Tags to find them, or "
            "select the silent ranges and ripple-delete (Shift-Delete).")
    return True


def _is_silent(element):
    """True if a rebuilt asset-clip carries our 'Silence' keyword/marker tag."""
    for child in element:
        if child.tag in ("keyword", "marker") and child.get("value") == "Silence":
            return True
    return False


def _reflow_offsets(rebuilt, spine, frame_dur):
    """For --remove: lay the surviving spine elements out contiguously from the storyline's
    original start, so deleting silent pieces ripples everything up."""
    first = spine.find("*")
    cursor = parse_time(first.get("offset", "0s")) if first is not None else Fraction(0)
    for el, _ in rebuilt:
        el.set("offset", format_time(cursor))
        cursor += parse_time(el.get("duration", "0s"))


def _recompute_sequence_duration(sequence, spine):
    """Set the sequence duration to span the rebuilt spine (offset+duration of the last
    element), keeping FCP's reported timeline length correct."""
    end = Fraction(0)
    for child in spine:
        if child.get("duration") is None:
            continue
        off = parse_time(child.get("offset", "0s"))
        end = max(end, off + parse_time(child.get("duration")))
    if end > 0:
        sequence.set("duration", format_time(end))


def _rename_project(root, log):
    """Append ' - Split' to the project name so the import lands as a separate project."""
    project = next(root.iter("project"), None)
    if project is not None and project.get("name"):
        project.set("name", project.get("name") + " - Split")
    else:
        log("NOTE: no <project> element to rename; importing may add to the current event.")


# ===========================================================================
# Output
# ===========================================================================

def write_document(root, out_path):
    """Serialize the modified tree back to an .fcpxml file, restoring the XML declaration
    and the <!DOCTYPE fcpxml> that Final Cut Pro expects (ElementTree drops both)."""
    if hasattr(ET, "indent"):
        ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write("<!DOCTYPE fcpxml>\n")
        fh.write(body)
        if not body.endswith("\n"):
            fh.write("\n")


def default_output_path(in_path):
    """Derive the "<name> - Split.fcpxml" path next to the input."""
    base = in_path.rstrip("/")
    if base.endswith(".fcpxmld"):
        base = base[: -len(".fcpxmld")]
    elif base.endswith(".fcpxml"):
        base = base[: -len(".fcpxml")]
    return base + " - Split.fcpxml"


# ===========================================================================
# CLI
# ===========================================================================

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Split a Final Cut Pro timeline (FCPXML) at speech/silence boundaries "
                    "and tag the silent ranges.")
    p.add_argument("input", help="Project exported from Final Cut Pro as .fcpxml "
                                  "(or an .fcpxmld bundle).")
    p.add_argument("output", nargs="?", help="Output .fcpxml path "
                                             "(default: '<input> - Split.fcpxml').")
    p.add_argument("--threshold-db", type=float, default=core.DEFAULT_THRESHOLD_DB,
                   help="Audio below this level (dB) counts as silence "
                        "(default %(default)s).")
    p.add_argument("--min-silence", type=float, default=core.DEFAULT_MIN_SILENCE,
                   help="Ignore silences shorter than this many seconds "
                        "(default %(default)s).")
    p.add_argument("--padding", type=float, default=core.DEFAULT_PADDING,
                   help="Seconds of silence kept around speech (default %(default)s).")
    p.add_argument("--ffmpeg", default="", help="Path to ffmpeg if not auto-detected.")
    p.add_argument("--remove", action="store_true",
                   help="Drop the silent segments and ripple the storyline (default: keep "
                        "and tag them for manual review).")
    p.add_argument("--no-keyword", action="store_true",
                   help="Do not add a 'Silence' keyword to silent segments.")
    p.add_argument("--no-marker", action="store_true",
                   help="Do not add a to-do marker to silent segments.")
    p.add_argument("--dry-run", action="store_true",
                   help="Analyze and report, but do not write an output file.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    settings.threshold_db = args.threshold_db
    settings.min_silence = args.min_silence
    settings.padding = args.padding
    settings.ffmpeg_path = args.ffmpeg
    settings.add_keyword = not args.no_keyword
    settings.add_marker = not args.no_marker
    settings.remove = args.remove
    settings.dry_run = args.dry_run

    if not os.path.exists(args.input):
        print("ERROR: input not found: " + args.input)
        return 1

    try:
        root = load_document(args.input)
    except ET.ParseError as exc:
        print("ERROR: could not parse FCPXML: " + str(exc))
        return 1

    def log(message):
        print(message)

    if not process_document(root, settings, log):
        return 1

    if settings.dry_run:
        log("")
        log("Dry run -- no file written.")
        return 0

    out_path = args.output or default_output_path(args.input)
    write_document(root, out_path)
    log("")
    log("Wrote: " + out_path)
    log("Import it in Final Cut Pro:  File > Import > XML...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
