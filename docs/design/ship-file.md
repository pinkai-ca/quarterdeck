# Quarterdeck design: The ship file

A page of [the design](../DESIGN.md).

## 4. The ship file

`ship.toml` is the ship's only configuration. It declares sessions, seats, workspaces and ways of paying for each program. Loading it validates the declaration without running a program or reading an account. Nothing undeclared runs.

The example declares a captain, an engineer and Maeve as desk:

```toml
[ship]
name = "enterprise"
operator = "Admiral"                 # what this ship calls its human
workspace = "~/dev/quarterdeck"      # the default working directory of every seat
tick = 1.0                           # the service's wait after each pass, in seconds: the default
carry_bytes = 100000                 # policy bound for first message and each index page, UTF-8 bytes
# push_reminder = "reviewer"         # optional: the seat ordered to read the hosted runs after pushes of main

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
# For now no seat launches on a pool paid by a key: see "Seats, sessions and pools" below.

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

[seats.captain]
session = "bridge"
role = "captain"
persona = "Captain"
harness = "claude"
pool = "claude-secondary"
model = "claude-opus-5-5"
effort = "max"

[seats.engineer]
session = "bridge"
role = "engineering"
persona = "Engineer"
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

A pool paid by a key, a key pool or an endpoint with `api_key`, is declared and read as any other, but for now no seat launches on it, jailed or not: until the routed key isolation lands, a key reaches the seat's program, and so the model's tool commands. Every door that starts or proves a seat refuses it with one line, `<seat>: its pool <pool> is paid by a key, and no seat runs on a key until the routed key isolation lands; nothing was started`: qd up, fresh and restart, qd set and qd up `--pool`, the service's moves and relaunches, and a fresh start after compactions. qd check and qd doctor open no seat on it, qd doctor fails its pool, and the service never moves a seat onto it, so a seat whose other choices are spent is held at its limit. The ship file still loads, so a seat that was already running on a key can be seen and stopped. A subscription and a keyless endpoint are unchanged.

Canonical provider identifiers are `anthropic`, `openai`, `google`, `xai`, `meta`, `zai` and `openrouter`. Known company spellings normalize without case, including `z.ai`/`z-ai`, `x.ai`, `open-ai` and `open-router`; other explicit endpoint operators retain their trimmed spelling. `synthetic` identifies the fake program. Native harnesses accept their company's subscription or key; open harnesses accept compatible endpoint pools and refuse a key without an endpoint. Validation and preflight refuse incompatible choices before replacing a running pane.

Native harness names remain provider input aliases. Doctor names the company-spelling edit. A downgrade to a reader that requires native spellings needs those spellings restored and validated before installing it. Pool names, credentials and historical ownership stay the same. Public readings separate `provider_id`/`provider_name`, `harness`, executable `program` and paying `pool`; open harnesses have no native provider. Native model IDs and usage groups retain their source meanings.

A human session is where the person types. Its seats receive messages through their own inbox watch; the [door](door.md) defines the narrowly permitted waking line. `remote_control = true` enables remote access only on a harness that supports it; otherwise up refuses. `settings` are the program's own launch overrides, with no edits to the operator's configuration.

An omitted human-session title is its capital-first key (`office` becomes `Office`). An omitted nonhuman title is the ship name followed by the capital-first session key. Explicit titles win. A derived nonhuman title refuses `.` or `:` in the ship name. The existing exact-title session guard checks `QD_SHIP`: a different owner refuses with its folder, and an unproved owner remains unknown. Changing derived titles on an existing ship requires bringing it down before sync.

### Service and ship selection

One plain service process belongs to whoever ran `qd up`, outlives that shell and is restarted by up when dead. It renders inboxes, delivers messages, records attempts, applies deferred controls, moves seats at limits, writes its heartbeat and samples the machine. A ship may run it under a user unit; the framework installs none.

`ship.tick` is the wait after each service pass: 1 second by default, or a finite number from 0.05 to 5 inclusive. Each pass reloads the declaration; invalid edits retain the last valid ship and tick. Every duty and heartbeat still runs once per pass. The stale heartbeat bound remains 10 seconds. No environment variable overrides tick.

`ship.push_reminder` optionally names a declared seat. Twenty minutes after the framework checkout's `refs/remotes/origin/main` last moved, the service files that seat one order to read the hosted runs on the pushed tips ([post](post.md) describes it). Without the key the reminder is off and nothing is read or written. A name that is no declared seat is refused when the ship file is loaded.

`[pr_screen]` declares the pull-request screen (see [working](working.md#a-change-from-outside)): `hands = ["hand-a"]`, the hand that reads an outsider's text, or `hands = ["hand-a", "hand-b"]`, two hands, one on each of two models, and `reader = "reviewer"`, the seat ordered to read a request the hand, or both hands, and the search found clean. A flagged request is told to the seat whose role is desk, unless `desk = "seat"` names another. Loading refuses the table when:
- `hands` names no seat, more than two, or the same seat twice;
- any name is not a declared seat;
- a hand is not jailed (an endpoint pool, or `sandbox = true`);
- a hand has a pool that is not an endpoint pool: a hand that reads an outsider's text keeps no sign-in in its home;
- two hands share a pool or a declared model (a single hand has no second to differ from);
- the reader is one of the hands;
- a hand or the reader runs, on any of its pools, a program whose provider's safeguard can refuse, and so lose, a conversation that reads instructions planted for a reader (the adapter's `refuses_planted_instructions`; Codex today).

Without the table the ship has no screen, and the program says so and reads nothing.

`--ship` wins over `QD_SHIP`. Up sets `QD_SHIP` in each seat. Without either, a person's shell uses the invoking checkout's `ships/.default` or, when no mark exists, its sole non-template, non-practice ship. An invalid mark or several eligible ships without a mark refuses. `qd default` reads the checkout's mark and `qd default <ship>` validates the named eligible ship's declaration before writing it. Both forms stay in the checkout even with `QD_SHIP` set and refuse global `--ship`. A seat never selects a ship from the mark. A ship's installed copy needs `--ship` or `QD_SHIP` for ship selection and has no default mark of its own.

The invoking checkout passes the absolute selected folder to the ship's own copy. A copy that supports the default annotation includes it in the text proof or the JSON `default_ship` field. For an older copy the invoking checkout reports the selection on stderr before handing over the same command. Doctor warns about `QD_SHIP` in a shell that is no seat.

### Project and runtime

The framework checkout contains ship folders, each with its own declaration repository. The ignored runtime beside each declaration has a third Git repository with no commits: its fence keeps Git commands in a containing repository from entering runtime. Ordinary clean, stash, checkout and reset in the framework preserve the nested declaration/runtime; ordinary clean in a ship preserves runtime. A double-force clean can destroy nested repositories and is outside this protection. Up creates a missing fence and refuses tracked runtime. `qd set` edits the needed line, preserves comments and unrelated text, and commits declaration changes in a ship repository.

The tracked templates are default, drydock, forum, cascade, shuttle and office. They cannot be nested declaration repositories; copy a template before using it. Up reports a declaration still tracked by its parent. The live office is a separate ignored project-top folder with its own declaration and runtime fences. [Structures](structures.md) describes the templates, forum groups, cascade declaration and office creation.
