"""Pure SWTOR combat-log extraction helpers extracted from log_parser.py."""
import re
from typing import Optional

ABILITY_ID_RE = re.compile(r"\{(\d+)\}")
ANGLE_VALUE_RE = re.compile(r"<(-?\d+(?:\.\d+)?)>")
AVOIDANCE_RE = re.compile(r"-(miss|dodge|parry|deflect|resist)\b")
ENTITY_ID_RE = re.compile(r"\{[^{}]*\}")
LEADING_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")
OVERHEAL_RE = re.compile(r"~(\d+(?:\.\d+)?)")
SHIELD_ABSORBED_RE = re.compile(r"-shield\b.*?\((\d+(?:\.\d+)?)\s+absorbed", re.DOTALL)

def _extract_id(text: str) -> Optional[str]:
    """The numeric {id} out of a bracket field, e.g.
    'Force Leap {812105301229568}' -> '812105301229568'. Kept as a string:
    it's only ever compared for equality, and these are 16-digit values
    that have no business being arithmetic."""
    if not text:
        return None
    m = ABILITY_ID_RE.search(text)
    return m.group(1) if m else None

def _clean_name(text: str) -> Optional[str]:
    """Strip trailing {id} tags and leading '@' from a bracket field, e.g.
    'Force Leap {812105301229568}' -> 'Force Leap'
    '@Idrurrez' -> 'Idrurrez'
    """
    if text is None:
        return None
    text = ENTITY_ID_RE.sub("", text).strip()
    text = text.lstrip("@").strip()
    return text or None

def _first_balanced_paren(text: str) -> Optional[str]:
    """Returns the content of the first top-level parenthesized group in
    text (honoring nesting), or None if there isn't a balanced one. Needed
    because SWTOR sometimes nests a sub-annotation inside the value group,
    e.g. '(3238 energy {id} -shield {id} (4121 absorbed {id}))' or
    '(488 kinetic {id}(reflected {id}))' -- a naive non-nesting regex
    matches only the inner '(4121 absorbed {id})' / '(reflected {id})'
    piece, which silently substitutes the wrong number (the absorbed
    sub-amount, or even a stray {id} digit string when there's no leading
    number at all) for the real damage/heal amount.
    """
    start = text.find("(")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None

def _extract_angle_value(tail: str) -> float:
    match = ANGLE_VALUE_RE.search(tail)
    return float(match.group(1)) if match else 0.0

def _extract_amount(tail: str) -> float:
    """Pulls the event's primary amount out of the trailing value text
    (everything after the core brackets). The real amount is always the
    leading numeric token of the FIRST top-level parenthesized group, even
    when that group carries a nested reflected/absorbed-damage annotation
    (see _first_balanced_paren) -- e.g. '1234 energy...' or '1234*' (crit).
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return 0.0
    match = LEADING_NUMBER_RE.match(group)
    return float(match.group(1)) if match else 0.0

def _extract_is_critical(tail: str) -> bool:
    """The leading number in the value group carries the crit marker
    directly, e.g. '9578* energy {id}' -> True, '9578 energy {id}' -> False.
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return False
    match = LEADING_NUMBER_RE.match(group)
    if not match:
        return False
    return group[match.end():match.end() + 1] == "*"

def _extract_avoidance(tail: str) -> Optional[str]:
    """Pulls the defense-roll outcome out of the same value group
    `_extract_amount` reads, e.g. '(0 -dodge {id})' -> "dodge". Always
    accompanies a 0 amount -- the attack never landed, as opposed to a
    landed hit that got shielded down to 0 (see _extract_shield_absorbed).
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return None
    match = AVOIDANCE_RE.search(group)
    return match.group(1) if match else None

def _extract_shield_absorbed(tail: str) -> float:
    """Pulls the tank shield-mitigation amount out of the same value group
    `_extract_amount` reads, e.g. '3238 energy {id} -shield {id} (4121
    absorbed {id})' -> 4121. `amount` above already captured the 3238 that
    got through; this is the part that didn't.

    Deliberately keyed on the literal '-shield' marker, not just the
    presence of "(N absorbed {id})" alone: a bare "(N absorbed {id})" with
    no '-shield' sibling is a different mechanic entirely (a hard
    absorb-shield BUFF like Static Barrier/Force Armor covering its full
    hit, already tracked separately in dots_hots.py) -- conflating the two
    would misattribute a healer's shield buff as tank stat mitigation.
    """
    group = _first_balanced_paren(tail)
    if group is None:
        return 0.0
    match = SHIELD_ABSORBED_RE.search(group)
    return float(match.group(1)) if match else 0.0

def _extract_overheal(tail: str) -> float:
    """Pulls a heal's wasted-overheal amount out of the same value group
    `_extract_amount` reads, e.g. '10926* ~7382' -> 7382. Zero overheal
    (target wasn't near full) doesn't get a '~0' suffix on every line --
    it does in practice (SWTOR always emits it), but treat absence the
    same as explicit zero either way."""
    group = _first_balanced_paren(tail)
    if group is None:
        return 0.0
    match = OVERHEAL_RE.search(group)
    return float(match.group(1)) if match else 0.0
