"""Corrections Ken makes to transcripts, learned as word replacements for later calls.

corrections.csv columns: heard, correct, talkgroup, hint, count, source.
- heard -> correct is a whole-word, case-insensitive replacement, e.g. "never" -> "Nevin".
- talkgroup (optional): only apply on talkgroups whose label contains this text.
- hint: yes to also give the corrected words to Whisper as a spelling hint.
- count: how many times this correction was made on the Transcripts page.
- source: "learned" for rows the page added, anything else for hand-made rows.
Hand edits to the file apply without a restart.
"""

import csv
import difflib
import io
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FIELDS = ("heard", "correct", "talkgroup", "hint", "count", "source")
# Longest phrase on either side that gets learned as one correction. Longer rewrites are one-offs.
MAX_LEARN_WORDS = 4
WORD = re.compile(r"[\w'-]+|[^\w\s]")
# Single everyday words that are never learned on their own: changing one of them is a one-off edit,
# and replacing it in every later call would do more harm than good.
COMMON = {
    "a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "at", "by", "for", "with", "from",
    "is", "are", "was", "were", "be", "it", "its", "i", "you", "he", "she", "we", "they", "me", "him",
    "her", "us", "them", "that", "this", "there", "here", "yes", "no", "not", "ok", "okay", "yeah",
}


@dataclass
class Correction:
    heard: str
    correct: str
    talkgroup: str = ""
    hint: bool = True
    count: int = 1
    source: str = "learned"

    def row(self) -> dict:
        return {"heard": self.heard, "correct": self.correct, "talkgroup": self.talkgroup,
                "hint": "yes" if self.hint else "no", "count": str(self.count), "source": self.source}


def _tokens(text: str) -> list[str]:
    return WORD.findall(text or "")


def _words_only(tokens: list[str]) -> list[str]:
    return [t for t in tokens if re.match(r"[\w'-]", t)]


def learn_pairs(original: str, corrected: str) -> list[tuple[str, str]]:
    """Word replacements that turn the original into the corrected text, e.g. [("never", "Nevin")].
    Only replaced words are learned: added or removed words, and long rewrites, are one-off edits."""
    a = _words_only(_tokens(original))
    b = _words_only(_tokens(corrected))
    pairs = []
    matcher = difflib.SequenceMatcher(a=[w.lower() for w in a], b=[w.lower() for w in b], autojunk=False)
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op != "replace":
            continue
        heard, correct = a[a0:a1], b[b0:b1]
        if len(heard) > MAX_LEARN_WORDS or len(correct) > MAX_LEARN_WORDS:
            continue
        heard_text, correct_text = " ".join(heard), " ".join(correct)
        # Changing only capitals or a lone number is not a hearing mistake worth repeating everywhere.
        if heard_text.lower() == correct_text.lower() or heard_text.isdigit():
            continue
        if len(heard) == 1 and heard_text.lower() in COMMON:
            continue
        pairs.append((heard_text, correct_text))
    return pairs


class CorrectionBook:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = None
        self._items: list[Correction] = []
        self._patterns: list[tuple[re.Pattern, Correction]] = []
        self.reload_if_changed()

    # ---- loading and saving

    def reload_if_changed(self):
        try:
            mtime = self.path.stat().st_mtime if self.path else None
        except FileNotFoundError:
            mtime = None
        with self._lock:
            if mtime == self._mtime:
                return
            self._mtime = mtime
            self._set(self._read() if mtime is not None else [])

    def _read(self) -> list[Correction]:
        try:
            raw = self.path.read_bytes()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("cp1252", errors="replace")  # Excel's "CSV" on Windows
            items = []
            for row in csv.DictReader(io.StringIO(content, newline="")):
                heard = (row.get("heard") or "").strip()
                correct = (row.get("correct") or "").strip()
                if not heard or not correct or heard == correct:
                    continue
                count = (row.get("count") or "").strip()
                items.append(Correction(
                    heard=heard,
                    correct=correct,
                    talkgroup=(row.get("talkgroup") or "").strip(),
                    hint=(row.get("hint") or "yes").strip().lower() not in ("no", "n", "false", "0"),
                    count=int(count) if count.isdigit() else 1,
                    source=(row.get("source") or "").strip() or "hand-made",
                ))
            log.info("Loaded %d corrections from %s", len(items), self.path)
            return items
        except (OSError, csv.Error) as e:
            log.error("Could not read %s: %s", self.path, e)
            return []

    def _set(self, items: list[Correction]):
        self._items = items
        # Longest first, so "never mind" wins over "never"
        ordered = sorted(items, key=lambda c: len(c.heard), reverse=True)
        self._patterns = [(re.compile(r"(?<![\w'-])" + r"\s+".join(map(re.escape, c.heard.split())) +
                                      r"(?![\w'-])", re.IGNORECASE), c) for c in ordered]

    def _write(self):
        if self.path is None:
            return
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\r\n")
        writer.writeheader()
        for item in self._items:
            writer.writerow(item.row())
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
        os.replace(temp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ---- use

    def items(self) -> list[Correction]:
        self.reload_if_changed()
        return list(self._items)

    def normalize(self, text: str, talkgroup_label: str | None = None) -> str:
        """Applies every correction that fits this talkgroup."""
        self.reload_if_changed()
        if not text:
            return text
        label = (talkgroup_label or "").lower()
        for pattern, item in self._patterns:
            if item.talkgroup and item.talkgroup.lower() not in label:
                continue
            text = pattern.sub(lambda _m, c=item.correct: c, text)
        return text

    def prompt_terms(self) -> list[str]:
        """Corrected spellings for Whisper's hint list, the most often corrected last (Whisper weighs
        the end of its prompt most)."""
        self.reload_if_changed()
        seen, terms = set(), []
        for item in sorted(self._items, key=lambda c: c.count):
            if item.hint and item.correct.lower() not in seen:
                seen.add(item.correct.lower())
                terms.append(item.correct)
        return terms

    def forget(self, heard: str, correct: str) -> bool:
        """Removes a correction, e.g. when Ken undoes one the page just learned."""
        self.reload_if_changed()
        with self._lock:
            kept = [c for c in self._items
                    if not (c.heard.lower() == heard.lower() and c.correct.lower() == correct.lower())]
            if len(kept) == len(self._items):
                return False
            self._set(kept)
            try:
                self._write()
            except OSError as e:
                log.error("Could not save %s: %s", self.path, e)
        return True

    def learn(self, original: str, corrected: str) -> list[dict]:
        """Records the word replacements between a transcript and Ken's correction of it.
        Returns the corrections that were added or counted again."""
        pairs = learn_pairs(original, corrected)
        if not pairs or self.path is None:
            return []
        self.reload_if_changed()
        learned = []
        with self._lock:
            for heard, correct in pairs:
                existing = next((c for c in self._items if c.heard.lower() == heard.lower()
                                 and c.correct.lower() == correct.lower()), None)
                if existing:
                    existing.count += 1
                    item = existing
                else:
                    item = Correction(heard, correct)
                    self._items.append(item)
                learned.append(item.row())
            self._set(self._items)
            try:
                self._write()
            except OSError as e:
                log.error("Could not save %s: %s", self.path, e)
        if learned:
            log.info("Learned corrections: %s", ", ".join(f"{r['heard']} -> {r['correct']}" for r in learned))
        return learned
