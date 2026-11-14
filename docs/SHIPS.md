# Ships

A ship is a group of AI agents, with the group's orders and records: a folder under `ships/`, in the Quarterdeck folder you cloned, or `office/`, beside `ships/`, for your office. Each ship has its own service, runtime and tmux server. Each agent runs in a harness, a terminal coding program such as Claude Code or Codex. [The parts of Quarterdeck](PARTS.md) explains the other words.

## Ships to start from

- [`ships/office/`](../ships/office/README.md), the person's working office for the fleet: one desk initially, and any additional office seats the person declares. It has `kind = "office"`, no captain or crew, and no project workspace requirement. Creating an office does not move any seat or message from an existing ship.
- [`ships/drydock/`](../ships/drydock/ship.toml), the practice ship: three stand-in agents on a fake harness, for the drill (`qd drill`), a whole-crew exercise, and for Quarterdeck's own tests. Spends nothing.
- [`ships/default/`](../ships/default/ship.toml), the smallest real crew: a desk that you talk to, on Claude Code, and on Codex a captain who turns what you say into orders and reviews the work, and an engineer who builds it. Uses the sign-ins already on your machine.
- [`ships/forum/`](../ships/forum/README.md), a lead and four reviewers: give every reviewer the same order, and get one combined report from the lead. The forum's README has the steps and the limits.
- [`ships/cascade/`](../ships/cascade/README.md), three agents on Claude Code, Codex and Grok Build: the three take turns on one question before one conclusion. The cascade's README has the steps; the template still needs a live proof on installed harnesses.

## Start with your office

From the folder you cloned, make your office and open it:

```sh
.venv/bin/qd init
.venv/bin/qd --ship office attach  # opens the tmux session Office, where the desk is
```

Tell the desk what you want to work on.

`qd init` copies the tracked office template, `ships/office/`, to `office/`, and commits the office's declaration, orders and desk card in a git repository of the office's own. That repository ignores `office/runtime/`. Then `qd init` starts the office's service and the desk. The desk runs on Claude Code, with the sign-in already in `~/.claude`, and with Claude Code's own default model and effort. `qd init` runs no live check.

The office is saved before the desk starts. After you edit `office/ship.toml`, or sign in to the desk's harness, run `qd init` again: `qd init` keeps the saved office and starts the desk.

The office is always `office/`: a real folder, never a link, with no other place and no other name.

Where one of your ships under `ships/` already has a desk in a session with `human = true`, your office lives in that ship. `qd init` and the first `qd setup` then name that ship, make no second office, and leave that ship's declaration and records untouched. Moving the office out of that ship is your step, once forwarding is proven: `qd init`, `qd setup` and `qd raise` move no office seat and no message out of an existing ship.

Further office seats are yours to declare. Add each seat to `office/ship.toml` with a role such as counsel or communications, in a session with `human = true`, then bring each seat up with `.venv/bin/qd --ship office up <seat>`. `qd init`, run again, starts the desk alone, even with other office seats marked up.

While no office exists, `qd setup` runs `qd init`, and ends with the desk ready.

## Ask your desk for a project ship

The desk chooses the kind (`default`, `forum`, `cascade` or `shuttle`) and the office pools for the new ship, then runs `qd raise`. Declare those pools in `office/ship.toml` first. A template's pool takes the office pool of the same name, or the office pool that `--pool` maps the template's pool to, as in this proposal of a shuttle named `mission`:

```sh
qd raise shuttle mission --pool anthropic=claude --pool openai=codex --workspace /path/to/project
```

A new proposal from the desk shows the complete declaration and the files to copy, and starts nothing. You answer in your own shell, outside every seat's harness, where `QD_SEAT` is not set. The desk's `qd raise` prints the command for your answer, ending in `--accept yes`: run that command as printed for yes, or with `--accept no` for no. In your shell, `qd raise` with no `--accept` asks you directly. `qd raise` keeps your answer to that exact proposal in `office/runtime/`. Only you and the office desk raise ships, and only you answer a proposal.

For a new yes, `qd raise` reads the command's own process and every parent of that process. The yes counts when `qd raise` reads every one of those processes, and none is a seat program recorded in the office or in a ship under `ships/`. The run that takes your yes raises the ship. The answer records hold your consent, and are writable by every program running as your Unix user: the records are no guard against a hostile agent that edits the records.

With your yes, `qd raise` copies the complete template to `ships/mission`, makes `ships/mission` a git repository of its own, commits the ship's declaration, orders and cards, and brings up the ship's service and declared crew. The folder's name, `mission`, is the ship's fleet address. The fleet finds the ship by reading the ship's declaration: `qd raise` adds no routing configuration and no separate fleet list. Without `--workspace`, the crew works in the folder you cloned. `--model SEAT=MODEL` replaces the template's example model for that seat, and the cascade and forum templates need `--model` for each placeholder model. The new ship's pools copy the office pools' references, such as a sign-in folder, and never a credential. A key pool names the key's environment variable, as `env:NAME`.

A yes stays on record for that exact proposal. The same `qd raise`, run again by the desk or by you, takes that yes and resumes the matching ship, without edits and without a second set of panes. `qd raise` commits the declaration and records your answer before the crew starts: after you install a harness or sign in to one, the same `qd raise` starts the crew. A change to the template, to the pools or to any other part of the declaration makes a new proposal, which waits for your answer. `qd raise` never overwrites a ship: where `ships/` already holds a different ship of that name, keep that ship and choose a new name.

## A project ship of your own

Once the office exists, `qd setup` is the crew wizard, and writes a project ship for you. To write a ship by hand, install Quarterdeck as the README's [Quick start](../README.md#quick-start) says, then run these steps from the folder you cloned, where `qd` is `.venv/bin/qd`.

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

**6. Give the first order.** A desk on Claude Code, Codex, Grok Build, OpenCode or pi is woken by the ship with the desk's first waiting message. A desk on another harness cannot hear the crew until the desk has taken one turn: speak to that desk first, in the desk's pane, or an order given by hand waits for the desk.

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
