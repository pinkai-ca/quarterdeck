"""A jailed seat's qd thread prints its own part of a thread whose root it never saw (law: context-is-cache).
Real bubblewrap.

Inside a jail the seat's view of the post holds only the messages it sent or received. A hand's grading order sits
under a root the hand was never party to, so the view lacks that root: qd thread prints the hand's own messages and
names the root as not shown, by its id alone, and qd post --re that root is still refused. Synthetic messages only.
"""

from __future__ import annotations

import pytest

from quarterdeck import post, post_threads, ship as shipmod

from .test_jail_runtime_claude_gates import jailed_qd, namespaces_allowed

ROOT_SUBJECT = 'Synthetic root subject the hand never sees'
ROOT_BODY = 'Synthetic root body the hand never sees.'


def handed(drydock):
    """The drydock with one hand beside its worker, on the fake program."""
    file = drydock / 'ship.toml'
    file.write_text(file.read_text() + '\n[seats.hand-1]\nsession = "crew"\nrole = "hand"\nharness = "fake"\n'
                    'pool = "fake"\nmodel = "fake-1"\n')
    return shipmod.load(drydock)


@pytest.mark.skipif(not namespaces_allowed(), reason='bubblewrap is not installed, or cannot make a namespace here')
def test_a_hands_grading_order_under_a_root_it_never_saw_prints_its_own_part_and_names_the_root(drydock, tmp_path):
    ship = handed(drydock)
    shipmod.fence(ship)
    store = post.Store(ship.runtime)
    root = store.new('order', 'captain', 'worker-1', ROOT_BODY, subject=ROOT_SUBJECT)
    grading = store.new('order', 'worker-1', 'hand-1', 'Grade the synthetic commit.', re=root.id,
                        subject='Grade a synthetic commit')
    ack = store.new('ack', 'hand-1', 'worker-1', 'Synthetic ack.', re=grading.id)
    beside = store.new('report', 'captain', 'worker-1', 'Synthetic report the hand never sees.', re=root.id)
    jail, run = jailed_qd(ship, 'hand-1', tmp_path)  # its plan refreshes the view, as a launch does

    for reference in (grading.id, ack.id):
        shown = run('thread', reference)
        assert shown.returncode == 0, shown.stderr[-400:]
        assert 'thread ancestry is incomplete' not in shown.stdout + shown.stderr
        assert f'qd > {root.id} {post_threads.NOT_SHOWN}\n' in shown.stdout
        assert 'Grade the synthetic commit.' in shown.stdout and 'Synthetic ack.' in shown.stdout
        for unseen in (ROOT_SUBJECT, ROOT_BODY, beside.id, 'Synthetic report the hand never sees.'):
            assert unseen not in shown.stdout, unseen
        assert shown.stdout.rstrip('\n').endswith(f'qd > end of thread {root.id}')

    drop = ship.runtime / 'post' / 'drop' / 'hand-1'
    before = sorted(drop.rglob('*')) if drop.exists() else []
    refused = run('post', 'report', '--re', root.id, 'Synthetic report on a root the hand never saw.')
    assert refused.returncode != 0 and root.id in refused.stderr, (refused.stdout, refused.stderr[-400:])
    assert (sorted(drop.rglob('*')) if drop.exists() else []) == before, 'a refused post leaves nothing to file'
    assert run('post', 'report', '--re', grading.id, 'Synthetic grade.').returncode == 0, 'its own order still answers'

    outside = post_threads.whole(store, grading.id, 'hand-1')
    assert outside.root == root.id and outside.unshown == (), 'outside a jail the thread is unchanged'
    assert f'order {root.id}' in outside.others and f'report {beside.id}' in outside.others
