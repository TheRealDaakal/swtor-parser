# Parser Core Cleanup — Pass 8

The first real event-parsing boundary is now implemented in
`parser_core.event_parser.parse_event_record`.

The legacy `log_parser.py` remains the authoritative source for raw SWTOR log
interpretation. Parsed/tokenized records can now be normalized through a single
adapter before entering combat-state and analytics layers.

Target pipeline:

    raw log line
      -> legacy tokenization
      -> parse_event_record()
      -> NormalizedEvent
      -> CombatState
      -> encounter/analytics

The adapter is intentionally additive in this pass so existing parser results
remain unchanged while the new boundary gains regression coverage.
