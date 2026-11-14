"""The measured usage argv runs only a synthetic local executable."""

import json
import os
from pathlib import Path
import sys

from quarterdeck.harness.muse_usage import read_native


def test_the_explicit_check_usage_runs_the_native_subcommand_and_decodes_buckets(tmp_path):
    program = tmp_path / 'synthetic-usage'
    program.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
Path(os.environ['SYNTHETIC_CALL']).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({'schemaVersion': 1, 'allowance': {
    'window': {'usedPercent': 20, 'resetsAtMs': 1893459600000},
    'weekly': {'usedPercent': 40, 'resetsAtMs': 1894060800000}}}))
''')
    program.chmod(0o700)
    invocation = tmp_path / 'invocation.json'
    usage = read_native(program, tmp_path, {'PATH': os.defpath, 'SYNTHETIC_CALL': str(invocation)})
    assert usage.called is True and usage.error is None
    assert json.loads(invocation.read_text()) == ['usage', '--json']
    assert [(item.name, item.used) for item in usage.windows] == [('window', .2), ('weekly', .4)]
