# The recording

The recording at the top of the README is one run of a three-seat ship, made on 10 November 2026 with asciinema and shown at real speed, with three idle pauses shortened. This page says what is real in it, what was arranged for the camera, and what was edited afterwards.

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

The product page shows three recordings, one for each kind of ship. In each, one sentence is typed at the desk of an office. The desk passes it to a ship, the ship's crew does the work in its own programs, and the desk says what came of it. Each is one whole run on real programs and real models, recorded in a terminal with asciinema on 14 November 2026, on Quarterdeck at commit `0895228`. Nothing was typed into any agent's pane but that sentence.

Each recording can be watched in three views, Web, Portrait and Desktop: 112 by 46 characters, 80 by 71, and 225 by 63 with the office beside the ship. A view is a recording of its own run, made at that size, so the three views of one ship differ in what the crew said and did. Every recording is cut to about a minute, and the page marks, at every moment, how much faster than recorded it is playing.

| Recording | The sentence typed | The ship | Its three runs |
| --- | --- | --- | --- |
| Flagship | "Have our flagship perform a roll call: name, provider, model and effort please" | A captain and a worker on Claude Code (Opus 5.5), an engineer on Codex (GPT-6.1 Sol), a worker on Antigravity (Gemini 4 Argon), and two helpers on pi with Qwen 3.8 served on the same machine. | About 70 seconds of work each. Only the waits play faster. |
| Forum | "Use the forum to review the qd readme, with individual and combined reports in reviews folder" | A lead on Claude Code (Opus 5.5) and four reviewers, each alone: on Claude Code (Opus 5.5), Codex (GPT-6.1 Sol), Antigravity (Gemini 4 Argon) and pi (Qwen 3.8). | Between five and eight minutes of work each. Between the waits they play from 1.5 to 1.8 times faster. |
| Shuttle | "Have the shuttle make the headline glimmer when its last pink period is clicked" | A captain and a first officer on Claude Code (Opus 5.5) and an engineer on Codex (GPT-6.1 Sol). The work is a change to the product page itself, in a copy of its repository. | Web: seven minutes, a straight run, only the waits faster. Portrait: thirteen minutes, sent back twice, shown in a minute and a quarter at twice the speed. Desktop: thirteen minutes, the captain's review finds a fault and it goes back, 1.6 times faster between the waits. |

The desk in every recording is Claude Code with Fable 5.2. The programs are Claude Code 2.1.317, Codex 0.166.0, Antigravity 1.5.9 and pi 0.79.0.

### What was arranged for the camera

- **The view.** A script decides which agents are open on the screen. An agent opens when a message is stored for it, and closes a few seconds after its turn ends. Each open agent's pane is a read-only tmux client of that agent's own window; a closed agent is its label and nothing else. The label under each pane, with the agent's name, its ship, its program and its model, is the camera's, and so are the pink rules. A change of view plays over a third of a second at most.
- **The typing.** The script types the sentence at the desk with tmux `send-keys`, at about a person's speed.
- **The line under each Claude Code pane** is the camera's own, drawn with the framework's code. It lists only the subscriptions the recording uses, with the share of each one's week that was used.
- **The project.** The ships work in a copy of this repository under `/tmp`, so that no path carries a user name. The copy has no `AGENTS.md` and no `CLAUDE.md`: with either there, pi prints a notice in its pane that the project is not trusted. So when the forum's reviewers call the README's link to `AGENTS.md` broken, that is true of the copy, not of this repository.
- **The crews.** The flagship and the shuttle are the `default` and the `shuttle` ship, with each seat's program and model chosen for the recording; the flagship has two workers and two helpers added. The forum is the `forum` ship with Antigravity in one reviewer's seat. Claude Code's and Codex's tips and recap lines are turned off, and Claude Code's own announcements are marked as seen.
- **The ships' orders.** Each ship carries a few orders written for the recording, as any ship's owner may write them: how a roll call is answered (each agent for itself, in one line of a given form, posted once); that a review is one page, and the combined report as well; that a small change to a page is checked once, in one browser, at two widths and in both themes, and lands on `main`; that the desk passes a request on as it was asked, and words one for the forum for a reviewer, with a last sentence for its lead.
- **A clean start.** Before a take every agent is started fresh, the shuttle's project is put back to its first commit, and the forum's earlier reviews are removed.
- **More than one take.** Each recording is one whole run, chosen from several. Takes were set aside when a helper on a local model filed the same answer many times, when a message could not be delivered, and when the desk began by speaking of an earlier run. The first two were faults in the framework, found by these recordings and since mended or being mended; the third was the camera's own.

### What was changed in the recordings

1. Each is cut where the recorder detaches, and its last frame is held for three seconds.
2. Blanks, each character replaced by a space: the user's name and the machine's, a path under the home folder, a mail address, and the name of the plan that Claude Code and Antigravity print beside the model. Every frame is checked for these after the cut.
3. A message is held in its box. The ship submits a message about a sixth of a second after typing it. So that it can be read, the recording plays slower from the moment a message appears in an agent's box until the box is empty again: one second for one message, and a third of a second more for each further one.
4. A wait plays faster. Where no message is stored, typed or sent for more than two and a half seconds, outside the typing of the sentence, the wait keeps its first and last moments and what lies between plays a whole number of times faster, to last a second or two.
5. Where that alone leaves a recording longer than a minute, everything else plays faster too, by one factor for the whole recording and twice at most.
6. Chapter marks and the terminal's colours are added.

Nothing is spliced or reordered.

### What to know of what they show

- Each message typed into a pane ends with a line that names the reader's own inbox. Later versions leave it out.
- The desk's pane opens with the index of one earlier notice. The ship carried it into every fresh start of that desk: a fault these recordings found, whose mend is planned.
- The forum reviews the README as it stood that afternoon. What its four reviewers found was checked against the code the same day: it led to one mend in setup and to about twenty-five corrections in the README and the pages beside it. The README has them now.
