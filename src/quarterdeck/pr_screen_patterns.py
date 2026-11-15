"""The pull-request screen's one list of patterns (pr_screen): known forms of an instruction to a reader, and of a
command that would do harm. Each is a name and a regular expression, matched line by line, ignoring case, on the line
as written and again with underscores, dashes, dots and slashes read as spaces (a file name hides words that way). A hit
flags the pull request; the desk is told the pattern's name, the file's number and the line, never the text.

The list is deliberately broad: a false flag costs the Admiral one look on GitHub, a miss costs a seat reading an
outsider's instruction. It is one layer of three, beside two hands on two local models.
"""

import re

from .harness import NAMES

# The programs a seat can run, by the adapters' own names (program knowledge stays in harness/): an outsider may
# address a reader by one, and their homes hold credentials.
_PROGRAMS = '|'.join(re.escape(name) for name in NAMES)

PATTERNS = (
    # An instruction aimed at whoever reads the text.
    ('ignore-instructions', r'\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|'
                            r'your|system|any|the)\b.{0,30}\b(instructions?|prompts?|rules|directions|orders|guidelines|tasks?)\b'),
    ('new-instructions', r'\b(new|updated|real|actual|true|secret|hidden)\s+(instructions?|orders|task|directive)s?\b'),
    ('model-vocative', r'^\W*(ai|assistant|model|llm|bot|agent|reviewer( bot)?|screener|grader|automated reviewers?)'
                       r'\s*[:,]'),
    ('role-claim', r'\b(you are now|act as (an?|the)|you must now|from now on,? you|pretend (to be|you are))\b'),
    ('system-prompt', r'\b(system prompt|developer (message|mode)|jailbreak|do anything now|DAN mode)\b'),
    ('addressed-to-a-model', r'\b(dear|attention|note (to|for)|hey|hello|hi)\b.{0,20}\b(ai|assistant|model|llm|bot|'
                             r'agent|' + _PROGRAMS + r'|gpt|gemini|qwen|gemma|reviewer|whoever (reads|reviews))\b'),
    ('answer-coercion', r'\b(answer|reply|respond|report|mark|output|say|write|return|declare)\b.{0,40}\b(clean|safe|'
                        r'approved?|no (issues?|problems?|findings?)|lgtm|nothing (found|flagged)|harmless)\b'),
    ('already-screened', r'\b(already|previously|pre-?)\s*(screened|reviewed|approved|checked|verified|cleared)\b'),
    ('qd-command', r'\bqd\s+(post|fresh|restart|up|down|set|press|type|stop|sync|submit|check)\b'),
    ('post-to-a-seat', r'\b(send|post|forward|relay|pass|copy)\b.{0,40}\b(to|for)\b.{0,10}\b(una|pike|scott|desk|spock|'
                       r'uhura|laan|la\'an|captain|first officer|engineer|admiral|counsel|steward|media|communications)\b'),
    ('runtime-journal', r'(human-alert|unconfirmed-delivery-alert|selection-alert|manual-fresh-alert|compaction|refusal|'
                        r'launch|launch-price|record-launches)\.json(l)?\b|\bruntime/(seats|post|locks)\b|\bseat-holds\b'),
    # A command that would do harm, or reach what it should not.
    ('pipe-to-shell', r'\b(curl|wget|fetch)\b[^|\n]{0,200}\|\s*(sudo\s+)?(ba|z|da|k)?sh\b'),
    ('destructive-command', r'\brm\s+-[a-z]*[rf][a-z]*\s+(/|~|\$home|\*|\.\.)|\bmkfs(\.\w+)?\b|\bdd\s+if=.*\bof=/dev/'
                            r'|:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:|\bshred\b|\bchmod\s+(-r\s+)?0?777\s+/'),
    ('credential-path', r'(~|\$home|/home/[^/\s]+|/root)/\.(ssh|aws|gnupg|netrc|docker|kube|config/gh|' + _PROGRAMS
                        + r')\b|\.credentials\.json\b|\bid_(rsa|ed25519|ecdsa)\b|\bauthorized_keys\b'),
    ('secret-name', r'\b(api[_-]?key|access[_-]?token|secret[_-]?key|private[_-]?key|password|passwd|'
                    r'(gh|github|anthropic|openai|xai)[_-]?(token|key))\s*[=:]'),
    ('exfiltration', r'\b(curl|wget|nc|ncat|socat|scp|rsync|sftp)\b.{0,120}(--data|\s-d\s|\s-f\s|\s-t\s|@/|@~|'
                     r'--upload-file|\bpost\b)'),
    ('encoded-payload', r'\bbase64\s+(-d|--decode)\b|\beval\s*\(\s*(atob|base64|decode)|\bpython3?\s+-c\s+["\']?\s*'
                        r'(exec|import\s+os)|\bfromcharcode\b'),
    ('shell-eval', r'\b(eval|exec|system|popen|subprocess\.(run|call|popen)|os\.system)\s*\(\s*["\'`]?\s*'
                   r'(rm|curl|wget|bash|sh|nc)\b'),
    ('git-push-or-hooks', r'\bgit\s+(push|config\s+(--global\s+)?core\.hookspath)\b|\.git/hooks/|\bgh\s+(pr|repo|'
                          r'api|secret|auth)\b'),
    ('privilege', r'\b(sudo|pkexec|doas|su\s+-)\b'),
    ('persistence', r'\b(crontab|systemctl\s+(--user\s+)?(enable|start)|\.bashrc|\.profile|\.zshrc|autostart)\b'),
    # Text hidden from a human reader.
    ('hidden-characters', '[​-‏‪-‮⁠-⁤⁦-⁩﻿]'),
)
