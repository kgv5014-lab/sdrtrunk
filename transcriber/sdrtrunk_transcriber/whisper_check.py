"""Whisper check: scores Whisper against calls Ken typed out straight from the audio on the /checker page.

    python -m sdrtrunk_transcriber.whisper_check [--config config.toml] [--all]

Unlike compare.py, which scores against hand fixes of Whisper's own text, this is a fair test: Ken
never saw Whisper's version before typing. Each call is run through the service's own pipeline
(cleanup, hints, junk filter, places/codes fixes and learned corrections) with the model in
config.toml, or with --all also the other models that fit a GTX 10-series card. "As logged" is what
the service actually saved for the call at the time, so no card is needed for that column.

The report (data/whisper-check-<time>.html) has the share of words wrong for each model and channel,
the mistakes the first model made most often, and suggestions for vocabulary.txt and corrections.csv.
The service can keep running.
"""

import argparse
import difflib
import gc
import html
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import audio
from .compare import Runner
from .config import Config, load_config
from .corrections import COMMON, MAX_LEARN_WORDS, _tokens, _words_only
from .db import Database

log = logging.getLogger(__name__)

# Models that fit a GTX 10-series card in int8 next to the live transcriber (all four were measured
# on the 1070 Ti on 2026-10-29: 1.4-4.3 GB).
OTHER_MODELS = [
    "Systran/faster-whisper-large-v3",
    "Systran/faster-whisper-large-v2",
    "distil-whisper/distil-large-v3.5-ct2",
]
# A mistake seen on at least this many calls is worth a suggestion.
SUGGEST_MIN_CALLS = 2
# difflib's ops, turning Whisper's words into Ken's.
KINDS = {"replace": "wrong", "delete": "added", "insert": "missed"}


def words(text: str) -> list[str]:
    return [w.lower() for w in _words_only(_tokens(text or ""))]


def edits(reference: str, hypothesis: str) -> tuple[int, list[tuple[str, str, str]]]:
    """Word edits that turn Whisper's text into Ken's, as (kind, heard, meant) with kind one of
    'wrong' (heard instead of meant), 'missed' (meant, not written) or 'added' (heard, not said)."""
    ref, hyp = words(reference), words(hypothesis)
    count, found = 0, []
    matcher = difflib.SequenceMatcher(a=hyp, b=ref, autojunk=False)
    for op, h0, h1, r0, r1 in matcher.get_opcodes():
        if op == "equal":
            continue
        heard, meant = " ".join(hyp[h0:h1]), " ".join(ref[r0:r1])
        count += max(h1 - h0, r1 - r0)
        found.append((KINDS[op], heard, meant))
    return count, found


class Score:
    def __init__(self):
        self.errors = 0
        self.ref_words = 0
        self.calls = 0

    def add(self, reference: str, hypothesis: str) -> float:
        n, _ = edits(reference, hypothesis)
        ref = max(1, len(words(reference)))
        self.errors += n
        self.ref_words += ref
        self.calls += 1
        return n / ref

    @property
    def wrong(self) -> float | None:
        return self.errors / self.ref_words if self.ref_words else None


def typed_calls(db: Database) -> list[dict]:
    return [c for c in db.checks() if c["typed_at"] is not None and not c["skipped"] and c["typed"]]


def mistakes(rows: list[dict], key: str) -> dict[str, Counter]:
    """How many calls each mistake of one model's text appeared in, by kind."""
    out = {"wrong": Counter(), "missed": Counter(), "added": Counter()}
    for row in rows:
        _, found = edits(row["call"]["typed"], row["texts"][key])
        for kind, heard, meant in set(found):
            out[kind][(heard, meant)] += 1
    return out


def suggestions(found: dict[str, Counter]) -> tuple[list[tuple[str, str, int]], list[tuple[str, int]]]:
    """(corrections heard -> meant, vocabulary words) seen on enough calls to be worth adding."""
    corrections, vocab = [], Counter()
    for (heard, meant), n in found["wrong"].items():
        if n < SUGGEST_MIN_CALLS:
            continue
        h, m = heard.split(), meant.split()
        if len(h) <= MAX_LEARN_WORDS and len(m) <= MAX_LEARN_WORDS and not (len(h) == 1 and heard in COMMON):
            corrections.append((heard, meant, n))
    for kind in ("wrong", "missed"):
        for (_heard, meant), n in found[kind].items():
            for w in meant.split():
                if w not in COMMON and not w.isdigit() and len(w) > 2:
                    vocab[w] += n
    return (sorted(corrections, key=lambda c: -c[2]),
            [(w, n) for w, n in vocab.most_common() if n >= SUGGEST_MIN_CALLS])


def run_model(config: Config, backend, calls: list[dict], samples: dict) -> tuple[dict, list[float]]:
    runner = Runner(config, backend)
    texts, times = {}, []
    for n, call in enumerate(calls, 1):
        started = time.time()
        texts[call["id"]], _conf = runner.run(call, samples[call["id"]], after=True)
        times.append(time.time() - started)
        print(f"  {n}/{len(calls)}", end="\r", flush=True)
    print(flush=True)
    return texts, times


def check(config: Config, backends: list) -> tuple[list[dict], list[dict]]:
    """backends: (label, factory) pairs; each factory makes a backend, so only one model is loaded at a time."""
    db = Database(config.data_path / "calls.db")
    try:
        calls = typed_calls(db)
    finally:
        db.close()
    samples, kept = {}, []
    for call in calls:
        path = config.data_path / call["audio_path"]
        if path.exists() and not call.get("audio_deleted_at"):
            samples[call["id"]] = audio.decode(path)
            kept.append(call)
    rows = [{"call": c, "texts": {}} for c in kept]
    models = []

    logged = Score()
    for row in rows:
        call = row["call"]
        # A call fixed by hand on the call log page no longer holds what the service wrote.
        if call.get("corrected_at") is None:
            row["texts"]["As logged"] = call["transcript"] or ""
            logged.add(call["typed"], row["texts"]["As logged"])
    if logged.calls:
        models.append({"label": "As logged", "score": logged, "avg_s": None})

    for label, factory in backends:
        print(f"{label}...", flush=True)
        try:
            backend = factory()
        except Exception as e:
            print(f"  could not load: {e}", flush=True)
            models.append({"label": label, "error": str(e)})
            continue
        texts, times = run_model(config, backend, kept, samples)
        score, by_tg = Score(), defaultdict(Score)
        for row in rows:
            row["texts"][label] = texts[row["call"]["id"]]
            score.add(row["call"]["typed"], texts[row["call"]["id"]])
            by_tg[row["call"]["talkgroup_label"] or row["call"]["talkgroup"] or "?"].add(
                row["call"]["typed"], texts[row["call"]["id"]])
        models.append({"label": label, "score": score, "by_tg": dict(by_tg),
                       "avg_s": sum(times) / len(times) if times else None})
        del backend
        gc.collect()
        print(f"  {score.wrong * 100:.1f}% words wrong" if score.wrong is not None else "  no calls", flush=True)
    return rows, models


def report(rows: list[dict], models: list[dict]) -> str:
    e = html.escape

    def pct(v):
        return "–" if v is None else f"{v * 100:.1f}%"

    out = [
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Whisper check</title><style>",
        "body{font:14px/1.45 system-ui,sans-serif;margin:16px;background:#fafafa;color:#1d1d1f;max-width:1100px}",
        "table{border-collapse:collapse;margin:8px 0 20px}td,th{border-bottom:1px solid #e2e2e6;padding:5px 8px;"
        "vertical-align:top;text-align:left}.meta{color:#6b6b70;font-size:12px}.fix{font-weight:600}",
        "@media(prefers-color-scheme:dark){body{background:#161618;color:#ececf0}td,th{border-color:#2c2c31}}</style>",
        "<h1>Whisper check</h1>",
        f"<p class='meta'>{e(datetime.now().strftime('%Y-%m-%d %H:%M'))} · {len(rows)} calls you typed from the audio</p>",
    ]
    if not rows:
        out.append("<p>No typed calls yet. Open the Whisper check page from the call log and type a few.</p>")
        return "\n".join(out)

    out.append("<h2>Words wrong (lower is better)</h2><table><tr><th>Model</th><th>Words wrong</th>"
               "<th>Calls</th><th>Seconds per call</th></tr>")
    for m in models:
        if "error" in m:
            out.append(f"<tr><td>{e(m['label'])}</td><td colspan=3>could not load: {e(m['error'])}</td></tr>")
            continue
        speed = "" if m["avg_s"] is None else f"{m['avg_s']:.2f}"
        out.append(f"<tr><td>{e(m['label'])}</td><td><b>{pct(m['score'].wrong)}</b></td>"
                   f"<td>{m['score'].calls}</td><td>{speed}</td></tr>")
    out.append("</table>")

    main = next((m for m in models if "by_tg" in m), None)
    if main:
        out.append(f"<h2>By channel ({e(main['label'])})</h2><table><tr><th>Channel</th><th>Words wrong</th>"
                   "<th>Calls</th></tr>")
        for tg, s in sorted(main["by_tg"].items(), key=lambda kv: -(kv[1].wrong or 0)):
            out.append(f"<tr><td>{e(tg)}</td><td>{pct(s.wrong)}</td><td>{s.calls}</td></tr>")
        out.append("</table>")

        found = mistakes(rows, main["label"])
        corrections, vocab = suggestions(found)
        out.append(f"<h2>Most common mistakes ({e(main['label'])})</h2>"
                   "<p class='meta'>How many calls each one was on.</p>"
                   "<table><tr><th>Whisper wrote</th><th>You heard</th><th>Calls</th></tr>")
        for (heard, meant), n in found["wrong"].most_common(30):
            out.append(f"<tr><td>{e(heard)}</td><td class='fix'>{e(meant)}</td><td>{n}</td></tr>")
        out.append("</table><table><tr><th>Words Whisper left out</th><th>Calls</th><th>Words Whisper made up</th>"
                   "<th>Calls</th></tr>")
        missed, added = found["missed"].most_common(15), found["added"].most_common(15)
        for i in range(max(len(missed), len(added))):
            m_ = missed[i] if i < len(missed) else (("", ""), "")
            a_ = added[i] if i < len(added) else (("", ""), "")
            out.append(f"<tr><td>{e(m_[0][1])}</td><td>{m_[1]}</td><td>{e(a_[0][0])}</td><td>{a_[1]}</td></tr>")
        out.append("</table>")

        out.append("<h2>Suggestions</h2>")
        if corrections:
            out.append("<p>Lines for <b>corrections.csv</b> (heard,correct), for mistakes on "
                       f"{SUGGEST_MIN_CALLS} or more calls. Check each one first: a fix that is right on these "
                       "calls can be wrong on others.</p><pre>")
            out.extend(e(f"{h},{m},,yes,{n},check") for h, m, n in corrections)
            out.append("</pre>")
        if vocab:
            out.append("<p>Words for <b>vocabulary.txt</b>, which Whisper often got wrong or left out:</p><p>"
                       + e(", ".join(w for w, _ in vocab[:40])) + "</p>")
        if not corrections and not vocab:
            out.append("<p>No mistake came up on more than one call yet.</p>")

    labels = [m["label"] for m in models if "error" not in m]
    out.append("<h2>Calls</h2><table><tr><th>Call</th><th>You heard</th>"
               + "".join(f"<th>{e(label)}</th>" for label in labels) + "</tr>")
    for row in rows:
        call = row["call"]
        when = datetime.fromtimestamp(call["call_time"]).strftime("%m-%d %H:%M")
        cells = []
        for label in labels:
            text = row["texts"].get(label)
            if text is None:
                cells.append("<td class='meta'>–</td>")
                continue
            n, _ = edits(call["typed"], text)
            share = n / max(1, len(words(call["typed"])))
            cells.append(f"<td>{e(text) or '<i>empty</i>'}<div class='meta'>{share * 100:.0f}% wrong</div></td>")
        out.append(f"<tr><td class='meta'>{e(when)}<br>{e(call['talkgroup_label'] or call['talkgroup'] or '')}"
                   f"<br>{call['duration_s'] or 0:.1f} s</td><td class='fix'>{e(call['typed'])}</td>"
                   + "".join(cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.whisper_check")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--all", action="store_true", help="also try the other models that fit the card")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from .__main__ import add_nvidia_dll_dirs
    from .backends import FasterWhisperBackend, create_backend
    add_nvidia_dll_dirs()
    config = load_config(args.config if args.config.exists() else None)
    if config.transcription.backend == "none":
        sys.exit("Set [transcription] backend in config.toml to check Whisper")
    current = config.faster_whisper.model if config.transcription.backend == "faster-whisper" else "whisper.cpp"
    backends = [(current, lambda: create_backend(config))]
    if args.all and config.transcription.backend == "faster-whisper":
        def other(model):
            def make():
                config.faster_whisper.model = model
                return FasterWhisperBackend(config)
            return make
        backends += [(model.split("/")[-1], other(model)) for model in OTHER_MODELS]

    started = time.time()
    rows, models = check(config, backends)
    path = config.data_path / f"whisper-check-{datetime.now().strftime('%Y%m%d-%H%M')}.html"
    path.write_text(report(rows, models), encoding="utf-8")
    print(f"Done in {time.time() - started:.0f} s. Report: {path.resolve()}")
    if sys.platform == "win32":
        import os
        os.startfile(path.resolve())


if __name__ == "__main__":
    main()
