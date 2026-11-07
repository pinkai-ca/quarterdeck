# Quarterdeck: design

Version 0.19, 2026-11-07, after the review of 6 November and the Admiral's answers to what it left. The build begins on his word; section 14 says how.

## 1. What it is

Quarterdeck runs a crew of coding agents as visible terminal sessions under one human's command. Each agent is an ordinary terminal program (Claude Code, Codex, Antigravity and others) in its own tmux window, where the human can read it and type into it at any time. Quarterdeck seats the agents, carries orders and reports between them, watches them, and accounts for what they spend.

It is a control plane and nothing more. It is not the agents, not the code they write, and not the way they review or land that code. Those rules are the ship's standing orders, which Quarterdeck carries to every seat but does not enforce in code.

## 2. The parts

Six parts make the ship and two keep it honest. The parts name the modules and the commands, and the laws are grouped by the part that enforces them, so the eight words, learned once, carry through the code and the rest of this document.

**Six make the ship**

| Part | What it is | Module | Commands |
|---|---|---|---|
| **Ship** | The declared organization: one file, `ship.toml`, naming the tmux sessions, the seats in each, and every seat's role, harness, pool, model and persona; one project folder holding framework, declaration and state | `ship.py` | `qd up`, `qd down`, `qd status`, `qd status --line` |
| **Harness** | An adapter for one agent program: how to launch and brief it, recognize it, find and read its record, read its screen and its usage, which controls it takes and in what form, which of its dialogs may be dismissed, and how it takes a message while it works | `harness/` | none of its own; the other parts call it |
| **Seat** | One running harness in one tmux pane under one role, and its door, the one path anything is typed through. Identity is pane id, process id and record; the persona and the window name are display | `seat.py`, `door.py` | `qd up <seat>`, `qd down <seat>`, `qd restart <seat>`, `qd fresh <seat>`, `qd stop <seat>`, `qd set`, `qd controls` |
| **Post** | The messages: orders, reports, questions, answers and acknowledgments as immutable files, delivered in full to each addressee, through its door or through its own watch, with an inbox the service renders for each seat | `post.py` | `qd post`, `qd inbox`, `qd inbox --wait` |
| **Watch** | Reading a seat: what it is doing and in which tool, whether its door is open, context used, and the last reply; and the one line every ship session shows about the whole | `watch.py` | `qd watch` |
| **Ledger** | Accounting: tokens, cost, tool calls, time, machine load and disk by pool, by seat and by order, from each harness's own record and the seat's own processes | `ledger.py` | `qd ledger` |

**Two keep it honest**

| Part | What it is | Module | Commands |
|---|---|---|---|
| **Standing orders** | What every seat reads before it acts: the laws the framework enforces, the ship's own orders, and a card for its role and for its program. The laws live here, are enforced in the six parts above, and are proven by the Drill | `standing.py` | `qd laws`, `qd brief <seat>` |
| **Drill** | Proof that the ship works: a fake harness that stands in for every real program, and the all-hands exercise, every seat sending to every other, the service crashing and recovering, every seat relaunched fresh and the matrix run again. On the fake harness in the tests; on real programs when asked | `drill.py`, `fakeharness.py` | `qd drill` |

## 3. What the human sees

Three tmux sessions in the flagship ship: "Ship's Bridge" for the command seats, "Admiral's Office" for the human's own seats, and "Crew" for the workers. One window per seat, named by persona. Every ship session carries one status line, refreshed by tmux on its own timer: whether the service is alive, the oldest undelivered message and its age, the seats that need a human, and the walls. `qd status` fits on one screen: every seat's activity, door, current order and tokens today, then the open orders. On the Bridge and in the Crew every message is typed in full into its addressee's pane, so a pane's scrollback is the log of the seat's traffic, read by scrolling up. Everything else is a file a person can read: TOML for the ship, Markdown for inboxes and standing orders, JSON lines for messages, and one SQLite file for the Ledger.

The human rarely types `qd` at all. The Admiral's word goes to the Desk, an agent seat in the Office, and the Desk runs the ship with `qd`. So `qd` is written for an agent at the keyboard as much as for a person: one verb per act; every reading available as `--json`; every act followed by a proof line saying what was done and how it was confirmed; every refusal naming the reason and what would lift it; `qd --help` complete enough to be the Desk's manual, with the desk role card teaching the rest. What the human watches is the Desk's pane, where the commands and their proof lines appear, and the other panes, where each change lands as a typed line a person can read.

Two words the rest of this document leans on. A **turn** is everything from one input to the next idle: the program takes a message, the model works through its steps, and the program stops and waits again. A **step** is one entry in the record along the way: a reply, a tool call, a tool result. The Ledger counts by turn, summing steps. The Watch calls a seat working while a turn is open and steps keep coming.

## 4. The ship file

```toml
[ship]
name = "enterprise"
operator = "Admiral"                 # what this ship calls its human
workspace = "~/dev/quarterdeck"      # the default working directory of every seat

[pools.claude-primary]
provider = "claude"
config_dir = "~/.claude"

[pools.claude-secondary]
provider = "claude"
config_dir = "~/.config/helm/subscriptions/claude-max5x"

[pools.codex]
provider = "codex"
home = "~/.codex"

[pools.google]
provider = "antigravity"
config_dir = "~/.config/agy"

[pools.anthropic-api]
provider = "claude"
api_key = "env:ANTHROPIC_API_KEY"     # the key is named, never written here
budget = "200 USD/month"             # optional: a wall when spent

[sessions.bridge]
title = "Ship's Bridge"

[sessions.office]
title = "Admiral's Office"
human = true                         # the human types here; the ship never does

[sessions.crew]
title = "Crew"

[seats.pike]
session = "bridge"
role = "captain"
persona = "Pike"
harness = "claude"
pool = "claude-secondary"
model = "claude-opus-5-5"
effort = "max"

[seats.scott]
session = "bridge"
role = "engineering"
persona = "Scott"
harness = "codex"
pool = "codex"
model = "gpt-6-astra"
effort = "xhigh"

[seats.desk]
session = "office"
role = "desk"
persona = "Maeve"
harness = "claude"
pool = ["claude-secondary", "claude-primary"]   # starts on the first; moves on at a wall and stays
model = "claude-opus-5-5"
effort = "max"
cwd = "~/dev"                        # a seat may override the workspace
remote_control = true

[seats.worker-1]
session = "crew"
role = "worker"
harness = "codex"
pool = "codex"
model = "gpt-6-astra"
effort = "high"
up = false                           # declared, brought up on demand
fresh = "after-order"                # a clean context for every order

[seats.worker-1.settings]            # the program's own keys, passed verbatim at launch
model_auto_compact_token_limit = 200000
```

Rules: nothing undeclared runs. Any declared seat can be up or down; `qd up` brings up every seat marked up and resumes what has a record, and `qd up <seat>` brings up one that was declared down. A seat without a persona is shown by its name. A pool is one way of paying, named for its place in the ship: a subscription, named by its credential directory, or an API key, named by the environment variable or file that holds it. A provider may have any number of pools of either kind. Pools point at credentials and never contain them. A seat's `pool` is one pool or a list in order of preference (section 6). Seats start in the ship's workspace unless they name their own. A session marked `human = true` is where the human types, and the ship never types there; its seats are reached by their watch (section 7). `remote_control = true` opens the seat's session from the operator's phone and claude.ai as well, where the program has that feature; Claude Code has it, and `qd up` refuses the key on a program that does not. A seat's `settings` are its program's own keys, passed verbatim as launch overrides; nothing is ever written into the program's configuration. `qd up` also starts the ship's one service: one plain process per ship, not a seat, which renders inboxes, delivers messages, writes attempts, applies deferred controls, fails seats over between pools, writes the heartbeat and samples the machine. It belongs to whoever ran `qd up`, outlives that shell, and is restarted by `qd up` when the status line says it is down; a ship may run it under a systemd user unit if it likes, and the framework neither requires nor installs one. `qd` works on the ship folder named by `--ship` or by `QD_SHIP` in the environment, which `qd up` puts in every seat's environment. The ship file is written in the plain form shown here, one key per line, and `qd set` edits the one line a change needs and keeps everything else, comments included; a test round-trips this example.

**One project folder.** The framework, every ship's declaration and every ship's state live in one folder, because the crew edits all three in the same place and sorting user-edited from crew-edited from source from state across folders is the mess to avoid. What keeps them apart is git, with three nested repositories: the project folder is the framework repository; a ship folder inside it, `ships/enterprise/`, is its own repository holding the declaration, ignored by the framework repository; and the runtime inside the ship folder, always `runtime/` beside the ship file, is a third repository with no commits, a fence and nothing more, ignored by the ship repository. Git will not enter a nested repository without a second force, so `git clean -fdx`, `git stash --all`, `git checkout .` and `git reset --hard` run in the project folder leave the declaration and the state untouched, and `git clean -fdx` run in the ship folder leaves the state untouched, each checked on git 2.53. `qd set` commits what it writes when the ship folder is a repository, so no live change sits uncommitted for a stash to take, and the ship repository's log is the ship's own history of rotations. The double force, `git clean -ffdx`, destroys everything, as `rm -rf` does; nothing short of another disk prevents that, and it is accepted. `qd up` creates the fence if it is missing and refuses a ship whose runtime git tracks. The example ship travels inside the framework repository as tracked files, so it is the one ship that cannot be its own repository, and `qd up` says so in a line when it runs it.

## 5. Harness adapters

One class per agent program, all implementing the same small interface.

| Method | Answers |
|---|---|
| `argv(seat, resume, first)` and `env(seat)` | The exact command line and environment for a launch, with or without resume, carrying the first message where the program takes one on its command line |
| `identify(pane)` | Is this pane running me? Process id, version, model, session id, or none |
| `home(seat, pool)` | The variable that names this program's home, the files a fresh home needs to open without a dialog, and how the pool's credential reaches it: by environment, or by one link |
| `record(identity)` | The transcript file, joined exactly or not at all: the first record written after this launch |
| `state(record)` | The live reading: the last user entry, whether a turn is open, the open tool call if any, the last write time |
| `turns(record, since)` | Ended turns: reply text, tokens by class, tool calls with their durations and failures, the turn's duration, the subagent records it spawned |
| `screen(pane)` | What is on the screen: a dialog, a menu, the input box and its text, whether the program is working |
| `box(text)` | How this program shows `text` in its box before it is sent: the text itself, whitespace aside, or the pattern of the placeholder it folds a long paste into |
| `keys()` | The keys this version uses: submit, send now while working, interrupt, empty the box; or that it lacks one |
| `queues_while_working` | Whether a message typed during a turn is taken and queued, or refused until the turn ends |
| `brief(seat)` | How the brief reaches this program without touching the user's files: a file appended to its system prompt, a configuration override, or the first message |
| `controls()` | What can be changed and how: for each control, a launch form (a flag, an override, an environment variable), an in-session form (a slash command), or both, and the state that confirms the change took |
| `interruptions()` | The dialogs and notices this version shows: how each is recognized on the screen, and the key that dismisses it, or that it needs a human |
| `usage(pool)` | A subscription pool's utilization windows and reset times; a key pool has none and is measured by spend |
| `version()` | Installed version, and whether it is proven |

First round: `claude`, `codex`, `antigravity`. GLM is `claude` with a different endpoint and model map, so it is configuration, not a new adapter. Grok and Muse follow in the second round. Each adapter ships a card, `harnesses/<name>.md`, with the facts a seat needs about its own program, and a fixture set per proven version for the contract tests.

Our files, their flags. An adapter passes Quarterdeck's own files and overrides on the program's command line and never edits the program's configuration, the user's instruction files or the user's repository. The two forms of a control are not equal. The launch form is free when a seat comes up. The in-session form is the one for a change while it runs, because a slash command typed into the pane leaves the change in the session log, where the human can read it, and because it costs no restart; model and effort change often and quickly in a session, and `/model` or `/effort` in the pane is the visible record. A control with only a launch form needs a restart, and `qd set` says so rather than doing it quietly. A control is confirmed by the program's resulting state, the model it reports or the setting it shows, never by the echo of the command, and is left pending when that state cannot be read. The launch form also turns off whatever the program would write into its own box, such as generated prompt suggestions: each costs a model call, nobody at this keyboard uses them, and the box must hold only what someone typed. The switch that does this is a per-version fact pinned by fixture, because the door's empty-box check depends on it.

**Versions.** The programs update often, by their own hand or the system's, and a public framework cannot pin what a user's machine installs. An adapter therefore knows its program by version: a table of facts, each row the version it was proven on, the newest row applying to anything newer. At every launch `qd` reads the installed version, and `qd status` shows it beside the running one and says whether it is proven: `claude 2.1.317 running · 2.1.320 installed, unproven`. An unproven version runs with the newest known facts applied, best effort by design, because a new version is nearly always identical in everything the adapter touches; it is never a reason to hold the ship. `qd check <harness>` proves one: it opens the program in a throwaway pane and runs the contract probes live, one short turn, comparing launch, brief, paste, submit, the record's place and shape, and the usage reading against the fixtures, and ending with one task: the program is asked to write a named file with given contents and to post a report saying so, and the check verifies the file and the report. Controls and interruptions are proven by focused probes as each adapter is built. When everything matches, the version is marked proven in the runtime; when something differs, the differences are saved as fixtures for the adapter's next row, which in the public repository is a pull request. The adapter reads the least that changes most: the record and the command line change rarely and carry the facts; the screen changes often and is read only for the box and the dialogs. An update that changes what the adapter reads shows as a withheld line or an unknown obstruction, reported, never as a wrong action taken. A seat home turns off the program's self-update where the program has the switch, so on a ship versions change when the operator says, or when the system does, and either way the status table says so.

## 6. Seats

A seat is one running program in one pane, under one role. `qd up <seat>` launches it with what its adapter builds: the pool's credential, the model and effort, the brief and the seat's settings, all as flags and overrides on the program's own command line, with the seat's open messages as the first message where the program takes one there. `qd up` with no seat brings up the ship and resumes every seat that has a record. `qd down <seat>` ends one. Restart and fresh are one relaunch path with one difference: `qd restart <seat>` relaunches with resume, keeping the conversation, for a control that has only a launch form or a program that has stopped answering; `qd fresh <seat>` relaunches without resume, a new conversation that begins with the brief and the open messages. Nothing is typed to clear a seat, because a typed clear cannot change a seat's pool or program, leaves a second record in its home, and cannot refresh a brief that was passed at launch. The seat's current record is the first record written after its launch, which is exact because one process writes a home; older records stay for the Ledger.

**Controls.** `qd set <seat> model=claude-opus-5-5 effort=high` changes a seat. It writes the ship file first, and commits the write when the ship folder is a repository, so the change persists through down, up and restart; then it hands the change to the service, which applies it to the running program by the control's in-session form, a slash command typed through the door, so the change stands in the pane where the human can read it, and confirms it by the program's resulting state. A control with no in-session form is written and reported, not forced: `settings written; qd restart scott applies them`. A change of pool is a restart with resume, because the record lives in the seat's own home and only the credential changes: `pool written; qd restart pike applies it`. A change of harness is a fresh start by nature, because the conversation lives with the program: `harness written; qd fresh pike applies it`. On a seat in a human session, every control goes by restart with resume at turn end, because the ship never types there; the human at that keyboard may type the slash command by hand. `qd controls <seat>` lists a seat's controls with the form each has. A slash command typed by hand in a pane changes the program and not the ship file: the ship file says what should be, the Watch says what is, and `qd status` marks any difference, whether a change is waiting for a restart or a hand changed something in the pane: `model claude-fable-5-1 (ship file: claude-opus-5-5)`. `qd set <seat> --adopt` writes what the seat is running into the file in one command. Settings the framework does not name pass through: the keys of `[seats.<name>.settings]` are the program's own, given verbatim as launch overrides, so a program's new feature is available the day it ships, with no change to the adapter.

**Fresh starts.** Compaction after compaction leaves a context made of summaries of summaries. The framework's guarantee is narrow and kept: after any restart or fresh start a seat receives its brief and every open message in full. What a seat holds only in its context, it loses on a fresh start, so what must survive goes in a post, and the ship's orders say so. `qd fresh <seat>` is a fresh start on demand, and `fresh = "after-order"` in the ship file makes one each time a seat's last open order is closed and the closing turn has ended and been recorded, so every order begins on a clean context. It is for workers, who take one order at a time; command seats are long-lived and keep their context, and what a human types straight into a command seat's pane, outside the post, is theirs to lose. The Drill proves the guarantee by relaunching every seat fresh between rounds.

**Failover.** A seat's `pool` may be a list. The seat starts on the first; when its pool walls, the service moves it at a turn's end, by restart with resume, to the next pool in the list that is not walled, wrapping round, and it stays there until that pool walls in turn. It never moves back on its own, because the point of two subscriptions is to spend both to the full over the week, and a seat that bounced back at every reset would spend one and spare the other. `qd status` says `pool claude-primary · moved from claude-secondary at its wall, 11-07 09:12`. The Desk, whose silence costs the most, has two pools in the flagship. A model may be listed the same way in a later version, for the walls a provider puts on one model alone.

**From inside a seat.** `qd` inside a seat needs no argument: the ship folder is in its environment, and it knows the seat it runs in from the pane. Every control goes through the service, whichever seat asked for it, and a control on a working seat waits for the turn to end, so that nothing is restarted or cleared under a running tool call. That is how the Desk changes its own model at a wall: it asks, finishes its turn, and the change lands.

**Reaching a seat mid-turn.** A turn can run half an hour. Both first-round programs take a message typed during a turn and queue it, showing it in the pane, so a message is typed the moment it arrives, turn or no turn, and read at the turn's next stop. Two faster speeds exist where the program has the key: `qd post --now` types the message and presses the program's send-now chord, so the model reads it at its next step without losing the tool call in flight; `qd post --interrupt` presses the program's interrupt key first, which stops the turn, then delivers. `qd stop <seat>` is the interrupt alone. The adapter declares the keys, or their absence.

**A seat's home.** Each program keeps its settings, instruction files, transcripts and logs under one home directory named by one variable: `CLAUDE_CONFIG_DIR` for Claude Code, `CODEX_HOME` for Codex. The user's own home for a program is the user's, and a ship seat never runs in it. Quarterdeck builds every seat a home of its own under the runtime, from files it owns: the program's settings as the ship wants them, from the framework's template or the ship's own `homes/<harness>/` folder in the program's own format, with the seat's settings laid over; trust for the workspace and the first-run flags already set, so a fresh home opens without a dialog; the program's self-update turned off where it has a switch; permission for `qd post` and the post store granted through the program's own permissions and sandbox; and nothing of the user's. The pool's credential is the one thing shared: an API key by the environment variable the program reads; a subscription by environment where the program takes a long-lived token there, by a link to the pool's one credential file otherwise; and never by a copy, because refresh tokens are single use and a copied credential dies at the first refresh, which logged four of five seats out of the current ship on 5 September 2026. The link alone is not enough either, and the current ship learned that on 20 September: Claude Code writes a refreshed credential by atomic rename, which turns the seat's link into a private file holding the only live refresh token and strands the pool's file and every other seat on it within seconds. So the service watches every seat's credential file on its tick: when one stops being a link, a refresh happened there, and the service copies the refreshed file over the pool's, parks the stale one beside it, and relinks the seat, before the next seat needs it; a test launches two seats on one pool, refreshes in one, and checks that the other sees it. The grant itself also expires, about every four weeks, and no relink cures that, only the human's login into the pool; the service reads the credential's expiry times as metadata, never the token values, and the status line warns two days ahead: `claude-secondary · login needed by 11-13 22:24`. Whether Codex rewrites its file the same way is a probe for the spike, and the same watcher applies. So a ship seat and the user's own sessions share a subscription and nothing else: not settings, not instruction files, not memory, not transcripts, not logs. What follows: the programs' logs are in the runtime; a seat's record is the only record of its launch in its home, so the join is exact by construction; the user's own directory stops filling with the crew's sessions; and the ship's program settings are files in the ship folder, read and changed like everything else of the ship's. The pane itself is not logged: a program redraws its screen many times a second, and the record already holds every word.

```
runtime/
  .git/                a fence: no commits, never tracked, keeps git run above it out
  service.heartbeat    pid and time, written each tick
  seats/<seat>/home/   the program's home, built by qd: the ship's settings, the seat's records and logs
  seats/<seat>/record  the path of the current record, taken at launch
  seats/<seat>/watch   pid and time of the seat's armed watch, if any
  post/store/          every message, one immutable file each
  post/attempts/       every delivery attempt, written before typing
  post/inbox/<seat>.md what the seat reads: open obligations first
  brief/<seat>.md      what the seat read at boot
  ledger.sqlite        turns, tokens, tool calls, machine load, by pool, seat and order
  proven/              versions proven here by qd check
  log/                 the service's and the door's own logs, JSON lines
```

## 7. Post

A message is one immutable JSON file:

```json
{
  "id": "ord-20261106-2101-desk-7f3a",
  "kind": "order",
  "from": "desk",
  "to": "scott",
  "re": null,
  "subject": "Codex profile of the fake harness",
  "due": "2026-11-08T18:00:00-07:00",
  "at": "2026-11-06T21:01:12-07:00",
  "body": "Build the Codex profile of the fake harness. Report when its contract tests pass."
}
```

Five kinds: order, report, question, answer, ack. `re` names the message being answered. Only a report closes an order: a report from the order's addressee whose `re` names it. An ack claims: it names the order its sender is working on, for the Ledger, and never closes anything. `subject` is optional, one line.

Where files live, under the runtime: `qd post` writes each message atomically into `post/store/`, the one canonical copy; `post/inbox/<seat>.md` is the rendered view, open obligations first, oldest first, then what was already delivered; `post/attempts/` holds one record per delivery attempt. Messages are addressed to seats by name, whatever program a seat runs.

**Two ways in.** A message reaches its addressee by one of two paths, chosen by the session the seat sits in. On the Bridge and in the Crew, where the human reads but does not type, the service types the message in full into the seat's pane through the door, under a one-line header and over a one-line footer:

```
qd > order from desk · ord-7f3a · Codex profile of the fake harness
Build the Codex profile of the fake harness. Report when its contract tests pass.
qd > reply: qd post report --re ord-7f3a · inbox: post/inbox/scott.md
```

The pane is where the human reads, by scrolling up, so the whole message goes there and not a pointer to a file: a file name in a transcript tells a person nothing. Both first-round programs show a sent paste in full in their transcript, checked on 6 November 2026 on Claude Code 2.1.317 and Codex 0.166.0. Several messages arriving together are one paste, each under its own header. A message has no length limit.

In a human session, the Office, where the human types, the ship never touches the box. A seat there runs `qd inbox --wait` in the background: it blocks until something new is addressed to that seat, prints the new messages in full in the same form, and exits, and the program hands the output to the agent as a notification. The wait registers itself in the runtime while it runs, so the service knows a watch is armed; the brief tells the seat to arm it again as the last act of every turn. If no watch is armed, the message waits and the status line says so. The watch cannot reach a seat mid-turn or stop it, which the human at that keyboard does by hand, and which is the other reason the Bridge and the Crew keep the door.

**Delivery, exactly once or visibly not.** Before the door types, the service writes an attempt: the message, the seat, the launch it was aimed at, the time. The door then types once and confirms: by the screen that the message is in the box or queued, and by the record that the program took it. If the service dies between, it reconciles every open attempt against the record when it comes back: found means delivered; not found means blind, shown on the status line and never resent by the service. `qd post resend <id>` is the Desk's deliberate act after a look. A message typed into a running turn is confirmed queued at once by the screen and read later by the record, and the status line shows how long a queued message has waited.

**Attribution.** The Ledger charges each of a seat's turns to the order the seat has claimed with an ack; failing a claim, to its only open order; failing that, the turn is unattributed and `qd ledger --by order` says how many were. The turn in which a closing report is sent is charged to the order it closes.

## 8. Watch

Two readings, taken together, because they answer two different questions.

**Activity**, from the record, answers "what is it doing":

- `idle`: the last turn ended and nothing is queued.
- `working`: a turn is open and steps keep coming, with the open tool call named: `working, in Bash 3m`.
- `stalled`: a turn is open and ten minutes have passed without a step, with the open tool call named: `stalled, in Bash 14m`. A seat that has never moved is idle, not stalled.
- `off`: no harness is running in the pane, with the reason: exited with a code, crashed, no pane.

**Door**, from the screen, answers "can the typing path deliver":

- `open`: the box is empty and nothing is in the way. Generated suggestions are turned off at launch, so text in the box always means someone typed it. A pane left scrolled is open: a paste reaches the program in copy mode and a raw pasted carriage return submits it without leaving copy mode, checked on 7 November 2026, so the person reading keeps their place. A working seat is open where its program queues.
- `obstructed`, with the reason named and what clears it: a key the adapter knows (`daybreak notice, dismissable`); time (`limit banner, resets 14:00`; `working, program does not queue`); or a human (`text in the box`, `login`, `unknown dialog`).

Text in the box that the door did not type is an obstruction and nothing more is done about it: nothing is typed, nothing is lifted, nothing is restored. The message waits in the inbox, the status line names the seat as needing a human, and the Desk is told once. This is the whole of the guarding, and why a half-written message can never be sent: the door types only into an empty box, so the human's text is never in the box when the ship's goes in.

A reading the Watch could not take is `unknown`, with what it could not see. Unknown is never dressed as a state. Error is not a state either: a seat's own failure shows as `off` with its exit or as `obstructed` with the banner, and the Watch's own failure is `unknown`.

```
seat    activity              door                              order      today
pike    idle                  open                              -          12.1k
scott   working, in Bash 3m   open, queues                      ord-7f3a   48.9k
una     stalled, in Bash 14m  open, queues                      ord-9c21   3.2k
laan    idle                  obstructed: login, needs a human  -          0
spock   off                   exited 1 at 21:12                 -          0
```

Rules carried over from two months of operating the current ship, each of which is a law in section 11 or a definition above: titles are rewritten by the programs themselves and are never identity; a reading that could not see something says so; the typing path clears what it knows how to clear, refuses anything else in the way, types once, checks the box as the program shows it, and only then submits; no keystroke ever goes into a dialog it does not know, a menu, or a human's text. The box check compares whitespace-normalised text, or matches the placeholder a program folds a long paste into by pattern, ignoring its counter, because panes reflow whenever a person attaches from another screen and the second paste of a session carries a different number; the record carries exactness. A withheld delivery empties the box of what the door itself typed, so the door never leaves its own text behind to be read as a person's. An unknown obstruction is a fixture waiting to be saved: `qd watch <seat> --save-screen` records the screen for the adapter's next contract test, so a program's new notice becomes a known one in one commit. When a seat needs a human, the service says so where the human looks, with a report from `ship` to the desk seat: `qd > report from ship · laan obstructed, login needs a human`.

**The status line.** Every ship session's tmux status line carries `qd status --line`, refreshed by tmux on its own timer, independent of the service and of every door: `service ok · 0 waiting · laan needs a human · wall: claude-primary 7d · disk: root 94%`. The service writes a heartbeat each tick; when it is stale the line says `service down`, every `qd` command says so too, and `qd up` restarts it. This is how a dead service never looks like a healthy idle ship, and how an alarm reaches the human without passing through the Desk's own door.

None of this is a setting. The ship file declares a ship; it is not a settings file for the framework. If a behaviour ever needs a knob, the knob goes in the ship file under a table named for the part it tunes, because one file is the only place a ship is configured, and each such knob is a default the framework should have got right. Ship sessions keep a 50,000-line history so the scrollback holds days, not hours, and no window size is ever fixed.

`qd watch` prints the table above; `qd watch <seat>` prints the full reading.

## 9. Ledger

Pools: utilization for each window the provider exposes (five hours, seven days), reset times, burn (1.0 means on pace to use the whole quota by reset), a wall when a window is spent, and a stale flag when the reading is older than the provider's cache floor. A wall is a fact to route around, not an alarm. An API key pool has no windows. Its row is spend: cost where the program states it, else tokens priced by a table the adapter carries by model, and a `budget` in the pool's declaration makes a wall of it.

Turns: a row per ended turn, from the record. Tokens in as many classes as the provider bills, four for Anthropic: read cached, read uncached, cache written, written; cost where the program states it; the tool calls made, by tool, each with its duration and whether it failed; the turn's duration; and the turns of any subagents the seat spawned, charged to the seat. The three measures the fleet film states in public, read cached, read uncached and written, stay the display; the rest is a column away.

Machine: the service samples each seat's process tree every tick and charges the turn with the CPU seconds it used, its peak memory, the most processes it had running at once, and the bytes it wrote to disk, read from the processes' own I/O counters. Tokens say what a seat cost the provider; this says what it cost the machine, which is the shared thing on a box where five seats running a test suite at once have frozen the desktop, and where disk has filled more than once with nobody able to say which seat was writing. The status line shows the free space on every filesystem the ship writes to, the runtime's, the workspace's and the system's temporary space, and when one runs low it names the seat that wrote most in the last hour, so the question "who is filling the disk" has an answer before the disk is full.

Seats and orders: the same rows, rolled up by seat and by the order each turn was charged to.

`qd ledger` prints the pools; `qd ledger --by seat` and `--by order` print the rollups, with tokens, cost, tool calls, minutes, machine load and bytes written side by side. The Ledger is one SQLite file in the runtime; ingestion is idempotent, so a turn read twice is counted once.

## 10. Standing orders

Three tiers of document, each in its own file, each growing at its own pace, none allowed to become another:

- **Laws**, `laws.md`: what the framework itself guarantees, enforced in code and tests. One page, few by construction. They change rarely.
- **Ship's orders**, the ship's own `orders.md`: how this crew behaves. The ship changes them at will. The framework carries them to every seat and does not read them.
- **Cards**, `roles/<role>.md` and `harnesses/<name>.md`: facts a seat needs about its role (accountable for, may change, call when, never) and about its own program. They change with the crew and with program versions.

A seat's boot read is the laws, its ship's orders, its role card, its harness card and its inbox. Target: under 3,000 words, against about 21,000 today. `qd brief <seat>` prints exactly what that seat reads at boot, so a human can read what the agent read.

**Ours, and exposed.** The brief is Quarterdeck's own instruction set, independent of whatever `CLAUDE.md` or `AGENTS.md` a user keeps, and every word of it comes from a file the user can edit. The framework's `standing-orders/` folder holds `laws.md` and the generic cards. A ship's folder holds its `ship.toml` and `orders.md`, and may hold any card under the same path, `roles/<role>.md` or `harnesses/<name>.md`, which then replaces the framework's for that ship, and `homes/<harness>/`, the program's own settings files as this ship wants them, in the program's own format. To change what every seat reads, edit `orders.md`; what one role reads, its role card; what one program is told about itself, its harness card. The laws are the framework's, and the one part a ship does not override. Nothing is hidden in code: `qd brief <seat> --sources` prints which file each part of the brief came from, and names any instruction file a program will still load from elsewhere, for the programs that have no switch to stop it. Changing them is editing a file: the Desk, carrying the Admiral's word, edits `orders.md` or a card as it would any other file, and who may do so is a matter for the ship's orders, not the framework. An edit is an event like any other: the service re-renders the briefs it touches and tells each affected seat with an ordinary message from `ship`, `qd > ship's orders changed · read brief/scott.md`, and a seat coming up or starting fresh reads the new brief by itself.

**How the brief reaches a seat, and nothing else does.** Every program loads instruction files of its own. Claude Code reads every `CLAUDE.md` from the working directory up to the root and combines them, root-most first, with the one in its home; checked on 7 November 2026 with three nested folders, all three were loaded. Codex reads `AGENTS.md` the same way, and the others have their equivalents. A ship's seats use the ship's instructions and nothing else, at the Admiral's word. Claude Code is launched with `--setting-sources user`, which the same probe showed loads no instruction file from the working directory or its parents, and its home is ours; Codex is launched with its own switch for the same, pinned in the spike; where a program has no such switch, the brief states the precedence in one sentence: where these orders and a file the program loaded disagree about the ship, the orders win. The brief tells a seat where the workspace's own notes live, and reading them is the seat's work like reading any other file. Quarterdeck never writes a user's instruction files and never relies on them: the brief is complete on its own, and the fake harness, which loads no instruction files at all, is fully briefed by it. The brief is rendered to `brief/<seat>.md` in the runtime and reaches the seat by the channel its adapter declares: a file appended to the program's system prompt where the program takes one on its command line, which costs no turn; a configuration override where that is the program's way; or as the first message otherwise. In every case the seat's open messages follow, under a header saying the seat was briefed and with what: `qd > briefed as scott, engineering · 2 orders open`. The consequence worth wanting: ship doctrine that lives in a person's instruction files today, because there was nowhere else to put it, moves into the ship's orders, and those files go back to being about the person and the machine.

What keeps the laws few is not a number but a test of admission: a law exists only if the framework enforces it in code and tests. A rule nothing enforces is an order, not a law. A framework of a few thousand lines holds only so many such guarantees, so the page stays short by construction. If it ever needed a second page, the framework would have grown, and that is the thing to look at, not the page. The other habits: a law's statement is one or two sentences, its reason one sentence beside it, its history in git; laws have ids, not numbers, so retiring one never renumbers the rest; and the laws are grouped by the part that enforces them, so a reader who knows the parts knows where each law lives.

The connection to code is a comment and a command. Wherever code carries a law, the line is tagged with the law's id:

```python
# src/quarterdeck/door.py
def deliver(seat, text):
    clear_known(seat)                       # law: one-door
    shown = screen(seat).composer           # law: one-door
    if not harness(seat).box(text).matches(shown):
        empty_box(seat)
        return Withheld(seat, text, shown)
    submit(seat)
    return confirmed_by_record(seat, text)  # law: one-door
```

The same tag goes on the tests that prove it, and the test tagged with a law tries to break it and fails to. `qd laws` finds every tag and prints each law with the lines that carry it:

```
one-door   Everything typed into a seat goes through one path: it clears what it ...
           src/quarterdeck/door.py:141, 142, 148   tests/unit/test_door.py:88
```

A test fails when a law has no carrier, or a tag names a law that does not exist. Reading the law, you can jump to the code that enforces it. Reading the code, you know why a check is there.

## 11. The laws, second draft

**Ship**

- `ship-file` `qd` reads its configuration from the ship file alone; the ship file is the only configuration a ship has.
- `nothing-leaks` Nothing from the runtime enters a repository: no ids, records, credentials or usage.
- `one-folder` The `qd` program writes only inside its project folder: a ship's declaration, which it commits, and a ship's runtime, which git never tracks. The user's instruction files, program configuration and code are read, never written.

**Harness**

- `one-record` A seat is joined to one record, the first written after its launch. An ambiguous join is reported, never guessed.

**Seat**

- `identity` A seat's identity is its pane id, process id and record. Names and titles are display.
- `one-door` Everything typed into a seat goes through one path, in one process: it clears what it knows how to clear, refuses what it does not, types once into an empty box, checks the box as the program shows it, submits, and confirms by the record. It never types into a session the human owns. Never resend what you cannot confirm.
- `context-is-cache` After any restart or fresh start a seat receives its brief and every open message in full, so a seat may be relaunched at any time and the ship loses nothing it sent.

**Watch**

- `record-first` Read a seat from its record first and its screen second. A reading that could not see something says so, and never reports a default as a fact.
- `nothing-waits-silently` Every withheld or blind delivery, every seat needing a human, every wall, a filling disk, a grant about to expire and a dead service show in the status line of every ship session, refreshed by tmux itself and not by the service.

**Post**

- `post-is-files` Messages are immutable files addressed to seats by name, whatever program a seat runs. The service renders inboxes and delivers each message once, in full, through the seat's door or its watch.
- `open-until-closed` An order stays open until its addressee reports on it. Acknowledgments claim; they never close and never wake.
- `events-not-clocks` No seat is asked to poll. The service wakes seats on arrivals and retries withheld deliveries on one tick.
- `can-post` Every seat can run `qd post` from its launch: the adapter grants it through the program's own permissions and sandbox, and `qd check` proves it by posting once.

**Ledger**

- `every-turn-accounted` Every turn is accounted, by pool, by seat and by order, from the harness's own record, or counted unattributed and reported.

Retired from the first draft: "stalled means it moved and then stopped" is the Watch's definition of stalled; "walls are normal" is advice on conduct and belongs in a ship's orders; "the framework carries no law about code" is the scope statement in section 1, and nothing in code enforces it, so it is not a law. Restated after the review of 6 November: `ship-file` promised a runtime derived from the file and a file never edited by hand, neither of which code can enforce; `context-is-cache` promised what agents put in their posts; `events-not-clocks` forbade the clocks the Watch runs and the ticks the service needs, since the standard library has no file watcher; `every-turn-accounted` denied the unattributed turns section 7 admits. Two added: `nothing-waits-silently` and `can-post`. Declined: a wall law, because a wall is a fact to route around and the status line shows it; a human-first law, because `one-door` already never types into a person's text. Renamed on 7 November: `exact-join` became `one-record`, because the word exact read as a demand for exact program versions, and versions are best effort by design; the law was always about the record.

The flagship ship's orders begin: the Admiral's word reaches the bridge through the Desk; the Captain turns it into orders; no change reaches the ship's code until independent agents have reviewed it, it has passed its tests, and the First Officer has signed it.

## 12. Tests

Four layers:

1. **Unit.** Pure logic: the ship file and its line editor, message records, inbox rendering, the typed form, attribution, the Ledger's arithmetic, the status line from a fixture state.
2. **Contract.** Each adapter against recorded screens and transcripts, one directory per proven version, with a check that every proven version has a fixture set; `qd check <harness>` runs the same layer live against the installed program. Every interruption the adapter knows has a screen in the set, every control has the state that confirms it, the box's folding of a paste is recorded with its counter, and the claude set includes a subagent transcript.
3. **Integration.** The Drill on a real tmux server with `qd-fakeharness --as <name>`, a small program that behaves like each real one: it shows a box that folds a long paste, a dialog, a program that queues while working and one that does not; it accepts a message, works for a while through steps, writes the transcript shape of the program it imitates, and replies. Every seat sends to every other seat and the Watch confirms each delivery; the service is killed mid-delivery and recovers with no message lost or doubled; every seat is relaunched fresh and the matrix runs again; the Office seat is reached by its watch and never typed into.
4. **Live drill.** Opt-in, real programs on throwaway seats, the same matrix plus one duty per role from its card and one task per seat that the drill can check without reading prose: write a named file with given contents, run a given command and report its output, post a report with given words. The drill verifies each by the filesystem and the post store, not by the reply, and emits a pass table. It spends real tokens and runs only when asked.

Three tests guard the repository itself: no source file over 400 lines; no runtime content, seat id or credential pattern in any tracked file, and every `runtime/` ignored; every law carried somewhere, every law tag real, and every law's test attempting its violation.

## 13. Repository layout

```
quarterdeck/                       the project folder: the framework repository
  LICENSE  README.md  pyproject.toml  .gitignore (ships/*, !ships/example/)
  AGENTS.md      for whoever works on Quarterdeck itself, person or agent; CLAUDE.md is one line that imports it
  src/quarterdeck/
    cli.py         the qd command
    ship.py        ship.toml -> Ship: sessions, seats, roles, pools; the line editor; the fence
    tmux.py        the only module that runs tmux
    seat.py        seat identity, lifecycle and controls: up, down, restart, fresh, stop, set
    door.py        the one typing path: clear, type, check, submit, confirm; three speeds
    service.py     the ship's one long-running process: inboxes, deliveries, attempts, deferred controls, the heartbeat, the machine sample
    harness/       base.py claude.py codex.py antigravity.py grok.py muse.py
    post.py        messages: store, inbox, the typed form, the watch
    watch.py       reading a seat: activity and door; the status line
    ledger.py      turns, tokens, tool calls, machine load, by pool, seat and order
    standing.py    standing orders: laws, the ship's orders, cards, the brief
    drill.py       the all-hands drill
    fakeharness.py the stand-in for every real program, installed as qd-fakeharness
  standing-orders/ laws.md  roles/  harnesses/        generic cards
  ships/
    example/       ship.toml  orders.md    three generic seats, runs anywhere; tracked here, the one ship that is not its own repository
    enterprise/    a nested repository, ignored above: ship.toml  orders.md  roles/  harnesses/  homes/  runtime/
    */runtime/     a nested repository with no commits: the fence around a ship's state
  tests/           unit/ contract/ fixtures/ integration/ live/
  docs/DESIGN.md   docs/reviews/
```

Python 3.12 or later. No runtime dependencies beyond the standard library, none optional either. `pytest` for tests.

## 14. Build order

1. Ship and tmux: the ship file and its line editor, sessions and windows, the nested-repository fence, the status line.
2. The fake harness as the first adapter and the reference implementation of the interface: a box that folds a paste, a dialog, a program that queues while working and one that does not, a record in the shape of each real program.
3. Seat lifecycle: up, down, restart and fresh as one relaunch path; seat homes; the brief channel; `qd brief`.
4. Post, the door and the service: the store, the inbox, the typed form at three speeds, the attempt record, the watch.
5. Watch: activity with the open tool call, door, the status line's facts.
6. The fake drill green: the matrix, the service killed mid-delivery and recovered, every seat relaunched fresh, the matrix again.
7. In the first week, beside steps 2 to 6, the spike: a throwaway tmux session with one real Claude seat and one real Codex seat launched from hand-written command lines, the ones `qd` will later generate, exchanging one post each way. It exists to learn what the fake cannot teach before the adapters are written: whether each program asks permission before running `qd`, whether the Codex sandbox lets a seat write to the post store, how each folds a paste, what each does with a message typed mid-turn, and the exact shape of each record. Nothing from it is kept but the saved screens and records, which become the first contract fixtures, and a list of assumptions that held or did not.
8. The claude and codex adapters against screens saved from the spike, then `qd check`.
9. Ledger: pools, turns, tool calls, machine load, attribution.
10. Standing orders: the laws tagged as the code was written, the ship's orders, cards, `qd laws`, and the test that tries to break each law.
11. The antigravity adapter and the GLM configuration.
12. The flagship brought up on Quarterdeck with a few seats, the old runtime left untouched; README, the recorded demo, then grok and muse.

**The first night.** The Admiral's word on 7 November: the framework is built end to end in one night by the Desk, with the four reviewers of section 15 reviewing the code along the way. The order above is the order of the night, and it is a priority order: what the night does not reach is named in the morning report, never implied. Three things shape the night.

- **Reviews at three points, not per commit.** A review is of a piece of work at its tip, in a worktree of that commit that nobody edits while the review runs. Point A: the control plane green on the fake harness, steps 1 to 6. Point B: the real adapters, steps 7 and 8. Point C: the whole, at the tip, after steps 9 to 11. At A and B the three fast reviewers run in parallel, Codex Sol, Codex Astra and Argon, each with the worktree and leave to run the tests, under one brief: at most eight findings, each marked reproduced, meaning the reviewer ran it, or asserted, meaning it read it; a reproduced finding is fixed, an asserted one is written down. At C the same three run, and Claude Opus 5.5 at max runs on the tip while the night's last work, the README and the morning report, goes on beside it. Every review's raw output and its record of what was fixed and what was written down go under `docs/reviews/`. The fixes after C ship unreviewed and the record says so, because a review of a fix is a new tip, and that ladder has no end.
- **Commits small and often, local only, plain.** The repository stays on this machine until the Admiral copies it out; nothing is pushed. Every step lands as one or more commits so that each review has a tip to pin. Commit messages carry no attribution trailers, no co-author line and no session link, at the Admiral's word: several models build this by default, and the history says what changed, not who typed.
- **The ship's own machine rules hold.** Tests run one suite at a time with at most eight workers; scratch goes to the scratch filesystem and never to the system's temporary space; the spike's and the live checks' real programs run in throwaway tmux sessions named `qd-*`, in seat homes of their own under the runtime, and never in the ship's running sessions or the user's configuration directories. Step 12 is not the night's: the flagship moves to Quarterdeck on the Admiral's word, in daylight, with the old runtime untouched until then.

The morning report, in `docs/BUILD.md`, says what landed with its commits, what each review found and what became of it, what was reached and what was not, and what the Admiral has to decide next.

## 15. Decisions

Taken by the Admiral on 2026-11-06: the name; MIT with his copyright; the crew's public names and roles as stated in the film "The fleet is yours"; providers claude and codex first, Google with them because Google sponsors the conference, grok and muse in the second round; landing and governance out of the framework, with the Captain and First Officer practice kept in the ship's orders; a minimal board with token tracking by provider, seat and order; the Desk as a managed seat; the accounting noun is the Ledger, as the film names it; pools named for their place with any number per provider; the laws traceable to the code that carries them; the Watch reduced to activity and door; "role" in place of "billet"; the flagship ship is "enterprise" and no hostname appears in the repository; conference material lives outside the repository; the laws kept few by their admission test and grouped by the part that enforces them; the Engineering persona is Scott, as the film says; workers sit in a third session named "Crew"; the live drill runs only on request; Standing orders and Drill are parts in their own right, the two that keep the six honest; a control changes by slash command where the program has one, because the change then stands in the session log, and a control that needs a restart is reported, never applied quietly; a fresh start is a first-class control, the one the current ship never had; a program's own settings pass through as its own keys; a dialog the adapter knows is dismissed and one it does not is refused and reported; the human commands through the Desk and the Desk commands through `qd`, so `qd` is written for an agent at the keyboard; a control set through the Desk persists through down, up and restart, so `qd set` writes the ship file; the brief is Quarterdeck's own, independent of the user's instruction files, with every file behind it exposed in the ship folder; messages typed in full into the addressee's pane, because the transcript is what the human reads and a file name there tells a person nothing, settled by a probe showing both first-round programs render a sent paste in full; generated suggestions turned off at launch in every program; a seat's home holds nothing of the user's, so a ship session and the user's own are told apart in logs and instruction files alike; no links except the one credential, and only where the program takes no token by environment; versions proven, not pinned, an unproven version running and reported; API keys as pools beside subscriptions, named in the ship file and never written there, measured by spend with an optional budget as their wall; a home per seat built from the ship's files; orders and cards as files, the service re-briefing the seats they touch; no framework settings in the ship file.

Taken by the Admiral on 2026-11-07, after the review of 6 November by four reviewers (Claude Opus 5.5, Codex Sol, Codex Astra and Argon, their reports in `docs/reviews/2026-11-06/`) and his own notes: an ack claims and only a report closes; fresh and restart as one relaunch path, the typed clear dropped; the service as the only process that types; a delivery attempt written before typing, a blind attempt shown and never resent by the service; text in the box an obstruction and nothing more, the still-draft lift cut; the status line as the alarm that passes through no door, and the law `nothing-waits-silently`; no fixed window size, reflow handled by comparison; a scrolled pane delivered through, settled by a probe; one path per seat, the Office human-owned and reached by its watch, the Bridge and the Crew by the door; one project folder, not one folder per ship, fenced by three nested repositories, with `qd set` committing what it writes, settled by a probe on git 2.53; delivery into a running turn at three speeds, settled by a probe; slash-command controls kept in the first release; four laws restated and `can-post` added; the fake harness built first and the week-one spike; the typed clear, the runtime path override, the per-seat outbox, the optional dependency and the "last woken for" attribution cut; tokens in as many classes as the provider bills; tool calls, durations, machine load and disk written in the Ledger beside the tokens, with the free space of every filesystem the ship writes to on the status line.

Taken by the Admiral on 2026-11-07, later: the spike is part of development testing; the framework is built end to end on the night of 7 November by the Desk, with code reviews by the design's four reviewers at three points and a final review on the tip, as section 14 says.

Taken the same night: a slash command sent by `qd set` updates the ship file, since `qd set` writes and commits the file before it types; a slash command typed by hand changes the program and not the file, and `qd set --adopt` writes what a seat is running into the file. And from his answers to what the review left: versions are best effort and the join law is renamed `one-record`; `one-door` names the session the human owns; a ship's seats use the ship's instructions and nothing else, by each program's switch, settled for Claude Code by a probe; a seat's pool may be a list, the service moving the seat on at a wall by restart with resume and leaving it there until that pool walls too, never back on its own, so that two subscriptions are both spent to the full over a week; the fourteen laws of the second draft taken as they stand; the service is one process per ship, not a seat, run by whoever runs `qd up`, and nothing is pushed into systemd; `fresh = "after-order"` is for workers, command seats being long-lived; the live check and the live drill ask each program to do something and verify it did; commits carry no attribution trailers; credential handling copies the current ship's lessons, share by link, reseed and relink on the tick when a refresh breaks the link, warn two days before a grant expires; and the flagship moves to Quarterdeck only after the Admiral has tested and worked with it, never automatically. Nothing is open.
