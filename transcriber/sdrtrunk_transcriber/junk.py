"""Removes text Whisper made up rather than heard.

Three patterns show up in real radio calls:
- A whole call that is only a stock phrase ("Thank you.", "Bye-bye.") heard with low confidence.
  Whisper produces these on noise and squelch tails.
- Loops: the same word or phrase over and over, or a counting run ("11, 12, 13, 14, 14, 15...").
- Hint-list echoes: several terms from Whisper's own prompt, in prompt order, with nothing between.
- Foreign script: on noise Whisper sometimes writes Korean, Chinese, Cyrillic, Arabic and so on even
  when told the language is English.
"""

import re

# Whole-call phrases Whisper invents on noise. Dispatchers really say these too, so a call is only
# dropped when it is nothing but one of them and Whisper wasn't confident.
STOCK_PHRASES = {
    "thank you", "thank you very much", "thanks", "thank you so much", "bye", "bye-bye", "bye bye",
    "goodbye", "you", "never", "bravo", "oh", "hmm", "um", "uh",
}
# A phrase of up to this many items that repeats this many times or more in a row is a loop.
LOOP_MAX_ITEMS = 4
LOOP_MIN_REPEATS = 3
# This many bare numbers in a row, never going down, is Whisper counting.
COUNTING_RUN = 6
# This many hint-list terms in a row, each one of the next few terms in the hint list, is an echo of
# the prompt. Real calls name units and streets too, but not in the hint list's exact order.
ECHO_RUN = 3
ECHO_MAX_STEP = 2
# Letters and marks an English transcript can hold: ASCII, accented Latin letters, curly quotes,
# dashes, ellipsis and the degree sign. Anything else is another script (or a broken character).
NOT_ENGLISH = re.compile(r"[^\x00-\x7f\u00a0-\u024f\u2010-\u2027\u00b0]")
# When this share of the letters or more is in another script, the whole call is noise.
FOREIGN_SHARE = 0.25


def is_english(text: str) -> bool:
    return NOT_ENGLISH.search(text) is None


def drop_foreign(text: str) -> tuple[str, bool]:
    """Removes characters from other scripts. Returns ("", True) when a quarter or more of the letters
    are, since then the whole call is made up."""
    foreign = len(NOT_ENGLISH.findall(text))
    if not foreign:
        return text, False
    letters = foreign + sum(ch.isalnum() for ch in NOT_ENGLISH.sub("", text))
    if foreign / max(letters, 1) >= FOREIGN_SHARE:
        return "", True
    cleaned = re.sub(r"\s+", " ", NOT_ENGLISH.sub(" ", text))
    cleaned = re.sub(r"\s+([,.;!?])", r"\1", cleaned)
    return re.sub(r"([,.;!?])\1+", r"\1", cleaned).strip(), True


def _key(text: str) -> str:
    return re.sub(r"[^\w\s'-]", "", text.lower()).strip()


def _items(text: str) -> list[str]:
    """Splits a transcript into comma/period separated items, keeping each item's punctuation."""
    return [item for item in re.split(r"(?<=[,.;!?])\s+", text.strip()) if item]


def _item_key(item: str) -> str:
    return _key(item.rstrip(",.;!?"))


def is_stock_phrase(text: str) -> bool:
    return _key(text) in STOCK_PHRASES


def collapse_loops(items: list[str]) -> list[str]:
    """Keeps one copy of any phrase of 1-4 items that repeats 3 or more times in a row."""
    out = list(items)
    for size in range(1, LOOP_MAX_ITEMS + 1):
        i = 0
        result = []
        while i < len(out):
            unit = [_item_key(x) for x in out[i:i + size]]
            repeats = 1
            while (len(unit) == size and
                   [_item_key(x) for x in out[i + repeats * size:i + (repeats + 1) * size]] == unit):
                repeats += 1
            if len(unit) == size and repeats >= LOOP_MIN_REPEATS and any(unit):
                result.extend(out[i:i + size])
                i += repeats * size
                # A loop often ends mid-word when the call does ("10-7, 10-7, 10")
                if i == len(out) - 1 and unit[0].startswith(_item_key(out[i]) or "\0"):
                    i += 1
            else:
                result.append(out[i])
                i += 1
        out = result
    return out


def drop_counting(items: list[str]) -> list[str]:
    """Removes runs of 6 or more bare numbers that never go down ("10, 11, 11, 12, 13, 14")."""
    out, i = [], 0
    while i < len(items):
        j = i
        previous = None
        while j < len(items):
            key = _item_key(items[j])
            if not key.isdigit() or (previous is not None and int(key) < previous):
                break
            previous = int(key)
            j += 1
        if j - i >= COUNTING_RUN:
            i = j
        else:
            out.append(items[i])
            i += 1
    return out


def drop_prompt_echo(items: list[str], prompt_terms: list[str]) -> list[str]:
    """Removes 3 or more hint-list terms in a row that follow each other closely in the hint list."""
    order = {}
    for index, term in enumerate(prompt_terms):
        order.setdefault(_key(term), index)
    out, i = [], 0
    while i < len(items):
        j, last = i, -1
        while j < len(items):
            index = order.get(_item_key(items[j]), -1)
            if index <= last or (last >= 0 and index - last > ECHO_MAX_STEP):
                break
            last = index
            j += 1
        if j - i >= ECHO_RUN:
            i = j
        else:
            out.append(items[i])
            i += 1
    return out


def remove_junk(text: str, confidence: float | None, prompt_terms: list[str],
                stock_phrase_logprob: float = -0.7, english_only: bool = True) -> tuple[str, list[str]]:
    """Returns the transcript with made-up parts removed, and what was removed (for the log)."""
    if not text:
        return text, []
    removed = []
    if english_only:
        text, foreign = drop_foreign(text)
        if foreign:
            removed.append("foreign script")
            if not text:
                return "", removed
    if is_stock_phrase(text) and (confidence is None or confidence < stock_phrase_logprob):
        return "", removed + ["stock phrase"]

    items = _items(text)
    for name, step in (("loop", collapse_loops), ("counting", drop_counting),
                       ("hint echo", lambda x: drop_prompt_echo(x, prompt_terms))):
        before = len(items)
        items = step(items)
        if len(items) != before:
            removed.append(name)
    if not removed:
        return text, []
    if removed == ["foreign script"]:
        return text, removed

    cleaned = " ".join(items).strip()
    # Tidy the end: a removed tail can leave a trailing comma
    cleaned = re.sub(r"[,;]\s*$", ".", cleaned)
    if is_stock_phrase(cleaned) and (confidence is None or confidence < stock_phrase_logprob):
        return "", removed + ["stock phrase"]
    return cleaned, removed
