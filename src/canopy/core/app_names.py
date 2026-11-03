"""The client app-name rule, shared by every module that takes an ``{app}``.

Magpie keys stored configs by app and E-log picks an app's token by it, so one
client app name has to be valid in both. Keeping the rule here makes that so.
"""

import re

# URL/index-friendly app names.
_APP_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
# Prose form of _APP_RE, for error messages. Kept adjacent so the two stay in sync.
APP_NAME_RULE = "allowed: lowercase letters, digits, '_', '-'; 1-64 characters"


def is_app(value: str) -> bool:
    return bool(_APP_RE.fullmatch(value))
