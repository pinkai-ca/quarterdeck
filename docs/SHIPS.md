# Ships

A ship is a group of AI agents, with its orders and records: a folder under `ships/`, in the Quarterdeck folder you cloned. Each has its own service, runtime and tmux server. Each agent runs in a harness, a terminal coding program such as Claude Code or Codex. [The parts of Quarterdeck](PARTS.md) explains the other words.

## Ships to start from

- [`ships/office/`](../ships/office/README.md), the person's working office for the fleet: one desk initially, and any additional office seats the person declares. It has `kind = "office"`, no captain or crew, and no project workspace requirement. Creating an office does not move any seat or message from an existing ship.
- [`ships/drydock/`](../ships/drydock/ship.toml), the practice ship: three stand-in agents on a fake harness, for the drill (`qd drill`), a whole-crew exercise, and for Quarterdeck's own tests. Spends nothing.
- [`ships/default/`](../ships/default/ship.toml), the smallest real crew: a desk that you talk to, on Claude Code, and on Codex a captain who turns what you say into orders and reviews the work, and an engineer who builds it. Uses the sign-ins already on your machine.
- [`ships/forum/`](../ships/forum/README.md), a lead and four reviewers: give every reviewer the same order, and get one combined report from the lead. The forum's README has the steps and the limits.
- [`ships/cascade/`](../ships/cascade/README.md), three agents on Claude Code, Codex and Grok Build: the three take turns on one question before one conclusion. The cascade's README has the steps; the template still needs a live proof on installed harnesses.

## Start with your office

From the Quarterdeck project folder, run:

```sh
.venv/bin/qd init
```

This copies the tracked office template to `office/`, at the top of the project, and commits the declaration, orders and local desk card in the office's own git repository. Its `runtime/` is ignored. The office gets its own service and tmux server; only the desk starts. The template uses Claude Code's existing sign-in and the program's model and effort defaults. If the program or sign-in needs attention, the office remains saved: edit its declaration or complete the program's sign-in, then rerun `qd init`. No live check is run by init.

The office is always the real `office/` folder at the project top. It has no alternate location or alias. Neither init nor setup moves an existing ship's office seats or messages.

If an existing project ship already declares a desk in a human session, init and first-time setup name that ship and refuse to create a second office. The office still lives there. Moving it is your step once forwarding has been proven; these commands leave its declaration and records untouched.

To open the desk, run `.venv/bin/qd --ship office attach`. Tell the desk what you want to work on. Declaring further office seats is your choice: add them to `office/ship.toml` with a role such as counsel or communications and a session with `human = true`, then bring each up by name. Rerunning init starts only the desk, even if other declared seats are marked up.

`qd setup` remains the crew wizard. When no office exists, its first step runs init and returns with the desk ready; a later setup keeps the wizard for a project crew.

## Ask the desk for a project ship

The desk selects a kind (`default`, `forum`, `cascade` or `shuttle`) and the office pools it will use, then runs `qd raise`. Declare those pools in `office/ship.toml` first. A template pool can use an office pool of the same name, or be mapped explicitly:

```sh
qd raise shuttle mission --pool anthropic=claude --pool openai=codex --workspace /path/to/project
```

The command shows the complete declaration and the files to copy. A new proposal from the desk stays pending and starts nothing; it prints the command for you to approve or decline in your own shell, where `QD_SEAT` is absent. Use that command with `--accept yes` or `--accept no`. From your shell, `qd raise` can also ask directly. The exact answer is kept in the office's ignored runtime. Only the office desk or you can raise a ship; a crew seat cannot answer for you or raise one itself.

For a new yes, the command also checks its live process ancestry against the recorded programs of all project ships and the office. Run it from your own shell, outside any agent program; unreadable ancestry refuses the answer. The command that accepts your yes performs the raise itself. Earlier exact answers can still be reused. These records express your consent and are writable by programs running as your Unix user; they do not protect against a hostile agent that edits them directly.

An approved proposal copies the complete template to `ships/mission`, creates its own git repository, commits its declaration, orders and cards, and brings up its declared crew and service. Its folder name, `mission`, is its fleet address. Discovery reads that declaration; raising adds no routing configuration or separate fleet list. Without `--workspace`, the crew works in the Quarterdeck project folder. `--model SEAT=MODEL` replaces a template's example model; the cascade and forum examples require this for their placeholder models. Pool mapping copies declaration references, never credentials; key pools use `env:NAME` references.

The desk can reuse an exact approved proposal. Repeating it resumes the matching ship without edits or a second set of panes. If the template, selected pools or other declaration details change, a new proposal needs your answer. Raise never overwrites an existing ship: retain a differing ship and choose a new name. If launch fails, its committed declaration and your answer remain for a retry after the underlying program or sign-in is ready. The new office command does not move any existing office.

## A project ship of your own

After initializing the office, `qd setup` can write a project crew for you. To write a ship by hand, install Quarterdeck as the README's [Quick start](../README.md#quick-start) says, then run these steps from the folder you cloned, where `qd` is `.venv/bin/qd`.

**1. Practise on stand-ins.** Optional, and spends nothing.

```sh
.venv/bin/qd drill  # ends: all passed of 29 checks
.venv/bin/qd --ship ships/drydock up  # the practice ship: three stand-in agents in three tmux sessions
.venv/bin/qd --ship ships/drydock status  # service ok, a table of the three agents, open orders: 0
.venv/bin/qd --ship ships/drydock down  # ends: service stopped
```

**2. Copy the default ship.** The folder may have any name: here, `mine`.

```sh
cp -r ships/default ships/mine
cd ships/mine
git init  # the copy becomes a git repository of its own
git add ship.toml orders.md
git commit -m "My ship"  # prints: 2 files changed
cd ../..  # back to the folder you cloned
```

`ship.toml` describes the crew. `orders.md` says in plain words how the crew works, and stays as copied.

**3. Edit `ships/mine/ship.toml` before anything else.**

- `[ship]`: set `workspace` to your project's folder, absolute, or relative to the ship's folder. The copied `workspace = "../.."` points the crew at the Quarterdeck folder, not at your project. `name` and `operator` are the ship's name, and what the ship calls you.
- `[pools.<name>]`: a pool is what a harness runs on: a subscription you are signed in to, an API key or a local model. Change `config_dir` under `[pools.claude]` and `home` under `[pools.codex]` if your sign-in folders are not `~/.claude` and `~/.codex`. Remove a pool you do not use, and add one for what you have: [`ships/forum/ship.toml`](../ships/forum/ship.toml) shows a pool for a subscription.
- `[seats.<name>]`: a seat is one agent. Set `harness` to a harness you have, and `pool` to a pool that the harness can run on: [Harnesses](HARNESSES.md) gives each harness's name. Choose a `model` and an `effort` that your subscription offers: nothing checks either against your account. `qd controls --harness <name>` prints the efforts that a harness takes, or says that they are not measured. For the harness's own default, leave `model` or `effort` out and set `model_mode = "program-default"` or `effort_mode = "program-default"`; pi and OpenCode need `model`.

A local model is a pool of two lines:

```toml
[pools.local]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"
```

Commit your edits:

```sh
git -C ships/mine commit -am "My crew"
```

**4. Check the machine.**

```sh
.venv/bin/qd --ship ships/mine doctor
```

`qd doctor` prints one line for each check, marked PASS or FAIL:

- `unproven: qd check <name> proves it`: no live check has proved that version of the harness. The crew runs either way, and `qd check claude`, for one, spends a few turns.
- `no credential at <folder>; sign in to the program there`: sign in to that harness.
- `no copy yet; qd up makes one`: nothing to mend. The first `qd up`, in the next step, makes the ship's own copy of the Quarterdeck code.
- `effort '<effort>' is not supported by <harness>; the levels are …`: the harness does not take that effort. Set one of the levels the line names.
- `<harness> takes no effort; effort '<effort>' is not supported`: the harness takes no effort. Remove `effort` from the seat.
- `levels of <harness> <version> not measured; '<effort>' unchecked`, on a line marked PASS: Quarterdeck has no measurement of the efforts that version takes, and passes yours unchecked.

**5. Bring the crew up, and look at it.**

```sh
.venv/bin/qd --ship ships/mine up
.venv/bin/qd --ship ships/mine attach
```

- The first `qd up` makes the ship's own copy of the Quarterdeck code, and says so. The copy changes only when you run `qd sync`: see [Updating your ship](SYNC.md).
- `qd up` starts Quarterdeck's service, which delivers the crew's messages, and every agent marked up, then prints a line for each agent, such as `desk up in %1 · claude … · fresh`, then `service started`, and last a line that begins `tmux: the ship's own server`.
- `qd up` gives each agent one first turn, to read its brief. After that an agent takes a turn only when the agent has something to do.
- `attach` opens the tmux session `Office`, where the desk is. The captain and the engineer are in a session named for the ship, such as `Endeavour Bridge`: `.venv/bin/qd --ship ships/mine attach bridge` opens that one. Both sessions are on a tmux server of the ship's own, apart from any tmux you use yourself.

**6. Give the first order.** A desk on Claude Code, Codex, OpenCode or pi is woken by the ship with the desk's first waiting message. A desk on another harness cannot hear the crew until the desk has taken one turn: speak to that desk first, in the desk's pane, or an order given by hand waits for the desk.

Type what you want in plain words, as you would to any coding agent. The desk asks what you meant where it is unclear, gives the crew its orders, and tells you when the work is done, on any supported harness.

Or give an order by hand:

```sh
.venv/bin/qd --ship ships/mine post order --to captain "Build the thing. Report when its tests pass."
```

- The command prints `ord-<id> to captain · stored · the service types it into captain's pane`.
- An agent is addressed by seat name: in the default ship, `desk`, `captain` and `engineering`.
- The order stays open, and shows in `qd --ship ships/mine status`, until the captain reports on the order.

After that, [How to use it](../README.md#how-to-use-it) has the everyday commands. To leave `--ship ships/mine` out of them, make your ship the default:

```sh
.venv/bin/qd default mine
```

## Fresh starts

A fresh start gives an agent a new conversation. The agent receives the agent's brief and an index of the agent's open messages, one line for each message, by thread: the messages the agent still has to answer, and the messages the agent sent that still wait for an answer. The index tells the agent to read a thread whole before acting on any message of that thread, and the agent prints the thread with `qd thread <id>`. A handoff is a note that an agent addresses to itself before a fresh start; the agent receives each handoff that is still open in full, ahead of the index.

`carry_bytes`, under `[ship]` in `ship.toml`, bounds the size of an agent's first message after a restart or a fresh start, and of each page of the index. A longer index is split into pages, and a handoff too long for the first message is handed over as a file that the agent reads first.

```sh
qd fresh engineering  # by hand, at any time; in the middle of a turn, the fresh start waits for the turn to end
```

Two settings in `ship.toml`, each on a seat, let the ship do the same:

- `fresh = "after-order"`: the ship starts the agent fresh when the agent has closed its last open order.
- `fresh_after_compactions = 2`: when the agent's harness has compacted the conversation twice, the ship asks the agent for a handoff. The agent posts the handoff, and the ship starts the agent fresh when that turn ends. Claude Code and Codex count compactions; on the other harnesses the setting does nothing.

`qd setup` gives the second setting to every agent on Claude Code or Codex.

In two cases the ship does not restart the agent itself:

- When the agent's record does not show that the agent's work has ended, the status line names the agent as ready, and you run `qd fresh`.
- In `Office`, the session you work in, the ship never restarts an agent: the session is yours. For an agent on Claude Code or Codex, the ship still asks for the handoff and says when the agent is ready. Your desk then starts itself fresh, once you have been quiet for twenty minutes. Any other agent you keep in `Office` waits for you to run `qd fresh`.

Where a seat's `role` is `systems`, the ship also tells that seat once, when an agent is idle and has become ready for a fresh start that the ship does not make itself. While no seat with that role is up, the status line keeps naming the agent even when the line is full.
