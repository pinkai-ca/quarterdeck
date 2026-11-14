# Grok Build contract

The adapter targets native Grok Build 1.0.77, provider `xai`, harness `grok`.
Version facts come from isolated native measurements. Repository fixtures are newly
invented native-format examples, not copies of sessions, captures or usage.
Fixture checks and installed `qd check grok` establish different things. No
installed version is marked proven by adding fixtures.

| Contract | Implementation and evidence | Remaining limits |
| --- | --- | --- |
| Version context | Normal selected executable, never a PATH redirect. One selection: the first `grok` on the PATH a seat launches with (the folder holding `qd` first). The seat's launch names it by that absolute path, so `launch.json` records it; doctor shows it beside the Grok pool with the file a link there reaches and its version; `qd check` names it, proves that its seat ran it, and writes it into the proven mark. On a machine with two Grok programs on the PATH, the first one is the one every seat runs. Existing eligible runtime supplies a fresh empty version-call home; both HOME variables are replaced. No-context lookup reads only the selected resolved basename and executes nothing. | No operator version cache is adopted. Missing runtime remains missing; installed version proof still belongs to the check. |
| Home and launch | Private `GROK_HOME`, nested private `HOME`/XDG, native configuration, brief through `--rules`, updater off and normal credential linkage. Native `--trust` grants the whole repository root, or cwd outside a repository. The measured native store schema has one `folders."<absolute root>"` table with `trusted=true` and `decided_at` Unix seconds. The exact-grant guard never rewrites it; project contents never refuse a workspace. | Native validation established that project instructions/rules/skills load, project hooks run and project MCP servers and project LSP processes start. Home `managed_config.toml` is deleted at every native start and supplies no enforcement; it is neither written nor accepted. Project hook/MCP imports are accepted as for a person trusting the folder; the earlier source-absent launch does not prove this contract. Public marketplace fetching and some native bundled capabilities remain, as listed in the card. No blanket background-work absence claim. |
| Workspace notice | Doctor adds a passing information row per declared seat/pool. Grok launch carries the same names/source-only inventory into the optional launch record notice and its proof line. Read-only snapshots name direct hook events, MCP declarations and direct/plugin LSP source paths, with escaped paths. | LSP serialization is unestablished: readable standard files are declared by path with unknown shape, never parsed or assigned guessed server names. Unknown scope, unreadable/unrecognized sources, unresolved manifest layout/activation/path overrides and indirect plugin/agent imports remain explicitly unknown; inventory does not prove native activation. It never refuses, prompts or changes the jail. No command/URL/header/environment/token values are rendered. |
| Records and unknowns | Native ACP envelopes through the shared JSON-lines snapshot/cache. Ordinary input chunks, completed/cancelled turns, tool invocation/update identity, malformed/foreign/empty/unknown evidence and incomplete physical-line diagnostics have synthetic cases. | An unknown row cannot establish tool-set clearance. Missing or malformed information stays unknown. |
| Join | Adapter-owned UUIDv7 folder creation time, validated against every record row. Fractional launch boundaries and conflicting/non-v7 identities have synthetic cases. Normal first-record join and same-record resume were observed natively. | Shared join selection and ambiguity handling are unchanged. No whole-second row fallback, flooring or explicit `-s` launch option. |
| Exact input receipts | Native whole input text with one-second row timestamps. Declared resolution affects only local receipt comparison. Fresh exact receipts were observed; ordinary same/prior-second and Claude/Codex regressions remain separate from join. | A queue card, fold count, screen echo or disappeared box never proves delivery. No resend of an uncertain input. |
| Composer | Measured fullscreen prompt, rounded boundaries, model/effort footer, SGR and monochrome hints, the 256-column working-draft hint, 70-cell text wrap at 80 columns, counted multiline fold and retained blank rows. Synthetic boundaries cover LF and wrap thresholds. | Whitespace-only rendering collisions, clipped viewports, other control/line-ending layouts and unknown styles refuse exact visual comparison. No automatic folded-paste clearing. |
| Dialogs and controls | Commands/model picker, their measured dismissal keys and model/effort changes confirmed from resulting footer state. The first-run training-data question (`Help improve Grok`), measured above the composer in M1 to M3, is answered `[Opt out]` by one SGR click found by its text, once per launch, recorded in `launch.json` and named in `qd watch` and `qd check`; altered forms, transcript quotes, missing SGR mouse reporting, a changed frame and a screen that does not clear are held with nothing more pressed. The 256-colour palette (indexes 16 to 255) is decoded as measured. | Workspace trust is supplied at launch; unexpected dialogs still refuse; arbitrary model, theme and window layouts are unproved. The question is measured at 112 x 35 only. After a turn (M4: the carried first argument runs at once under the question) the cleared composer shows the measured idle hint; a fresh home with no turn shows no hint row after the press (M1 to M3: the empty one-row composer, the footer, and nothing at all under it). The reader keeps that form unknown; the door reads it idle only in a launch whose `launch.json` shows the training question answered and cleared, on a version whose fresh form is measured (1.0.77 in M1 to M3; 1.0.70 in M5, the same form under a different banner). Any other content under the footer, a draft in the box, another version, or an answer that did not clear stays an unmeasured hint for readiness. A press during a running turn is unmeasured and never made: the working hint under the question (M4's first frame) reads working, and a hint row that is neither measured tuple, nor the fresh form's empty rows, leaves the question unpressable. |
| Credentials and expiry | Normal pool/seat/usage-reader reconciliation. Expiry and freshness require one readable native refresh-grant metadata object; missing/ambiguous metadata stays unknown. The grant kind is `self-renewing`: `expires_at` is the short access expiry the program renews on use, shown in doctor's detail and never counted as a login needed. Synthetic metadata cases use no real credentials. | File freshness does not establish subscription tier. No custom authenticated service request. No field naming the end of the renewing grant, or a failed renewal, is known by name, so neither is read and neither warns. |
| Usage, quota and walls | Native ACP initialization/billing only, with a 20-second process-group deadline and reap. Weekly percentage/reset and live tier measured. Missing percentage remains unknown. Native rate-limit retry rows have a synthetic decoder case. Framework quota/cache/failover/doctor paths are reused. | No five-hour window. A real allowance exhaustion/failover and a screen-only wall were not induced; the adapter does not guess an unmeasured screen-wall shape. |
| Lifecycle and queue | Enter submitted a single follow-up during an owned working turn. One visible queue slot preceded exact record consumption after root completion. Both exact receipts resolved before controls. | Visible queue cards have no independent record acceptance event. Immediate send-now is unsupported/unproved. Default empty queue fields never establish clearance. |
| Cancel and resume | After positive current-turn/owner/empty-composer/queue-clearance evidence and resolved receipts, normal `C-c` produced native cancelled completion. Normal restart resumed the same owned record without a carried input. | Cancel is held while any receipt is unresolved. A resume carrying open work can submit new input; the measured resume did not. |
| Accounting | Native completion usage, one reported model and call identities. Begun calls count once even without a terminal result; unknown duration/outcome use existing `ToolCall` null fields. Synthetic ledger ingestion is idempotent and preserves those unknowns. | Nonzero cache-creation semantics, mixed-model turn attribution, individual model-call steps, subagent counts and `costUsdTicks` normalization remain unknown. Row-based duration has one-second precision. The reader never treats native `numTurns` as a child count. |
| Installed acceptance | Normal can-post permissions are configured; the installed check proves launch, long-paste receipt, file task and one `qd post` in a declared workspace containing both `AGENTS.md` and `CLAUDE.md`. | Installed `qd check grok` passed twice on 11 November 2026 (18:55 local on Grok Build 1.0.77 with grok-4.7, a 3,565-character paste confirmed by the record; 21:21 local after the trust change, in a working folder holding an AGENTS.md and a CLAUDE.md, a 3,683-character paste), on check ships of their own. Final affected checks and an exact-tip full suite remain separate acceptance obligations. A check mark belongs to the ship that actually ran the check. |

Compared with Claude Code and Codex, Grok loads trusted project instructions/rules/skills that
Claude Code excludes with `--setting-sources user`. Project hooks run and project MCP servers and LSP processes start under
trust. These imports use the maker's trusted-folder behavior; operator HOME remains private. Grok has no proved immediate-send action or remote-control support, no established
subagent/compaction accounting and no measured screen-only quota wall. It does
not offer arbitrary exact-content proof for clipped or whitespace-only drafts.

## Shared interface inventory

- `JsonLinesReader.identifies(entry)` defaults to the existing `type in kinds`
  rule; Grok recognizes native method/envelope markers. Snapshot, concurrent-read
  refusal, cache, physical line diagnostics and source bytes stay shared.
- `Harness.styled_screen` defaults false. Opt-in `Tmux.capture(styles=False)`
  and atomic `Tmux.read_pane(styles=False)` preserve plain adapter calls. Seat
  identity and styled capture remain in the same server queue; working/control
  evidence uses the same opted-in pane read.
- `Harness.usage_in_home(pool, home)` defaults to unchanged `usage(pool)`.
  `private_usage_home` opts Grok into the one framework usage orchestrator for
  background reads, live refresh and destination allowance checks. Declared
  runtime usage-reader homes participate in the existing credential reseed
  algorithm before and after billing, including failures.
- Read-only `Harness.preflight(home=None, cwd=None)` defaults to no operation.
  Grok build, launch/resume and billing use it; doctor checks global native pool
  readiness and every seat's actual resolved pool/harness choice. No native Grok
  call or mutation belongs in preflight. A bounded local Git
  metadata query identifies only the exact native trust root when a grant exists;
  it never inventories project sources. Workspace contents never refuse readiness.
- `Harness.workspace_notice(home, cwd) -> str | None` defaults to `None`, without
  inventory, home creation or native/provider calls. Doctor adds a passing notice
  row for each actual seat/pool when text exists; preflight keeps its separate
  readiness contract. Grok launch fills the appended `Launch.notice`; Seat copies
  it by keyword to appended `Launched.notice` and prints the persisted snapshot in
  its launch proof. Both default to `None`, omitted on write; old records and
  current positional callers remain compatible. Older revisions without this
  field cannot read a launch record containing it; no record stripping or
  speculative format migration is performed. Claude validation owns shared
  default, old-record, persistence/proof and actual doctor/Seat callers.
- `Harness.version(home: Path | None = None)` adds optional empty runtime context.
  `Harness.private_version_home` defaults false; Grok explicitly sets true.
  Private dispatch at doctor, Seat launch and installed check preserves one old
  no-argument call and no fresh home for every adapter without that flag, even
  when its method accepts a home parameter. Only the explicit flag gets an empty
  temporary directory in an existing owned real fenced runtime. Missing context
  creates nothing; an executed callback is never retried. Grok sets both HOME
  variables after `env -i`, supplying only caller PATH and controlled native ENV;
  no-context basename lookup executes
  nothing and uses Seat's same selected program, without PATH rewriting.
- `Harness.record_time_resolution` defaults to zero. Grok declares one second;
  the canonical receipt matcher floors only local `since`, preserving exact
  payload, original attempt time, owned record and one transport attempt.
  `Harness.join` is unchanged; UUID time belongs entirely to Grok's adapter.

## Affected verification

The ordinary source targets are `test_grok.py`, `test_grok_home.py`,
`test_grok_workspace.py`, `test_grok_screen.py`, `test_grok_receipt_time.py`,
`test_grok_record.py`, `test_grok_credentials.py`, `test_grok_join_time.py`,
`test_grok_version.py` and `test_grok_training_question.py`
under `tests/contract/`, with the shared grant-kind cases in `tests/unit/test_grant_kind.py`. These cover native-format decoders and synthetic
configuration (including native trust schema and launch output), receipt
precision, join-time boundaries and ledger ingestion;
they make no provider call. Version cases use synthetic selected files and
subprocess stubs, including normal doctor/check context propagation and missing
runtime behavior. Independent validation owns normal Seat launch propagation and
its lifecycle interactions; `test_grok_shared_paths.py` carries that, with the
shared receipt proof, styled pane read, usage-reader reseed and usage
orchestration call sites, all synthetic.

Shared receipt callers are `pending.Proof.confirmed`,
`delivery_record.receipt`/`receipt_anywhere`, `Door.confirm`, drill receipt checks
and `Seat._queued_unread`. Other affected consumers are service/watch record
reads, ledger ingestion, atomic styled Seat reads, doctor readiness, credential
reseed and quota/failover usage orchestration. Their affected/full verification
must be selected separately. Framework protection validation remains with its
independent validation owner, with verdict and private evidence path only.

## Workspace trust, as the README stated it

This paragraph stood in the README until 12 November 2026. The contract table above holds each of its facts with its evidence and limits.

Grok Build uses provider `xai` and harness `grok`. It grants native trust to the whole repository containing the seat's workspace, or the workspace itself outside a repository. Project instructions, rules and skills load; project hooks run, project MCP servers start and project LSP processes start. The ship's brief takes precedence over project instructions. These project imports follow the maker's trusted-folder behavior; the operator's home files stay out of the seat. Doctor and launch report declared hook/MCP/LSP source paths and established names, with incomplete inventory shown as unknown. LSP formats are unestablished, so their notices report source paths with unknown shape. Its [contract inventory](#grok-build-contract) distinguishes measured 1.0.77 behavior from remaining limits and installed acceptance.
