"""The run's evidence recorder, loaded into the target's pytest with ``-p qd_testrun_evidence``.

The runner copies this file into its own run venv; nothing in the target imports it. It uses public pytest
hooks only, and xdist's hooks only as optional hooks. Each process (the controller as ``main``, each xdist
worker by its id) appends one JSON line per event to its own file in ``$TESTRUN_EVIDENCE`` and flushes it,
so a process that dies leaves every event it had recorded.
"""
from contextlib import contextmanager
import json
import os
import sys
import tempfile
import time

import pytest

PLUGIN_VERSION = 2  # 2: a 'collected' event says whether the collection completed


def _text(value):
    return '' if value is None else str(value)


def _node(report):
    node = getattr(report, 'node', None)
    gateway = getattr(node, 'gateway', None)
    return getattr(gateway, 'id', None) or getattr(report, 'worker_id', None)


class Recorder:
    def __init__(self, config, folder):
        workerinput = getattr(config, 'workerinput', None)
        self.role = workerinput['workerid'] if workerinput else 'main'
        self.file = open(os.path.join(folder, self.role + '.jsonl'), 'a', encoding='utf-8')
        self.collection_complete = False
        self.ci_sessions = None
        context = os.environ.get('TESTRUN_CI_SESSION_CONTEXT')
        if context:
            self.ci_sessions = CISessions(json.loads(context))

    def emit(self, kind, **fields):
        self.file.write(json.dumps({'kind': kind, 'role': self.role, 'at': time.time(), **fields}) + '\n')
        self.file.flush()

    def pytest_sessionstart(self, session):
        if self.ci_sessions is not None:
            self.ci_sessions.start()
        try:
            import xdist
            xdist_version = xdist.__version__
        except ImportError:
            xdist_version = None
        plugins = sorted({f'{dist.project_name} {dist.version}'
                          for _, dist in session.config.pluginmanager.list_plugin_distinfo()})
        self.emit('start', plugin=PLUGIN_VERSION, pytest=pytest.__version__, xdist=xdist_version,
                  python=sys.version.split()[0], executable=sys.executable, plugins=plugins,
                  layout={'cwd': os.getcwd(), 'TMPDIR': os.environ.get('TMPDIR'),
                          'tempdir': tempfile.gettempdir(), 'testruns_run_present': 'TESTRUNS_RUN' in os.environ,
                          'disk_only': os.environ.get('TESTRUNS_CI_DISK') == '1',
                          'registry_present': 'TESTRUN_SESSION_RECORDS' in os.environ,
                          'basetemp': _text(session.config.option.basetemp) or None})

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.emit('collect', nodeid=report.nodeid, outcome=report.outcome, longrepr=_text(report.longrepr),
                      sections=list(getattr(report, 'sections', [])))

    def pytest_deselected(self, items):
        self.emit('deselected', nodeids=[item.nodeid for item in items])

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        self.collection_complete = True  # pytest calls this only once collection has completed, errors included

    def pytest_collection_finish(self, session):
        # pytest calls this from a ``finally``: an interrupted collection reaches it with a partial list
        self.emit('collected', nodeids=[item.nodeid for item in session.items], complete=self.collection_complete)

    def pytest_runtest_logreport(self, report):
        self.emit('report', nodeid=report.nodeid, when=report.when, outcome=report.outcome,
                  wasxfail=hasattr(report, 'wasxfail'), longrepr=_text(report.longrepr) if report.longrepr else '',
                  sections=list(report.sections), duration=getattr(report, 'duration', None), node=_node(report))

    def pytest_internalerror(self, excrepr, excinfo):
        self.emit('internalerror', text=_text(excrepr))

    def pytest_keyboard_interrupt(self, excinfo):
        self.emit('interrupt', text=_text(excinfo.getrepr(style='short')))

    def pytest_sessionfinish(self, session, exitstatus):
        if self.ci_sessions is not None:
            self.ci_sessions.finish()
        self.emit('finish', exitstatus=int(exitstatus), testscollected=getattr(session, 'testscollected', None))

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.emit('node_collected', node=node.gateway.id, nodeids=list(ids))

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.emit('node_down', node=node.gateway.id, error=None if error is None else _text(error))

    @pytest.hookimpl(optionalhook=True)
    def pytest_handlecrashitem(self, crashitem, report, sched):
        self.emit('crash', nodeid=crashitem, node=_node(report), longrepr=_text(report.longrepr))


    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if self.ci_sessions is not None and fixturedef.argname == 'private_socket_root':
            with self.ci_sessions.fixture_container(fixturedef) as containers:
                outcome = yield
                if containers != [outcome.get_result()]:
                    raise RuntimeError('CI private socket fixture did not use its supported container scope')
        else:
            yield


class CISessions:
    """Trusted instrumentation, separate from the target's ordinary conftest registry environment."""
    def __init__(self, context):
        self.context = context
        self.patch = pytest.MonkeyPatch()

    def start(self):
        from _qd_testrun_ci_support import servers
        from quarterdeck.tmux_socket import OwnedSocket
        original = OwnedSocket.record

        def record(owner, held, query):
            original(owner, held, query)
            servers.record(owner, **self.context)

        self.patch.setattr(OwnedSocket, 'record', record)

    @contextmanager
    def fixture_container(self, fixturedef):
        """Unmark inside the fixture's own container, after its child cleanup.

        A request finalizer added after the fixture yields runs too early: its
        native tmux folder still exists then. Enter the known container context
        during setup, so its existing unwinding order retires children first,
        releases our marker second, and removes the empty container last.
        The temporary binding ends before any test body runs; CI's environment
        and the target fixture's own cleanup/checks are unchanged.
        """
        namespace = getattr(fixturedef.func, '__globals__', {})
        original = namespace.get('socket_container')
        if not callable(original):
            raise RuntimeError('CI private socket fixture has no supported container scope')
        containers = []

        @contextmanager
        def registered(*args, **kwargs):
            with original(*args, **kwargs) as folder:
                release = self._container_release(folder)
                containers.append(folder)
                try:
                    yield folder
                finally:
                    if release is not None:
                        release()

        with pytest.MonkeyPatch.context() as patch:
            patch.setitem(namespace, 'socket_container', registered)
            yield containers

    def container(self, folder, request):
        """Registration for a caller whose container has no pending child teardown."""
        release = self._container_release(folder)
        if release is not None:
            request.addfinalizer(release)

    def _container_release(self, folder):
        from pathlib import Path
        from _qd_testrun_ci_support import sessions
        from _qd_testrun_ci_support.layout import DIRECTORY
        folder = Path(folder)
        descriptor = os.open(folder, DIRECTORY)
        try:
            token = sessions.register(folder.parent, folder, descriptor, **self.context)
        except BaseException:
            os.close(descriptor)
            raise
        if token is None:
            os.close(descriptor)
            return

        def release():
            try:
                sessions.unmark_empty(descriptor, token)
            finally:
                os.close(descriptor)

        return release

    def finish(self):
        self.patch.undo()


def pytest_configure(config):
    folder = os.environ.get('TESTRUN_EVIDENCE')
    if folder:
        config.pluginmanager.register(Recorder(config, folder), 'qd-testrun-evidence-recorder')
