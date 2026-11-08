# Quarterdeck

Quarterdeck runs a crew of coding agents as visible terminal sessions under one human's command. Each agent is an ordinary terminal program, Claude Code, Codex or Antigravity, in its own tmux window, where the human can read it and type into it at any time. Quarterdeck seats the agents, carries orders and reports between them, watches them, and accounts for what they spend.

It is a control plane and nothing more. It is not the agents, not the code they write, and not the way they review or land that code. Those rules are the ship's standing orders, which Quarterdeck carries to every seat and does not enforce in code.

## The shape of it

A **ship** is one file, `ship.toml`, naming the tmux sessions, the seats in each, and every seat's role, program, pool, model and persona. A **pool** is one way of paying: a subscription, named by its credential directory, or an API key, named by the variable that holds it. A **seat** is one running program in one pane under one role, with a home of its own built from the ship's files and nothing of the user's. Orders, reports, questions, answers and acknowledgments are immutable files in the **post**; the ship's one service types each message in full into its addressee's pane, through one door, or hands it to the seat's own watch in a session the human owns. The **watch** reads a seat from its record first and its screen second. The **ledger** accounts every turn by pool, by seat and by order, with the tool calls it made and what it cost the machine. Fourteen **laws** say what the framework itself guarantees, each tagged where the code enforces it and where a test tries to break it; `qd laws` lists them. The **drill** proves the whole thing on a fake harness, and `qd check` proves an installed program live.

The design is `docs/DESIGN.md`.

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

## What is proven, and what is not

Claude Code 2.1.317 and Codex 0.166.0 are proven live by `qd check` on 7 November 2026; their adapters know each program's record, screen, keys, dialogs and controls, pinned by fixtures under `tests/fixtures/`. Antigravity 1.5.2 has no home switch, so its seat runs in the user's Antigravity state; its record, a SQLite database of protobuf messages, is read from a snapshot of its bytes, so deliveries are confirmed and turns accounted like the others, and what the record does not hold is marked rather than guessed. The fields read are in `docs/ANTIGRAVITY.md`; its live check is still to run. A newer version of any program runs with the newest known facts and is reported as unproven until `qd check` proves it.

## Working on it

`AGENTS.md` is for whoever works on the framework itself. The tests run with `.venv/bin/python -m pytest -q`: unit tests, contract tests against recorded screens, and integration tests on a private tmux server with the fake harness. Nothing from a runtime may be tracked, and every law must be carried in code and tried by a test; two tests guard those.

MIT licence. Copyright (c) 2026 Tyler Dwyer.
