# Working on Quarterdeck

For whoever works on the framework itself, person or agent. The design is `docs/DESIGN.md`; read its sections 2, 11 and 13 before changing anything, and the build log `docs/BUILD.md` for how it was built.

## Shape

- `src/quarterdeck/` is the framework: standard library only, Python 3.12 or later, no file over 400 lines (a test enforces it). One module per part, named in `docs/DESIGN.md` section 13. `tmux.py` is the only module that runs tmux.
- `standing-orders/` holds the laws and the generic cards every seat reads. `ships/example/` is the one ship tracked here; every other ship under `ships/` is its own repository, ignored by this one, and its `runtime/` is a nested repository with no commits.
- `tests/unit` is pure logic, `tests/integration` runs a private tmux server and the fake harness, `tests/contract` runs each adapter against recorded fixtures, `tests/live` spends real tokens and runs only with `-m live`.

## Rules

- A law (`standing-orders/laws.md`) is a guarantee the code enforces. Tag the line that enforces it `# law: <id>` and the test that tries to break it the same way; `qd laws` lists both and a test fails when a law has no carrier or a tag names no law.
- Nothing from a runtime enters this repository: no seat ids, records, credentials or usage. Fixtures are scrubbed before they are saved.
- Every `qd` verb prints a proof line saying what was done and how it was confirmed; every refusal names the reason and what would lift it. A reading that could not see something says so.

## Running

```
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -e .
.venv/bin/python -m pytest -q            # one suite at a time, -n 8 at most
.venv/bin/qd --help
```
