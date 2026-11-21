"""How each program takes an API key at launch: a table of names, never a value (keyfile, key_launch).

A program that is not here takes no key. No record reader needs this, and it is in no reader's stamp set.
"""

from ..keyfile import ENDPOINT_VARIABLE, HELPER_VARIABLE, Route

ROUTES = {
    # Measured on 2.1.317: its usual variable stops at a question whose preselected answer is No, and answering it
    # would keep the key's last 20 characters in its state. Its helper setting asks nothing and keeps nothing.
    'claude': Route(HELPER_VARIABLE, helper=True, state_file='.claude.json', state_account='oauthAccount'),
    # Measured on 0.166.0: the variable is ignored and it stops at its sign-in; its own key login is what it reads.
    'codex': Route(None, login=('login', '--with-api-key'), login_home='CODEX_HOME', login_file='auth.json'),
    # Measured on 1.0.92: the variable is taken, with no sign-in screen.
    'grok': Route('XAI_API_KEY'),
    'opencode': Route(ENDPOINT_VARIABLE),
    'pi': Route(ENDPOINT_VARIABLE),
    'fake': Route(HELPER_VARIABLE),
}
