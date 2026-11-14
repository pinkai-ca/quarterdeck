# Quarterdeck design: The ship file

A page of [the design](../DESIGN.md).

## 4. The ship file

`ship.toml` is the ship's only configuration. It declares sessions, seats, workspaces and ways of paying for each program. Loading it validates the declaration without running a program or reading an account. Nothing undeclared runs.

The example names Pike as captain, Scott as engineer and Maeve as desk:

```toml
[ship]
name = "enterprise"
operator = "Admiral"                 # what this ship calls its human
workspace = "~/dev/quarterdeck"      # the default working directory of every seat
tick = 1.0                           # the service's wait after each pass, in seconds: the default
carry_bytes = 100000                 # policy bound for first message and each index page, UTF-8 bytes

[pools.claude-primary]
provider = "anthropic"
config_dir = "~/.claude"

[pools.claude-secondary]
provider = "anthropic"
config_dir = "~/.config/claude-second"

[pools.codex]
provider = "openai"
home = "~/.codex"

[pools.google]
provider = "google"
config_dir = "~/.config/agy"

[pools.anthropic-api]
provider = "anthropic"
api_key = "env:ANTHROPIC_API_KEY"     # the key is named, never written here
budget = "200 USD/month"             # optional: a wall when spent

[pools.local]
provider = "local"
endpoint = "http://127.0.0.1:8000/v1"  # a model served on this machine: no credential, no quota; a seat on it is jailed
# output = 8192                     # optional response-token allowance, separate from the context window

[sessions.bridge]
title = "Ship's Bridge"

[sessions.office]
title = "Admiral's Office"
human = true                         # Into a session the human owns it types only when a message waits there, no watch is armed and the box is empty.

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
pool = ["claude-secondary", "claude-primary"]   # starts on the first; at a wall tries later choices, then earlier ones
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

[seats.worker-2]
session = "crew"
role = "worker"
harness = "opencode"
pool = "local"
model = "qwen3.8-27b"
up = false
sandbox = true                       # what a seat on an endpoint pool gets anyway; written to say so, or false to lift it
```

### Seats, sessions and pools

`qd up` starts the service and every seat marked up, resuming seats with records. `qd up <seat>` also starts a declared seat marked down. A seat uses the ship workspace unless it declares `cwd`; its persona is display, defaulting to its seat name. [Seats](seats.md) describes launch, controls and subscription moves.

A harness is the terminal program; a provider is the company or declared endpoint operator; a pool is one payment/access choice. A model's brand does not identify its endpoint operator. Seats name harnesses and ordered pools. Pools point to credentials rather than containing them: a subscription's directory, a key's environment variable or file, or an OpenAI-compatible endpoint with an optional key. Endpoint pools have no subscription quota wall. Their seats are jailed unless `sandbox = false`; any seat can request `sandbox = true`. [Harnesses](harnesses.md) owns compatibility and jail behavior.

Canonical provider identifiers are `anthropic`, `openai`, `google`, `xai`, `meta`, `zai` and `openrouter`. Known company spellings normalize without case, including `z.ai`/`z-ai`, `x.ai`, `open-ai` and `open-router`; other explicit endpoint operators retain their trimmed spelling. `synthetic` identifies the fake program. Native harnesses accept their company's subscription or key; open harnesses accept compatible endpoint pools and refuse a key without an endpoint. Validation and preflight refuse incompatible choices before replacing a running pane.

Native harness names remain provider input aliases. Doctor names the company-spelling edit. A downgrade to a reader that requires native spellings needs those spellings restored and validated before installing it. Pool names, credentials and historical ownership stay the same. Public readings separate `provider_id`/`provider_name`, `harness`, executable `program` and paying `pool`; open harnesses have no native provider. Native model IDs and usage groups retain their source meanings.

A human session is where the person types. Its seats receive messages through their own inbox watch; the [door](door.md) defines the narrowly permitted waking line. `remote_control = true` enables remote access only on a harness that supports it; otherwise up refuses. `settings` are the program's own launch overrides, with no edits to the operator's configuration.

An omitted human-session title is its capital-first key (`office` becomes `Office`). An omitted nonhuman title is the ship name followed by the capital-first session key. Explicit titles win. A derived nonhuman title refuses `.` or `:` in the ship name. The existing exact-title session guard checks `QD_SHIP`: a different owner refuses with its folder, and an unproved owner remains unknown. Changing derived titles on an existing ship requires bringing it down before sync.

### Service and ship selection

One plain service process belongs to whoever ran `qd up`, outlives that shell and is restarted by up when dead. It renders inboxes, delivers messages, records attempts, applies deferred controls, moves seats at limits, writes its heartbeat and samples the machine. A ship may run it under a user unit; the framework installs none.

`ship.tick` is the wait after each service pass: 1 second by default, or a finite number from 0.05 to 5 inclusive. Each pass reloads the declaration; invalid edits retain the last valid ship and tick. Every duty and heartbeat still runs once per pass. The stale heartbeat bound remains 10 seconds. No environment variable overrides tick.

`--ship` or `QD_SHIP` selects a ship explicitly. Up sets `QD_SHIP` in each seat. Without either, a person's shell uses `ships/.default`, managed by `qd default`; without a mark it uses the sole non-template, non-practice ship. An invalid mark or several eligible ships without a mark refuses rather than guesses. The selected folder is forwarded through installed-copy handoff, shown in the act's text proof and exposed as JSON `default_ship`. A seat never reads the default mark. Doctor warns about `QD_SHIP` in a shell that is no seat.

### Project and runtime

The framework checkout contains ship folders, each with its own declaration repository. The ignored runtime beside each declaration has a third Git repository with no commits: its fence keeps Git commands in a containing repository from entering runtime. Ordinary clean, stash, checkout and reset in the framework preserve the nested declaration/runtime; ordinary clean in a ship preserves runtime. A double-force clean can destroy nested repositories and is outside this protection. Up creates a missing fence and refuses tracked runtime. `qd set` edits the needed line, preserves comments and unrelated text, and commits declaration changes in a ship repository.

The tracked templates are default, drydock, forum, cascade, shuttle and office. They cannot be nested declaration repositories; copy a template before using it. Up reports a declaration still tracked by its parent. The live office is a separate ignored project-top folder with its own declaration and runtime fences. [Structures](structures.md) describes the templates, forum groups, cascade declaration and office creation.
