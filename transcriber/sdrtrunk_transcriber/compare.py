"""Before/after check: runs logged calls through Whisper the old way and the new way, side by side.

    python -m sdrtrunk_transcriber.compare [--config config.toml] [--calls 50]

"Before" is Whisper on the audio as SDRTrunk sent it, with no cleanup. "After" is what the service
does now: audio cleanup, the junk filter and learned corrections. Calls fixed by hand on the call log
page are always included, and each side is scored against the fix: the share of words it got wrong
(lower is better). The report is written to data/compare-<time>.html. The service can keep running.

    python -m sdrtrunk_transcriber.compare --hints

compares only the hints learned from the call log (hints.py): "before" is the service without them,
"after" with the ones each call would have had at its own time.
"""

import argparse
import html
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from . import audio
from .codes import CodeBook
from .config import Config, load_config
from .corrections import CorrectionBook, _tokens, _words_only
from .junk import remove_junk
from .hints import LearnedHints
from .places import PlaceBook
from .worker import load_vocabulary, prompt_terms

log = logging.getLogger(__name__)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word-level edit distance divided by the reference length, ignoring case and punctuation."""
    ref = [w.lower() for w in _words_only(_tokens(reference))]
    hyp = [w.lower() for w in _words_only(_tokens(hypothesis))]
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        current = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
        previous = current
    return previous[-1] / len(ref)


def pick_calls(db_path: Path, count: int) -> list[dict]:
    """Every hand-fixed call (newest first) plus the newest other transcribed calls, up to count."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(calls)")}
    fixed = []
    if "corrected_at" in columns:
        fixed = [dict(r) for r in conn.execute(
            "SELECT * FROM calls WHERE corrected_at IS NOT NULL ORDER BY id DESC LIMIT ?", (count,))]
    rest = [dict(r) for r in conn.execute(
        "SELECT * FROM calls WHERE status = 'done' AND duration_s >= 1 "
        + ("AND corrected_at IS NULL " if "corrected_at" in columns else "")
        + "ORDER BY id DESC LIMIT ?", (max(0, count - len(fixed)),))]
    conn.close()
    return fixed + rest


class Runner:
    def __init__(self, config: Config, backend):
        self.config = config
        self.backend = backend
        self.codebook = CodeBook(config.codes_path, config.codes.time_window_minutes)
        self.placebook = PlaceBook(config.places_path, config.places.prompt_chars, config.places.areas)
        self.corrections = CorrectionBook(config.corrections_path)
        self.vocabulary = load_vocabulary(config.vocabulary_path)
        self.hints = LearnedHints(config.data_path / "calls.db", config.hints)

    def run(self, call: dict, samples, after: bool, learned: bool = False) -> tuple[str, float | None]:
        context = (call["talkgroup_label"], call["talkgroup_group"])
        lists = [self.placebook.prompt_terms(*context), self.codebook.prompt_terms()]
        if after:
            lists.append(self.corrections.prompt_terms())
        if learned:
            lists.append(self.hints.terms(call["talkgroup"], call["call_time"]))
        terms = prompt_terms(*lists, self.vocabulary)
        if after and self.config.audio.clean:
            samples, _tones = audio.clean(samples, self.config.audio.remove_tones,
                                          self.config.audio.reduce_hiss)
        result = self.backend.transcribe(samples, ", ".join(terms))
        text = result.text
        if after and self.config.transcription.junk_filter:
            text, _junk = remove_junk(text, result.confidence, terms, self.config.transcription.stock_phrase_logprob,
                                      self.config.transcription.english_only)
        text = self.placebook.normalize(text, *context)
        text = self.codebook.normalize(text, datetime.fromtimestamp(call["call_time"]))
        if after:
            text = self.corrections.normalize(text, call["talkgroup_label"])
        return text, result.confidence


def compare(config: Config, backend, count: int, hints: bool = False) -> tuple[list[dict], dict]:
    runner = Runner(config, backend)
    if hints:
        runner.hints.config.enabled = True
    rows = []
    for n, call in enumerate(pick_calls(config.data_path / "calls.db", count), 1):
        path = config.data_path / call["audio_path"]
        if not path.exists():
            continue
        samples = audio.decode(path)
        if hints:
            before, before_conf = runner.run(call, samples, after=True)
            after, after_conf = runner.run(call, samples, after=True, learned=True)
        else:
            before, before_conf = runner.run(call, samples, after=False)
            after, after_conf = runner.run(call, samples, after=True)
        row = {"call": call, "before": before, "after": after, "before_conf": before_conf,
               "after_conf": after_conf, "fixed": call.get("corrected_at") is not None}
        if row["fixed"]:
            row["before_wer"] = word_error_rate(call["transcript"], before)
            row["after_wer"] = word_error_rate(call["transcript"], after)
        rows.append(row)
        print(f"{n}/{count} call {call['id']}: {'same' if before == after else 'changed'}", flush=True)

    fixed = [r for r in rows if r["fixed"]]
    summary = {
        "calls": len(rows),
        "changed": sum(r["before"] != r["after"] for r in rows),
        "emptied": sum(bool(r["before"]) and not r["after"] for r in rows),
        "fixed": len(fixed),
        "before_wer": sum(r["before_wer"] for r in fixed) / len(fixed) if fixed else None,
        "after_wer": sum(r["after_wer"] for r in fixed) / len(fixed) if fixed else None,
        "hints": hints,
    }
    return rows, summary


def report(rows: list[dict], summary: dict, model: str) -> str:
    def pct(value):
        return "–" if value is None else f"{value * 100:.0f}%"

    e = html.escape
    out = [
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Transcript before/after</title><style>",
        "body{font:14px/1.45 system-ui,sans-serif;margin:16px;background:#fafafa;color:#1d1d1f}",
        "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e2e2e6;padding:6px 8px;"
        "vertical-align:top;text-align:left}th{position:sticky;top:0;background:#fafafa}",
        ".meta{color:#6b6b70;font-size:12px}.same{color:#6b6b70}.better{background:#e7f6ea}"
        ".worse{background:#fdeaea}.fix{font-weight:600}",
        "@media(prefers-color-scheme:dark){body,th{background:#161618;color:#ececf0}"
        ".better{background:#1d3324}.worse{background:#3a1f1f}}</style>",
        "<h1>Transcript before/after</h1>",
        f"<p class='meta'>{e(datetime.now().strftime('%Y-%m-%d %H:%M'))} · {e(model)}</p>",
        f"<p>{summary['calls']} calls. The new way changed {summary['changed']} of them, and left "
        f"{summary['emptied']} empty (made-up text removed).</p>",
    ]
    if summary.get("hints"):
        out.append("<p>Before: the service without learned hints. After: with the streets and units each call "
                   "would have been given from the calls before it.</p>")
    if summary["fixed"]:
        learned_note = ("" if summary.get("hints") else
                        " The after column includes the corrections learned from these "
                        "same calls, so the score shows they work; new calls are the real test.")
        out.append(f"<p>On the {summary['fixed']} calls you fixed by hand, words wrong: before "
                   f"<b>{pct(summary['before_wer'])}</b>, after <b>{pct(summary['after_wer'])}</b> "
                   f"(lower is better).{learned_note}</p>")
    else:
        out.append("<p>No calls have been fixed by hand yet. Fix a few with the Edit button on the call log "
                   "page and run this again to get a score.</p>")
    out.append("<table><tr><th>Call</th><th>Before</th><th>After</th><th>Your fix</th></tr>")
    for r in rows:
        call = r["call"]
        cls = "same" if r["before"] == r["after"] else ""
        if r["fixed"]:
            cls = "better" if r["after_wer"] < r["before_wer"] else "worse" if r["after_wer"] > r["before_wer"] else cls
        when = datetime.fromtimestamp(call["call_time"]).strftime("%m-%d %H:%M:%S")
        out.append(
            f"<tr class='{cls}'><td class='meta'>{e(when)}<br>{e(call['talkgroup_label'] or call['talkgroup'] or '')}"
            f"<br>{call['duration_s'] or 0:.1f} s</td>"
            f"<td>{e(r['before']) or '<i>empty</i>'}{_score(r, 'before')}</td>"
            f"<td>{e(r['after']) or '<i>empty</i>'}{_score(r, 'after')}</td>"
            f"<td class='fix'>{e(call['transcript'] or '') if r['fixed'] else ''}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def _score(row: dict, side: str) -> str:
    if not row["fixed"]:
        return ""
    return f"<div class='meta'>{row[side + '_wer'] * 100:.0f}% words wrong</div>"


def main():
    parser = argparse.ArgumentParser(prog="sdrtrunk_transcriber.compare")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--calls", type=int, default=50, help="how many calls to compare (default 50)")
    parser.add_argument("--hints", action="store_true", help="compare only the hints learned from the call log")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from .__main__ import add_nvidia_dll_dirs
    from .backends import create_backend
    add_nvidia_dll_dirs()
    config = load_config(args.config if args.config.exists() else None)
    if config.transcription.backend == "none":
        sys.exit("Set [transcription] backend in config.toml to compare transcripts")
    print("Loading Whisper...", flush=True)
    backend = create_backend(config)
    started = time.time()
    rows, summary = compare(config, backend, args.calls, args.hints)
    path = config.data_path / f"compare-{datetime.now().strftime('%Y%m%d-%H%M')}.html"
    path.write_text(report(rows, summary, f"{backend.name} {backend.model}"), encoding="utf-8")
    print(f"Done in {time.time() - started:.0f} s. Report: {path.resolve()}")
    if sys.platform == "win32":
        import os
        os.startfile(path.resolve())


if __name__ == "__main__":
    main()
