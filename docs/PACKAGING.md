# Packaging

For maintainers and packagers: what travels with the code, and how `qd sync` treats a commit older than the present packaging. To keep a ship up to date you need only [Updating your ship](SYNC.md).

## What is installed beside the code

- The laws, the role cards and harness cards, and the drydock, forum and cascade templates: exactly the files listed in `pyproject.toml`.
- In a ship's copy of the code these files are in `share/quarterdeck`, under the ship's `runtime/venv`. Each sync replaces that folder with the files the commit lists. So a sync brings templates the old copy never had, and going back removes files the earlier commit did not have.
- `qd sync` is the one command that writes these files, and `qd sync --force` writes them again at the same commit. A command that only reads never repairs them.

## A commit from before the list

- A commit from before the list in `pyproject.toml` existed has a step that copies files instead. Sync reads that step, in the one form sync understands, and runs none of that commit's code.
- If the step is in a form sync does not understand, sync copies only the commit's standing orders, and the drydock if the commit has one. Sync then checks the installed laws and says in one line that it fell back. Other templates may be left as they were.
- A commit so old that it has no standing orders is checked with `--help` alone. Sync warns that the commit's laws and cards are missing, and that agents' briefs may not work.

## Installing as a package

- The same list serves an ordinary install from a wheel or a source distribution.
- An installed package leaves out the repository's tests, the test runner, the design documentation, and development ships such as `ships/default`. The package holds only the templates on the list.
- A fresh install is accepted on two checks: `qd laws` run on the installed copy, and the drill, which is the all-hands exercise on stand-in programs.
