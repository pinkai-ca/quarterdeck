# Modules

The modules that grew beside the parts, in `src/quarterdeck/` and in its `harness/` folder, each with what it holds. The module of each part itself, such as `ship.py`, `seat.py`, `door.py`, `post.py`, `watch.py` and `ledger.py`, and the adapter of each harness are named in [section 13 of the design](DESIGN.md#13-repository-layout), which lays out the whole repository. [CONTRIBUTING.md](../CONTRIBUTING.md) says how to change them.

## The framework

| Module | What it holds |
| --- | --- |
| `check.py` | `qd check`, the live proof of an installed program |
| `compaction_alert.py` | an idle seat that is ready for a fresh start by hand: one report to the systems seats for each such reading, and a named alarm while no systems seat is up |
| `cost_evidence.py` | a turn's cost evidence, kept beside the Ledger's turns |
| `credential.py` | the one link a seat shares with its pool, reseeded and relinked on the tick |
| `doctor.py` | `qd doctor`, the installation checked and handed to a seat to fix |
| `endpoint.py` | endpoint pools: what a server serves, the model a seat takes from it, the key beside it |
| `fakeharness.py` | the entry point installed as `qd-fakeharness`, for the simulator under `harness/` |
| `headlines.py` | operational status-line formatting; provider allowances belong in detailed readings |
| `input_receipt.py` | the one rule that matches a delivery to the record's evidence of its input |
| `install.py` | `qd sync`: the framework at one commit installed as the ship's own copy, which every `qd` then hands off to |
| `ledger_reading.py` | the Ledger's rows and usage windows as a reading takes them, and their tables |
| `machine.py` | a seat's process tree, sampled for the Ledger |
| `output.py` | one shape for every reading, rendered for a person or serialized as JSON |
| `paste_refusal.py` | proved owned-unsubmitted paste cleanup, and refusal evidence scoped by payload, version and geometry |
| `pending.py`, `delivery_record.py` | durable paste evidence and historical receipts; the service schedules each message |
| `pricing.py` | a pool's stated rates, and the price saved beside a launch or a model choice |
| `provenance.py` | which launch a record belongs to |
| `providers.py` | normalizes declared companies and builds public identity metadata |
| `quota.py` | quota walls and the usage refresh |
| `record_owner.py` | which program a record belongs to |
| `refusal.py` | a provider-refused crew seat started fresh once, the refused message withheld and its sender told |
| `sandbox.py` | the bubblewrap jail around a sandboxed seat's program |
| `servicectl.py` | the service's heartbeat, lock, start, stop and requests |
| `tmux.py` | the only module that runs tmux |
| `turn_failure.py` | a turn that ended in a recorded error: the door's hold, its alarm, the receipts it unsettles and a human seat's operational metadata |
| `usage_cache.py` | the latest real reading of each usage window, and its last attempt |

## Under `harness/`

| Module | What it holds |
| --- | --- |
| `antigravity_quota.py` | the Antigravity record's quota facts |
| `antigravity_record.py` | the Antigravity record decoded from its snapshot |
| `antigravity_wire.py` | the Antigravity record's wire format |
| `costs.py` | an endpoint program's step costs: what its record states, what stated rates prove, and what stays unpriced |
| `failures.py` | a recorded error's bounded first words |
| `home.py` | building a seat's home: a pool's credential brought into it, a key pool's environment, and the ship's settings layered over a program's own |
| `json_record.py` | JSON-lines records |
| `limits.py` | usage windows and walls |
| `opencode_record.py` | the OpenCode database read session by session: the latest root session is the seat's, and a task's child sessions are accounted on their own |
| `simulator.py` | the fake program the drill runs |
| `sqlite_snapshot.py` | a SQLite record read from a snapshot of its bytes and its log, shared by the Antigravity and OpenCode readers |
| `synthetic_control.py` | the rendezvous a test holds the fake program at |
| `synthetic_record.py` | the fake program's records, in Claude Code's or Codex's shape |
| `types.py` | the values the adapters and the framework pass between them: inputs, tokens, turns and their recorded failures, state, keys, controls, screens, boxes, credentials, launches |
