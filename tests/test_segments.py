#!/usr/bin/env python3
"""Unit tests for the pure logic in trim_timelapse_silence.py.

These exercise ffmpeg-output parsing, per-clip silence segmentation, the timeline
interval algebra used to combine multiple tracks, and item splitting -- without
needing DaVinci Resolve or ffmpeg installed.  Run:  python3 tests/test_segments.py
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trim_timelapse_silence as t

_failures = []


def check(name, condition, detail=""):
    if condition:
        print("ok   - " + name)
    else:
        print("FAIL - " + name + ("  (" + detail + ")" if detail else ""))
        _failures.append(name)


# --- parse_silencedetect --------------------------------------------------

SAMPLE_STDERR = """
Input #0, mov, from 'clip.mov':
  Duration: 00:10:00.50, start: 0.000000, bitrate: 12000 kb/s
  Stream #0:0[0x1]: Video: h264 (High), yuv420p, 1920x1080, 24 fps
  Stream #0:1[0x2]: Audio: aac (LC), 48000 Hz, stereo, fltp, 192 kb/s
[silencedetect @ 0x600] silence_start: 3.0
[silencedetect @ 0x600] silence_end: 6.0 | silence_duration: 3.0
[silencedetect @ 0x600] silence_start: 20.5
[silencedetect @ 0x600] silence_end: 25.25 | silence_duration: 4.75
[silencedetect @ 0x600] silence_start: 595.0
"""


def test_parse_silencedetect():
    intervals = t.parse_silencedetect(SAMPLE_STDERR)
    check("parse: three intervals", len(intervals) == 3, str(intervals))
    check("parse: first interval", intervals[0] == (3.0, 6.0), str(intervals[0]))
    check("parse: second interval", intervals[1] == (20.5, 25.25), str(intervals[1]))
    check("parse: dangling end is inf", math.isinf(intervals[2][1]), str(intervals[2]))
    check("parse: dangling start", intervals[2][0] == 595.0, str(intervals[2]))


def test_negative_silence_start_clamped():
    intervals = t.parse_silencedetect("silence_start: -0.01\nsilence_end: 2.0\n")
    check("parse: negative start clamped to 0", intervals == [(0.0, 2.0)], str(intervals))


def test_has_audio_and_duration():
    check("audio: detected", t.has_audio_stream(SAMPLE_STDERR))
    check("audio: absent", not t.has_audio_stream("Stream #0:0: Video: h264"))
    check("duration: parsed",
          abs(t.parse_duration(SAMPLE_STDERR) - 600.5) < 1e-6,
          str(t.parse_duration(SAMPLE_STDERR)))
    check("duration: missing -> None", t.parse_duration("no duration here") is None)


# --- compute_segments -----------------------------------------------------

def labels(segs):
    return [("S" if s["silent"] else ".", round(s["start"], 3), round(s["end"], 3))
            for s in segs]


def test_segments_basic():
    segs = t.compute_segments(0.0, 10.0, [(3.0, 6.0)], 0.2)
    check("segments: basic count", len(segs) == 3, str(labels(segs)))
    check("segments: speech/silence/speech order",
          [s["silent"] for s in segs] == [False, True, False], str(labels(segs)))
    check("segments: padded silence bounds",
          abs(segs[1]["start"] - 3.2) < 1e-9 and abs(segs[1]["end"] - 5.8) < 1e-9,
          str(labels(segs)))
    check("segments: contiguous & cover range",
          segs[0]["start"] == 0.0 and segs[-1]["end"] == 10.0
          and segs[0]["end"] == segs[1]["start"] and segs[1]["end"] == segs[2]["start"],
          str(labels(segs)))


def test_segments_short_silence_dropped():
    segs = t.compute_segments(0.0, 10.0, [(3.0, 3.3)], 0.2)
    check("segments: short silence dropped",
          len(segs) == 1 and not segs[0]["silent"], str(labels(segs)))


def test_segments_no_silence():
    segs = t.compute_segments(2.0, 8.0, [], 0.2)
    check("segments: no silence -> single speech segment",
          segs == [{"start": 2.0, "end": 8.0, "silent": False}], str(segs))


def test_segments_clip_to_used_range():
    segs = t.compute_segments(5.0, 20.0, [(1.0, 50.0)], 0.2)
    check("segments: fully-covered clip -> single flush silent segment",
          len(segs) == 1 and segs[0]["silent"]
          and segs[0]["start"] == 5.0 and segs[0]["end"] == 20.0,
          str(labels(segs)))


def test_segments_dangling_inf_clipped():
    segs = t.compute_segments(0.0, 10.0, [(7.0, float("inf"))], 0.2)
    check("segments: inf silence padded at start, flush at used_end",
          len(segs) == 2 and segs[-1]["silent"]
          and abs(segs[-1]["start"] - 7.2) < 1e-9 and segs[-1]["end"] == 10.0,
          str(labels(segs)))


# --- interval algebra -----------------------------------------------------

def test_merge_intervals():
    check("merge: overlapping merged",
          t.merge_intervals([(0, 5), (3, 8)]) == [(0, 8)])
    check("merge: touching merged",
          t.merge_intervals([(0, 5), (5, 8)]) == [(0, 8)])
    check("merge: disjoint kept & sorted",
          t.merge_intervals([(10, 12), (0, 3)]) == [(0, 3), (10, 12)])
    check("merge: zero-length dropped",
          t.merge_intervals([(4, 4), (1, 2)]) == [(1, 2)])


def test_intersect_intervals():
    a = [(0, 10), (20, 30)]
    b = [(5, 25)]
    check("intersect: basic overlap",
          t.intersect_intervals(a, b) == [(5, 10), (20, 25)], str(t.intersect_intervals(a, b)))
    check("intersect: disjoint -> empty",
          t.intersect_intervals([(0, 5)], [(10, 15)]) == [])


def test_complement_intervals():
    check("complement: gaps between coverage",
          t.complement_intervals([(2, 4), (6, 8)], 0, 10) == [(0, 2), (4, 6), (8, 10)],
          str(t.complement_intervals([(2, 4), (6, 8)], 0, 10)))
    check("complement: full coverage -> empty",
          t.complement_intervals([(0, 10)], 0, 10) == [])
    check("complement: no coverage -> whole range",
          t.complement_intervals([], 0, 10) == [(0, 10)])


def test_point_in_intervals():
    ivals = [(0, 5), (10, 15)]
    check("point: inside", t.point_in_intervals(3, ivals))
    check("point: on end is exclusive", not t.point_in_intervals(5, ivals))
    check("point: on start is inclusive", t.point_in_intervals(10, ivals))
    check("point: outside", not t.point_in_intervals(7, ivals))


def test_interior_boundaries():
    check("boundaries: interior edges only",
          t.interior_boundaries([(0, 5), (8, 10)], 0, 10) == [5, 8],
          str(t.interior_boundaries([(0, 5), (8, 10)], 0, 10)))


def test_interpolate():
    check("interpolate: midpoint", t.interpolate(5, 0, 10, 100, 200) == 150)
    check("interpolate: degenerate range -> out_lo", t.interpolate(5, 4, 4, 100, 200) == 100)


# --- mapping silence onto the timeline ------------------------------------

def test_item_silence_on_timeline():
    # Clip uses source seconds 0..10 placed on timeline frames 1000..1240 (24fps span).
    segs = t.compute_segments(0.0, 10.0, [(3.0, 6.0)], 0.2)  # silent 3.2..5.8s
    ivals = t.item_silence_on_timeline(segs, 0.0, 10.0, 1000, 1240)
    # 3.2/10 * 240 + 1000 = 1076.8 -> 1077 ; 5.8/10 * 240 + 1000 = 1139.2 -> 1139
    check("map: one silent interval", len(ivals) == 1, str(ivals))
    check("map: rounded timeline frames", ivals == [(1077, 1139)], str(ivals))


# --- split_item -----------------------------------------------------------

def test_split_item_inclusive_and_tail():
    # Item on timeline frames 0..240, source frames 0..240 (ratio 1), one cut at 100.
    silence = [(100, 240)]  # second half silent
    subs = t.split_item(0, 240, 0, 240, [100], silence)
    check("split: two sub-segments", len(subs) == 2, str(subs))
    first, second = subs
    check("split: first record/start", first["record"] == 0 and first["startFrame"] == 0,
          str(first))
    check("split: inclusive endFrame at cut", first["endFrame"] == 99, str(first))
    check("split: second starts at cut", second["startFrame"] == 100, str(second))
    check("split: final sub ends exactly at source end", second["endFrame"] == 240,
          str(second))
    check("split: silent flags via midpoint",
          first["silent"] is False and second["silent"] is True, str(subs))


def test_split_item_no_cuts():
    subs = t.split_item(0, 240, 0, 240, [], [])
    check("split: no cuts -> single whole sub",
          len(subs) == 1 and subs[0]["startFrame"] == 0 and subs[0]["endFrame"] == 240,
          str(subs))


def test_split_item_offset_and_ratio():
    # Item at timeline 100..300 (span 200) over source frames 50..150 (span 100) -> ratio 2,
    # with a cut at timeline frame 200 (= source frame 100).
    subs = t.split_item(100, 300, 50, 150, [200], [(200, 300)])
    check("split: ratio maps cut to source frame",
          subs[0]["startFrame"] == 50 and subs[0]["endFrame"] == 99
          and subs[1]["startFrame"] == 100 and subs[1]["endFrame"] == 150,
          str(subs))
    check("split: record frames in timeline space",
          subs[0]["record"] == 100 and subs[1]["record"] == 200, str(subs))


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
