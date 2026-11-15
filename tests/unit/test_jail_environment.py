"""jail_environment: the allowlist and the pattern of secret-looking names, in one place, and the launcher."""

from __future__ import annotations

import os

import pytest

from quarterdeck import jail_environment as je

SECRET = ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'JMAP_TOKEN', 'AUTH0_CLIENT_SECRET', 'HELM_SESSION_SECRET',
          'CODEX_PASS', 'QD_ENDPOINT_KEY', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'npm_config__authToken',
          'DATABASE_PASSWORD', 'PRIVATE_KEY_PATH', 'SSH_PASSPHRASE', 'HTTP_AUTHORIZATION', 'GH_OAUTH',
          'SESSION_COOKIE', 'APIKEY', 'MY_CREDS', 'LC_DECOY_TOKEN', 'X_TOKENS', 'SIGNING_KEYS')
PLAIN = ('PATH', 'HOME', 'TERM', 'LANG', 'LC_ALL', 'TZ', 'USER', 'LOGNAME', 'XDG_RUNTIME_DIR', 'QD_SHIP', 'QD_SEAT',
         'BASH_FUNC_qd%%', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'PI_CODING_AGENT_DIR', 'PI_SKIP_VERSION_CHECK',
         'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX', 'OPENCODE_PERMISSION', 'OPENCODE_DISABLE_AUTOUPDATE',
         'OPENCODE_CONFIG_CONTENT', 'XDG_CONFIG_HOME', 'QD_WORKSPACE', 'TMPDIR')


@pytest.mark.parametrize('name', SECRET)
def test_a_secret_looking_name_is_seen(name):
    assert je.looks_secret(name)


@pytest.mark.parametrize('name', PLAIN)
def test_the_allowlist_and_the_harnesses_names_do_not_look_secret(name):
    assert not je.looks_secret(name)


def test_nothing_the_allowlist_names_looks_secret():
    assert not [name for name in (*je.ALLOWED, *je.FROM_PANE) if je.looks_secret(name)]


def test_a_token_is_a_count_only_before_a_word_of_counting():
    assert not je.looks_secret('MAX_OUTPUT_TOKENS_LIMIT') and je.looks_secret('OUTPUT_TOKEN') \
        and je.looks_secret('TOKEN_MAXIMUM')


def test_the_pane_keeps_term_lang_tz_and_locale_only():
    environ = {'TERM': 'tmux-256color', 'LANG': 'C.UTF-8', 'TZ': 'UTC', 'LC_ALL': 'C', 'LC_DECOY_TOKEN': 'x',
               'OPENAI_API_KEY': 'x', 'DISPLAY': ':0', 'PATH': '/bin', 'TERMINFO_BAD': 'a\0b', 'LC_NUL': 'a\0b'}
    words = je.pane_settings(environ)
    assert words == ['--setenv', 'LANG', 'C.UTF-8', '--setenv', 'LC_ALL', 'C', '--setenv', 'TERM', 'tmux-256color',
                     '--setenv', 'TZ', 'UTC']


def test_the_launcher_execs_with_only_path_and_puts_the_panes_values_after_clearenv(monkeypatch):
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in {'PATH': '/usr/bin:/bin', 'TERM': 'xterm', 'OPENAI_API_KEY': 'decoy', 'DECOY': 'x'}.items():
        monkeypatch.setenv(name, value)
    seen = {}

    def execve(program, argv, env):
        seen.update(program=program, argv=argv, env=env)
        raise SystemExit(0)
    monkeypatch.setattr(os, 'execve', execve)
    with pytest.raises(SystemExit):
        je.main(['--', 'bwrap', '--ro-bind', '/', '/', '--clearenv', '--setenv', 'HOME', '/h', '--', 'prog'])
    assert seen['env'] == {'PATH': '/usr/bin:/bin'}
    assert seen['argv'] == ['bwrap', '--ro-bind', '/', '/', '--clearenv', '--setenv', 'TERM', 'xterm',
                            '--setenv', 'HOME', '/h', '--', 'prog']
    assert os.path.basename(seen['program']) == 'bwrap'


def test_the_launcher_refuses_a_command_without_clearenv(capsys):
    assert je.main(['--', 'bwrap', '--', 'prog']) == 2 and je.main([]) == 2
