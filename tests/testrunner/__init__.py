"""The repository's own test tool: runs the suite of a Git-resolved target in fresh owned scratch, keeps a
fixed result, report and complete per-failure output, and cleans up after a change.

Standard library only, and nothing from ``src/``: the framework never imports it and it never imports the
framework. The entry point is ``python3 -I tests/testrun.py``; everything it creates lives under the
project's ignored ``.testruns/`` folder, the project being the parent of the Git common directory.
"""

FORMAT = 1  # control state, journals, results: a reader refuses a format it does not know
