"""
Covers the Kolto Pods pile-up bug: TimerEngine.start_timer() used to always
append a new ActiveTimer, so a rapidly-retriggered effect (e.g. a HoT ticking
every ~0.9s on the same target) piled up near-duplicate rows -- 92
simultaneous entries measured on a real log before the fix. The fix is
dedupe_key: reapplying the same (label, category, dedupe_key) refreshes the
existing entry instead of stacking a new one, while a different dedupe_key
(different target) still gets its own row.
"""
from conftest import log_line
from log_parser import parse_line
from timers import TimerEngine, TimerRule
from cooldowns import register_defensive_cooldowns


def test_a_targets_death_clears_a_dot_still_ticking_on_it(sim_clock):
    """Reported live: a dot kept showing as "ticking" on an NPC well after
    it actually died. SWTOR doesn't reliably log a RemoveEffect line for
    every dot at the moment of death, so the target's own Death event has
    to be a second, independent clear signal alongside RemoveEffect and
    the dot's own timer running out -- otherwise it just sits there
    counting down on an entity that's already gone."""
    engine = TimerEngine()
    sim_clock(0.0)
    engine.add_rule(TimerRule(
        keyword="Affliction", label="Affliction", duration_seconds=18.0,
        category="dot", event_type="applied", only_local_player=True,
    ))
    applied = parse_line(
        log_line("00:00:00.000", "@Sorc#1", target="Add", ability="Affliction {1}",
                  effect_type="ApplyEffect", effect_name="Affliction {1}"),
        line_number=1,
    )
    engine.feed(applied, local_player_name="Sorc")
    assert len(engine.active) == 1, "sanity check: the dot is tracked before the death"

    death = parse_line(
        log_line("00:00:02.000", "Add", target="Add", effect_type="ApplyEffect", effect_name="Death {1}"),
        line_number=2,
    )
    engine.feed(death, local_player_name="Sorc")

    assert engine.active == [], (
        "the dot must be cleared the instant its target dies, not linger until its own timer runs out"
    )


def test_a_different_targets_death_does_not_clear_unrelated_dots(sim_clock):
    engine = TimerEngine()
    sim_clock(0.0)
    engine.add_rule(TimerRule(
        keyword="Affliction", label="Affliction", duration_seconds=18.0,
        category="dot", event_type="applied", only_local_player=True,
    ))
    for target in ("Add1", "Add2"):
        applied = parse_line(
            log_line("00:00:00.000", "@Sorc#1", target=target, ability="Affliction {1}",
                      effect_type="ApplyEffect", effect_name="Affliction {1}"),
            line_number=1,
        )
        engine.feed(applied, local_player_name="Sorc")
    assert len(engine.active) == 2

    death = parse_line(
        log_line("00:00:02.000", "Add1", target="Add1", effect_type="ApplyEffect", effect_name="Death {1}"),
        line_number=3,
    )
    engine.feed(death, local_player_name="Sorc")

    assert len(engine.active) == 1
    assert engine.active[0].dedupe_key == "Add2"


def test_death_of_an_unrelated_entity_does_not_touch_cooldown_category_timers(sim_clock):
    """Cooldown-category entries track the LOCAL PLAYER's own defensives,
    keyed by dedupe_key too -- some other entity dying must not clear
    those, only dot/hot entries."""
    engine = TimerEngine()
    sim_clock(0.0)
    engine.start_timer("Deflection", 120.0, category="cooldown", dedupe_key="Sorc")

    death = parse_line(
        log_line("00:00:02.000", "Add", target="Add", effect_type="ApplyEffect", effect_name="Death {1}"),
        line_number=1,
    )
    engine.feed(death, local_player_name="Sorc")

    assert len(engine.active) == 1, "a cooldown entry must survive an unrelated entity's death"


def test_same_dedupe_key_refreshes_in_place(sim_clock):
    engine = TimerEngine()
    sim_clock(0.0)
    engine.start_timer("Kolto Probe", duration_seconds=15.0, category="hot", dedupe_key="Ally1")
    assert len(engine.active) == 1

    sim_clock(5.0)
    engine.start_timer("Kolto Probe", duration_seconds=15.0, category="hot", dedupe_key="Ally1")
    assert len(engine.active) == 1, "reapplying on the same target must refresh, not stack"

    remaining = engine.active[0].remaining()
    assert remaining == 15.0, "refreshing should reset the countdown to the full duration"


def test_different_dedupe_key_gets_its_own_row(sim_clock):
    engine = TimerEngine()
    sim_clock(0.0)
    engine.start_timer("Kolto Probe", duration_seconds=15.0, category="hot", dedupe_key="Ally1")
    engine.start_timer("Kolto Probe", duration_seconds=15.0, category="hot", dedupe_key="Ally2")
    assert len(engine.active) == 2, "different targets are genuinely separate instances"


def test_dedupe_scoped_by_category_too(sim_clock):
    """Same label, same target, different category (e.g. a dot vs. a hot
    that happen to share a name) must not collide."""
    engine = TimerEngine()
    sim_clock(0.0)
    engine.start_timer("Corrosive", duration_seconds=10.0, category="dot", dedupe_key="Boss")
    engine.start_timer("Corrosive", duration_seconds=10.0, category="hot", dedupe_key="Boss")
    assert len(engine.active) == 2


def test_no_dedupe_key_never_collapses(sim_clock):
    """dedupe_key=None (the default, used by custom/boss timers) must keep
    the old always-append behavior -- this scoping is deliberate, not
    accidental: boss mechanic timers can legitimately have several
    simultaneous instances (e.g. the same alert firing for several add
    spawns) and collapsing those would hide real information."""
    engine = TimerEngine()
    sim_clock(0.0)
    engine.start_timer("Add Spawn", duration_seconds=20.0, category="custom")
    engine.start_timer("Add Spawn", duration_seconds=20.0, category="custom")
    assert len(engine.active) == 2


def test_realistic_kolto_pods_burst_stays_bounded(sim_clock):
    """Reproduces the actual bug shape: one HoT retriggering every ~0.9s on
    a handful of different raid members during a burst-heal moment. Before
    the fix this piled up unboundedly (92 simultaneous rows measured on a
    real log); after it, the count is capped at the number of DISTINCT
    targets, regardless of how many ticks land."""
    engine = TimerEngine()
    targets = [f"Player{i}" for i in range(5)]
    t = 0.0
    max_seen = 0
    for _tick in range(30):  # far more ticks than targets
        for target in targets:
            sim_clock(t)
            engine.start_timer("Kolto Pods", duration_seconds=2.5, category="hot", dedupe_key=target)
            max_seen = max(max_seen, len(engine.active))
            t += 0.9

    assert max_seen == len(targets), (
        f"expected the pile-up to stay bounded at {len(targets)} distinct targets, "
        f"saw {max_seen} simultaneous entries"
    )


def test_adrenaline_rush_ticks_dont_pile_up(sim_clock):
    """Same bug shape as Kolto Pods, but for personal cooldowns: Adrenaline
    Rush is itself a periodic self-heal, so the game logs one ApplyEffect
    line per tick under the ability's own name, not just once on cast.
    TimerEngine.feed() only wired dedupe_key through for category in
    ("dot", "hot"), so every tick appended a fresh "Adrenaline Rush" buff
    row instead of refreshing the one already counting down -- reported
    live as "multiple Adrenaline Rushes" showing at once."""
    engine = TimerEngine()
    register_defensive_cooldowns(engine)
    log_name = "@Vanguard#1"  # source/target as written in the log line
    parsed_name = "Vanguard"  # what parse_line strips it down to on event.source --
                               # local_player_name is compared against that, not the raw log form

    t = 0.0
    for _tick in range(15):  # ~60s of 4s heal ticks -- one full buff uptime
        sim_clock(t)
        ev = parse_line(
            log_line(f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}",
                      log_name, target=log_name, ability="Adrenaline Rush {1}",
                      effect_name="Heal {2}", amount="500"),
            line_number=1,
        )
        engine.feed(ev, local_player_name=parsed_name)
        t += 4.0

    rush_rows = [row for row in engine.snapshot("cooldown") if row[0] == "Adrenaline Rush"]
    assert len(rush_rows) == 1, (
        f"expected exactly one 'Adrenaline Rush' buff row after repeated ticks, "
        f"got {len(rush_rows)}"
    )


def test_modify_charges_does_not_refresh_an_applied_scoped_rule(sim_clock):
    """A charge-shield's per-hit ModifyCharges line (Kolto Shell, Trauma
    Probe) isn't a fresh cast. An "applied"-scoped rule matching on the
    ability name would otherwise keep resetting to full duration on every
    single absorb, never actually counting down while the shield is being
    used -- the same category of bug as Adrenaline Rush's ticks, just for a
    rule with event_type="applied" instead of dedupe_key."""
    engine = TimerEngine()
    engine.add_rule(TimerRule(
        keyword="Kolto Shell", label="Kolto Shell", duration_seconds=180.0,
        voice_alert=False, category="hot", event_type="applied", only_local_player=True,
    ))

    sim_clock(0.0)
    ev = parse_line(
        log_line("00:00:00.000", "@Healer#1", target="@Tank#1", ability="Kolto Shell {1}",
                  effect_type="ApplyEffect", effect_name="Kolto Shell {1}", amount="7 charges {2}"),
        line_number=1,
    )
    engine.feed(ev, local_player_name="Healer")
    assert len(engine.active) == 1
    assert engine.active[0].remaining() == 180.0

    sim_clock(50.0)
    modify_ev = parse_line(
        log_line("00:00:50.000", "@Healer#1", target="@Tank#1", ability="Kolto Shell {1}",
                  effect_type="ModifyCharges", effect_name="Kolto Shell {1}", amount="4 charges {2}"),
        line_number=2,
    )
    assert modify_ev.is_charges_modified
    engine.feed(modify_ev, local_player_name="Healer")

    assert len(engine.active) == 1, "a charge-loss event must not spawn a second timer"
    remaining = engine.active[0].remaining()
    assert remaining == 130.0, (
        f"expected the countdown to keep running from the original cast (130s left after "
        f"50s), but a ModifyCharges line reset it to {remaining}s"
    )


def test_ability_activate_does_not_spawn_a_ghost_dot_entry(sim_clock):
    """Reported live: a DoT tracker panel showing two simultaneous entries
    for the same DoT (e.g. Interrogation Probe) on one boss. Root cause:
    the AbilityActivate line (the cast itself) carries the same ability
    name as the ApplyEffect line that follows it, so it also matches an
    "applied"-scoped rule's keyword -- but AbilityActivate's target logs as
    "=" (same as source, i.e. the CASTER), not the real recipient. That
    created a permanent ghost entry keyed to the caster's own name, which
    never gets refreshed again since every real tick targets the actual
    recipient (the boss) instead -- one real entry, one stuck ghost,
    showing as "two of the same DoT"."""
    engine = TimerEngine()
    engine.add_rule(TimerRule(
        keyword="Interrogation Probe", label="Interrogation Probe", duration_seconds=18.0,
        voice_alert=False, category="dot", event_type="applied", only_local_player=True,
    ))

    sim_clock(0.0)
    # The AbilityActivate line: real logs mark the target "=" (same as
    # source) here -- log_line() doesn't have a literal "=" mode, so this
    # passes the caster's own name as target to reach the same end state
    # (event.target == event.source), which is what actually matters for
    # this bug. Ability name is already "Interrogation Probe" even though
    # nothing has actually been applied to anything yet.
    activate_ev = parse_line(
        log_line("00:00:00.000", "@Sniper#1", target="@Sniper#1", ability="Interrogation Probe {1}",
                  effect_type="Event", effect_name="AbilityActivate {2}"),
        line_number=1,
    )
    assert activate_ev.is_ability_activate
    engine.feed(activate_ev, local_player_name="Sniper")
    assert len(engine.active) == 0, "AbilityActivate alone must not start a timer"

    sim_clock(0.2)
    apply_ev = parse_line(
        log_line("00:00:00.200", "@Sniper#1", target="Boss", ability="Interrogation Probe {1}",
                  effect_type="ApplyEffect", effect_name="Interrogation Probe {1}"),
        line_number=2,
    )
    engine.feed(apply_ev, local_player_name="Sniper")

    dot_rows = engine.snapshot("dot")
    assert len(dot_rows) == 1, (
        f"expected exactly one 'Interrogation Probe' entry (keyed to the boss), "
        f"got {len(dot_rows)}: {dot_rows}"
    )
    assert dot_rows[0][0] == "Interrogation Probe"


class TestAlacrityScaling:
    """apply_alacrity() and TimerRule.alacrity_affected -- SWTOR's combat
    log never reports a character's actual Alacrity%, so this is driven by
    a manually-entered value (see main.CharacterSettingsHolder), applied
    at the moment a rule fires rather than baked into the rule at
    registration time -- the character (and their typed-in Alacrity%)
    usually isn't known yet when dot/hot rules are registered at startup."""

    def test_apply_alacrity_formula(self):
        from timers import apply_alacrity
        assert apply_alacrity(18.0, 0.0) == 18.0, "0% alacrity must not change anything"
        assert apply_alacrity(18.0, 100.0) == 9.0, "100% alacrity halves duration (SWTOR's own formula)"
        assert abs(apply_alacrity(18.0, 20.0) - 15.0) < 0.001  # 18 / 1.2 = 15.0

    def test_negative_alacrity_is_a_noop_not_a_lengthening(self):
        from timers import apply_alacrity
        assert apply_alacrity(18.0, -5.0) == 18.0

    def test_feed_scales_duration_for_alacrity_affected_rules(self, sim_clock):
        engine = TimerEngine()
        sim_clock(0.0)
        engine.add_rule(TimerRule(
            keyword="Affliction", label="Affliction", duration_seconds=18.0,
            category="dot", event_type="applied", only_local_player=True,
            alacrity_affected=True,
        ))
        event = parse_line(
            log_line("00:00:00.000", "@Sorc#1", target="Boss", ability="Affliction {1}",
                      effect_type="ApplyEffect", effect_name="Affliction {1}"),
            line_number=1,
        )
        engine.feed(event, local_player_name="Sorc", alacrity_pct=20.0)
        assert len(engine.active) == 1
        assert abs(engine.active[0].duration_seconds - 15.0) < 0.001  # 18 / 1.2

    def test_feed_ignores_alacrity_for_unaffected_rules(self, sim_clock):
        """e.g. Kolto Shell -- a charge-based shield, not tick-based, must
        keep its full nominal duration regardless of alacrity_pct."""
        engine = TimerEngine()
        sim_clock(0.0)
        engine.add_rule(TimerRule(
            keyword="Kolto Shell", label="Kolto Shell", duration_seconds=180.0,
            category="hot", event_type="applied", only_target_is_local_player=True,
            alacrity_affected=False,
        ))
        event = parse_line(
            log_line("00:00:00.000", "@Healer#1", target="@Tank#1", ability="Kolto Shell {1}",
                      effect_type="ApplyEffect", effect_name="Kolto Shell {1}"),
            line_number=1,
        )
        engine.feed(event, local_player_name="Tank", alacrity_pct=20.0)
        assert len(engine.active) == 1
        assert engine.active[0].duration_seconds == 180.0, "unaffected rules must ignore alacrity_pct entirely"

    def test_feed_with_no_alacrity_pct_behaves_exactly_as_before(self, sim_clock):
        """Default alacrity_pct=0.0 -- an affected rule with no alacrity
        set yet must produce its plain nominal duration, same as
        pre-alacrity-feature behavior."""
        engine = TimerEngine()
        sim_clock(0.0)
        engine.add_rule(TimerRule(
            keyword="Affliction", label="Affliction", duration_seconds=18.0,
            category="dot", event_type="applied", only_local_player=True,
            alacrity_affected=True,
        ))
        event = parse_line(
            log_line("00:00:00.000", "@Sorc#1", target="Boss", ability="Affliction {1}",
                      effect_type="ApplyEffect", effect_name="Affliction {1}"),
            line_number=1,
        )
        engine.feed(event, local_player_name="Sorc")  # no alacrity_pct passed
        assert engine.active[0].duration_seconds == 18.0


class TestCustomAudioTrigger:
    """A TimerRule/start_timer() with audio_path set plays that .wav via
    audio.play_wav() instead of speaking the label -- see timers.py's
    _announce(). Custom Timers tab lets a user attach a .wav per rule."""

    def test_start_timer_with_audio_path_plays_wav_not_speech(self, monkeypatch, sim_clock):
        spoken, played = [], []
        import timers as timers_mod
        monkeypatch.setattr(timers_mod.audio, "speak", lambda text: spoken.append(text))
        monkeypatch.setattr(timers_mod.audio, "play_wav", lambda path: played.append(path))

        engine = TimerEngine()
        sim_clock(0.0)
        engine.start_timer("Interrupt Now", 5.0, audio_path=r"C:\sounds\interrupt.wav")

        assert played == [r"C:\sounds\interrupt.wav"]
        assert spoken == [], "a custom sound replaces the spoken label, doesn't play alongside it"
        assert engine.active[0].audio_path == r"C:\sounds\interrupt.wav"

    def test_start_timer_without_audio_path_still_speaks(self, monkeypatch, sim_clock):
        spoken, played = [], []
        import timers as timers_mod
        monkeypatch.setattr(timers_mod.audio, "speak", lambda text: spoken.append(text))
        monkeypatch.setattr(timers_mod.audio, "play_wav", lambda path: played.append(path))

        engine = TimerEngine()
        sim_clock(0.0)
        engine.start_timer("Taunt Swap", 5.0)

        assert spoken == ["Taunt Swap"], "no audio_path -- falls back to the existing TTS behavior"
        assert played == []

    def test_feed_threads_the_rules_audio_path_through(self, monkeypatch, sim_clock):
        played = []
        import timers as timers_mod
        monkeypatch.setattr(timers_mod.audio, "speak", lambda text: None)
        monkeypatch.setattr(timers_mod.audio, "play_wav", lambda path: played.append(path))

        engine = TimerEngine()
        sim_clock(0.0)
        engine.add_rule(TimerRule(
            keyword="Massive Slam", label="Massive Slam", duration_seconds=10.0,
            audio_path=r"C:\sounds\slam.wav",
        ))
        event = parse_line(
            log_line("00:00:00.000", "Boss", ability="Massive Slam {1}",
                      effect_name="AbilityActivate {1}"),
            line_number=1,
        )
        engine.feed(event)
        assert played == [r"C:\sounds\slam.wav"]

    def test_dedupe_refresh_updates_the_audio_path_too(self, monkeypatch, sim_clock):
        """A refreshed (same dedupe_key) timer must pick up whatever
        audio_path the LATEST start_timer() call passed, not keep
        whatever the first one had."""
        played = []
        import timers as timers_mod
        monkeypatch.setattr(timers_mod.audio, "speak", lambda text: None)
        monkeypatch.setattr(timers_mod.audio, "play_wav", lambda path: played.append(path))

        engine = TimerEngine()
        sim_clock(0.0)
        engine.start_timer("Pods", 5.0, dedupe_key="Boss", audio_path="a.wav")
        engine.start_timer("Pods", 5.0, dedupe_key="Boss", audio_path="b.wav")

        assert len(engine.active) == 1
        assert engine.active[0].audio_path == "b.wav"
        assert played == ["a.wav", "b.wav"]
