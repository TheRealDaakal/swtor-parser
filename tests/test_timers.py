"""
Covers the Kolto Pods pile-up bug: TimerEngine.start_timer() used to always
append a new ActiveTimer, so a rapidly-retriggered effect (e.g. a HoT ticking
every ~0.9s on the same target) piled up near-duplicate rows -- 92
simultaneous entries measured on a real log before the fix. The fix is
dedupe_key: reapplying the same (label, category, dedupe_key) refreshes the
existing entry instead of stacking a new one, while a different dedupe_key
(different target) still gets its own row.
"""
from timers import TimerEngine


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
