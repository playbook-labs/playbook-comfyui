"""Where the Playbook API key lives.

Never a node widget: widget values are serialised into workflow.json and into the
`prompt` / `workflow` PNG text chunks, so a key put on a node travels with every
render the user shares. The key is kept in ComfyUI's user directory instead --
outside this folder, so updating or reinstalling the node does not wipe it and it
can never end up in the archive published to the Comfy Registry.
"""

import json
import os
from pathlib import Path

import folder_paths

API_BASE = os.environ.get("PLAYBOOK_API_BASE", "https://api.playbook.com/v1")

CONFIG_DIR = Path(folder_paths.get_user_directory()) / "playbook"
CONFIG_FILE = CONFIG_DIR / "credentials.json"


def multi_user():
    """Whether this ComfyUI serves several profiles from one process.

    The stored file is per-machine, not per-profile: a node executing a prompt has no
    request and therefore no identity, so one saved token would be usable by everyone
    signed into the server. Rather than share it silently, refuse to store one at all.
    """
    try:
        from comfy.cli_args import args
    except ImportError:
        return False
    return bool(getattr(args, "multi_user", False))


MULTI_USER_MESSAGE = (
    "This ComfyUI runs in multi-user mode, where a saved key would be shared with every "
    "profile on the server. Set PLAYBOOK_API_KEY in the environment instead, or wire a key "
    "into the node's api_key input."
)


def load():
    """Resolution order: environment (headless installs) beats the stored file."""
    if multi_user():
        return {
            "api_key": os.environ.get("PLAYBOOK_API_KEY"),
            "slug": os.environ.get("PLAYBOOK_WORKSPACE"),
        }

    stored = {}
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            stored = {}
        # Valid JSON is not necessarily our JSON: a file holding `[]` would parse and then
        # break every node with an AttributeError on .get.
        if not isinstance(stored, dict):
            stored = {}

    return {
        "api_key": os.environ.get("PLAYBOOK_API_KEY") or stored.get("api_key"),
        "slug": os.environ.get("PLAYBOOK_WORKSPACE") or stored.get("slug"),
    }


def save(api_key, slug):
    if multi_user():
        raise PermissionError(MULTI_USER_MESSAGE)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # would silently keep whatever mode it already had -- 0644 from a restore or a
    # sync, for instance, which leaves the token readable by other local users.
    temporary = CONFIG_FILE.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"api_key": api_key, "slug": slug}, handle, indent=2)
    os.replace(temporary, CONFIG_FILE)


def clear():
    CONFIG_FILE.unlink(missing_ok=True)


def mask(api_key):
    """What the browser is allowed to see back. Never the key itself."""
    if not api_key:
        return ""
    return f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 12 else "..."
