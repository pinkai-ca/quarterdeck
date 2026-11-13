# Setting up a ship

For an agent whose person said "set up a ship for me". A person can follow it too. It takes a few minutes, and spends nothing until the crew is brought up.

A ship is one crew, with its orders and its record. You are commissioning one for your person: they name it, choose its crew, and say what each of the crew runs on. Keep it quick and make it enjoyable: this is where a person makes the ship theirs.

## 1. Install

```sh
git clone https://github.com/pinkai-ca/quarterdeck.git
cd quarterdeck
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .  # ends: Successfully installed quarterdeck-0.1.0
mkdir -p ~/.local/bin  # your folder for commands, made if it is missing
ln -sf "$PWD/.venv/bin/qd" ~/.local/bin/qd  # qd is linked into it, so that it runs from any folder
```

Run everything after this from inside the checkout, where `qd` is `.venv/bin/qd`. The last line makes `qd` a command in every folder for your person; if `~/.local/bin` is not on their PATH, tell them.

## 2. Look at the machine

`command -v claude codex grok agy opencode pi` shows which harnesses are installed (`agy` is Antigravity; `qd doctor` later names the version each one runs). Tell your person what you found.

## 3. Ask your person

Ask in this order, a question or two at a time. Offer a suggestion with each, so that a person who only says "yes" still gets a good ship. Decide none of it silently.

1. **What the crew calls them.** `Admiral` is traditional.
2. **The ship's name.** Suggest two or three.
3. **The office's name**: the tmux session they will work from. Suggest `Office`. Neither name may hold a `.` or a `:`, which tmux does not take.
4. **The project**: the folder the crew works in.
5. **The crew.** Three to begin with: a desk, the agent they talk to; a captain, who turns their word into orders and reviews the work; an engineer, who builds it. For each:
   - a name: the default ship's are Maeve, Picard and Geordi;
   - a harness, from those installed;
   - a model and an effort. `qd --json controls --harness <name>` gives the efforts that harness takes, where they are measured, and a model and an effort to suggest. No suggestion is checked against their account: say so when you offer one.
6. **What each harness runs on**: a subscription they are signed in to, an API key, or a local model. For a key, ask for the name of the variable that holds it, never its value. For a local model, ask for its address. Where a seat may fall back on a second of these, in which order.

Then show them the whole ship on one screen, and let them change anything before you write it:

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

## 4. Write the ship

Follow [A ship of your own](docs/SHIPS.md#a-ship-of-your-own), steps 2 to 4: copy the default ship into a folder named for theirs, set `ship.toml` to their answers (the office's name is the `title` under `[sessions.office]`), remove the pools they do not use, commit, and run `qd doctor`. The workspace is your person's project, never this repository: the copied `workspace = "../.."` points here. Report what the doctor fails; mend only what is yours to mend. Step 1 there is practice on stand-ins, and optional.

## 5. Bring it up, and hand over

When your person says so, run `qd up`. Then hand them to their desk: `qd attach`, which opens their own session, the office. They start by typing to the desk; it hears the crew only after it has taken a turn.

## Never decide for them

Signing in to a harness; installing one; answering a question a harness puts to the holder of its account, such as training on their work; anything paid; `qd check`, which spends their turns. Until it is run, `qd doctor` calls a harness unproven, and the crew runs all the same: say so, and let them choose.
