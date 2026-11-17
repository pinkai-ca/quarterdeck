# Quarterdeck design: Guided setup

A page of [the design](../DESIGN.md).

## Guided setup

Setup turns explicit program/payment and crew choices into a validated declaration. It previews before writing, asks separately before spending turns and starting programs, and preserves existing ships unless an accepted update names its changes. The [office and ship-raising structure](structures.md#office-and-raising-a-ship) owns init, first-time office creation and reusable raise consent. Once an office exists, `qd setup` runs this crew wizard.

### Proposal and acceptance

The wizard asks for operator title, ship name, office title, workspace, then desk/captain/engineering names, harnesses, models and efforts. Suggestions come from templates/adapter metadata and do not prove account access. One program can supply a crew except Antigravity's one-seat restriction. Payment choices are subscription, key or local, with legacy endpoint accepted. Key answers name environment variables only. Endpoint operator, URL and model are explicit; model brands never identify payment/provider.

New ships are direct valid nonhidden children of the checkout's `ships/`, regardless of workspace or display name. Existing ships may update an existing inside-project path. Outside paths refuse with an inside-project remedy. Setup uses the installer's source-repository resolver, including cwd/parents for an installed entry; without an identifiable checkout it asks to run from one. The parent must exist. It creates only the new ship's declaration, orders, runtime ignore and scoped first commit, never the person's project or an existing directory. Successful new creation marks that ship default; keep/update leaves the mark alone.

Before writing, the full proposal shows operator/office, ship/project path, then Seat/Name/Harness/Model/Effort columns. Runs on describes subscription locations, key-variable names and endpoint URLs. Fallback and retained-seat fresh notes follow the table. Text abbreviates home paths; JSON keeps them complete. New Claude/Codex seats default to fresh_after_compactions=2; unchanged harnesses retain their setting/absence. Leaving a counting harness retains the ineffective key with a note, since the writer cannot remove it. Default orders require a self-addressed handoff before fresh.

Flags, one JSON answers file and ordered edits build the same proposal. File values merge with flags, followed by file/flag edits. Per-seat flags are `--seat ROLE.FIELD=VALUE`, edits `--edit FIELD=VALUE`; every edit needs acceptance again. Model/effort modes follow the [explicit-default contract](harnesses.md#selections-and-explicit-defaults). `/default` is a terminal action, never a literal model alias.

Non-TTY or answers-file invocation never opens a dialogue. Missing fields refuse with exact flag/file remedies. `use_defaults=yes` accepts known suggestions only; accept/check/start still need answers. Unknown homes, credentials, URLs, operators or models are never invented.

### Checks, updates and launch

Acceptance precedes writes and readiness probes. Paid check and crew start are separate consents labeled as spending subscription turns; declining leaves the saved declaration and next command. Version/home/account uncertainty remains visible. Setup never signs in, buys or installs native programs. Account questions belong to the person. Informational sign-in hints are never executed.

Before acceptance, PATH/metadata and credential presence may inform suggestions, but native version/readiness waits. Anonymous model listing is limited to loopback or explicitly selected documented OpenRouter endpoint, without redirects, proxies or authentication. Other remote URLs are not queried; missing/ambiguous models require an explicit choice, not a key request.

Existing keep refuses changed answers. Update goes through set's validation/apply/defer/refuse path, preserving unrelated declarations and orders. Unsupported inline changes, removing named selectors and changing session titles refuse before writing with an explicit-edit remedy. Failed first commits require resolving Git identity and committing before rerun.

After start consent, ordinary up prepares/delegates to the ship's [installed copy](working.md#installed-copies-and-sync) before any seat. Only observed readiness/proofs establish a running crew. Checks use `Tmux.for_ship`; desk access names the ship explicitly so another default cannot redirect it. Derived session-title changes require taking a hand-made ship down before sync; explicit titles remain authoritative.

### Answers format

Scalar text keys are operator, name, office, workspace, folder, programs, desk, captain, engineer, existing (keep/update), use_defaults, accept, check, start and wait_for_sign_in (yes/no for consents). Program-keyed objects are payment, subscription_home, provider, key_env, endpoint and model. The seats object has desk/captain/engineering entries with name, harness, model, effort, model_mode, effort_mode and ordered pools. A pool is its name or an object with pool/harness/model/effort and modes. Edits are ordered field/value objects; consents are separate. Flags use hyphens instead of underscores.

While no office exists, an answers file or flags make the office alone unless they hold an answer that only a crew reads (`setup_answers.CREW_SCALARS`: name, workspace, folder, existing, programs, captain, engineer, check and wait_for_sign_in; or a captain or engineering seat); then the office comes first and the crew follows in the same run. The office's own answers (accept, start, use_defaults, operator, office, desk, the desk's seat and its program's program-keyed answers) never ask for a crew, and the office alone applies them, building its desk as a run with a crew does, so the office it writes is the same either way. A program-keyed answer for a program the desk does not run is refused there, never dropped. With none of them, the office is the template's.

```sh
qd setup --answers crew-answers.json
```

```json
{"name":"Aurora","workspace":"/path/to/project","programs":"codex",
 "payment":{"codex":"subscription"},"subscription_home":{"codex":"/path/to/native-home"},
 "use_defaults":"yes","accept":"yes","check":"yes","start":"yes"}
```

Project-wide sync lives with the [installer](working.md#syncing-the-project), not setup's consent flow.
