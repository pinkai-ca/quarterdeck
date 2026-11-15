# For an agent that arrives here

- Your person asked for a ship to be set up: follow [SETUP.md](SETUP.md).
- You are here to change Quarterdeck itself: read [CONTRIBUTING.md](CONTRIBUTING.md).
- You are a seat of a Quarterdeck ship, which you are if `QD_SEAT` is set in your shell: your brief governs, and this file does not apply.

## What Quarterdeck is

Quarterdeck runs AI coding agents from different companies as one crew, on your person's machine. It is a command, `qd`, and one small service for each ship; every agent runs in its normal terminal interface, in a tmux pane. Your person directs the crew by talking to its desk, one of its agents, in plain words, and may type any of its commands themselves.

## Commands

Your person works with the desk. If they ask you to give an order or to look for them, you are the operator's hands from your own shell: what you post goes out in the operator's name, and the crew's reports to the operator come back to you. Only one agent does this at a time: two waiting at once would split the reports.

- `qd post order --to <seat> "..."`: give a seat work.
- `qd inbox --wait`: wait for the next message to the operator and print it. Keep it running in the background if your harness wakes you when it ends; otherwise wait in the foreground, one order at a time.
- `qd status`: every seat, then the open orders.
- `qd watch <seat>`: one seat in full.
- `qd ledger --by order`: each order's turns, tokens and time, and its cost where one is known.
- `qd doctor`: is the machine ready for this ship.
- `qd up`, `qd down`: bring the crew up; end every seat and the service.
- `qd drill`: stand-in programs; spends nothing.
- `qd check <harness>`: prove a harness live. Ask first.
- `qd laws`: every law, with its code and test.

With no ship named, `qd` acts on your person's default ship and says which; name another with `--ship <folder>` before the verb. In the checkout `qd` is `.venv/bin/qd`.

## Rules

- Work goes to another agent by `qd post` only, never by typing into its pane, tmux keys or MCP.
- Never read, print or copy a sign-in file or key.
- You type nothing into a session a person owns: in a ship made from the default, its `Office`. That pane is your person's.
- Muse is not in the code yet: do not run or promise it.
- A refusal names its reason and what would lift it: report it, do not retry. Report a failed check as failed.
