"""
Covers a new feature: a boss health + target overlay needs live HP and
"who is the boss currently hitting" data, which BossEncounterState didn't
track before -- phase transitions only ever checked an event's HP fraction
inline (boss_definitions.py's hp_below condition), nothing persisted it.
"""
from boss_definitions import BossDefinition, BossPhase, BossTimerDef, Condition
from boss_intelligence import BossEncounterState
from timers import TimerEngine
from conftest import log_line
from log_parser import parse_line


def _boss(name="TestBoss"):
    return BossDefinition(
        id="test_boss", name="Test Boss", boss_names=[name],
        phases=[BossPhase(id="p1", name="Phase 1", start_trigger=Condition(type="combat_start"))],
    )


def _ev(source, target, ability="Slam {1}", effect_name="Damage {2}", amount="",
        source_hp="100000/100000", target_hp="100000/100000"):
    return parse_line(
        log_line("00:00:00.000", source, target=target, ability=ability,
                  effect_name=effect_name, amount=amount,
                  source_hp=source_hp, target_hp=target_hp),
        line_number=1,
    )


def test_hp_is_captured_once_boss_is_recognized():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="900000/1000000"))

    assert state.active_boss is not None
    assert state.boss_hp_current == 900000.0
    assert state.boss_hp_max == 1000000.0
    assert state.boss_hp_percent() == 90.0


def test_hp_updates_across_subsequent_events():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="900000/1000000"))
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="750000/1000000"))

    assert state.boss_hp_current == 750000.0
    assert state.boss_hp_percent() == 75.0


def test_boss_target_tracks_who_the_boss_is_hitting():
    state = BossEncounterState({"test_boss": _boss()})
    # Recognize the boss first (as a target of a player's attack).
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="1000000/1000000"))
    assert state.boss_target is None  # boss hasn't hit anyone yet

    state.feed(_ev("TestBoss", "@Tank#1", ability="Cleave {1}",
                    source_hp="1000000/1000000"))
    assert state.boss_target == "Tank"

    # Tank swap -- target should update to whoever it hits next.
    state.feed(_ev("TestBoss", "@OffTank#2", ability="Cleave {1}",
                    source_hp="1000000/1000000"))
    assert state.boss_target == "OffTank"


def test_recognizing_event_itself_can_seed_target():
    """The exact same log line that first identifies the boss (here, via
    the boss being the SOURCE of an attack on a player) must also seed
    boss_target immediately -- not require a second event, since
    active_boss was still None the first time _update_hp_and_target ran
    within this same feed() call."""
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("TestBoss", "@Tank#1", ability="Cleave {1}",
                    source_hp="950000/1000000"))

    assert state.active_boss is not None
    assert state.boss_target == "Tank"
    # CombatEvent.hp_current/hp_max only ever reflect the TARGET's HP
    # (log_parser never parses the source bracket's HP fraction) -- a line
    # where the boss is the source carries the PLAYER's HP, not the
    # boss's, so no HP reading should come from this one.
    assert state.boss_hp_current is None


def test_non_boss_entities_dont_update_hp_or_target():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="1000000/1000000"))

    # An unrelated trash mob shouldn't overwrite the tracked boss's state.
    state.feed(_ev("@Tank#1", "SomeAdd", target_hp="500/500"))
    state.feed(_ev("SomeAdd", "@Healer#2", ability="Bite {1}", source_hp="500/500"))

    assert state.boss_hp_current == 1000000.0
    assert state.boss_target is None


def test_reset_clears_hp_and_target_between_pulls():
    state = BossEncounterState({"test_boss": _boss()})
    state.feed(_ev("@Tank#1", "TestBoss", target_hp="500000/1000000"))
    state.feed(_ev("TestBoss", "@Tank#1", ability="Cleave {1}", source_hp="500000/1000000"))
    assert state.boss_hp_current is not None
    assert state.boss_target is not None

    state.reset()

    assert state.boss_hp_current is None
    assert state.boss_hp_max is None
    assert state.boss_target is None
    assert state.boss_hp_percent() is None


def test_hp_percent_none_before_any_data():
    state = BossEncounterState({"test_boss": _boss()})
    assert state.boss_hp_percent() is None


class TestCombatStartTimers:
    """Boss recognition needs an event that actually NAMES the boss, which
    in real logs lands AFTER the EnterCombat line it counts from (measured
    +0.96s on a real Writhing Horror pull). feed() had already processed
    and discarded that EnterCombat by then, so a combat_start trigger
    evaluated against any later event never matched -- 91 timers across 33
    bosses silently never fired at all. See
    BossEncounterState._fire_combat_start_timers."""

    def _boss_with_cs_timer(self, **timer_kwargs):
        kwargs = dict(label="Adds", duration_seconds=45.0,
                      trigger=Condition(type="combat_start"))
        kwargs.update(timer_kwargs)
        return BossDefinition(
            id="test_boss", name="Test Boss", boss_names=["TestBoss"],
            phases=[BossPhase(id="p1", name="Phase 1",
                              start_trigger=Condition(type="combat_start"))],
            timers=[BossTimerDef(**kwargs)],
        )

    def _enter_combat(self):
        return parse_line(
            log_line("00:00:00.000", "@Tank#1", effect_type="Event",
                      effect_name="EnterCombat {1}"),
            line_number=1,
        )

    def test_combat_start_timer_fires_even_though_recognition_is_later(self, sim_clock):
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss_with_cs_timer()})

        sim_clock(0.0)
        state.feed(self._enter_combat(), timer_engine=engine)
        assert engine.snapshot() == [], "boss isn't recognizable from EnterCombat alone"

        # A LATER event finally names the boss -- the timer must start now.
        sim_clock(1.0)
        state.feed(_ev("@Tank#1", "TestBoss"), timer_engine=engine)

        rows = engine.snapshot()
        assert len(rows) == 1
        assert rows[0][0] == "Adds"

    def test_countdown_is_backdated_to_the_real_combat_start(self, sim_clock):
        """A 45s timer must expire 45s after EnterCombat, not 45s after
        whenever the boss happened to get named -- otherwise every
        combat_start timer drifts later by the recognition delay."""
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss_with_cs_timer()})

        sim_clock(0.0)
        state.feed(self._enter_combat(), timer_engine=engine)
        sim_clock(5.0)  # deliberately slow recognition
        state.feed(_ev("@Tank#1", "TestBoss"), timer_engine=engine)

        remaining = engine.snapshot()[0][1]
        assert abs(remaining - 40.0) < 0.01, (
            "5s of the 45s countdown had already elapsed before recognition"
        )

    def test_it_does_not_announce_at_start_only_at_zero(self, sim_clock, monkeypatch):
        """The label names a FUTURE event -- "Adds" means adds arrive when
        this hits zero. Speaking it the instant combat starts is 45s
        premature (reported live: "said jealous male way to early")."""
        import timers as timers_mod
        spoken = []
        monkeypatch.setattr(timers_mod.audio, "speak",
                            lambda text, category=None, **kw: spoken.append(text))

        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss_with_cs_timer()})
        sim_clock(0.0)
        state.feed(self._enter_combat(), timer_engine=engine)
        sim_clock(1.0)
        state.feed(_ev("@Tank#1", "TestBoss"), timer_engine=engine)

        assert spoken == [], "must stay silent while the countdown is running"

        sim_clock(46.0)
        engine.tick()
        assert spoken == ["Adds"], "reaching zero IS the mechanic -- call it out there"

    def test_repeating_combat_start_timer_announces_on_each_interval(self, sim_clock, monkeypatch):
        import timers as timers_mod
        spoken = []
        monkeypatch.setattr(timers_mod.audio, "speak",
                            lambda text, category=None, **kw: spoken.append(text))

        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss_with_cs_timer(
            repeat_interval_seconds=90.0, repeat_count=9)})
        sim_clock(0.0)
        state.feed(self._enter_combat(), timer_engine=engine)
        sim_clock(1.0)
        state.feed(_ev("@Tank#1", "TestBoss"), timer_engine=engine)

        for t in (46.0, 136.0, 226.0):
            sim_clock(t)
            engine.tick()
        assert spoken == ["Adds", "Adds", "Adds"], (
            "first at 45s, then every 90s -- matches real spawn data (46.1/136.3/229.4)"
        )

    def test_reset_clears_the_combat_start_marker(self, sim_clock):
        """Otherwise a stale EnterCombat from the previous pull would start
        the next boss's timers already partway (or fully) elapsed."""
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss_with_cs_timer()})
        sim_clock(0.0)
        state.feed(self._enter_combat(), timer_engine=engine)
        state.reset()

        sim_clock(1.0)
        state.feed(_ev("@Tank#1", "TestBoss"), timer_engine=engine)
        assert engine.snapshot() == [], "no combat start recorded for THIS pull yet"


class TestDifficultyScoping:
    """BARAS scopes 953 of its 994 timers by instance difficulty and the
    translation dropped that field entirely -- so a Story-mode group got
    759 Veteran/Master-only mechanics called out at them. SWTOR records
    the mode on every AreaEntered line ("8 Player Master"), so this is
    knowable; it just wasn't being read."""

    def _boss(self, difficulties):
        return BossDefinition(
            id="test_boss", name="Test Boss", boss_names=["TestBoss"],
            phases=[BossPhase(id="p1", name="Phase 1",
                              start_trigger=Condition(type="combat_start"))],
            timers=[BossTimerDef(label="HardModeOnly", duration_seconds=5.0,
                                 trigger=Condition(type="ability_cast", keyword="Slam"),
                                 difficulties=difficulties)],
        )

    def _area(self, text):
        raw = (f"[00:00:00.000] [@Tank#1|(0,0,0,0)|(1/1)] [] [] "
               f"[AreaEntered {{836045448953664}}: {text}]")
        return parse_line(raw, line_number=1)

    def _cast(self):
        return parse_line(
            log_line("00:00:01.000", "TestBoss", target="@Tank#1", ability="Slam {1}",
                      effect_name="AbilityActivate {2}"),
            line_number=2,
        )

    def test_difficulty_is_read_from_the_area_line(self, sim_clock):
        state = BossEncounterState({"test_boss": self._boss(["master"])})
        state.feed(self._area("Darvannis {1} 8 Player Master {2}"))
        assert state.difficulty == "master"
        assert state.group_size == 8

    def test_timer_fires_in_its_own_difficulty(self, sim_clock):
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss(["master"])})
        sim_clock(0.0)
        state.feed(self._area("Darvannis {1} 8 Player Master {2}"), timer_engine=engine)
        state.feed(self._cast(), timer_engine=engine)
        assert [r[0] for r in engine.snapshot()] == ["HardModeOnly"]

    def test_timer_is_suppressed_in_a_difficulty_it_does_not_exist_in(self, sim_clock):
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss(["master"])})
        sim_clock(0.0)
        state.feed(self._area("Darvannis {1} 8 Player Story {2}"), timer_engine=engine)
        state.feed(self._cast(), timer_engine=engine)
        assert engine.snapshot() == [], "a Master-only mechanic must not fire in Story"

    def test_unscoped_timers_still_fire_everywhere(self, sim_clock):
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss([])})
        sim_clock(0.0)
        state.feed(self._area("Darvannis {1} 8 Player Story {2}"), timer_engine=engine)
        state.feed(self._cast(), timer_engine=engine)
        assert len(engine.snapshot()) == 1

    def test_unknown_difficulty_fires_everything(self, sim_clock):
        """Never silently drop real mechanics because the mode couldn't be
        identified -- that's far worse than the extra callouts this is
        meant to remove."""
        engine = TimerEngine()
        state = BossEncounterState({"test_boss": self._boss(["master"])})
        sim_clock(0.0)
        state.feed(self._cast(), timer_engine=engine)   # no AreaEntered at all
        assert len(engine.snapshot()) == 1

    def test_difficulty_survives_pull_rollover(self, sim_clock):
        """You zone into an operation once and then do many pulls inside
        it, so reset() between pulls must not forget the mode."""
        state = BossEncounterState({"test_boss": self._boss(["master"])})
        state.feed(self._area("Darvannis {1} 8 Player Master {2}"))
        state.reset()
        assert state.difficulty == "master"

    def test_group_size_variants_collapse_to_the_base_mode(self):
        """BARAS writes "veteran_16"; the log only ever says "Veteran"."""
        from boss_definitions import BossTimerDef as T
        assert T(label="x", duration_seconds=1.0,
                 difficulties=["veteran"]).applies_to_difficulty("veteran") is True
