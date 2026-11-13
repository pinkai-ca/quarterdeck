# Mission record

Copy this form into `runtime/missions/<mission>/record.md` before non-repository work. Keep the filled form, before-copies and evidence in runtime. Do not edit this reference form to store a mission. This is a crew-maintained record, not an automatic completeness guarantee.

## Scope and acceptance

- Mission and captain's order:
- Operator's approved scope, working folder and allowed external effects:
- What done looks like:
- Privileged work, if any: existing maintenance door and explicit authority:
- Open questions, known limits and unknown outcomes:

## Before-copies and external files

Add an entry before each file is written, changed, replaced or deleted. Keep the original copy unchanged; later revisions are separate evidence. Record directory/link/special-file handling explicitly where relevant.

| External path | Prior existence/type, mode and relevant metadata | Original before-copy and hash, or absent | Intended change | Observed result | Proposed undo |
|---|---|---|---|---|---|
| | | | | | |

## Commands

Record every command, including inspection, tests and service commands. Record intent before execution and completion/exit after it. Missing completion is unknown, not success. Avoid credential values in this record and retain only appropriate supporting output.

| Sequence | Working directory | Exact argv or shell command and intent | Started / completed | Exit or unknown | Output/effect evidence | Proposed undo or no side effect |
|---|---|---|---|---|---|---|
| | | | | | | |

## Services

Record each service enabled or started. Include its initial state, not only its final state. This template authorizes no service operation itself.

| User-level service | Prior active/enabled state | Unit/config before-copy | Command entries | Observed active/enabled state | Proposed undo |
|---|---|---|---|---|---|
| | | | | | |

## Review packet

- Record revision and evidence index (publish its final hash in the post, not in this file):
- Engineer's report, result and limitations:
- Undo instructions reviewed; no undo executed:
- Remaining unknowns:

Freeze the completed revision for review. The captain's review, first officer's
exact-revision signature and final mission report are posts naming that revision
and its hash. Do not backfill their references into an already signed record;
further work or corrections need a new revision and review.

An undo requires the operator's explicit word. If one is later ordered, record that authority, its commands, exits and observed effects as a new revision; do not rewrite this signed record or erase what happened.
