# Quarterdeck design: Working on the framework from a ship

A page of [the design](../DESIGN.md).

## 15. Working on the framework from a ship

Build changes in disk-backed worktrees under the ignored `work/` folder, preserving uncommitted work across reboot. A commit identifies the reviewed change; no parallel manifest or backup replaces it. Review, signature and landing roles belong to the ship's orders. The running ship normally uses its installed copy, so landing source and activating it are separate steps. The service alone writes the Ledger; read commands must remain safe beside an older writer.

### Command output

Once a CLI act has returned its outcome, a broken stdout pipe while writing or flushing its final proof does not change the act's exit status or repeat the act. The same applies to a closed stderr diagnostic pipe for an already determined refusal. Before returning from the console entry point, qd flushes both streams and redirects only a broken descriptor to `/dev/null`, avoiding Python's second flush error at interpreter shutdown. Open output files retain their complete output; other I/O errors and failures during an unfinished act keep their existing behavior. This follows [Python's SIGPIPE guidance](https://docs.python.org/3/library/signal.html#note-on-sigpipe) without resetting the process's signal handler.

Receipt-bearing output remains strict: `qd inbox --wait` must serialize, write and flush its whole batch before recording delivery. Its emitter does not opt into the completed-act behavior, and a broken pipe leaves its receipt uncommitted. Rendering errors are also outside the completed proof's pipe handling.

### Running tests

`python3 -I tests/testrun.py` is the repository's standard-library-only test and cleanup tool. Runtime code neither imports nor installs it. Its project is the parent of Git's common directory, shared by every worktree; retained results/control records live in ignored `.testruns/`. Use main's tool with `--target PATH` for a worktree older than the tool.

| Command | Purpose |
| --- | --- |
| `run [--fast\|--whole\|targets] [--target PATH] [-n N] [--timeout S] [--wait S]` | Test the resolved worktree's committed HEAD in an owned detached checkout. Dirty work refuses. |
| `run --ci-layout --whole -n 8` | Use the independent repository/environment layout described below. |
| `run --jail` | Execute target code inside the tool's bubblewrap boundary. |
| `keep RUN --reason TEXT` | Retain one run's scratch with its reason. |
| `retire RUN [--wait S]` | Retry ownership-checked cleanup of one exact crashed run. |
| `clean --change BRANCH [--drop --reason TEXT]` | Remove a registered merged change, or a tip-bound recorded drop. |

Default fast selection uses existing unit/contract directories, in that order. Explicit `--fast` selects the same. A project with neither uses ordinary whole-project discovery and states that fallback. `--whole` passes all `tests`; signing requires it. Named targets cannot combine with lane flags and remain labeled named even when the target is `tests`. Selection, exact targets and workers are retained; old results never imply a signing selection.

Four project locks each reserve eight workers through setup, pytest and finalization. Fast takes two together and defaults to sixteen workers, maximum sixteen. Whole/named runs take one and default to eight, maximum eight. Smaller `-n` retains the lane's full reservation. Partial acquisitions release before waiting; insufficient slots refuse or wait up to `--wait`. `tests.testrunner.layout.all_slots` takes all four for a proof section that cannot share the machine.

Setup has a fixed 600-second limit excluding slot wait. Positive finite `--timeout` bounds pytest alone from its start, default 3600 seconds. Timeout exits 3; ordinary test failure exits 1. Termination uses bounded interrupt/terminate/kill waits on the step's entire recorded session, including child groups after its shell exits. Finalization has a separate sixty-second alarm and a five-second emergency-report opportunity. Finalization timeout exits 7, preserving first results and remaining scratch; its explicit notice overrides a result whose cleanup was pending. Direct pytest needs its own external wall limit.

The tool makes an offline editable install with no index/dependency fetch and verifies source imports. It uses ordinary Python's read-only test tools and a plain path entry for the previously resolved user site, with no user-site hooks executed. Bytecode must already be current there; changes hold cleanup. Run code is precompiled and package folders made read-only. Private HOME/XDG/TMPDIR/build/cache stay in scratch, redirect variables are removed by name, and child interpreters use `PYTHONNOUSERSITE=1`. Nested runs carry the resolved tool base through `TESTRUNS_USERBASE`, never PYTHONUSERBASE.

Both soft/hard per-process `RLIMIT_AS` are lowered to 8 GiB or a tighter inherited bound, before tool-launched pytest or direct collection. Fork/exec/spawn children inherit it. `--memory-mib` or direct `TESTRUNS_MEMORY_MIB` can only lower it. This bounds virtual address space per process, not aggregate resident memory. `OPENBLAS_NUM_THREADS=1` must be set before Python/plugins start; the tool fixes it early, while direct authors supply it. A later conftest observation cannot prove earlier imports saw it. MemoryError reports the effective bound.

### Results, storage and cleanup

Run IDs reserve a fresh mode-0700 result directory with an exclusive create, retrying random-name collisions before any record is written. Standing memory promises use the same reservation, so records for different devices cannot overwrite each other. A run reserves its ID after acquiring its slots and publishes that ID in every held slot.

Every ending retains `results/<run>/{result.json,report.md,failures/NNN.txt}` with complete failing output. Evidence includes target tip/tree, tool hashes, exact command, versions/safe settings, manifests and separate wrapper/pytest/signal/cleanup exits. Counts require every expected terminal item and collection marker from every known worker; partial collection says unknown expected count and retains observed items. Malformed plugin/version evidence becomes named incompleteness rather than losing finalization. Nothing is automatically retried.

Memory scratch is preferred from TMPDIR, /dev/shm, /tmp, only on writable executable tmpfs/ramfs. A project-root flock covers fresh space measurement and saved reservations. Admission requires 4 GiB per run plus an 8 GiB margin and all outstanding same-device project reservations. Kept/crashed runs keep their full reservation until exact retirement. Insufficient capacity produces one disk-fallback line; unknown storage refuses. A mode-0700 `qd-<uid>-<project-hash>` root and saved device/inode/owner token bind removal. A one-second sampler stops a run exceeding 4 GiB or with unreadable measurement, including a conservative per-entry allowance. This sampling is not an exact quota or a lock against unrelated writers.

The report keeps sampled peaks, process I/O and machine-wide device counters. Reboot may remove memory contents, but saved ownership still permits Git administration cleanup. Missing roots in the same boot or changed mounts hold until ownership is provable. Memory reservation locks are separate from slot locks and never authorize changing another run's reservation.

Direct pytest uses owned memory `t` storage, disk fallback `.testruns/t`, retaining failing tmp_path products for at most three sessions. Each test sets TMPDIR to its own tmp_path. Teardown removes only exact synthetic-project roots with owner/mode/link/identity checks; the controller's end census only reports leftovers belonging to this session. Other sessions' projects are left alone. Test-session memory sampling uses the same 4 GiB policy.

Before detached checkout creation, ownership records save its intended `checkout-<run>` path and Git admin entry. Cleanup validates the exact backlink/admin identity; a partial add or mismatched path holds rather than removing another worktree. Result directories have random owner tokens and their own guards. Registration/clean serialize briefly on the changes directory; clean refuses active registered runs, even before scratch creation.

Tool fixtures use `/tmp/qd-<uid>-<project-hash>/r`; direct pytest uses the sibling `s`. These fixed-length, mode-0700 socket roots are derived from the canonical Git common project and checked against its full identity, so linked worktrees share an anchor and checkout length does not consume the socket budget. Results and recovery records remain in `.testruns`; old project-relative socket records remain recoverable. The small shared root persists while its project exists; each run retires only its own random session containers. After run cleanup, the tool sweeps derived roots only when their recorded project is gone and both anchors are empty. `clean-sockets --root PATH` applies the same checks to an explicit root; no recursive removal or process signalling is used. Each direct child container/server birth is recorded before use, including lock/socket/namespace identities, pid/start/boot and random server owner. Cleanup targets only those records, never a global namespace scan. It addresses private servers explicitly with `tmux -S <socket> -N` and verifies their birth identity in-server. Dead endpoint removal needs the same socket and ended process plus refused connection. Z/X/x process states count as exited; unreadable identity does not. Only exact empty owned containers/lock directories are removed. Live, replaced or uncertain endpoints remain held.

`retire` needs an available run lock, free recorded process groups and intact ownership; it signals no arbitrary process. It may close only the proved private server. Its separate `.testruns/retirements/<run>.json` preserves the first result/report/failures byte-for-byte. Owner records survive partial cleanup. Retirement shares a forty-five-second and two-million-check traversal budget; refilling directories retry only inside that budget. Partial removals retain tokens, exact remaining paths and original outcomes. Absent proved-retired paths succeed idempotently; missing authority is never adopted.

`clean` uses unforced Git worktree removal and branch removal after merge/drop verification. It refuses main, unknown/replaced/in-use worktrees and any Git-visible or ignored contents. Results cited by the record stay. Recovery covers only a run's own change after printing its scope; other changes' leftovers are reported and left. Versioned control formats newer than the tool refuse.

`--jail` runs venv/install/import/compile/pytest steps in bubblewrap with a cleared allowlisted environment, no external network, read-only system/test tools/Git, writable owned scratch and checkout admin, and no operator home, agent, bus or live tmux socket. A tool inside its own target refuses. Because project Git is visible, credential helpers, extraheaders and credential-bearing URLs in included config refuse by key/file without values. The invoked tool supplies the jail and is named in evidence; ordinary crew runs are unjailed unless requested.

Shared installation fixtures build per-worker wheels/prefixes once per session, fingerprint them read-only and give upgrade cases writable copies with rebound script/RECORD prefixes. No cache survives a run. Layout, first-install-failure and distribution-build cases retain their own fresh installs.

### CI layout

CI mode checks out exact HEAD/tree with complete reachable local history into an independent repository with a real `.git`, no hooks, remote config, borrowed alternates, hardlinked objects or network fetch. It verifies history count/hash and source origins. Shallow sources refuse. It needs a real memory reservation and never falls back to disk; nested CI tests skip with that named refusal where memory is unavailable.

`--ci-layout` alone changes layout, not selection: only whole/eight-worker invocation records `ci_full_invocation`. The checkout owns `.venv`, PATH and VIRTUAL_ENV; pytest uses `-q -rs -n 8` with default xdist distribution for that whole invocation. HOME contains workflow Git identity. The child filesystem seam `TESTRUNS_CI_DISK=1` models the runner's absence of usable memory while actual controller scratch remains admitted memory. Child runs use their normal checkout-local disk fallback, physically within that scratch.

Run/session-registry variables are absent from setup/pytest/children and no basetemp is forced. A separately copied/hash-recorded evidence plugin observes cwd, TMPDIR/tempdir, source origins, disk policy and owned private-server births; ordinary conftest cannot use that context. Nested tools strip it. Missing observations make CI evidence incomplete. Independent-repository retirement retains its recorded identity requirements.

This mode does not reproduce hosted OS/native versions, provisioning, Actions services, the Python matrix, whole-job limit or separate drill. Native sockets retain the common owned short anchor rather than RUNNER_TEMP. Its disk observation is a seam, not a physical no-room namespace. A local CI-layout result is not GitHub execution.

### Installed copies and sync

Without `ship.code`, a ship owns `runtime/venv`. The only explicit alternative is `code = "checkout"`; invalid values refuse pure load. A never-installed ship can pass doctor as no copy yet when its source repository is identifiable; up installs from local HEAD immediately before launch, then delegates once to the proven executable. Failure launches no seats and never falls back to checkout. Existing copies retain their commits; first installation/recovery uses a fresh process even at the same interpreter path.

`runtime/bin` contains only qd and synthetic qd-fakeharness, first on seat PATH, without the framework interpreter/pip. Briefs name its shell-quoted absolute qd for every command/watch. A Bash function in launch environment also forwards bare qd there, though startup redefinition or non-Bash shells can bypass it; the absolute command remains authoritative. [Seats](seats.md) describes launch and watch identity.

`qd sync [ref]` archives one resolved local commit, allowing dirty source with a commit-not-working-files notice. `qd upgrade` is its compatibility alias through the same handler/API, appending its rename notice once. No fetch occurs. Checkout-following ships refuse: `this ship follows the checkout; it has no copy to sync; remove code = "checkout" to give it one`. `copy_state.py` selects only a usable recorded live copy for handoff; missing, corrupt, pending or failed copies keep caller qd available with a warning and repair/back paths. Reads never repair or recover state.

Sync journals before mutation, stops the service and captures brief/launch baselines. It copies live to absent `venv.backup` before changing live; the complete backup is staged before activation. Canonical executable/proofs always run at `runtime/venv`, never the backup's stale shebang path. `venv.prev` changes only after successful activation, when backup becomes previous; peak storage is three copies. Failure restores complete backup and restarts the old service if it was running. A first failed installation removes incomplete code and starts no seats. Storage/stop/restore/cleanup failures remain visible rather than claiming success.

`installed.json` stores current/previous facts, bounded pending journal and fixed failure plus historical projections. Each rename is journaled before it; no file or directory sync is forced. Identities distinguish even same-commit forced copies. Up/sync recover under the install lock before any seat. Bootstrap recovers before Service/Ledger/service-lock construction and execs restored code if modules may have changed. Reads do not recover. The live parent retains the lock through startup; its pid-bound token permits only the ready/restored child bootstrap. The child acknowledges loaded copy/source/process/token after taking its own service lock, within thirty seconds. Unknown acknowledgement is no startup success. Historical COPY_SAFETY behavior is warned about explicitly.

`--back` needs no Git/archive/pip/build: three journaled renames swap live and previous through `venv.swap`, followed by canonical prefix/package/resource, version and laws proof. Failure performs recoverable inverse renames before cleanup. Back can swap again after success. Unforced stable same-commit sync is a true no-op. Proof sanitizes inherited source/ship redirects, checks version exit and records command/disposition kinds rather than native output; it cannot promise detection of later arbitrary tampering.

Target `pyproject.toml` per-file data-files declarations are the sole packaging authority for laws/cards/templates. Sync builds a replacement share tree from the archived declaration after pip, including rollback. Legacy targets may use one bounded, non-executed literal-copy AST interpretation. Unsupported legacy shapes use the named baseline fallback of standing-orders/drydock if present, warning that extra templates may remain. A target without standing orders gets help proof and no usable-brief promise. No diagnostic command synchronizes resources. Historical recovery that lacks the backup role can hold it, so unresolved installs require matching code.

### Rebrief and receipt refresh

After installation, one request in `runtime/requests` lets only that running revision re-render briefs. Content hashes and complete launch fingerprints captured before stop determine notices: unchanged content spends no turn; missing baselines hold; down/replaced launches read current briefs at ordinary launch. Each seat's planned text/hash and immutable notice intent precede publication. Only matching stored posts advance the offered baseline. Retries reuse identity; changed text chains a new notice, including return to earlier text. Pending upgrade requests own card publication until completion. Offered is not read, stored is not delivered. Ordinary restart creates no request; a stopped service waits for up.

The installed service also revisits unconfirmed closed-launch submissions once per revision through saved record/reader and the exact matcher. Only accepted/consumed original input proves delivery; unreadable, absent, queued or ambiguous evidence changes nothing. Live launches use ordinary reconciliation. Never-pasted reservations follow cancellation, not receipt refresh. Revision-scoped `post/receipt-refresh.json` stores opaque observed keys/progress, never payloads or receipt verdicts. One invocation buffers and publishes at most one checkpoint; failure cannot certify completion.

Positive refresh saves an intent in the original journal bound to immutable submission and pre-transition attempt snapshots, under admission then attempts lock. Attempts settle delivered before the journal; a cut resumes that intent without another match or resend. Competing settled outcomes remain untouched; superseded intents release their exclusion. Later carried launch attempts stay byte-identical. Failures log once per unchanged reason and cannot block brief publication. Changed matcher revisions revisit negative evidence; terminal delivered/superseded journals keep their normal path.

### Syncing the project

`qd sync --all [REF]` stays in the typed checkout and resolves one local target before snapshotting direct ship declarations. It passes each through the ordinary installer with its own `Tmux.for_ship`. Tracked templates and checkout-mode ships are named once as passed over. Other crew folders sort by basename; an explicitly marked default goes last, using the shared mark reader once. No mark means ordinary ordering, not arbitrary default selection.

First installer/observation failure stops; earlier successes remain and later ships are untried. Final refreshed copy observations establish dispositions and full commits, including changed/unknown copies; a post-activation/reporting error never implies restoration. JSON retains target, ok and ship rows with before/observed commits, reasons and proof lines. Failure exits 2. `--all` conflicts with explicit `--ship` and `--back`; force/alias retain single-ship behavior. There is no fleet rollback or persistent registry.

### A change from outside

A pull request from outside the crew is screened by a program before any seat reads a word of it (`pr_screen`, run as `python -m quarterdeck.pr_screen --repository OWNER/NAME` on Operations' daily timer).

1. It lists the open pull requests by number and head commit.
2. For each new number and head, it fetches the outsider's title, description, comments, commit messages, file names and changed files into `runtime/pr-screen/`, which no seat is shown. Each text is written under a number the program makes up (`f001`, ...), never under the outsider's path, and the real names are kept in a separate list.
   - Lines and files are cut to bounds.
   - A binary file, a change over the total bound, or a file name over the length bound flags the request, and that text goes to no hand.
   - Nothing is fetched into a repository: the change is read as the API's patches, so no hook, submodule or large file can run.
3. It searches every line with the one list of patterns (`pr_screen_patterns`), and with one more built at run time from the ship file's own seat names, for a text that asks to send or post to one of them. Each line is searched as written, and again with underscores, dashes, dots and slashes read as spaces.
4. It copies each piece, with a planted file beside it, into the folders of the two hands the ship file names in `[pr_screen]`, one on each of two models. The planted file holds a harmless instruction under a number like the outsider's, so a hand cannot tell it apart.
5. It orders each hand with one fixed order from ship, and judges the answer by its shape alone: `CLEAN`, or `FLAGGED` with `file:line` places. The order is `answer.txt` in the piece's folder.
   - An answer in no shape, none in time, or one that does not flag the planted file counts as FLAGGED.
   - While a hand screens, the service files nothing it posts (`pr_screen.held`, read by the drop filing), and the status line says so.
   - Afterwards the hand is taken down while the hold still stands. Its drops are swept: only a report of exactly `done` on its own order is filed, and anything else it wrote is moved aside under `runtime/pr-screen/held/`, never filed.
   - The hand stays down until the next screen starts it fresh.
6. Clean from both hands and nothing from the search: the reader `[pr_screen]` names (the First Officer, on a crew) is ordered to read the change and report to the desk. Flagged: no seat reads it, and the desk is told the number, the head and what flagged it, in the screen's own terms (the pattern's name or the hand, the numbered file's kind, the lines). Nothing the outsider wrote is quoted.
7. A new push starts it over.

**The residual.** A pull request written to fool both local models and to miss the search can still pass, and then the First Officer reads it with the key in reach. That is why the hold on a screening hand's posts and the planted check matter: the first keeps a fooled hand from carrying the words to a seat, and the second keeps a hand that did no work from passing anything. The planted cases under `tests/fixtures/pr_screen/` include one written to slip past the search; before a naive reader it passes, which the fixture tests show.
