"""Tells the numbers in a transcript apart: unit call signs, house numbers, apartment numbers, years and
911, so a "650 Main" or a "2026 white Ford" isn't shown as a unit and a "County, 1346" isn't turned into
a time.

Whisper writes every number the same way, so this goes by the words around it:
    house number   "1360 Butler Street", "2056, Bally Drive", "4710, BethPike" (a street from places.csv
                   follows), "block of", or the same number said as an address elsewhere in the call
    apartment      "Apartment 505", "Room 12", "Lot 4"
    year           "a 2026 White Ford", "in 2024"
    911            "a 911 hang-up", "911 open line"
    unit           "County, 2851", "2851, County", "2851 responding": a unit calling or being called
Numbers with no such clue are left for the caller to decide (None).
"""

import re
from dataclasses import dataclass

STREET_WORDS = (r"street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|way|court|ct|pike|highway|hwy|"
                r"place|pl|circle|terrace|parkway|pkwy|alley")
# 3 to 5 digits on their own ("2851", not "10-23", "13:46" or "21,876").
NUMBER = re.compile(r"(?<![\d:.,-])(\d{3,5})(?![\d:]|-\d|,\d{3}\b)")
# One to three capitalized words between (not "1385, St. Luke's"): "1360 Butler Street", "631, South 25th Street".
STREET_AFTER = re.compile(rf",?\s+(?:(?:north|south|east|west|n|s|e|w)\.?\s+)?(?-i:(?:(?:[A-Z][\w'.-]*|\d+(?:st|nd|rd|th))\s+){{1,3}})"
                          rf"(?:{STREET_WORDS})\b\.?", re.IGNORECASE)
BLOCK_AFTER = re.compile(r",?\s+block\b", re.IGNORECASE)
ADDRESS_BEFORE = re.compile(r"\b(?:block\s+of|address(?:\s+is|\s+of)?|located\s+at|it'?s\s+going\s+to\s+be)\W*$",
                            re.IGNORECASE)
APARTMENT_BEFORE = re.compile(r"\b(?:apartment|apt|room|rm|suite|ste|lot|building|bldg|floor|unit\s+number|"
                              r"pod|bed|bay)\.?\s*#?\s*$", re.IGNORECASE)
COLORS = (r"white|black|silver|gr[ae]y|red|blue|green|tan|gold|maroon|brown|beige|orange|yellow|purple|burgundy|"
          r"dark|light")
MAKES = (r"ford|chevy|chevrolet|honda|toyota|nissan|dodge|jeep|gmc|ram|hyundai|kia|subaru|bmw|mercedes|audi|"
         r"volkswagen|vw|tesla|lexus|acura|mazda|buick|cadillac|chrysler|lincoln|mitsubishi|volvo|infiniti|"
         r"freightliner|kenworth|peterbilt|mack|harley|pontiac|saturn|mini|genesis|sedan|suv|pickup|truck|van|"
         r"motorcycle|model|plate|registration|tag")
YEAR = re.compile(r"(?:19[5-9]\d|20[0-3]\d)$")
YEAR_AFTER = re.compile(rf"\s+(?:{COLORS}|{MAKES})\b", re.IGNORECASE)
YEAR_BEFORE = re.compile(r"\b(?:in|since|year|of|back\s+in)\s+$", re.IGNORECASE)
NINE_ONE_ONE_AFTER = re.compile(r"\W*(?:call|calls|caller|hang|hangup|open|line|phase|transfer|center|"
                                r"dispatch|text|abandoned|disconnect)", re.IGNORECASE)
# A unit calling or being called: "County, 2851", "2851, County", "2851 responding", "from 2857". Not
# "..., County, 1914." at the very end, which is how dispatch signs off with the time.
COUNTY = r"(?:county|norco)"
UNIT_BEFORE = re.compile(rf"\b{COUNTY}\W*$", re.IGNORECASE)
FROM_BEFORE = re.compile(r"\bfrom\s+$", re.IGNORECASE)
UNIT_CALLING = re.compile(rf"\W*{COUNTY}\b", re.IGNORECASE)
CALLING = "a unit calling or being called"
STATUS = "a unit giving its status"
UNIT_AFTER = re.compile(rf"\W*(?:{COUNTY}\b|(?:is\s+|i'?m\s+|we'?re\s+|will\s+be\s+|i'?ll\s+be\s+)?(?:responding|"
                        rf"en ?route|on (?:the )?scene|on location|arriving|arrived|clear|available|in service|"
                        rf"out of service|copy|copies|go ahead|10-\d+))", re.IGNORECASE)


@dataclass
class Number:
    start: int
    end: int
    text: str
    kind: str | None          # address, apartment, year, 911, unit or None
    why: str = ""


def read_numbers(text: str | None, streets=None) -> list[Number]:
    """Every 3 to 5 digit number in the text with what it most likely is. streets(text, position) is True
    when a known street name starts there (PlaceBook.street_at), for addresses said without "Street"."""
    if not text:
        return []
    found = []
    for match in NUMBER.finditer(text):
        found.append(Number(match.start(1), match.end(1), match.group(1),
                            *kind_at(text, match.start(1), match.end(1), streets)))
    # A number said as an address once is that address everywhere in the call ("1360 Butler Street,
    # 1360 Butler" or "1360, 1360 Butler Street").
    addresses = {n.text for n in found if n.kind == "address"}
    for n in found:
        if n.kind is None and n.text in addresses:
            n.kind, n.why = "address", "said as an address elsewhere in the call"
    return found


def not_unit(numbers: list[Number], start: int, end: int) -> bool:
    """True when the number at start..end is a house or apartment number, a year or 911."""
    return any(n.kind in ("address", "apartment", "year", "911") and n.start < end and start < n.end
               for n in numbers)


def kind_at(text: str, start: int, end: int, streets=None) -> tuple[str | None, str]:
    """What the number at start..end is ("13 46" may have a space), and why."""
    digits = re.sub(r"\D", "", text[start:end])
    before, after = text[:start], text[end:]
    if APARTMENT_BEFORE.search(before):
        return "apartment", "after apartment/room/lot"
    if STREET_AFTER.match(after) or BLOCK_AFTER.match(after):
        return "address", "a street follows"
    # A street name without "Street" only right after the number: "1360 Butler", not "2714, Janet".
    if streets is not None and re.match(r"\s+\S", after) and streets(text, end + len(after) - len(after.lstrip())):
        return "address", "a known street follows"
    if ADDRESS_BEFORE.search(before):
        return "address", "said as an address"
    if digits == "911" and (NINE_ONE_ONE_AFTER.match(after) or re.search(r"\b(?:a|the)\s+$", before, re.I)):
        return "911", "911 call"
    if YEAR.match(digits) and (YEAR_AFTER.match(after) or YEAR_BEFORE.search(before)):
        return "year", "a year"
    if len(digits) in (3, 4) and (UNIT_CALLING.match(after) or FROM_BEFORE.search(before) or
                                  (UNIT_BEFORE.search(before) and after.strip(" .!?"))):
        return "unit", CALLING
    if len(digits) in (3, 4) and UNIT_AFTER.match(after):
        return "unit", STATUS
    return None, ""
