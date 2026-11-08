# Working on Quarterdeck

For whoever works on the framework itself, person or agent. The design is `docs/DESIGN.md`; read its sections 2, 11, 13 and 16 before changing anything.

## Shape

- `src/quarterdeck/` is the framework: standard library only, Python 3.12 or later. One module per part, named in `docs/DESIGN.md` section 13, plus the pieces that grew beside them: `servicectl.py` (the service's heartbeat, lock, start, stop and requests), `credential.py` (the one link a seat shares with its pool, reseeded and relinked on the tick), `machine.py` (a seat's process tree sampled for the Ledger), `check.py` (`qd check`, the live proof of an installed program), `doctor.py` (`qd doctor`, the installation checked and handed to a seat to fix), `install.py` (`qd upgrade`, the framework at one commit installed as the ship's own copy, which every `qd` then hands off to), `fakerecord.py` (the fake harness's records in the real programs' shapes), `pending.py`, `composer.py`, `delivery_queue.py` and `delivery_record.py` (the door's evidence of one paste and the transport around it), `provenance.py` and `record_owner.py` (which launch and which program a record belongs to), `quota.py` (quota walls and the usage refresh), `endpoint.py` (endpoint pools: what a server serves, the model a seat takes from it, the key beside it), `sandbox.py` (the bubblewrap jail around a sandboxed seat's program). Under `harness/`: `limits.py` (usage windows and walls), `json_record.py` (JSON-lines records), `sqlite_snapshot.py` (a SQLite record read from a snapshot of its bytes and its log, shared by the Antigravity and OpenCode readers), `opencode_record.py` (the OpenCode database read session by session: the latest root session is the seat's, a task's child sessions are accounted on their own), `antigravity_wire.py` and `antigravity_quota.py` (the Antigravity record's wire format and its quota facts). `tmux.py` is the only module that runs tmux.
- Keep files small by splitting them by concept. About 400 lines is a good size to aim for; it is guidance, not a rule, and code is never compressed to fit a number.
- `standing-orders/` holds the laws and the generic cards; each ship carries the orders and cards for its own crew and may replace any generic card. Two ships are tracked here: `ships/drydock/`, three fake seats for the tests and the drill, and `ships/default/`, the smallest crew on real programs. Every other ship under `ships/` is its own repository, ignored by this one, and its `runtime/` is a nested repository with no commits.
- `tests/unit` is pure logic, `tests/integration` runs a private tmux server and the fake harness, `tests/contract` runs each adapter against recorded fixtures. Nothing in the suite spends real tokens: `qd check` and `qd drill --ship` do that, when asked.
- Work on the framework happens in worktrees under `work/`, inside the project folder and ignored; nothing is edited in the main checkout in place. A ship runs its own installed copy once `qd upgrade` has made one, and until then the main checkout, so main can move without the ship moving.

## Rules

- A law (`standing-orders/laws.md`) is a guarantee the code enforces. Tag the line that enforces it `# law: <id>` and the test that tries to break it the same way; `qd laws` lists both and a test fails when a law has no carrier or a tag names no law.
- Nothing from a runtime enters this repository: no seat ids, records, credentials or usage. Fixtures are scrubbed before they are saved.
- Every `qd` verb prints a proof line saying what was done and how it was confirmed; every refusal names the reason and what would lift it. A reading that could not see something says so, and a figure built on turns that could not be measured carries a trailing `+`.
- A reading never writes: `qd status`, `qd watch` and `qd ledger` open the Ledger read-only, and the service is the only process that writes it.
- A seat runs its program without a sandbox and without approval prompts; a ship that wants a seat sandboxed asks for it in the ship file with `sandbox = true`, and a seat on an endpoint pool (a model served at a URL, trusted less) is sandboxed unless the ship says `sandbox = false`. A program is found on the PATH or named in the ship file, and its version is proven live by `qd check`, never pinned by path or by hash.
- A commit message says what changed, in plain words, and carries no attribution trailer: several models work on this code by default, and the history records what changed, not who typed.

## Running

```
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e .
.venv/bin/python -m pytest -q            # one suite at a time, -n 8 at most
.venv/bin/qd --help
```
