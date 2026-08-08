"""Rotation and idle-time analysis helpers."""

from typing import List

from log_parser import parse_line

GCD_BASE_SECONDS = 1.5
GCD_GAP_MULTIPLIER = 1.5

def rotation_segments(lines, player_name: str, keyword: str, alacrity_pct: float = 0.0) -> List[dict]:
    """Splits a pull's raw log lines into segments bounded by every
    occurrence of `keyword` (case-insensitive substring against
    ability+effect_name, matched from ANY source -- same convention
    timers.py's TimerRule uses) and reports `player_name`'s own casts
    within each segment: DPS, EHPS, crit%, idle time, and the ordered
    sequence of casts landed (with synthetic "gap" entries interleaved
    wherever idle time was detected). Answers "how did my rotation look
    between each occurrence of this mechanic" -- e.g. keyed to a
    recurring boss ability, this shows the burst window between each
    occurrence.

    Idle-time detection is a real, honest floor -- "you activated
    nothing for N seconds" -- not a rotation optimizer. It has no
    per-class/discipline priority data (nobody maintains that here) to
    say WHAT you should have cast instead, only that a GCD-sized (or
    bigger) window went by with no ability_activate from you at all. See
    GCD_BASE_SECONDS/GCD_GAP_MULTIPLIER. alacrity_pct scales the GCD
    floor down (apply_alacrity, same formula as DoT/HoT durations) --
    pass the LOCAL player's own known alacrity when analyzing their own
    rotation; for a teammate's, callers generally don't have that value,
    so it defaults to the unscaled base GCD.

    Deliberately NOT backed by stored per-event data: PlayerStats/
    Encounter only keep running totals plus a bucketed (timestamp,
    amount) pair for burst-window math (see PlayerStats.damage_events'
    own comment on history.json size) -- a full per-cast log for every
    player of every historical pull would multiply that cost for a
    feature most pulls will never use. Instead this re-parses the raw
    log lines on demand, the same "re-read the original file's line
    range" approach Parsely uploads and log_merger already use, so the
    cost is only paid when a rotation view is actually requested.

    lines: raw text lines already sliced to the pull's own range (the
    caller owns finding start_line/end_line -- see Encounter.log_path).
    Returns [] if the keyword never occurs at least twice (nothing to
    bound a segment with)."""
    from log_merger import LogClock  # local import: log_merger imports Encounter from here

    clock = LogClock()
    parsed = []
    for i, line in enumerate(lines):
        event = parse_line(line, line_number=i)
        if event is None:
            continue
        parsed.append((clock(event.timestamp or ""), event))
    if not parsed:
        return []

    keyword_l = keyword.lower().strip()
    if not keyword_l:
        return []
    boundaries = sorted({
        t for t, event in parsed
        if keyword_l in " ".join(filter(None, [event.ability, event.effect_name])).lower()
    })
    if len(boundaries) < 2:
        return []

    from timers import apply_alacrity  # local import: keeps stats.py's own import graph unchanged for callers that never touch rotation_segments()
    gcd = apply_alacrity(GCD_BASE_SECONDS, alacrity_pct)
    gap_threshold = gcd * GCD_GAP_MULTIPLIER

    segments = []
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        duration = seg_end - seg_start
        if duration <= 0:
            continue
        timeline = []  # (t, entry) -- casts and detected idle gaps, merged and sorted below
        activation_times = []
        damage_total = 0.0
        heal_total = 0.0
        landed = 0
        crits = 0
        for t, event in parsed:
            if not (seg_start <= t < seg_end) or event.source != player_name:
                continue
            if event.is_ability_activate:
                activation_times.append(t)
            is_attack = event.is_damage and event.ability is not None
            if is_attack:
                landed += 1
                if event.is_critical:
                    crits += 1
                if event.amount:
                    damage_total += event.amount
                    timeline.append((t, {"kind": "cast", "ability": event.ability or event.effect_name or "Unknown",
                                         "amount": round(event.amount), "is_critical": event.is_critical,
                                         "is_heal": False}))
            elif event.is_heal and event.amount:
                heal_total += event.amount
                timeline.append((t, {"kind": "cast", "ability": event.ability or event.effect_name or "Unknown",
                                     "amount": round(event.amount), "is_critical": event.is_critical,
                                     "is_heal": True}))

        idle_seconds = 0.0
        activation_times.sort()
        for prev_t, next_t in zip(activation_times, activation_times[1:]):
            gap = next_t - prev_t
            if gap >= gap_threshold:
                idle_seconds += gap
                # Positioned just BEFORE the next activation, not just after
                # the previous one -- a cast's damage/heal chip lands
                # slightly AFTER its own activation (travel time/tick
                # timing), so anchoring off prev_t could sort the gap
                # marker ahead of the very cast that preceded it.
                timeline.append((next_t - 0.001, {"kind": "gap", "seconds": round(gap, 1)}))

        timeline.sort(key=lambda pair: pair[0])
        casts = [entry for _t, entry in timeline]

        segments.append({
            "duration": round(duration, 1),
            "dps": round(damage_total / duration, 1),
            "ehps": round(heal_total / duration, 1),
            "crit_pct": round(100.0 * crits / landed, 1) if landed else 0.0,
            "idle_seconds": round(idle_seconds, 1),
            "casts": casts,
        })
    return segments
