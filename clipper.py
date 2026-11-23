#!/usr/bin/env python3
"""
clipper.py - find the clips hiding inside a long video, from its transcript.

Pure Python 3 standard library. No API key, no network, no dependencies.

    python3 clipper.py transcript.srt --video talk.mp4 --count 8

Reads .srt or .vtt, builds every sentence-aligned window between --min and --max
seconds, scores each one on six properties, keeps the best non-overlapping set,
and writes into --out:

    report.md        the ranked shortlist, with the reasons behind every score
    clips.json       the same data, machine readable
    clip_01.srt ...  one caption file per clip, re-timed to start at zero
    cut.sh           ffmpeg commands (only when --video is given)

The score is a heuristic, not a view-count predictor. It is good at rejecting
clips that start mid-thought, ramble, or never land. It cannot tell you which
good clip will perform. A low score is a reason to look again.
"""
import argparse
import json
import os
import re
import shlex
import sys

# ----------------------------------------------------------------- parsing

TS = re.compile(r"(?:(\d+):)?(\d+):(\d+)[.,](\d+)")


def _sec(m):
    h, mn, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000


def parse_transcript(path):
    raw = open(path, encoding="utf-8-sig", errors="replace").read().replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [l for l in block.strip().split("\n") if l.strip()]
        for k, line in enumerate(lines):
            if "-->" not in line:
                continue
            a, b = line.split("-->", 1)
            ma, mb = TS.search(a), TS.search(b)
            if not (ma and mb):
                break
            text = " ".join(lines[k + 1:])
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            if text and not (cues and cues[-1]["text"] == text):  # rolling-caption dupes
                cues.append({"start": _sec(ma), "end": _sec(mb), "text": text})
            break
    cues.sort(key=lambda c: c["start"])
    return cues


# ----------------------------------------------------------------- lexicon

WEAK_OPEN = {
    "and", "so", "but", "because", "which", "that", "it", "this", "they", "he",
    "she", "then", "also", "anyway", "like", "or", "yeah", "right", "okay",
    "well", "these", "those", "them", "its", "their",
}
FILLER_OPEN = ("um", "uh", "you know", "i mean", "kind of", "sort of")
FILLERS = re.compile(
    r"\b(um+|uh+|er|you know|i mean|basically|literally|sort of|kind of|"
    r"actually|like,)\b", re.I)
POWER = {
    "never", "nobody", "stop", "wrong", "mistake", "mistakes", "secret", "truth",
    "biggest", "worst", "best", "real", "why", "how", "free", "only", "nothing",
    "everything", "fail", "failed", "lost", "hidden", "insane", "crazy",
    "shocking", "myth", "lie", "lies", "expensive", "dangerous", "easy", "simple",
    "wasted", "regret", "unbelievable", "mind", "changed", "except", "instead",
}
CONCLUDE = re.compile(
    r"\b(that'?s why|which means|the point is|bottom line|so the lesson|"
    r"in other words|that'?s the|the takeaway|and that'?s|so if you)\b", re.I)
SEQ_OPEN = re.compile(
    r"^(second(ly)?|third(ly)?|fourth|fifth|next|finally|lastly|number \w+|"
    r"step \w+|point \w+|another|one more|moving on|as i said|like i said)\b", re.I)
BOILER = re.compile(
    r"\b(thanks for (watching|listening|joining)|see you (next|in the)|"
    r"don'?t forget to (like|subscribe)|smash (that|the) (like|subscribe)|"
    r"welcome back|hey (guys|everyone|everybody)|in today'?s video|"
    r"link in the (description|bio)|subscribe)\b", re.I)
TERMINAL = re.compile(r"[.?!\u2026][\"'\u201d)]*$")


def words_of(text):
    return re.findall(r"[A-Za-z0-9'\u2019%$]+", text)


def first_sentence(text):
    parts = re.split(r"(?<=[.?!\u2026])\s+", text, maxsplit=1)
    s = parts[0]
    w = s.split()
    if len(w) > 30:  # unpunctuated captions: take the opening breath
        s = " ".join(w[:14])
    return s


def last_sentence(text):
    parts = re.split(r"(?<=[.?!\u2026])\s+", text.strip())
    return parts[-1] if parts else text


# ----------------------------------------------------------------- scoring

W = {"hook": 35, "density": 15, "payoff": 15, "intensity": 15, "clean": 10, "length": 10}


def score_window(cues, i, j, gap_end):
    seg = cues[i:j + 1]
    text = " ".join(c["text"] for c in seg)
    dur = seg[-1]["end"] - seg[0]["start"]
    ws = words_of(text)
    n = max(1, len(ws))
    reasons = []

    # 1. hook: does the first sentence stand alone and make you want the next one?
    first = first_sentence(text)
    fw = words_of(first)
    fl = [w.lower() for w in fw]
    h = 3.0
    if "?" in first:
        h += 2; reasons.append("opens on a question")
    if any(ch.isdigit() for ch in first):
        h += 1.5; reasons.append("opens with a number")
    hits = sum(1 for w in fl if w in POWER)
    if hits:
        h += min(2.0, 0.8 * hits); reasons.append("power words in the first line")
    if "you" in fl or "your" in fl:
        h += 1; reasons.append("speaks to 'you'")
    if 5 <= len(fw) <= 16:
        h += 1.5
    elif len(fw) < 4:
        h -= 2; reasons.append("first line is too short to hook")
    elif len(fw) > 28:
        h -= 2; reasons.append("first line is a long wind-up")
    if fl and fl[0] in WEAK_OPEN:
        h -= 3; reasons.append("starts mid-thought ('%s...')" % fw[0])
    if SEQ_OPEN.match(first):
        h -= 2.5; reasons.append("opens on a list marker; needs context the clip lacks")
    if BOILER.search(first):
        h -= 3; reasons.append("opens on intro boilerplate")
    if first.lower().startswith(FILLER_OPEN):
        h -= 2; reasons.append("starts on a filler")
    hook = max(0.0, min(10.0, h)) / 10

    # 2. density: words per second, spoken-clip sweet spot is about 2.8
    wps = n / max(dur, 1e-6)
    density = 1 - min(1.0, abs(wps - 2.8) / 1.6)
    if density < 0.4:
        reasons.append("pace is %s (%.1f words/sec)" % ("slow" if wps < 2.8 else "rushed", wps))

    # 3. payoff: does it end on a landed thought, not mid-sentence or on a question?
    last = last_sentence(text).strip()
    payoff = 0.0
    if TERMINAL.search(last) and not last.endswith("?"):
        payoff += 0.5
    elif gap_end:
        payoff += 0.3
    if CONCLUDE.search(last) or CONCLUDE.search(text[-160:]):
        payoff += 0.5; reasons.append("lands on a conclusion")
    if last.endswith("?"):
        payoff = 0.0; reasons.append("ends on an open question")
    if BOILER.search(text):
        payoff -= 0.6; reasons.append("contains intro/outro boilerplate")
    payoff = max(0.0, min(1.0, payoff))

    # 4. intensity: numbers, exclamations, strong words per word spoken
    hitsall = sum(1 for w in ws if w.lower() in POWER) + sum(1 for w in ws if any(c.isdigit() for c in w)) + text.count("!")
    intensity = min(1.0, (hitsall / n) / 0.08)

    # 5. cleanliness: filler rate and internal dead air
    fill = len(FILLERS.findall(text)) / n
    fill_s = 1 - min(1.0, fill / 0.08)
    gaps = [seg[k + 1]["start"] - seg[k]["end"] for k in range(len(seg) - 1)]
    maxgap = max(gaps) if gaps else 0.0
    gap_s = 1.0 if maxgap < 1.0 else (0.5 if maxgap < 2.0 else 0.0)
    if maxgap >= 1.5:
        reasons.append("%.1fs of dead air inside" % maxgap)
    if fill > 0.05:
        reasons.append("lots of filler words")
    clean = 0.6 * fill_s + 0.4 * gap_s

    # 6. length: 25-45 seconds is the pocket for most short-form platforms
    if 25 <= dur <= 45:
        length = 1.0
    else:
        d = (25 - dur) if dur < 25 else (dur - 45)
        length = max(0.0, 1 - d / 20)

    parts = {"hook": hook, "density": density, "payoff": payoff,
             "intensity": intensity, "clean": clean, "length": length}
    total = sum(parts[k] * W[k] for k in W)
    return {
        "score": round(total, 1),
        "parts": {k: round(v * W[k], 1) for k, v in parts.items()},
        "reasons": reasons,
        "first_line": first,
        "text": text,
        "words": n,
    }


# ----------------------------------------------------------------- search

def candidates(cues, mn, mx, strict=True):
    """Every sentence-aligned window between mn and mx seconds."""
    nxt_gap = []
    for k, c in enumerate(cues):
        nxt_gap.append((cues[k + 1]["start"] - c["end"]) if k + 1 < len(cues) else 99.0)

    def end_ok(j):
        return (not strict) or bool(TERMINAL.search(cues[j]["text"])) or nxt_gap[j] >= 0.6

    def start_ok(i):
        return (not strict) or i == 0 or bool(TERMINAL.search(cues[i - 1]["text"])) or nxt_gap[i - 1] >= 0.6

    out = []
    for i in range(len(cues)):
        if not start_ok(i):
            continue
        for j in range(i, len(cues)):
            dur = cues[j]["end"] - cues[i]["start"]
            if dur > mx:
                break
            if dur >= mn and end_ok(j):
                r = score_window(cues, i, j, nxt_gap[j] >= 0.6)
                r.update(i=i, j=j, start=cues[i]["start"], end=cues[j]["end"],
                         duration=round(dur, 2))
                out.append(r)
    return out


def select(cands, count, min_score, gap):
    chosen = []
    for c in sorted(cands, key=lambda c: (-c["score"], c["duration"])):
        if c["score"] < min_score:
            break
        if any(c["start"] < k["end"] + gap and c["end"] > k["start"] - gap for k in chosen):
            continue
        chosen.append(c)
        if len(chosen) >= count:
            break
    return sorted(chosen, key=lambda c: c["start"])  # timeline order; rank set below


# ----------------------------------------------------------------- output

def stamp(t, sep=","):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d%s%03d" % (h, m, int(s), sep, round((s - int(s)) * 1000) % 1000)


def clock(t):
    m, s = divmod(int(t), 60)
    return "%d:%02d" % (m, s)


def write_srt(path, cues, i, j, t0):
    with open(path, "w", encoding="utf-8") as f:
        for n, c in enumerate(cues[i:j + 1], 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (n, stamp(c["start"] - t0), stamp(c["end"] - t0), c["text"]))


STYLE = "FontName=Arial,FontSize=11,Bold=1,Outline=2,Shadow=0,Alignment=2,MarginV=35"
VF = {
    "vertical": "crop=ih*9/16:ih,scale=1080:1920",
    "square": "crop=ih:ih,scale=1080:1080",
    "original": None,
}


def ffmpeg_line(video, clip, aspect, captions):
    vf = VF[aspect]
    chain = [vf] if vf else []
    if captions:
        chain.append("subtitles=%s:force_style='%s'" % (clip["srt"], STYLE))
    cmd = "ffmpeg -y -hide_banner -loglevel error -ss %.3f -i %s -t %.3f" % (
        clip["cut_start"], shlex.quote(video), clip["cut_end"] - clip["cut_start"])
    if chain:
        cmd += ' -vf "%s"' % ",".join(chain)
    cmd += " -c:v libx264 -preset medium -crf 20 -c:a aac -b:a 160k -movflags +faststart %s" % clip["file"]
    return cmd


def main():
    ap = argparse.ArgumentParser(description="Find the best clips in a transcript.")
    ap.add_argument("transcript", help=".srt or .vtt file")
    ap.add_argument("--video", help="source video; enables cut.sh")
    ap.add_argument("--count", type=int, default=8, help="how many clips to keep (default 8)")
    ap.add_argument("--min", type=float, default=20, dest="mn", help="shortest clip, seconds")
    ap.add_argument("--max", type=float, default=60, dest="mx", help="longest clip, seconds")
    ap.add_argument("--min-score", type=float, default=35, help="drop anything below this")
    ap.add_argument("--gap", type=float, default=2.0, help="min seconds between clips")
    ap.add_argument("--pad", type=float, default=0.25, help="seconds of breathing room each side")
    ap.add_argument("--aspect", choices=list(VF), default="vertical")
    ap.add_argument("--no-captions", action="store_true", help="do not burn captions in")
    ap.add_argument("--only", help="comma list of ranks to export, e.g. 1,3,4")
    ap.add_argument("--out", default="clips", help="output folder")
    ap.add_argument("--json", action="store_true", help="print JSON to stdout too")
    a = ap.parse_args()

    cues = parse_transcript(a.transcript)
    if not cues:
        sys.exit("No cues found. Is this an .srt or .vtt file?")
    total = cues[-1]["end"]

    cands = candidates(cues, a.mn, a.mx, strict=True)
    relaxed = False
    if not cands:  # captions with no punctuation and no pauses
        cands = candidates(cues, a.mn, a.mx, strict=False)
        relaxed = True
    if not cands:
        sys.exit("Transcript is shorter than --min (%.0fs). Nothing to clip." % a.mn)

    picked = select(cands, a.count, a.min_score, a.gap)
    if not picked:
        best = max(c["score"] for c in cands)
        sys.exit("Nothing scored >= %.0f (best was %.1f). Lower --min-score or widen --min/--max." % (a.min_score, best))

    # rank by score, remember timeline order
    for rank, c in enumerate(sorted(picked, key=lambda c: -c["score"]), 1):
        c["rank"] = rank
    only = {int(x) for x in a.only.split(",")} if a.only else None
    export = [c for c in sorted(picked, key=lambda c: c["rank"]) if not only or c["rank"] in only]

    os.makedirs(a.out, exist_ok=True)
    for c in export:
        c["cut_start"] = max(0.0, c["start"] - a.pad)
        c["cut_end"] = min(total + a.pad, c["end"] + a.pad)
        c["srt"] = "clip_%02d.srt" % c["rank"]
        c["file"] = "clip_%02d.mp4" % c["rank"]
        write_srt(os.path.join(a.out, c["srt"]), cues, c["i"], c["j"], c["cut_start"])

    # report
    L = ["# Clip shortlist", "",
         "Source: `%s`  |  length %s  |  %d candidates scanned  |  %d kept%s" % (
             os.path.basename(a.transcript), clock(total), len(cands), len(picked),
             "  |  punctuation-free transcript, boundaries relaxed" if relaxed else ""),
         "", "Scores are a heuristic, not a view predictor. Read each one before you cut it.", ""]
    for c in export:
        L += ["## #%d  score %.0f   %s - %s  (%ds)" % (c["rank"], c["score"], clock(c["start"]), clock(c["end"]), round(c["duration"])),
              "", "**First line:** %s" % c["first_line"], "",
              "hook %.0f/35 | pace %.0f/15 | payoff %.0f/15 | intensity %.0f/15 | clean %.0f/10 | length %.0f/10" % (
                  c["parts"]["hook"], c["parts"]["density"], c["parts"]["payoff"],
                  c["parts"]["intensity"], c["parts"]["clean"], c["parts"]["length"]),
              ""]
        if c["reasons"]:
            L += ["Notes: " + "; ".join(c["reasons"]), ""]
        L += ["> " + c["text"], ""]
    with open(os.path.join(a.out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    keep = ("rank", "score", "start", "end", "duration", "cut_start", "cut_end",
            "first_line", "text", "parts", "reasons", "srt", "file")
    data = [{k: c[k] for k in keep} for c in export]
    with open(os.path.join(a.out, "clips.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    if a.video:
        video = os.path.abspath(a.video)
        lines = ["#!/usr/bin/env bash", "# Generated by clipper.py. Needs ffmpeg with libass for captions.",
                 'set -e', 'cd "$(dirname "$0")"', ""]
        for c in export:
            lines.append("echo 'clip %d: %s - %s'" % (c["rank"], clock(c["start"]), clock(c["end"])))
            lines.append(ffmpeg_line(video, c, a.aspect, not a.no_captions))
        lines.append('echo "done"')
        p = os.path.join(a.out, "cut.sh")
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(p, 0o755)

    print("%d clips from %s (%d candidates). Wrote %s/" % (len(export), clock(total), len(cands), a.out))
    for c in export:
        print("  #%d  %5.1f  %s-%s  %s" % (c["rank"], c["score"], clock(c["start"]), clock(c["end"]),
                                          c["first_line"][:70]))
    if a.json:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
