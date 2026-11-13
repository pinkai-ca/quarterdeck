"""The bounded inbox names both ways to recover older messages."""

from datetime import timedelta

from quarterdeck import post


def test_rendered_inbox_footer_explains_whole_thread_reading(tmp_path):
    store = post.Store(tmp_path)
    older = post.Message('rep-synthetic-older', 'report', 'sender', 'reader',
                         post.now() - timedelta(days=2), 'synthetic older body')
    store.write(older)

    rendered = post.render_inbox(tmp_path, store, 'reader').read_text()

    assert older.body not in rendered
    assert 'Older received messages: 1. Read the whole history with `qd inbox --all`.' in rendered
    assert ('`qd thread <id>` prints a thread whole, '
            'as far as the seat sent or received it.') in rendered
