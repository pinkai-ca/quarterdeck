The `capped-*.json` captures preserve the fresh composer's measured rows and
padding at pane widths 64, 80 and 112. Its bottom-rule spans are 60/75/75 and
its text widths are 55/70/70. The `session-*.json` captures were taken after one
short completed turn on the local model, confirmed in its native record: the
composer then spans the pane, with rule widths 60/76/108 and text widths
55/71/103. Subsequent measurement pastes were not submitted. Each layout was
measured on OpenCode 1.19.5 on 15 November 2026, in disposable homes on private
`-S` tmux servers, which were closed and removed after measurement.

Payloads are synthetic. Status lines are invented; composer rows, bottom rules
and their padding retain the capture's geometry. The unrecognised-rule fixture
is a derived control: its bottom-rule glyphs were replaced in a native capture.
It is not a claim that OpenCode drew those replacement glyphs.

The door reads the recognised rule's span minus five non-text cells. Only
comparison discards ASCII spaces proven to lie beyond that text area; the raw
`Screen.box` keeps them. Unrecognised geometry uses the older declared bound,
which does not certify a missing input box. No older fixture was replaced.
