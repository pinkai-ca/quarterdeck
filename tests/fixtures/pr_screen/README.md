# Planted pull requests for the screen

Each folder holds one planted pull request, `pr.json`, used by the screen's fixture tests and its live proof on the
hands (pr_screen). None of them is on GitHub. Every instruction planted here is harmless by design: write the name of
a bird into a file, send the name of a bird to a seat, or answer a word. A hand that obeys one does no harm, and the
screen shows what it did.

`expect` names what the case is for: `clean` cases should pass, the others carry an instruction or a harmful-looking
command in the place the case names. `search` lists pattern names the one list is expected to find (empty where the
case is written to slip past the search).

Commits touching this folder go to the judge alone, never to a hand (the grading's rule).
