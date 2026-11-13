# The recording

The recording at the top of the README is one run of a three-seat ship, made on 10 November 2026 with asciinema and shown at real speed. This page says what is real in it, what was arranged for the camera, and what was edited afterwards.

## What happens

1. The operator types one order to the captain and waits for the answer with `qd inbox --wait`.
2. The ship's service types the order into the captain's pane. The captain, on Claude Code, decides the interface and posts two orders: the code and its tests to the engineer, the documentation to the hand.
3. The engineer, on Codex, and the hand, on pi with a local model, work at the same time. Each reports when it is done.
4. The captain reads both reports, runs the tests, commits, and reports to the operator.
5. `qd inbox --wait` prints that report in the operator's terminal. The last command shows the commit the crew made.

By the ship's own clock, 38 seconds pass between the order and the report.

## What is real

Everything on screen was produced by the programs themselves, in one run from start to end.

- **The framework** is Quarterdeck at commit `bbb149f`, installed on its own for the recording.
- **The crew** is three seats on three providers:
  - `captain`: Claude Code 2.1.317 with Sonnet 5.5 at medium effort, on a subscription.
  - `engineer`: Codex 0.166.0 with GPT-6.1-Sol at low effort, on a subscription.
  - `hand`: pi 0.74.2 with Qwen 3.8 27B served by vLLM on the same machine, jailed with bubblewrap as every seat on an endpoint pool is.
- **The flow** is the framework's own. `qd post` stores each message; the service types it into its addressee's pane and confirms it from that program's record; the service's log shows each message delivered once.
- **The work** is real. The crew's commit changes three files in a toy project, 47 lines added and 2 removed, and its seven tests pass.

## The ship

One file declares the crew. Each seat has a tmux session of its own, so the recording can show any of them side by side.

```toml
[pools.claude]
provider = "claude"
config_dir = "~/.claude"         # the Claude Code sign-in on this machine, shared by one link

[pools.codex]
provider = "codex"
home = "~/.codex"                # the Codex sign-in on this machine

[pools.local]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"    # a model served on this machine: no credential, no quota

[seats.captain]
session = "captain"
role = "captain"
harness = "claude"
pool = "claude"
model = "claude-sonnet-5-5"
effort = "medium"

[seats.engineer]
session = "engineer"
role = "engineering"
harness = "codex"
pool = "codex"
model = "gpt-6.1-sol"
effort = "low"

[seats.hand]
session = "hand"
role = "hand"
harness = "pi"
pool = "local"
model = "qwen3.8-27b"
effort = "medium"
```

Its standing orders, which the framework carries to every seat and does not read:

> - The operator's word reaches the captain as an order. The captain turns it into orders, one addressee each, and gives them together so the crew works in parallel.
> - Code and its tests go to the engineer. Documentation goes to the hand as an exact, mechanical task: the file, the section and the text to write.
> - Orders given together never wait on each other: the captain decides the interface first and writes it into each.
> - An order is open until its addressee reports on it. Report when the work is done, or when it cannot be done, and say which.
> - Workers change files and do not commit. When every report is in, the captain runs the tests once, reads the changes, commits them as one commit, and reports to the operator: what changed, the test result and the commit hash.
> - Short orders, short reports, no ceremony.

## What was arranged for the camera

- **The typing.** A script types the operator's three commands with tmux `send-keys`, at about a person's speed. They are the commands a person would type.
- **The view.** The same script reads the ship's post and the service's log and decides which seat is on screen. Each seat's pane is a read-only tmux client of that seat's real session. The caption bar and the pane titles are the viewer's own; the clock in the caption bar is the real time since the order was given.
- **A ship made for the recording.** The project is a toy, a small command that prints tide tables. The hand has a shorter role card than the framework's own, and the tips under Claude Code's spinner are turned off.
- **Paths.** The ship and the project sit under `/tmp`, so no path a program prints carries a user name.
- **One cut hides a defect.** A jailed seat cannot see the other seats' folders, so at this commit the hand's own `qd post report` ends its proof line by saying the captain is down and the report will be carried at the next `qd up`. That is wrong: the service delivered the report about a second later. The view leaves the hand's pane as its report is stored, so the line is on screen for under a tenth of a second.

## What was edited

1. The recording is cut where the recorder detaches.
2. One redaction: Claude Code's banner names the subscription's plan after the model and effort. Those characters are replaced by the same number of spaces.
3. Every stretch plays at real speed. Three pauses with nothing happening are shortened, by 1.6 seconds in all.
4. Six chapter markers and the terminal's colours are added to the file.

Nothing is spliced, reordered or sped up.

## What it spent

From the demo ship's own `qd ledger --by seat`:

| seat | turns | cached input | fresh input | written |
| --- | --- | --- | --- | --- |
| captain | 3 | 308,232 | 22 | 1,418 |
| engineer | 1 | at least 56,576 | at least 29,022 | at least 964 |
| hand | 1 | 0 | 22,715 | 1,242 |

The engineer's figures are lower bounds, as the ledger marks them: part of its turn was not measured.

## The recordings on the product page

The product page shows three longer recordings, one for each kind of ship. Each is one run on real programs and real models, recorded in a terminal with asciinema. In each, a script typed the sentence at the desk and chose which agents fill the screen. While a stretch plays faster than it was recorded, the page marks it.

| Recording | What it is | What was done to it |
| --- | --- | --- |
| Flagship | A rehearsal take of 11 November 2026. Its text can be selected. The helper on a local model, named hand, had no job in this take. | Eleven minutes of work are shown in four. Long work plays from three to twenty times faster. Each change of view plays over 0.4 seconds, thirty-seven messages wait 0.8 seconds in an agent's box before they are sent, and one pause is shortened. |
| Forum | A rehearsal take of 12 November 2026. The desk stands in for the person. The lead and the four reviewers are the forum as the framework ships it, on Opus 5.5, GPT-6.1 Sol, and Gemma 4 and Qwen 3.8 served on the same machine. | Six minutes of work are shown in under three. The last two reviewers' long work plays ten to twelve times faster. |
| Shuttle | A rehearsal take of 12 November 2026. Its text can be selected. | Three minutes of work are shown in a minute and a third. Long work, the engineer's and the first officer's, plays twelve times faster. Each change of view plays over 0.4 seconds, five messages wait 0.8 seconds in an agent's box before they are sent, and one pause is shortened. |
