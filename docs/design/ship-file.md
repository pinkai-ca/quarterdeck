# Quarterdeck design: The ship file

A page of [the design](../DESIGN.md).

## 4. The ship file

The example gives names to roles: Pike, the captain; Scott, the engineer; and Maeve, the desk.

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

Rules: nothing undeclared runs. Any declared seat can be up or down; `qd up` brings up every seat marked up and resumes what has a record, and `qd up <seat>` brings up one that was declared down. A seat without a persona is shown by its name. A pool is one way of paying, named for its place in the ship: a subscription, named by its credential directory; an API key, named by the environment variable or file that holds it; or an endpoint, a model served at an OpenAI-compatible URL, local or remote, with no credential and no quota wall, and a key named beside it where a remote service wants one. A provider may have any number of pools of any kind. Pools point at credentials and never contain them. A seat on an endpoint pool runs its program in a jail unless the ship says `sandbox = false`, and any seat does with `sandbox = true` ([section 5](harnesses.md#5-harness-adapters)). A seat's `pool` is one pool or a list in order of preference ([section 6](seats.md#6-seats)). Seats start in the ship's workspace unless they name their own. A session marked `human = true` is where the human types; its seats receive messages through their watch ([section 7](post.md#7-post)). Into a session the human owns it types only when a message waits there, no watch is armed and the box is empty. `remote_control = true` opens the seat's session from the operator's phone and claude.ai as well, where the program has that feature; Claude Code has it, and `qd up` refuses the key on a program that does not. A seat's `settings` are its program's own keys, passed verbatim as launch overrides; nothing is ever written into the program's configuration. `qd up` also starts the ship's one service: one plain process per ship, not a seat, which renders inboxes, delivers messages, writes attempts, applies deferred controls, fails seats over between pools, writes the heartbeat and samples the machine. It belongs to whoever ran `qd up`, outlives that shell, and is restarted by `qd up` when the status line says it is down; a ship may run it under a systemd user unit if it likes, and the framework neither requires nor installs one. `qd` works on the ship folder named by `--ship` or by `QD_SHIP` in the environment, which `qd up` puts in every seat's environment. With neither, a person's shell acts on the default ship: the folder named in `ships/.default`, which `qd default <ship>` writes and `qd default` prints with how it was chosen; with no mark, the one ship under `ships/` that is not a template or the practice ship. Several ships and no mark, or a mark naming a missing folder, are refused by name, never guessed. The choice is passed on as `--ship`, so a ship's own copy acts on the same folder, and every verb that acts on it says so: its first line ends `(default ship: <name>)`, its JSON carries `default_ship`. A seat never reads the mark. `qd doctor` warns of `QD_SHIP` set in a shell that is no seat. The ship file is written in the plain form shown here, one key per line, and `qd set` edits the one line a change needs and keeps everything else, comments included; a test round-trips this example. `tick` in `[ship]` is how many seconds the service waits after each pass: 1.0 when absent, and otherwise a number from 0.05 to 5 inclusive, refused by name as `ship.tick` outside those bounds or when it is not a finite number. The service reads the ship file afresh at the start of every pass, so a new tick takes effect from the wait after that pass. A ship file that no longer loads keeps the last valid ship, its tick included. Every duty and its heartbeat run once per pass whatever the tick, and the stale age the service is judged by stays 10 s, so the longest tick keeps a running service fresh. The tick is set only in the ship file: no environment variable changes it. The test ships and the drill's generated drydock declare a short tick.

An untitled human session uses its key with the first letter capitalized (`office`
becomes `Office`). An untitled nonhuman session uses the exact ship name, a space,
and the capital-first key (`Endeavour Bridge`). Explicit titles win. A ship-name
`.` or `:` is refused, naming the character, only when a derived title uses that
name; human-only or explicitly titled sessions do not impose that ship-name rule.
The default ship declares no titles. The drydock keeps explicit Bridge/Crew and
uses `Drydock Office`. At launch the existing exact-title session guard reads its
`QD_SHIP` environment: a different owner refuses with that actual folder, and an
unproved owner stays unknown. This adds no registry or lifecycle path; it does not
promise an earlier atomic refusal before runtime preparation.

**One project folder.** The framework, every ship's declaration and every ship's state live in one folder, because the crew edits all three in the same place and sorting user-edited from crew-edited from source from state across folders is the mess to avoid. What keeps them apart is git, with three nested repositories: the project folder is the framework repository; a ship folder inside it, `ships/enterprise/`, is its own repository holding the declaration, ignored by the framework repository; and the runtime inside the ship folder, always `runtime/` beside the ship file, is a third repository with no commits, a fence and nothing more, ignored by the ship repository. Git will not enter a nested repository without a second force, so `git clean -fdx`, `git stash --all`, `git checkout .` and `git reset --hard` run in the project folder leave the declaration and the state untouched, and `git clean -fdx` run in the ship folder leaves the state untouched, each checked on git 2.53. `qd set` commits what it writes when the ship folder is a repository, so no live change sits uncommitted for a stash to take, and the ship repository's log is the ship's own history of rotations. The double force, `git clean -ffdx`, destroys everything, as `rm -rf` does; nothing short of another disk prevents that, and it is accepted. `qd up` creates the fence if it is missing and refuses a ship whose runtime git tracks. Five templates travel inside the framework repository as tracked files: drydock, default, forum, cascade and shuttle. These template folders cannot be their own repositories; copy one to a ship folder of its own before using it, and `qd up` reports when a declaration still travels with its parent repository.

**The shuttle template.** `ships/shuttle` is a separate mission crew: a captain receives the order, an engineer builds it and a first officer independently checks and signs the reviewed result. Ship-local role cards replace the shared captain/engineering assumptions; no desk or worker is added, and the onboarding default remains unchanged until the Office/fleet change. A different provider from the engineer is used for the first officer where available, with any limitation disclosed. The cards use ordinary posts for review, exact-result signatures and captain-ordered landing; the framework does not enforce review quality or cryptographic signing.

Repository worktrees live under the shuttle's runtime. A named destination advances only by fast-forward after tests, review and signature; a moved branch requires rebase, retest, review and a new signature. A branchless brief ends at a signed mission branch and returns a single merge command for an operator-chosen target worktree, without executing it. Without a repository, `mission-record.md` is copied into runtime before external changes: original file copies/metadata, every command and exit, each service enabled/started, and proposed undo are recorded. The captain reviews that record and the live result; the first officer checks completeness and signs the exact revision. No undo is tried without the operator's explicit word. User-level scope and the existing security maintenance door remain; this adds no sandbox, law or automatic mission-record enforcement.

The existing explicit packaging declaration installs the template, its three local cards and its reference record form. No runtime module or ship-file schema is added. The shared template registry excludes `ships/shuttle` from default fallback and bulk sync. A copy under a different folder name remains an ordinary ship, even with the declared name `shuttle`. This preserves onboarding and does not add Office/fleet behavior. Drydock fake scripts exercise the brief/post/artifact flow only; live repository and user-timer proving require their own grant, and the timer's undo is read, never trialled.

A harness is the terminal program; a provider is the company whose models or plans the pool uses, or the declared operator of an endpoint. A model's brand does not establish the endpoint operator. A pool is one payment/access choice. Seats name harnesses; pools name providers.

Canonical company identifiers and displays are `anthropic` (Anthropic), `openai` (OpenAI), `google` (Google), `xai` (xAI), `meta` (Meta), `zai` (Z.ai) and `openrouter` (OpenRouter). Known company spellings normalize without case; `z.ai`/`z-ai`, `x.ai`, `open-ai` and `open-router` normalize to those identifiers. Other explicit endpoint operators retain their trimmed spelling. `synthetic` is the fake program's test provider. Native provider facts live on adapters: Claude Code is Anthropic, Codex OpenAI, Antigravity Google; open harnesses OpenCode and pi have no native provider.

Subscriptions run only in their provider's native harness; a refusal names it. A native harness also accepts its own provider's key. Open harnesses take compatible endpoint pools, optionally with a key, and refuse a key without an endpoint. Native cross-provider endpoints are deferred to the Claude endpoint integration. Validation and launch preflight enforce acceptance; a refused restart or move leaves the old pane intact.

Existing native harness spellings remain input aliases. Doctor prints the exact pool declaration edit to its company spelling. Install the new reader with the old ship file first, then edit and commit the provider spellings. Before downgrading, restore the previous native spellings while the new reader is installed and validate, then downgrade. Pool names, credential roots, seat harnesses, saved selections, launch history, records and Ledger ownership do not change. There is no runtime migration or provider network call. Company spellings are not readable by the preceding code. Aliases have no removal date; new examples use companies.

Public readings use `provider_id` and `provider_name` for declared company identity, separately from `harness`, executable `program`, and paying `pool`. Pool metadata includes `native_harness`; harness metadata includes `native_provider_id`/`native_provider_name` (null for open harnesses). Status/watch/doctor and Ledger readings carry declared `pools` and `harnesses` collections. Ledger's former `pools[].provider` is replaced by `provider_id`; there is no same-name semantic switch. Usage groups/windows, launch/record harness names, native model/provider IDs and accounting pool names retain their source meaning. Status-line JSON facts and its operational-only text remain unchanged.

An optional `[cascade]` declares three ordered `seats`, a separate `orchestrator`,
`efforts` (default `low`, `medium`, `high`), `notes = "ring"` and `note_chars` (default
800, at most 4000). The seats retain their conversations between laps; the real ring
starts on three providers. The fake drydock may use one synthetic provider. An explicit
`qd cascade --re QUESTION [--efforts low,high]` from the orchestrator or operator starts
one active question, addressed to the orchestrator, once by its canonical post identity.
The question remains open until the orchestrator's ordinary answer.

The service publishes child orders as `ship` in the question's thread. Reports naturally
return to `ship`, close only their ordinary child order, and are relayed whole to the next
seat. A report begins with `## Cascade note`, a bounded note, then `## Answer` and the
full answer. No intermediate report is addressed to the orchestrator. Only the service
appends the note under seat and lap to `runtime/ring/<question>/responses.md`, with
requested and observed/unknown effort. The journal beside it saves immutable post intents
before publication and offset/hash byte-append intents before writing. Recovery finishes
only the missing append suffix or publication; it never repeats a possibly typed control.
Posts keep their existing JSON shape. A changed notes prefix holds that append and causes
an early final notice; it is not rewritten. The notes path is independent of the workspace.

The existing confirmed in-session control changes effort before the seat's next lap order.
Unavailable, busy, unconfirmed or interrupted controls leave the current effort in place
with an honest fallback in the notes and final notice; the orchestrator's card requires these
limitations in its answer. This cut adds no lifecycle transition or launch-only restart.
The shared `group_post.current_seat_failure` supplies positive owned-dead or held-limit with
no eligible destination evidence. An absent pane is unknown. A recorded failure ends a lap
only when existing canonical input matching ties its inputs to that step's order, or its
latest failed turn began at/after the order's measured native taking. Consumed queues use
their consumption time. Posted-at and attempt clocks alone prove no taking. Unknown or
unreadable evidence holds, and no reader forms are added. One immutable final service report
tells the orchestrator where the notes are, includes requested and actual/unknown effort
observations and limitations, and normally carries the last full answer. An explicit
`provider-refusal` stop category in the run journal instead withholds that full answer and
refused step input. The service names the fixed category, seat/lap, notes path and last
completed report id for a person to open. The orchestrator concludes from completed notes
only, names the stop and never automatically reads/retypes the referenced report. The
category comes only from recognized current-step failure evidence or that exact current
order's membership in the existing exclusion set; an unreadable exclusion journal holds.
Unlike the forum, which gives filed reports priority because they return only to the lead,
the cascade checks exact current-order exclusion and the existing positively associated
reader refusal before accepting a report because it would relay it onward.
One `associated_failure` helper retains the existing native match and lifecycle/launch
rechecks; the service's pre-report callback and ordinary health share that proof. A failure
belongs to the current step when its recorded inputs name the step's order; when they name another
message it is that message's, never this step's; only inputs that name no message at all fall back
on time, the failed turn having begun at or after the step's taking. The
callback promotes only the existing provider-refusal category, so dead/limit and generic
errors remain report-first and cannot mask a positive associated reader refusal. Unknown
native evidence supplies no positive refusal; an unreadable exclusion journal still holds.
An already filed report remains immutable and is named separately by `stopped_report` for
a person; it is not parsed, appended as a note, marked accepted or sent to the next seat.
No reason/body substring classifier or Message fields are added. The notice does not answer
the question. An interrupted child order stays open until its addressee reports; a late
report cannot resume the ended run. The template beside drydock carries its own orders,
cards and instructions and travels with the installed copy. There is no compiler, fleet
raising or external workspace write in this cut.

Each retained run is processed independently. Malformed, foreign, conflicting or unreadable
run evidence produces a fixed derived hold under `runtime/post/cascade-holds/`, shown by
read-only `Facts.cascade_held` even with the service down and counted on crowded status lines.
The service logs changed holds after the pass, once per unchanged signature across restarts;
successful recovery clears the derived hold. This leaves group behavior unchanged. The
holds reader observes only fixed reasons/signatures, never report/provider bodies or usage.
Untyped legacy stopped journals hold rather than guessing their stop category. Notes reads,
prefix checks and suffix appends use one checked nofollow regular-file descriptor under a
lock, with identity rechecked; an uncertain observation is retained, never a terminal
success. `cascade_runtime.py` owns this runtime evidence and no native control or probe.
