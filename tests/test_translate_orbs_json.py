"""
Covers the P0 fixes to tools/translate_orbs_json.py, found by a post-merge
audit: 34.5% of the timers merged from ORBS were silently non-functional --
well-formed JSON that loaded without error but could never fire. Three
independent causes, each with its own test class here so a regression in
any one is caught in isolation:

  - numeric ids passed through as unmatchable keywords (326 timers)
  - colliding timer ids from non-unique slugify(Name) (34 ids / 70 timers)
  - dangling timer_expires references (35 timers)

Plus the difficulty-variant dedupe (a real bug found the same night, via
live verification rather than the later audit): ORBS keeps a separate timer
per raid size/mode for the same mechanic, and our engine has no difficulty
filtering to tell them apart.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import translate_orbs_json as orbs


def _timer(id, name, trigger_type, **kwargs):
    """Minimal real-shaped ORBS timer dict -- only the fields
    translate_timer actually reads, defaulted the way ORBS's own export
    does (empty string/0/False, not missing keys)."""
    base = {
        "Id": id, "Name": name, "TriggerType": trigger_type,
        "Source": kwargs.pop("Source", "Ignore"), "Target": kwargs.pop("Target", "Ignore"),
        "Ability": kwargs.pop("Ability", ""), "Effect": kwargs.pop("Effect", ""),
        "DurationSec": kwargs.pop("DurationSec", 10.0),
        "HPPercentage": kwargs.pop("HPPercentage", 0.0),
        "ExperiationTimerId": kwargs.pop("ExperiationTimerId", ""),
        "IsAlert": kwargs.pop("IsAlert", False),
        "AlertText": kwargs.pop("AlertText", ""),
        "Clause1": kwargs.pop("Clause1", None), "Clause2": kwargs.pop("Clause2", None),
    }
    base.update(kwargs)
    return base


def _block(*timers):
    return {"TimerSource": "Test Op|Test Boss", "Timers": list(timers)}


class TestNumericKeywordResolution:
    def test_display_text_passes_through_unchanged(self):
        block = _block(_timer("t1", "Smash", 2, Ability="Smash"))
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        assert rows[0]["trigger"]["keyword"] == "Smash"

    def test_resolvable_numeric_id_becomes_real_text(self):
        # a real id known to tools/ability_id_names.json
        block = _block(_timer("t1", "Mystery", 2, Ability="836045448953664"))
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        keyword = rows[0]["trigger"]["keyword"]
        assert keyword != "836045448953664", "a resolvable numeric id must not survive verbatim"
        assert not keyword.isdigit()

    def test_unresolvable_numeric_id_becomes_a_visible_placeholder(self):
        block = _block(_timer("t1", "Mystery", 2, Ability="999999999999999999"))
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        keyword = rows[0]["trigger"]["keyword"]
        assert keyword.startswith("REPLACE_WITH_REAL_ABILITY_NAME_"), (
            "an id neither table knows must become an explicit, visible "
            "placeholder -- never a silently-wrong guess"
        )


class TestTimerIdUniqueness:
    def test_two_rules_sharing_a_display_name_get_distinct_ids(self):
        """The real audit finding (apex_vanguard.json): two genuinely
        different rules named "Acid Deluge" -- one triggered by an ability
        cast, one chained off a different timer's expiry -- both slugify
        to the same id, so the second silently overwrote the first. Unlike
        the difficulty-variant case, these have DIFFERENT trigger types/
        keys, so they must both survive as separate rules, not collapse."""
        block = _block(
            _timer("orbs-uuid-src", "Voltinator 2nd", 2, Ability="Voltinator", DurationSec=8.0),
            _timer("orbs-uuid-1", "Acid Deluge", 6, ExperiationTimerId="orbs-uuid-src", DurationSec=60.0),
            _timer("orbs-uuid-2", "Acid Deluge", 2, Ability="Acid Deluge", DurationSec=122.0),
        )
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        acid_rows = [r for r in rows if r["label"] == "Acid Deluge"]
        ids = [r["id"] for r in acid_rows]
        assert len(ids) == len(set(ids)), "distinct rules must never collide on id"
        assert len(acid_rows) == 2, "both differently-triggered rules must survive, not just one"


class TestDanglingRefPruning:
    def test_timer_expires_referencing_a_dropped_timer_is_itself_dropped(self):
        """A TimerExpired chaining off a timer that was itself dropped
        (unsupported trigger type here) must not survive with a reference
        to nothing -- that condition can never become true, so the timer
        would silently never fire."""
        block = _block(
            _timer("uuid-dead", "Unsupported", 17),  # VariableCheck -- not in SUPPORTED_TRIGGER_TYPES
            _timer("uuid-chain", "Chained", 6, ExperiationTimerId="uuid-dead"),
        )
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        assert rows == [], "the chained timer must be pruned along with what it depends on"

    def test_timer_expires_referencing_a_surviving_timer_works(self):
        block = _block(
            _timer("uuid-a", "First", 2, Ability="Cast One", DurationSec=5.0),
            _timer("uuid-b", "Second", 6, ExperiationTimerId="uuid-a", DurationSec=10.0),
        )
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        assert len(rows) == 2
        second = next(r for r in rows if r["label"] == "Second")
        first = next(r for r in rows if r["label"] == "First")
        assert second["trigger"]["timer_id"] == first["id"]

    def test_forward_reference_resolves(self):
        """ExperiationTimerId pointing at a timer defined LATER in ORBS's
        own list must still resolve -- ids are assigned in a pass before
        any trigger is built, specifically to support this."""
        block = _block(
            _timer("uuid-b", "Second", 6, ExperiationTimerId="uuid-a", DurationSec=10.0),
            _timer("uuid-a", "First", 2, Ability="Cast One", DurationSec=5.0),
        )
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        assert len(rows) == 2
        second = next(r for r in rows if r["label"] == "Second")
        first = next(r for r in rows if r["label"] == "First")
        assert second["trigger"]["timer_id"] == first["id"]


class TestDifficultyVariantDedupe:
    def test_collapses_to_the_longest_duration(self):
        block = _block(
            _timer("t1", "Vomit Pool 16m", 2, Ability="Vomit Pool", DurationSec=15.0),
            _timer("t2", "Vomit Pool 8m VM", 2, Ability="Vomit Pool", DurationSec=12.0),
            _timer("t3", "Vomit Pool 8m", 2, Ability="Vomit Pool", DurationSec=10.0),
        )
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        assert len(rows) == 1, "same mechanic, different difficulty guesses -- must collapse to one"
        assert rows[0]["duration_seconds"] == 15.0, "must keep the longest (safer than expiring early)"

    def test_genuinely_different_mechanics_sharing_a_trigger_are_not_collapsed(self):
        """Same trigger (an NPC spawn), but different mechanics -- must NOT
        be treated as difficulty variants of each other just because they
        share a trigger."""
        block = _block(
            _timer("t1", "Choke Clone 1st", 14, Source="9999", DurationSec=52.0),
            _timer("t2", "Charge Clone 1st", 14, Source="9999", DurationSec=40.0),
        )
        rows = orbs.translate_source_block(block, "test", "[ctx]")
        assert len(rows) == 2, "different mechanics must both survive despite sharing a trigger"
