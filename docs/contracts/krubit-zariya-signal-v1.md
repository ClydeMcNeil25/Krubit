# `krubit.zariya-signal.v1`

## Purpose

This is Krubit's outbound evidence envelope for Zariya. Phase 0 generates a test envelope only;
it does not deliver signals to KSHQ or modify Zariya's KAI package.

## Fields

| Field | Type | Phase 0 rule |
|---|---|---|
| `schema_version` | string | Exactly `krubit.zariya-signal.v1` |
| `signal_id` | string | Non-empty and traceable to the source event |
| `guild_id` | numeric string | Positive Discord guild snowflake |
| `kind` | string | Exactly `foundation_test` |
| `severity` | string | Exactly `info` |
| `occurred_at` | string | UTC ISO-8601 timestamp |
| `source_event_id` | string | Non-empty provenance identifier |
| `summary` | string | Functional, non-conversational summary |
| `evidence` | object | Recursively redacted JSON data |
| `action_request` | null | Must remain null in Phase 0 |

## Example

```json
{
  "schema_version": "krubit.zariya-signal.v1",
  "signal_id": "signal:phase0-smoke-1",
  "guild_id": "356068206034550784",
  "kind": "foundation_test",
  "severity": "info",
  "occurred_at": "2026-08-03T17:30:00Z",
  "source_event_id": "phase0-smoke-1",
  "summary": "Krubit Phase 0 signal path is healthy.",
  "evidence": {
    "phase": 0,
    "member_data": false
  },
  "action_request": null
}
```

## Security behavior

Evidence is redacted when the contract is parsed and again when serialized. The contract rejects
unknown schema versions, missing provenance, non-UTC-aware timestamps, non-object evidence,
non-info severity, non-test kinds, and every non-null action request.

Future signal kinds and governed action proposals require a new reviewed contract revision or an
explicit backward-compatible extension. They must not be smuggled through the Phase 0 test kind.
