# Quarterdeck: design

Version 0.20, 2026-11-07.

## 1. What it is

Quarterdeck runs a crew of coding agents as visible terminal sessions under one human's command. Each agent is an ordinary terminal program (Claude Code, Codex, Antigravity and others) in its own tmux window, where the human can read it and type into it at any time. Quarterdeck seats the agents, carries orders and reports between them, watches them, and accounts for what they spend.

It is a control plane and nothing more. It is not the agents, not the code they write, and not the way they review or land that code. Those rules are the ship's standing orders, which Quarterdeck carries to every seat but does not enforce in code.

## 2. The parts

Six parts make the ship and two keep it honest. The parts name the modules and the commands, and the laws are grouped by the part that enforces them, so the eight words, learned once, carry through the code and the rest of this document.

**Six make the ship**

| Part | What it is | Module | Commands |
|---|---|---|---|
| **Ship** | The declared organization: one file, `ship.toml`, naming the tmux sessions, the seats in each, and every seat's role, harness, pool, model and persona; one project folder holding framework, declaration and state | `ship.py` | `qd up`, `qd down`, `qd attach`, `qd status`, `qd status --line` |
| **Harness** | An adapter for one agent program: how to launch and brief it, recognize it, find and read its record, read its screen and its usage, which controls it takes and in what form, which of its dialogs may be dismissed, and how it takes a message while it works | `harness/` | none of its own; the other parts call it |
| **Seat** | One running harness in one tmux pane under one role, and its door, the one path anything is typed through. Identity is pane id, process id and record; the persona and the window name are display | `seat.py`, `door.py` | `qd up <seat>`, `qd down <seat>`, `qd restart <seat>`, `qd fresh <seat>`, `qd stop <seat>`, `qd set`, `qd controls` |
| **Post** | The messages: orders, reports, questions, answers and acknowledgments as immutable files, delivered in full to each addressee, through its door or through its own watch, with an inbox the service renders for each seat | `post.py` | `qd post`, `qd thread`, `qd inbox`, `qd inbox --wait` |
| **Watch** | Reading a seat: what it is doing and in which tool, whether its door is open, context used, and the last reply; and the one line every ship session shows about the whole | `watch.py` | `qd watch` |
| **Ledger** | Accounting: tokens, cost, tool calls, time, machine load and disk by pool, by seat and by order, from each harness's own record and the seat's own processes | `ledger.py` | `qd ledger` |

**Two keep it honest**

| Part | What it is | Module | Commands |
|---|---|---|---|
| **Standing orders** | What every seat reads before it acts: the laws the framework enforces, the ship's own orders, and a card for its role and for its program. The laws live here, are enforced in the six parts above, and are proven by the Drill | `standing.py` | `qd laws`, `qd brief <seat>` |
| **Drill** | Proof that the ship works: a fake harness in the shape of Claude Code or Codex, and the all-hands exercise, every seat sending to every other, the service crashing and recovering, every seat relaunched fresh and the matrix run again. On the fake harness in the tests; on real programs when asked | `drill.py`, `fakeharness.py` | `qd drill` |

One verb is for the machine the ship runs on. `qd doctor` (`doctor.py`) checks tmux and git, the program each harness needs and its version, the sign-in each pool names and when its grant expires, every seat's working directory, the runtime's fence, the service and the disk, and prints a proof line per check. With `--fix` it opens a throwaway seat on the first pool whose program runs, hands it the failures as an order, waits for its report and checks again: the programs that run the ship are the help that can mend it, where a script could only name what is wrong.

## 3. What the human sees

The Admiral, the human operator, has "Admiral's Office". The flagship's other two tmux sessions are "Ship's Bridge" for command seats and "Crew" for workers. One window per seat, named by persona. Every ship session carries one status line, refreshed by tmux on its own timer: whether the service is alive, the oldest undelivered message and its age, the seats that need a human, seats held at a limit with no eligible declared destination, delivery and watch obstructions, nearly full disks and grants nearing expiry. `qd status` fits on one screen: every seat's activity, door, current order and tokens today, then the open orders. On the Bridge and in the Crew every message is typed in full into its addressee's pane, so a pane's scrollback is the log of the seat's traffic, read by scrolling up. Everything else is a file a person can read: TOML for the ship, Markdown for inboxes and standing orders, JSON lines for messages, and one SQLite file for the Ledger. Into a session the human owns it types only when a message waits there, no watch is armed and the box is empty.

The human rarely types `qd` at all. Their word goes to the Desk, an agent seat in the Office, and the Desk runs the ship with `qd`. So `qd` is written for an agent at the keyboard as much as for a person: one verb per act; every reading available as `--json`; every act followed by a proof line saying what was done and how it was confirmed; every refusal naming the reason and what would lift it; `qd --help` complete enough to be the Desk's manual, with the desk role card teaching the rest. What the human watches is the Desk's pane, where the commands and their proof lines appear, and the other panes, where each change lands as a typed line a person can read.

Two words the rest of this document leans on. A **turn** is everything from one input to the next idle: the program takes a message, the model works through its steps, and the program stops and waits again. A **step** is one entry in the record along the way: a reply, a tool call, a tool result. The Ledger counts by turn, summing steps. The Watch calls a seat working while a turn is open and steps keep coming.

## The pages

Every other part of the design has a page of its own:

- [The ship file](design/ship-file.md)
- [Harness adapters](design/harnesses.md)
- [Seats](design/seats.md)
- [Post](design/post.md)
- [Watch](design/watch.md)
- [Ledger](design/ledger.md)
- [Standing orders and the laws](design/orders-and-laws.md)
- [Tests](design/tests.md)
- [Working on the framework from a ship](design/working.md)
- [Guided setup](design/guided-setup.md)
- [Repository layout](MODULES.md#13-repository-layout), in the list of modules
- [Decisions](DECISIONS.md#14-decisions), with the other design decisions

## Where the old sections went

Until version 0.20 the design was one file. Its sections now stand here:

| Old section | Now |
| --- | --- |
| 1. What it is | this page |
| 2. The parts | this page |
| 3. What the human sees | this page |
| 4. The ship file | [design/ship-file.md](design/ship-file.md#4-the-ship-file) |
| 5. Harness adapters | [design/harnesses.md](design/harnesses.md#5-harness-adapters) |
| 6. Seats | [design/seats.md](design/seats.md#6-seats) |
| 7. Post | [design/post.md](design/post.md#7-post) |
| 8. Watch | [design/watch.md](design/watch.md#8-watch) |
| 9. Ledger | [design/ledger.md](design/ledger.md#9-ledger) |
| 10. Standing orders | [design/orders-and-laws.md](design/orders-and-laws.md#10-standing-orders) |
| 11. The laws, second draft | [design/orders-and-laws.md](design/orders-and-laws.md#11-the-laws-second-draft) |
| 12. Tests | [design/tests.md](design/tests.md#12-tests) |
| 13. Repository layout | [MODULES.md](MODULES.md#13-repository-layout) |
| 14. Decisions | [DECISIONS.md](DECISIONS.md#14-decisions) |
| 15. Working on the framework from a ship | [design/working.md](design/working.md#15-working-on-the-framework-from-a-ship) |
| Guided setup | [design/guided-setup.md](design/guided-setup.md#guided-setup) |
