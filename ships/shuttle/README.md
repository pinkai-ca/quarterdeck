# Shuttle

A mission crew of three: the captain receives the task, the engineer builds it,
and the first officer checks and signs the reviewed result. There is no desk or
separate builder. This is a fifth named template beside drydock, default, forum
and cascade. The default onboarding crew remains unchanged; replacing it waits
for the separate Office/fleet design.

Copy this entire folder into a ship folder of its own. Edit `ship.toml` for the
mission workspace and programs actually available. Choose a unique ship name and
crew session title for concurrent shuttles. Put the first officer on another
provider from the engineer where one exists; disclose when one does not. The
example subscriptions and models do not establish availability. There is no
automatic provider binding, sign-in, fleet forwarding or `qd raise` here.

From a checkout, after the live mission has been authorized:

```sh
cp -R ships/shuttle /path/to/mission-shuttle
# Edit /path/to/mission-shuttle/ship.toml for this machine and mission.
qd --ship /path/to/mission-shuttle upgrade COMMIT
qd --ship /path/to/mission-shuttle up
qd --ship /path/to/mission-shuttle post order --to captain --body-file /path/to/mission.txt
qd --ship /path/to/mission-shuttle status
qd --ship /path/to/mission-shuttle inbox operator
```

The installed template is under
`runtime/venv/share/quarterdeck/ships/shuttle`, using the existing explicit
resource declaration. Copy from that folder when using an installed ship's
framework. Installation itself does not launch a mission. These commands are a
plan, not evidence of a live run or an authorization to spend turns.

For repository work the brief names the repository, optional destination branch,
test command (or the documented one), and acceptance criteria. The engineer uses
a mission worktree under runtime. The captain reviews the exact candidate and
the first officer signs it. A named branch lands only by captain-ordered
fast-forward. A moved branch requires rebase, tests, review and signature again.
With no branch named, the signed commit stays on its own branch and the captain
returns one merge command for an operator-chosen target worktree; it never picks
or changes the operator's current branch.

Without a repository, copy `mission-record.md` into the mission's runtime folder
before work. It records before-copies of external files, all commands and exits,
services started/enabled and undo instructions. The captain reviews the record
and live result; the first officer checks completeness and signs that revision.
Nobody tries an undo without the operator's explicit word. User-level scope is
the default; root goes only through the existing security maintenance door with
authority for this mission.

These are cards and orders, not new enforcement code. A signature is an ordinary
post attesting the exact result, not a promise of a cryptographic Git signature.
The framework carries the work and evidence; it cannot guarantee the quality of
an agent's review or completeness of a manually maintained mission record.

## Proving plan

First use the drydock's fake program with all three shuttle roles. Prove the
brief/post/report flow to a named-branch signed fast-forward and a separate
branchless signed commit. A scripted fake proves transport and artifact
association, not model judgment or obedience to prose. Independent protective
and failure checks remain separate from ordinary happy-path plumbing.

For a later live grant, prepare a small disposable repository with its own test
command. Give the captain a sentence change, with a destination branch and
acceptance criteria. Observe the engineer's candidate, captain's review, first
officer's exact-tip signature and confirmed fast-forward. Then give a second
brief without a destination and observe its signed separate commit and returned
merge command. No live native program or repository task runs until authorized.

The second live task, separately granted, is a user timer that writes the size
of `~/dev` to a log hourly. Its mission record lists unit/log paths and their
before-copies, every command/exit, prior and observed service state, and proposed
undo. The captain reviews that record and the running result; the first officer
signs without trying the undo. Leave or remove the timer only as the operator
then directs. No timer, service command, root operation or undo is performed by
copying, installing or loading this template.

The queued runtime default-selection and sync-all changes must explicitly
recognize `shuttle` as a tracked template alongside the other four. That later
composition keeps templates out of real-ship fallback and fleet sync. It is not
implemented by changing another template or adding runtime behavior here.
