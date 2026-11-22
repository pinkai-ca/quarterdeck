"""Synthetic terminal task, installed only in the nightly regression's fixture."""

import re
import shlex

from . import simulator


class CheckProgram(simulator.Program):
    @staticmethod
    def commands(text):
        order = re.search(r'^qd > order from ship · (\S+)', text, re.M)
        task = re.search(r'^Write the file (.+) containing exactly one line: (\S+)$', text, re.M)
        if order is None or task is None:
            return []
        path, token = task.groups()
        return [
            f"printf '%s\\n' {shlex.quote(token)} > {shlex.quote(path)}",
            f"qd post report --re {shlex.quote(order[1])} written",
        ]


def main():
    simulator.Program = CheckProgram
    return simulator.main()
