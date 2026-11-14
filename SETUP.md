# Setting up a ship

For an agent whose person said "set up a ship for me". A person at their own terminal runs `qd setup` after step 1 instead, and answers the same questions there. It takes a few minutes, and spends nothing until the crew is checked or brought up.

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

## 4. Show them the ship

Write their answers into a file outside the checkout, and let setup show the ship it would write. A file with no `accept` in it writes nothing.

```sh
.venv/bin/qd setup --answers ~/ship-answers.json
```

For the ship below, this is the whole file:

```json
{
  "use_defaults": "yes",
  "name": "Endeavour",
  "workspace": "~/code/my-app",
  "desk": "claude",
  "provider": {"pi": "local"},
  "endpoint": {"pi": "http://127.0.0.1:8000/v1"},
  "key_env": {"pi": "none"},
  "seats": {
    "captain": {"harness": "codex", "effort": "xhigh"},
    "engineering": {"harness": "pi", "model": "qwen3.8-27b", "effort": "medium"}
  }
}
```

- `use_defaults` takes setup's own suggestion for every answer the file leaves out.
- A harness is named as `qd check` names it: `claude`, `codex`, `grok`, `antigravity`, `opencode`, `pi`.
- A key is given under `key_env` as the name of the variable that holds it, never as its value.
- The project folder must exist: setup never makes it.
- `qd setup --help` lists every answer. An answer's name in the file is its flag's name, with `_` for `-`.
- Run setup from your person's own shell, never from the shell of a ship's agent.

Setup prints a line for each harness it found, and then the ship:

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

It ends `qd: missing answer accept; pass --accept ANSWER or --answers FILE`, and has written nothing. Show your person the ship, change the file as they say, and run it again, until they say yes.

## 5. Write it, check it, bring it up

Three answers are your person's, and each is asked on its own:

- `accept`: write the ship.
- `check`: run `qd check` on each harness, which spends a few of their turns.
- `start`: bring the crew up, which gives each agent one first turn.

Add all three to the file, each as `"yes"` or `"no"`, and run the command once more. Setup then:

- writes the ship under `ships/` in the checkout, commits it, and marks it as your person's default ship;
- reads whether each harness is ready. A missing sign-in is shown with that harness's own sign-in command: tell your person, and never run it for them;
- runs the checks and brings the crew up, where they said yes. Where they said no, setup prints the command that continues from there.

A refusal names its reason and what would lift it: report it to your person as setup words it, and mend only what is yours to mend.

## 6. Hand over

After a start, setup's last lines name the command that opens their desk, `Desk access: qd --ship <folder> attach office`, and a first order to type there. `qd attach` alone does the same from any folder, since the ship is their default.

A desk on Claude Code, Codex, Grok Build, OpenCode or pi is woken by the ship with the desk's first waiting message. A desk on another harness hears the crew only after the desk has taken a turn: your person starts by typing to it.

## Never decide for them

Signing in to a harness; installing one; answering a question a harness puts to the holder of its account, such as training on their work; anything paid; `qd check`, which spends their turns. Until it is run, `qd doctor` calls a harness unproven, and the crew runs all the same: say so, and let them choose.
