"""What we record about the graph that produced an asset.

Kept free of torch/PIL so it can be tested without a ComfyUI environment.
"""

import json
import os
import re

MAX_BYTES = 4096

SECRET_INPUT = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)
MODEL_INPUTS = ("ckpt_name", "unet_name", "lora_name", "model_name", "vae_name",
                "control_net_name")


def version():
    path = os.path.join(os.path.dirname(__file__), "pyproject.toml")
    try:
        with open(path) as handle:
            for line in handle:
                if line.startswith("version"):
                    return line.split("=")[1].strip().strip('"')
    except OSError:
        pass
    return "unknown"


def redact(prompt, secrets=()):
    """Other node packs do put API keys in widgets, and widget values are in the prompt.

    We are about to upload that prompt into somebody's Playbook workspace, so anything
    that looks like a credential is dropped before it leaves the machine. Names are only
    half of it: our own key arrives through `forceInput`, and the node feeding it may call
    its widget `text` or `value`, so the known secrets are matched by value too.
    """
    known = {value for value in secrets if isinstance(value, str) and len(value) >= 8}
    clean = {}
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            clean[node_id] = node
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            clean[node_id] = node
            continue
        clean[node_id] = {**node, "inputs": _scrub(inputs, known)}
    return clean


def _scrub(value, known, name=""):
    """Recursive: a credential is as likely to sit in `{"config": {"api_key": ...}}` or in
    `{"headers": ["Bearer pb_live_..."]}` as at the top level, and a known secret is often
    embedded in a longer string rather than being the whole of it."""
    if isinstance(value, dict):
        return {key: _scrub(item, known, key) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, known, name) for item in value]
    if not isinstance(value, str):
        return value

    if SECRET_INPUT.search(name):
        return "[redacted]"
    for secret in known:
        if secret in value:
            return "[redacted]"
    return value


def summarise(prompt):
    """Node count and the checkpoints/LoRAs used -- what survives when the graph does not."""
    nodes, models = 0, []
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        nodes += 1
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for name in MODEL_INPUTS:
            value = inputs.get(name)
            if isinstance(value, str) and value not in models:
                models.append(value)
    return nodes, models


def _size(payload):
    """Measured the way the server measures it, not the way Python would.

    Rails serialises with escape_html_entities_in_json on, so every <, > and & leaves as a
    six-byte \\uXXXX escape. json.dumps leaves them one byte each, and a graph carrying a
    row of <lora:...> tags can pass this check and be REJECTED by the 4 KB validation --
    after the image is already in storage, which loses the render and orphans the object.
    """
    text = json.dumps(payload, ensure_ascii=False)
    size = len(text.encode("utf-8"))
    # <, > and & are one byte here and six there.
    size += 5 * sum(text.count(char) for char in "<>&")
    # U+2028 and U+2029 are escaped too, always: three UTF-8 bytes become six characters.
    size += 3 * sum(text.count(char) for char in "\u2028\u2029")
    return size


def build(prompt, secrets=()):
    payload = {"source": "comfyui", "node_version": version()}
    if not isinstance(prompt, dict):
        return payload

    nodes, models = summarise(prompt)
    payload["node_count"] = nodes
    if models:
        payload["models"] = models[:5]

    with_workflow = {**payload, "workflow": redact(prompt, secrets)}
    if _size(with_workflow) <= MAX_BYTES:
        return with_workflow

    # The fallback is checked too rather than assumed small: model filenames are arbitrary
    # strings, and a payload the server rejects fails the upload AFTER the bytes are stored.
    while _size(payload) > MAX_BYTES and payload.get("models"):
        payload["models"] = payload["models"][:-1] or None
        if not payload["models"]:
            payload.pop("models")

    return payload
