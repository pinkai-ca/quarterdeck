# Contributing to Quarterdeck

For whoever changes Quarterdeck itself, person or agent. An agent that arrives to set Quarterdeck up or to use it reads [AGENTS.md](AGENTS.md) instead. [The parts of Quarterdeck](docs/PARTS.md) defines the words used here.

## Read first

- [Design decisions](docs/DECISIONS.md)
- [The laws](docs/LAWS.md)
- [The design](docs/DESIGN.md), sections [2](docs/DESIGN.md#2-the-parts), [11](docs/DESIGN.md#11-the-laws-second-draft), [13](docs/DESIGN.md#13-repository-layout) and [15](docs/DESIGN.md#15-working-on-the-framework-from-a-ship)

## Set up

Work in a worktree under `work/`, which the repository ignores, never in the main checkout.

```sh
git worktree add work/<branch> -b <branch>  # in the main checkout
```

Only to run `qd` from the worktree:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
```

## Run the tests

`tests/testrun.py` runs the suite for a change's signature. The tool tests the worktree's committed HEAD, one tool run at a time. The tool caps every test process at 8 GiB of address space and stops a run at a timeout, 3600 seconds by default.

```sh
python3 -I tests/testrun.py run                       # the whole suite, 8 workers
python3 -I tests/testrun.py run tests/unit/test_x.py  # named targets
python3 -I tests/testrun.py retire <run-id>           # clear up after one run that crashed
python3 -I tests/testrun.py clean --change <branch>   # after the branch is merged, or --drop --reason TEXT
```

- A run's working files live in memory: the run's checkout, its virtual environment and pytest's temporary folders. They sit in a folder the project owns, under the first of `TMPDIR`, `/dev/shm` and `/tmp` that is in memory with 4 GiB free, and the tool removes them when the run ends.
- The run's result stays on the drive, under `.testruns/results/<run-id>/`: `result.json`, `report.md` and one file of output for each failing test.
- On a machine with no such folder in memory, the tool keeps the working files on the drive under `.testruns/`, and prints `testrun: working files on disk: <reason>`.
- `retire` takes the exact id of one run, as in `.testruns/results/<run-id>/`. It sends no signal to any process, and holds while that run's processes are alive. The run's result, report and failure output stay as they were.

A worktree older than the tool is run from the main checkout:

```sh
python3 -I tests/testrun.py run --target work/<branch>
```

A change from outside the crew is run jailed, by main's copy of the tool, never by the change's own:

```sh
python3 -I tests/testrun.py run --jail --target work/<branch>
```

The jail is bubblewrap with no network, a cleared environment and only the run's own files, `/usr` and a short list of `/etc` inside. The result and the report say that the run was jailed, and by which commit of the tool.

While you build, run pytest directly, under the same bounds:

```sh
env -u TMUX -u TMUX_PANE -u QD_TMUX_SOCKET OPENBLAS_NUM_THREADS=1 TMPDIR=/tmp timeout --kill-after=15s 1200s python3 -m pytest --basetemp=/tmp/<new-folder> -n 2
```

- `/tmp` must be in memory: `stat -f -c %T /tmp` prints `tmpfs`. Where `/tmp` is on a drive, name a folder that is in memory, for `TMPDIR` and for `--basetemp`.
- Give each run a new `--basetemp` folder, and remove that one folder by its exact path when the run has ended.
- The suite makes private tmux servers of its own and never touches yours. The three `-u` keep a shell that is inside tmux, or inside a ship, from pointing the suite at a live server.

## Rules

- A law (`standing-orders/laws.md`) is a guarantee the code enforces. Tag the line that enforces the law, and the test that tries to break the law, `# law: <id>`. `qd laws` lists both. A test fails on a law with no carrier or a tag naming no law.
- Nothing from a runtime enters this repository: no seat ids, records, credentials or usage. Fixtures are scrubbed before saving.
- Every `qd` verb prints a proof line: what was done and how it was confirmed. A refusal names the reason and what would lift the refusal.
- A reading says what it could not see, and a figure built on unmeasured turns carries a trailing `+`.
- A reading never writes: `qd status`, `qd watch` and `qd ledger` open the Ledger read-only, and the service alone writes it.
- A seat runs its program without a sandbox or approval prompts, unless the ship file says `sandbox = true`. A seat on an endpoint pool, trusted less, is sandboxed unless the ship says `sandbox = false`.
- The ship file names a seat's harness; the harness's program is found on the PATH, and `qd check` proves the installed version live. Nothing pins a program by path or by hash.
- Native provider facts belong in each adapter, and pool declarations use company identifiers. A native record's provider and model identifiers stay as source evidence.
- A commit message says what changed, in plain words, with no attribution trailer: several models work on this code by default, and the history records what changed, not who typed.

## Layout

- `src/quarterdeck/`: the framework, standard library only, Python 3.12 or later. One module per part, plus the pieces that grew beside them: [docs/MODULES.md](docs/MODULES.md).
- `standing-orders/`: the laws and the generic cards. A ship may replace any generic card.
- `ships/`: four tracked ships. Every other ship is its own repository.
- `tests/`: `unit` is pure logic, `integration` runs a private tmux server and the fake harness, and `contract` runs each adapter against recorded fixtures. Nothing in the suite spends tokens.

## How a change lands

Quarterdeck is written by its own crew of agents, on a ship that runs Quarterdeck itself, and one person, Tyler Dwyer, directs it. The captain and the first officer are two of that crew's agents. A change lands in five steps:

1. One agent builds the change on a branch of its own, in a worktree under `work/`, with the tests that prove the change.
2. The captain reviews the change once.
3. The first officer runs the whole suite at the branch's last commit, and signs that exact commit.
4. On the captain's order, the first officer lands the signed commit on `main` and pushes `main` to GitHub.
5. GitHub runs the tests again, as a second witness.

A change from outside the crew comes as a pull request on GitHub. The pull request's branch takes the place of step 1, and steps 2 to 5 follow.
