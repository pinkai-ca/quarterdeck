# Quarterdeck

**One crew, every provider.**

Quarterdeck runs AI coding agents from different companies as one crew, on your machine and under your command. You direct it by talking to its agents in plain words, or by typing its commands yourself. The agents hand each other orders, report back, and rotate providers when a quota is spent.

**Supported harnesses:** Claude Code (Anthropic) · Codex (OpenAI) · Grok (xAI) · Antigravity (Google) · OpenCode and pi (OpenRouter, local models) · Muse (Meta), being added

<p align="center">
  <img src="docs/assets/quarterdeck-demo.gif" alt="A terminal recording. One order is typed in a control window. The window splits as a captain on Claude Code takes the order and hands out two more; an engineer on Codex and a hand on a local model work at the same time, each in its own pane; the captain checks the work, and one report comes back to the control window." width="100%">
</p>

<p align="center"><sub>One order, typed once, crossing three programs and back in 38 seconds. Real programs, real models, real speed. <a href="docs/DEMO.md">How this was recorded</a>.</sub></p>

Every agent runs in its normal terminal interface, the one made for people, in a tmux pane. A ship is a crew of such agents, its panes grouped into tmux sessions: `Office`, where the agent you talk to sits, and one named for the ship, such as `Endeavour Bridge`, for the rest of the crew. The sessions are on a tmux server of the ship's own, apart from any tmux you use yourself, and `qd attach` opens them. At any time you can attach, watch an agent work, scroll back through what it did, give it a command, or simply prompt it yourself.

Quarterdeck is a command, `qd`, and one small service on your machine. It starts the agents, types orders and reports into their panes as you would, watches them, and accounts for what they spend. It adds no plug-ins, tools, hooks or protocols to your harnesses, and runs no agent as a hidden background process. Restart any agent at any moment, and that agent comes back with its brief and a list of its open messages: the ones it still has to answer, and the ones it sent that still wait for an answer.

## Quick start

You need Linux, with Python 3.12 or later, tmux 3.x and git; Quarterdeck itself uses Python's standard library only. macOS is not supported yet. You also need at least one of the harnesses listed at the top, installed and signed in. Any of them will do: a crew can sit on one harness or on several, so use what you already have and like.

### Ask your agent (recommended)

**1. Ask the harness you already use to install it.**

Copy this as a prompt to your agent:

```text
Clone https://github.com/pinkai-ca/quarterdeck and follow its SETUP.md to set up my first ship.
```

Your agent installs Quarterdeck and sees which harnesses you have. It will not sign in to anything, install a harness or spend anything without asking you first.

**2. Commission your ship.**

Your agent asks, and you choose:

- **Your name and your office's**: what the crew calls you, and the name of the office you work from.
- **Your ship**: its name, and your project, the folder the crew works in.
- **Your desk and your crew.** The desk is the agent you talk to, and it sits in your office. The crew is a captain, who turns what you say into orders and reviews the work, and an engineer, who builds it. Each of the three gets a name, a harness, a model and an effort.
- **What each harness runs on**: a subscription you are signed in to, an API key, or a local model.

It shows you the whole ship before it writes anything:

```
Operator name:  Admiral
Office name:    Office

Ship name:      Endeavour
Project path:   ~/code/my-app

Seat       Name     Harness       Model             Effort
Desk       Maeve    Claude Code   claude-opus-5-5   max
Captain    Picard   Codex         gpt-6-astra       xhigh
Engineer   Geordi   pi            qwen3.8-27b       medium

Runs on
  Claude Code   a subscription you are signed in to (~/.claude)
  Codex         a subscription you are signed in to (~/.codex)
  pi            a local model at http://127.0.0.1:8000/v1
```

Then it checks the machine, and brings the crew up when you say so.

**3. Move into your office.**

```sh
qd --ship office attach  # opens your office, where the desk is
```

Your desk is there. Say what you want in plain words, and the desk gives the crew its orders and tells you when the work is done.

### Or do it yourself

**1. Install.**

```sh
git clone https://github.com/pinkai-ca/quarterdeck.git
cd quarterdeck
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .  # ends: Successfully installed quarterdeck-0.1.0
mkdir -p ~/.local/bin  # your folder for commands, made if it is missing
ln -sf "$PWD/.venv/bin/qd" ~/.local/bin/qd  # qd is linked into it, so that it runs from any folder
```

**2. Commission your ship.**

Either let the setup command ask you:

```sh
qd setup
```

It asks the same questions in your terminal, with a suggestion for every one but your project's folder: on a harness you are already signed in to, name the folder, press Enter at the rest, and you have a working ship. It shows you the whole ship, makes your office with its desk, writes the ship and brings the crew up.

Or write the ship's file yourself: [A ship of your own](docs/SHIPS.md#a-project-ship-of-your-own) goes through it step by step, and ends with the crew up.

**3. Move into your office.**

```sh
qd --ship office attach  # opens your office, where the desk is
```

Your desk is there. [How to use it](#how-to-use-it) has what to say to it, and the commands you can use beside it.

## How to use it

### Ask your desk (recommended)

Your desk is the agent in your office. Say what you want in plain words:

> Add a dark theme to the settings page, and have it reviewed.

The desk asks what you meant where it is unclear, gives the crew its orders, and tells you when the work is done. It runs the ship for you as well, so ask it for the rest in the same way:

- "What is everyone working on?"
- "What did the dark theme cost?"
- "Put the engineer on a stronger model."
- "The captain looks stuck. Start it fresh."

This is the way its creator works.

### Or use the commands

Anything the desk does for you, you can do yourself.

Bring the crew up, and down:

```sh
qd up      # start the service and every agent, each resuming where it left off
qd attach  # open your office in this terminal; qd attach bridge opens the rest of the crew
qd down    # end every agent and the service; qd down captain ends one
```

Give work, and follow it:

```sh
qd post order --to captain "Build the thing. Report when its tests pass."
qd inbox --wait   # wait for the crew's next message to you, and print it
qd status         # every agent on one screen, then the open orders
qd watch captain  # one agent in full
```

Change an agent:

```sh
qd set engineering model=gpt-6.1-sol  # written to the ship's file and committed, then applied
qd stop engineering                   # interrupt its turn, nothing more
qd restart engineering                # relaunch it; its conversation goes on
qd fresh engineering                  # a new conversation, with its brief and a list of its open messages
```

See what it cost, and that all is well:

```sh
qd ledger --by order  # what each order cost
qd doctor             # is the machine ready for this ship
qd check claude       # prove an installed harness live; spends a few turns
```

Keep it up to date:

```sh
git pull       # get the latest Quarterdeck code
qd sync        # bring your ship's copy of the code up to it
qd sync --all  # the same for every ship you have, one after another
```

- An order from your own shell goes out in your name, unchecked: the machine is yours.
- Agents go by their names in the ship's file: `desk`, `captain` and `engineering` by default.
- With no ship named, `qd` acts on your default ship, the one setup made, and says which. Name another with `--ship <folder>`, or make another the default with `qd default <ship>`.
- Every command says what it did, and every refusal says why and what would lift it.
- These are the commands you will use most. `qd --help` lists them all.
- Add `--json` to get any reading as JSON instead of text, as in `qd --json status`: [Reading as JSON](docs/JSON.md).
- What `qd sync` does, and how to go back: [Updating your ship](docs/SYNC.md).

## More

- [Harnesses](docs/HARNESSES.md): each supported harness, what it can run on, and what has been proven of it.
- [Ships](docs/SHIPS.md): the ships to start from, and how to write one by hand.
- [The parts of Quarterdeck](docs/PARTS.md): ship, seat, pool, post, watch and ledger, in a line each.
- [The laws](docs/LAWS.md): fourteen guarantees, each enforced in code and tried by a test.
- [Design decisions](docs/DECISIONS.md): the five choices that shape it, each with the code and the test behind it.
- [CONTRIBUTING.md](CONTRIBUTING.md) is for whoever changes Quarterdeck itself. [SETUP.md](SETUP.md) and [AGENTS.md](AGENTS.md) are for an agent that arrives here.

## Free and open source

Everything in this repository is free under the MIT licence.

Designed and directed by one person, [Tyler Dwyer](https://tylerdwyer.ca), and written by its own crew of agents. Write to him at [tyler@pinkai.ca](mailto:tyler@pinkai.ca). Copyright (c) 2026 Tyler Dwyer.
