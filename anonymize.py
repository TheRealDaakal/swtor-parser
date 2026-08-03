"""
anonymize.py

Scrubs player-identifying tokens from a raw combat log before it's shared
(posted for help, attached to a bug report, etc.) -- SWTOR embeds a
player's exact character name and account id directly in every line they
appear on: `@Name#accountid` (see log_parser.py's own docstring for the
full entity grammar). NPC/boss names are left untouched -- they aren't
personally identifying and stripping them would make the log useless for
its actual purpose (mechanic timing, boss recognition).

The SAME real player always maps to the SAME placeholder within one file
(Player1, Player2, ...), assigned in first-seen order -- so who-did-what-
to-whom stays analyzable (rotation timing, taunt swaps, interrupts) while
the actual identity is gone. A fake but well-formed account id is kept on
each placeholder so the anonymized file still parses cleanly through this
app's own log_parser.py (or any other parser expecting the real grammar),
not just readable by a human.
"""

import re
from typing import Dict, List, Tuple

# @Name#accountid -- name can contain almost anything (including spaces,
# confirmed against real logs) except the bracket/pipe delimiters that
# structurally can't appear inside it; non-greedy up to the first #digits
# so a pet's owner token (@Owner#id/PetName {id}:instanceid) is matched
# correctly too, without also eating the pet's own name.
_PLAYER_TOKEN_RE = re.compile(r"@[^\]|]*?#\d+")


def anonymize_lines(lines: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """Returns (scrubbed_lines, name_map) where name_map is {original
    token: placeholder token}, e.g. {"@Hawt Sauce#689...": "@Player1#00000001"}
    -- handed back so a caller can show the user what was actually
    replaced, if wanted, without them having to diff the files."""
    name_map: Dict[str, str] = {}
    counter = [0]

    def _replace(m: "re.Match[str]") -> str:
        token = m.group(0)
        if token not in name_map:
            counter[0] += 1
            name_map[token] = f"@Player{counter[0]}#{counter[0]:08d}"
        return name_map[token]

    scrubbed = [_PLAYER_TOKEN_RE.sub(_replace, line) for line in lines]
    return scrubbed, name_map


def anonymize_file(source_path: str, dest_path: str) -> Dict[str, str]:
    """Reads source_path (same cp1252/errors=replace convention every
    other reader in this app uses for real SWTOR log files), writes the
    scrubbed copy to dest_path, and returns the name_map."""
    with open(source_path, "r", encoding="cp1252", errors="replace") as f:
        lines = f.readlines()
    scrubbed, name_map = anonymize_lines(lines)
    with open(dest_path, "w", encoding="cp1252", errors="replace") as f:
        f.writelines(scrubbed)
    return name_map
