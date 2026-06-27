#!/usr/bin/env python3
"""Unit tests for fcp_trim_timelapse_silence.py.

These cover FCPXML time parsing/formatting, frame snapping, clip splitting, and a full
in-memory document rebuild -- without needing Final Cut Pro, ffmpeg, or media files
(silence detection is stubbed). Run:  python3 tests/test_fcpxml.py
"""

import os
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fcp_trim_timelapse_silence as f
import trim_timelapse_silence as core

_failures = []


def check(name, condition, detail=""):
    if condition:
        print("ok   - " + name)
    else:
        print("FAIL - " + name + ("  (" + detail + ")" if detail else ""))
        _failures.append(name)


# --- time parse/format ----------------------------------------------------

def test_parse_time():
    check("parse: rational", f.parse_time("600/30s") == Fraction(20))
    check("parse: whole seconds", f.parse_time("3s") == Fraction(3))
    check("parse: zero", f.parse_time("0s") == Fraction(0))
    check("parse: none -> 0", f.parse_time(None) == Fraction(0))
    check("parse: ntsc rational",
          f.parse_time("1001/30000s") == Fraction(1001, 30000))


def test_format_time():
    check("format: zero", f.format_time(Fraction(0)) == "0s")
    check("format: whole", f.format_time(Fraction(3)) == "3s")
    check("format: rational", f.format_time(Fraction(20)) == "20s")
    check("format: keeps denominator",
          f.format_time(Fraction(1001, 30000)) == "1001/30000s")


def test_time_roundtrip():
    for s in ["0s", "3s", "600/30s", "1001/30000s", "3003/30000s"]:
        check("roundtrip: " + s, f.format_time(f.parse_time(s)) == _norm(s), s)


def _norm(s):
    # round-trip normalizes 600/30s -> 20s; compare against re-formatted value
    return f.format_time(f.parse_time(s))


# --- frame snapping -------------------------------------------------------

def test_snap_seconds():
    fd = Fraction(1, 30)  # 30 fps
    check("snap: exact frame stays", f.snap_seconds(1.0, fd) == Fraction(30, 30))
    check("snap: rounds to nearest frame",
          f.snap_seconds(1.02, fd) == Fraction(31, 30),
          str(f.snap_seconds(1.02, fd)))
    check("snap: result is frame-aligned",
          (f.snap_seconds(3.27, fd) / fd).denominator == 1)
    check("snap: no grid -> bounded fraction",
          f.snap_seconds(1.5, None) == Fraction(3, 2))


# --- build_subclips -------------------------------------------------------

def test_build_subclips_basic():
    # Clip: source seconds 0..10 placed at timeline offset 5s, 30fps grid, asset start 0.
    segs = core.compute_segments(0.0, 10.0, [(3.0, 6.0)], 0.2)  # silent 3.2..5.8
    subs = f.build_subclips(segs, Fraction(0), Fraction(0), Fraction(5), Fraction(1, 30))
    check("subclips: three pieces", len(subs) == 3, str(subs))
    check("subclips: speech/silence/speech",
          [s["silent"] for s in subs] == [False, True, False], str(subs))
    # 3.2s -> frame 96 -> 96/30s ; offset = 5 + (96/30 - 0)
    check("subclips: silent start snapped to frame",
          subs[1]["start"] == Fraction(96, 30), str(subs[1]["start"]))
    check("subclips: offset tracks timeline position",
          subs[1]["offset"] == Fraction(5) + Fraction(96, 30), str(subs[1]["offset"]))


def test_build_subclips_contiguous_no_gaps():
    segs = core.compute_segments(0.0, 10.0, [(3.0, 6.0)], 0.2)
    subs = f.build_subclips(segs, Fraction(0), Fraction(0), Fraction(0), Fraction(1, 30))
    for a, b in zip(subs, subs[1:]):
        check("subclips: piece end meets next start",
              a["offset"] + a["duration"] == b["offset"], str((a, b)))
    check("subclips: cover whole clip",
          subs[0]["offset"] == Fraction(0)
          and subs[-1]["offset"] + subs[-1]["duration"] == Fraction(10),
          str(subs))


def test_build_subclips_source_offset():
    # asset.start = 0, clip.start = 2s (an in-trimmed clip): source times are offset by 2s.
    segs = core.compute_segments(2.0, 12.0, [(5.0, 8.0)], 0.2)  # file seconds
    subs = f.build_subclips(segs, Fraction(0), Fraction(2), Fraction(0), Fraction(1, 30))
    check("subclips: first start is the clip in-point",
          subs[0]["start"] == Fraction(2), str(subs[0]["start"]))
    check("subclips: first offset starts at 0 on the timeline",
          subs[0]["offset"] == Fraction(0), str(subs[0]["offset"]))


def test_build_subclips_no_silence_single_piece():
    segs = core.compute_segments(0.0, 10.0, [], 0.2)
    subs = f.build_subclips(segs, Fraction(0), Fraction(0), Fraction(0), Fraction(1, 30))
    check("subclips: no silence -> one speech piece",
          len(subs) == 1 and not subs[0]["silent"], str(subs))


# --- file url ------------------------------------------------------------

def test_file_url_to_path():
    check("url: basic", f.file_url_to_path("file:///Users/me/clip.mov") == "/Users/me/clip.mov")
    check("url: percent-encoded space",
          f.file_url_to_path("file:///Users/me/my%20clip.mov") == "/Users/me/my clip.mov")
    check("url: non-file scheme -> None",
          f.file_url_to_path("http://example.com/x.mov") is None)


# --- end-to-end document rebuild (ffmpeg stubbed) -------------------------

SAMPLE_FCPXML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>
    <asset id="r2" name="timelapse" start="0s" duration="300/30s" hasVideo="1" hasAudio="1"
           audioSources="1" audioChannels="2" audioRate="48000" format="r1">
      <media-rep kind="original-media" src="file:///tmp/timelapse.mov"/>
    </asset>
  </resources>
  <library>
    <event name="Test">
      <project name="My Timelapse">
        <sequence format="r1" duration="300/30s" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
            <asset-clip ref="r2" offset="0s" name="timelapse" start="0s" duration="300/30s" format="r1" tcFormat="NDF"/>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


def _stub_core(monkeypatch_silence):
    """Replace ffmpeg discovery + silencedetect with a fixed result for the 10s asset."""
    f.core.find_ffmpeg = lambda explicit=None: "/usr/bin/true"
    f.core.run_silencedetect = lambda *a, **k: (monkeypatch_silence, True, "")


def _build_root():
    return ET.fromstring(SAMPLE_FCPXML)


def test_document_split_and_tag():
    saved = (core.find_ffmpeg, core.run_silencedetect)
    try:
        # Asset is 10s (300/30). Silence 3..6s -> padded 3.2..5.8.
        _stub_core([(3.0, 6.0)])
        root = _build_root()
        settings = f.Settings()
        ok = f.process_document(root, settings, lambda m: None)
        check("doc: process succeeded", ok)
        spine = next(root.iter("spine"))
        clips = spine.findall("asset-clip")
        check("doc: split into 3 clips", len(clips) == 3, str(len(clips)))
        silent = [c for c in clips if f._is_silent(c)]
        check("doc: exactly one silent clip", len(silent) == 1, str(len(silent)))
        check("doc: silent clip has Silence keyword",
              any(k.get("value") == "Silence" for k in silent[0].findall("keyword")))
        check("doc: silent clip has to-do marker",
              any(m.get("completed") == "0" for m in silent[0].findall("marker")))
        # Pieces tile the original 10s with no gaps/overlaps.
        offs = [f.parse_time(c.get("offset")) for c in clips]
        durs = [f.parse_time(c.get("duration")) for c in clips]
        check("doc: first offset 0", offs[0] == Fraction(0), str(offs[0]))
        check("doc: contiguous",
              offs[1] == offs[0] + durs[0] and offs[2] == offs[1] + durs[1],
              str(list(zip(offs, durs))))
        check("doc: covers full 10s", offs[2] + durs[2] == Fraction(10), str(offs[2] + durs[2]))
        check("doc: project renamed",
              next(root.iter("project")).get("name") == "My Timelapse - Split")
    finally:
        core.find_ffmpeg, core.run_silencedetect = saved


def test_document_remove_ripples():
    saved = (core.find_ffmpeg, core.run_silencedetect)
    try:
        _stub_core([(3.0, 6.0)])
        root = _build_root()
        settings = f.Settings()
        settings.remove = True
        f.process_document(root, settings, lambda m: None)
        spine = next(root.iter("spine"))
        clips = spine.findall("asset-clip")
        check("remove: silent piece dropped -> 2 clips", len(clips) == 2, str(len(clips)))
        check("remove: none marked silent",
              not any(f._is_silent(c) for c in clips))
        offs = [f.parse_time(c.get("offset")) for c in clips]
        durs = [f.parse_time(c.get("duration")) for c in clips]
        check("remove: rippled contiguous from 0",
              offs[0] == Fraction(0) and offs[1] == durs[0], str(list(zip(offs, durs))))
        seq = next(root.iter("sequence"))
        check("remove: sequence duration shrunk",
              f.parse_time(seq.get("duration")) == offs[1] + durs[1],
              seq.get("duration"))
    finally:
        core.find_ffmpeg, core.run_silencedetect = saved


def test_document_no_audio_left_whole():
    saved = (core.find_ffmpeg, core.run_silencedetect)
    try:
        _stub_core([])  # would be ignored; asset.hasAudio flipped off below
        root = _build_root()
        next(root.iter("asset")).set("hasAudio", "0")
        f.process_document(root, settings=f.Settings(), log=lambda m: None)
        spine = next(root.iter("spine"))
        check("no-audio: clip left whole", len(spine.findall("asset-clip")) == 1)
    finally:
        core.find_ffmpeg, core.run_silencedetect = saved


def main():
    for fn in sorted(g for g in globals() if g.startswith("test_")):
        globals()[fn]()
    print()
    if _failures:
        print("{} test(s) FAILED: {}".format(len(_failures), ", ".join(_failures)))
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
