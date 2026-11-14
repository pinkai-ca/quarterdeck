# Quarterdeck design: Guided setup

A page of [the design](../DESIGN.md).

## Guided setup

The entry is `qd init`: it copies the tracked `ships/office/` template to the fixed
`<project>/office/` directory and commits its declaration, orders and local desk card
in a repository of its own. The office has `kind = "office"`, no captain or crew,
only human sessions, and no required project workspace. Its ignored runtime,
tmux server and service are independent of every project ship. Init uses ordinary
`qd up desk`, preserving any existing office declaration and starting no other seat.
It neither raises a project ship nor moves existing office seats or messages.
The first `qd setup` initializes this office and returns; after an office exists,
setup retains the crew wizard below. There is no office alias, alternate path or
additional routing configuration.

`qd setup` asks, in order, what the crew calls the person (Admiral), ship name,
office (Office), workspace, then desk, captain and engineering: display name,
harness, model and effort. Suggestions come from the default template and adapter
metadata; a suggestion does not prove account access. One supported program can
supply the whole crew, except Antigravity's one-seat limit. Next come each harness's
payment and each seat's ordered fallback pools. Payment answers are `subscription`,
`key`, `local`; flags and answers files also accept the legacy `endpoint` spelling.
Keys are environment variable names only. Endpoint operator, URL and model are
explicit facts; a model name never identifies its operator or subscription.

The ship defaults to `<Quarterdeck project>/ships/<name>`, independently of its
workspace. New ships must be direct children of `<Quarterdeck project>/ships`,
with a valid nonhidden folder name; the folder name may differ from the display
name. Other new folders refuse before acceptance with the reason and remedy.
Existing ships may keep or update any existing path inside the Quarterdeck
project. An outside folder refuses with the one-folder reason and an inside-folder remedy.
The same `install.source_repo` resolver owns the checkout identity for installation
and setup. An installed copy without an identifiable checkout refuses and asks for
setup from the Quarterdeck checkout. Its parent must exist. Setup creates only the ship folder,
with validated
`ship.toml`, `orders.md`, runtime `.gitignore`, and a scoped first Git commit;
it never overwrites an existing directory or creates the person's project.
After creation it marks that folder as the default through `ship.mark_default`
and explains that qd acts on it when no ship is named. Existing keep/update runs
leave the default mark alone. Setup itself never selects the existing default
as its target; a rerun names its folder explicitly.

Before any write, the whole proposed ship is shown: operator and office names,
a blank line, ship name and project path, a blank line, then five plain columns:
Seat, Name, Harness, Model, Effort. The project path uses `~` under the person's
home. A separate Runs on block describes subscriptions, key variable names and
model URLs. Fallback orders and necessary retained-seat fresh-start notes appear
beneath the table, never on its rows. JSON retains full paths and optional
`fresh_after_compactions`/`fresh_start_note` fields after the five seat fields.
New Claude Code and Codex seats default to `fresh_after_compactions = 2` without
a visible note; other new seats have no threshold. Retained unchanged harnesses
keep their explicit setting or absence. Moving into a counting harness adds 2;
moving away retains the key with a no-effect note, since the shared writer cannot
remove it. Default orders require a self-addressed handoff before a fresh start.

Every answer is available as a flag or in one JSON answers file. File values merge
with direct flags, followed by file edits and flag edits in order. Per-seat flags
use `--seat ROLE.FIELD=VALUE`; `--edit FIELD=VALUE` and terminal edits rebuild the
same proposal and require acceptance again. `ROLE` is desk, captain or engineering.
`model_mode`/`effort_mode` use the shared named/program-default contract; `/default`
is a terminal action, never a reinterpretation of a literal model name. Non-TTY
never starts a dialogue: missing answers refuse by name with their flag/file
remedy. Explicit `use_defaults=yes` takes known suggestions only; acceptance,
check and start still require their own answers. No unknown home, URL, model,
operator or key variable is silently invented.

Acceptance precedes declaration writes and real readiness checks. Paid `check`
and crew `start` are separate consents, each suggested yes interactively and
labelled as spending turns on each program's subscription. An answers file never
opens a dialogue, even in a terminal; it requires explicit `check` and `start`
answers, including with `use_defaults=yes`. Declining either stops with the saved
declaration and a concrete next step.
Readiness refusals give the doctor's reason and exact setup rerun. Sign-in hints
are informational, unknown unless documented, and never executed; installed
version/home/account uncertainty remains visible. Native account questions stay
with the person. Setup never signs in, buys, installs native programs or changes
their settings. Before acceptance, PATH/metadata and credential-file presence may
inform suggestions; native version/readiness probes wait. Model-listing reads are
anonymous, direct, without redirects/proxies/auth: loopback URLs, or explicitly
selected OpenRouter at its documented endpoint. Other remote URLs are not queried.
A missing or ambiguous listing requires a named model; it never triggers key use.

Existing ships explicitly keep or update. Keep refuses changed answers. Updates
use `qd set`'s validation, application, deferral and refusal path, preserving
unrelated seats, pools and orders. Unsupported inline edits, named-to-default
changes requiring key removal, and session-title changes refuse before writing,
with the explicit declaration-edit remedy. Failed first commits require the
person to resolve Git identity and commit before rerunning. After start consent, the existing `cmd_up` path prepares the installed copy and
delegates launch to its proven executable before starting any seat. Successful starts
show status, desk access and a first-order example; only actual readiness/proofs
can establish a running crew. Checks select the ship's own server with
`Tmux.for_ship(ship)`; desk access uses `qd --ship <folder> attach <session>`,
so an existing ship's desk remains reachable even when another ship is default.

Absent titles derive as Office for the person's session and `<ship> <Session>`
for others; explicit titles remain authoritative. **Compatibility:** syncing a
hand-made ship with omitted session titles applies these derived titles and
renames its tmux sessions. Bring that ship down before syncing. Every tracked ship,
including the operating ship, declares its titles explicitly.


The stable JSON answers object uses these keys (flags use hyphens instead of
underscores). Scalar text keys are `operator`, `name`, `office`, `workspace`,
`folder`, `programs` (comma-separated registered names), `desk`, `captain`,
`engineer`, `existing` (`keep|update`), and `use_defaults`, `accept`, `check`,
`start`, `wait_for_sign_in` (`yes|no`). Program-keyed text objects are `payment`,
`subscription_home`, `provider`, `key_env`, `endpoint`, `model`. The `seats`
object is keyed by `desk`, `captain`, `engineering`; each accepts `name`,
`harness`, `model`, `effort`, `model_mode`, `effort_mode`, and ordered `pools`.
A pool choice is its name or an object with `pool`, `harness`, `model`, `effort`
and the two mode keys. `edits` is an ordered list of `{"field": FIELD,
"value": VALUE}` objects; acceptance/check/start are supplied separately.
For example, after choosing an existing project and native sign-in directory:

```sh
qd setup --answers crew-answers.json
```

```json
{"name":"Aurora","workspace":"/path/to/project","programs":"codex",
 "payment":{"codex":"subscription"},"subscription_home":{"codex":"/path/to/native-home"},
 "use_defaults":"yes","accept":"yes","check":"yes","start":"yes"}
```


**Sync all project ships.** `qd sync --all [REF]` coordinates the existing safe-copy installer, one ship at a time. The typed checkout remains the controller; neither QD_SHIP nor a ship's installed copy selects the project operation. `--all --back` refuses before discovery, as rollback remains a single-ship operation. An explicit global `--ship` conflicts with `--all`. The compatible `upgrade` alias shares this parser and handler, including one alias notice in text and unchanged structured results. `--force` delegates to the existing per-ship force option.

The coordinator resolves REF (HEAD by default) to one complete local commit before planning. `install.resolve_target` supplies the pinned repository/commit tuple to each call of the existing `install.upgrade` path through its optional `target` keyword. Immediately before each installer call, `Tmux.for_ship(ship)` selects that ship's server; the controller shares no tmux instance across ships. Archive, installation, proof, activation and restoration remain that same path; installer text never establishes success. No fetch or network request is added.

Planning snapshots direct declarations under this project's ships folder. The tracked template names default, drydock, forum, cascade and shuttle are named once as passed over, without loading their declarations. Ships marked `code = "checkout"` are named once as passed over too. Other names, including practice and hidden ships with declarations, remain in scope. Crew folders sort by basename; the validated persistent default mark's ship is last if present. The shared `ship.default_ship(project)` reader alone interprets and validates an existing mark; only its `mark` source designates the last ship. Without a mark, multiple ships are synced in order without invoking default fallback or choosing one arbitrarily. Planning does not reread the mark or reorder a run.

Each successful installer call is followed by an observation through the existing installed-copy selector. The first installer or observation failure stops the run; earlier ships remain synced and later ships are not tried. Their final copy observations are refreshed rather than assuming initial snapshots remain current during concurrent activity. Proof lines state each ship's disposition and complete observed commit: synced to, failed and still on, not tried and still on, or passed over with its reason. A changed copy is described as observed on its actual commit, and an unreadable/unusable copy as unknown; a reporting or post-activation error never falsely claims restoration. No fleet rollback or persistent fleet registry is introduced. The operation exits2 on failure.

JSON exposes target, ok and ships. Each ship row contains its folder, disposition, initial before commit, observed commit, reason and ordinary installer proof lines; unknown commits are null. Ordinary dirty-source/historical notices are retained before the final per-ship dispositions. An empty set reports no project ships to sync and performs no ship operation. This composes on the reviewed default-ship predecessor and bypasses per-ship default selection for the project operation; synthetic tests prove the coordinator and CLI without syncing a live ship.
