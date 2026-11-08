# Quarterdeck

Quarterdeck runs a crew of coding agents as visible terminal sessions under one human's command. Each agent is an ordinary terminal program, Claude Code, Codex, Antigravity, OpenCode or pi, in its own tmux window, where the human can read it and type into it at any time. Quarterdeck seats the agents, carries orders and reports between them, watches them, and accounts for what they spend.

It is a control plane and nothing more. It is not the agents, not the code they write, and not the way they review or land that code. Those rules are the ship's standing orders, which Quarterdeck carries to every seat and does not enforce in code.

## The shape of it

A **ship** is one file, `ship.toml`, naming the tmux sessions, the seats in each, and every seat's role, program, pool, model and persona. A **pool** is one way of paying: a subscription, named by its credential directory; an API key, named by the variable that holds it; or an endpoint, a model served at a URL, local or remote, with no quota and no credential of its own. A **seat** is one running program in one pane under one role, with a home of its own built from the ship's files and nothing of the user's. Orders, reports, questions, answers and acknowledgments are immutable files in the **post**; the ship's one service types each message in full into its addressee's pane, through one door, or hands it to the seat's own watch in a session the human owns. The **watch** reads a seat from its record first and its screen second. The **ledger** accounts every turn by pool, by seat and by order, with the tool calls it made and what it cost the machine. Fourteen **laws** say what the framework itself guarantees, each tagged where the code enforces it and where a test tries to break it; `qd laws` lists them. The **drill** proves the whole thing on a fake harness, and `qd check` proves an installed program live.

Read [Design decisions](docs/DECISIONS.md) for the choices and their code and test evidence. The full design is `docs/DESIGN.md`.

## Running it

Python 3.12 or later, tmux 3.x, git. The framework uses the standard library only.

```
git clone <this repository> quarterdeck && cd quarterdeck
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e .
.venv/bin/qd drill                      # the all-hands exercise on the fake harness, about 25 seconds
.venv/bin/qd --ship ships/drydock up    # the drydock: three fake seats in three tmux sessions
.venv/bin/qd --ship ships/drydock status
.venv/bin/qd doctor                     # is this machine ready: programs, versions, sign-ins, tmux, disk
```

A ship of your own is a folder under `ships/`, its own git repository, holding a `ship.toml` and an `orders.md`. `ships/default/` is the smallest crew on real programs, a desk on Claude Code with a captain and an engineer on Codex, using the sign-ins already on the machine; copy it and change what differs. Point `qd` at a ship with `--ship` or `QD_SHIP`. Then:

```
qd up                               # the service and every seat marked up, resuming what has a record
qd post order --to scott "Build the thing. Report when its tests pass."
qd status                           # every seat on one screen, then the open orders
qd watch scott                      # one seat in full
qd set scott model=gpt-6-astra      # writes the ship file, commits it, applies what it can
qd fresh worker-1                   # a new conversation, briefed, with its open messages
qd ledger --by order                # what each order cost
qd check claude                     # prove the installed Claude Code on a throwaway seat (spends tokens)
qd upgrade                          # install the checkout's HEAD as the ship's own copy and run on it; qd upgrade <commit> rolls back
```

Every verb prints a proof line saying what was done and how it was confirmed; every refusal names the reason and what would lift it; every reading is available as `--json`. The command is written for an agent at the keyboard as much as for a person: the human's word reaches the ship through a desk seat that runs `qd`.

### Reading as JSON

Put `--json` before the verb, for example `qd --json status` or
`qd --json ledger --by seat`. Each invocation emits one JSON document, followed
by a newline. No table or Markdown wrapper is used for structured readings:

- `status` and `watch` without a seat: seat readings, open order messages, and
  status facts. `status --line`: the facts themselves, including waiting counts,
  warnings and the enabled cached usage windows.
- `watch <seat>`: the seat reading, full record state and parsed screen, with
  separate record absence/error fields. Observed model, declared model, harness
  and version remain separate; an unobserved value is `null`.
- `ledger`: stored windows and their freshness, declared pools, key-pool spend,
  usage attempts and active wall events. `ledger --by seat|order|pool` and
  `ledger --tools`: named row fields with their unmeasured/untimed/unjudged
  counts. A missing book has `available: false` and a reason; an empty book is
  available with empty collections. These commands neither write the book nor
  request usage from a provider.
- `controls`: adapter control definitions and the ship's relaunch controls.
  `laws`: each law's id, part, text and comment-token carrier paths/line numbers.
  `brief`, including `--sources`: the brief text, source files and word count.
  `doctor` without `--fix`: checks with names, boolean results and details.
- `inbox`: open and received message objects plus closing-report references.
  `inbox --wait`: one object with `seat` and a `messages` array, then exit. This
  is one batch, not JSON Lines or a continuing stream. The complete document is
  serialized, printed and flushed before watch receipts are recorded. An output
  failure leaves attempts unresolved; emitted messages do not assert a receipt.

Dataclass fields retain numbers, booleans, `null` and collections; timestamps use
ISO 8601 and paths are strings. Stored usage keeps its original reading/reset
pair and latest error; `current`, `complete`, `reason` and `age_seconds` describe
freshness at `observed_at`. Unknown token/price semantics and headline selection
are the same as in text. Mutating commands, including `watch --save-screen` and
`doctor --fix`, retain their proof output.

## What is proven, and what is not

Claude Code 2.1.317 and Codex 0.166.0 were proven live by `qd check` on 7 November 2026: each launched, took a long paste its record confirmed, wrote a named file, posted a report and had its turn read. What their adapters know of each program's record, screen, keys, dialogs and controls is pinned separately, by the contract tests' fixtures under `tests/fixtures/`. Antigravity 1.5.2 has no home switch, so its seat runs in the user's Antigravity state; its record, a SQLite database of protobuf messages, is read from a snapshot of its bytes, so deliveries are confirmed and turns accounted like the others, and what the record does not hold is marked rather than guessed. The fields read are in `docs/ANTIGRAVITY.md`; its live check is still to run. OpenCode 1.19.5 and pi 0.74.2 run on endpoint pools, a model served at an OpenAI-compatible URL; both were spiked against a local vLLM server on 7 November 2026, and their records, screens, folds and wraps are pinned by fixtures. A seat on an endpoint pool runs in a bubblewrap jail unless the ship says otherwise: the operator's home hidden, the workspace writable, `qd post` working from inside.

The readings keep uncertainty visible. A trailing `+` marks an incomplete measured total: at least what was read, with unmeasured contributions counted separately. An unreadable or unavailable reading says what could not be seen, and an unknown measurement remains distinct from measured zero. New or unproven program versions run with the newest known adapter facts and remain unproven until `qd check` proves the installed program live.

## A law from statement to test

The complete **one-door** law, from [the framework's laws](standing-orders/laws.md#seat):

> Everything typed into a seat goes through one path, in one process: it clears what it knows how to clear, refuses what it does not, types once into an empty box, checks the box as the program shows it, submits, and confirms by the record. It never types into a session the human owns. Never resend what you cannot confirm.

A tagged carrier excerpt from [Door.deliver](src/quarterdeck/door.py#L192):

```python
return self.confirm(text)  # law: one-door
```

A tagged assertion excerpt from [the foreign-draft test](tests/integration/test_door.py#L96):

```python
assert delivery.result == "withheld" and "text in the box" in delivery.detail  # law: one-door
```

The [full test](tests/integration/test_door.py#L89) starts with an occupied box and checks that the original draft stays untouched and nothing is submitted. Box verification permits submission; exact, fresh record evidence confirms delivery. Queue acceptance remains queued until consumption is established, and uncertain submissions are not automatically resent.

Both tags can be found from the checkout:

```sh
rg -n 'law: one-door' src/quarterdeck/door.py tests/integration/test_door.py
```

Run `env -u QD_SHIP -u QD_SEAT .venv/bin/qd laws` from the checkout. Two verbatim entries from one-door's carrier line are shown below on separate lines; the other carrier entries and the law statement are omitted here. A checkout lists repository tests; an installed package does not ship them.

```text
src/quarterdeck/door.py:110, 160, 178, 192
tests/integration/test_door.py:41, 96, 163, 179, 203
```

## Working on it

`AGENTS.md` is for whoever works on the framework itself. The tests run with `.venv/bin/python -m pytest -q`: unit tests, contract tests against recorded screens, and integration tests on a private tmux server with the fake harness. Nothing from a runtime may be tracked, and every law must be carried in code and tried by a test; two tests guard those.

MIT licence. Copyright (c) 2026 Tyler Dwyer.
