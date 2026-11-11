"""Synthetic Codex 0.166.0 editor shapes from the measured exact-width rule."""
import pytest

from quarterdeck.harness.codex import CodexHarness


def editor(rows):
    return '› '+rows[0]+'\n'+''.join('  '+row+'\n' for row in rows[1:])+"\n  synthetic-model low · /synthetic · main\n"


@pytest.mark.parametrize('width',[12,422])
@pytest.mark.parametrize('multiple',[1,2])
@pytest.mark.parametrize('delta',[-1,0,1])
@pytest.mark.parametrize('blank',[False,True])
def test_exact_width_editor_row_is_layout_not_an_input_blank(width,delta,blank,multiple):
    harness=CodexHarness();line='x'*(multiple*width+delta)
    payload='header\n'+line+'\n'+('\n' if blank else '')+'reply'
    wrapped=[line[i:i+width] for i in range(0,len(line),width)]
    rows=['header',*wrapped]+([''] if delta==0 else [])+([''] if blank else [])+['reply']
    shown=harness.screen(editor(rows))
    assert harness.box(payload).matches(shown.box,harness.box_columns(width+3))  # law: one-door
    altered=rows.copy();altered[-1]='replX'
    assert not harness.box(payload).matches(harness.screen(editor(altered)).box,width)


def test_other_blank_rows_are_not_discarded():
    harness=CodexHarness();width=12
    payload='header\n'+'x'*width+'\n\nreply'
    assert not harness.box(payload).matches(harness.screen(editor(['header','x'*width,'','reply'])).box,width)
    assert not harness.box('short\nreply').matches(harness.screen(editor(['short','','reply'])).box,width)
    assert not harness.box('x'*(width+1)+'\nreply').matches(
        harness.screen(editor(['x'*width,'','x','reply'])).box,width)
