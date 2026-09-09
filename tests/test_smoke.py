"""Runs without ComfyUI: only the modules that have no torch/PIL dependency.

Loaded by path because the repo directory name is not a valid Python identifier.
"""

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
USER_DIR = tempfile.mkdtemp()

folder_paths = ModuleType("folder_paths")
folder_paths.get_user_directory = lambda: USER_DIR
sys.modules["folder_paths"] = folder_paths


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


credentials = load("credentials")
provenance = load("provenance")


class Provenance(unittest.TestCase):
    def test_redacts_credential_shaped_inputs(self):
        prompt = {
            "1": {"class_type": "SomeAPINode", "inputs": {"api_key": "sk-live-123", "steps": 20}},
            "2": {"class_type": "KSampler", "inputs": {"seed": 7, "auth_token": "abc"}},
        }

        clean = provenance.redact(prompt)

        self.assertEqual(clean["1"]["inputs"]["api_key"], "[redacted]")
        self.assertEqual(clean["2"]["inputs"]["auth_token"], "[redacted]")
        self.assertEqual(clean["1"]["inputs"]["steps"], 20)
        self.assertEqual(clean["2"]["inputs"]["seed"], 7)
        self.assertEqual(prompt["1"]["inputs"]["api_key"], "sk-live-123", "must not mutate")

    def test_redacts_a_known_secret_whatever_the_input_is_called(self):
        # Our key arrives through forceInput from another pack's widget, and that widget can
        # be called anything -- `text` here. Name matching alone would ship it.
        prompt = {"1": {"inputs": {"text": "pb_live_supersecret", "steps": 20}}}

        clean = provenance.redact(prompt, secrets=["pb_live_supersecret"])

        self.assertEqual(clean["1"]["inputs"]["text"], "[redacted]")
        self.assertEqual(clean["1"]["inputs"]["steps"], 20)

    def test_redacts_secrets_nested_inside_objects_and_lists(self):
        prompt = {
            "1": {"inputs": {"config": {"api_key": "pb_live_supersecret"}}},
            "2": {"inputs": {"headers": ["Bearer pb_live_supersecret", "Accept: */*"]}},
        }

        clean = provenance.redact(prompt, secrets=["pb_live_supersecret"])

        self.assertEqual(clean["1"]["inputs"]["config"]["api_key"], "[redacted]")
        self.assertEqual(clean["2"]["inputs"]["headers"][0], "[redacted]")
        self.assertEqual(clean["2"]["inputs"]["headers"][1], "Accept: */*")

    def test_counts_the_separators_rails_escapes(self):
        # U+2028 is three bytes here and six characters there; a graph full of them would
        # otherwise pass this check and be rejected by the server.
        payload = {"a": " "}

        self.assertEqual(provenance._size(payload), len('{"a": ""}'.encode()) + 3 + 3)

    def test_measures_size_the_way_the_server_does(self):
        # Rails escapes < > & into six bytes each; Python does not. A payload that looks
        # small here and is rejected there loses a render that is already in storage.
        payload = {"a": "<lora:x>"}

        self.assertEqual(provenance._size(payload), len(json.dumps(payload)) + 10)

    def test_survives_odd_shapes(self):
        self.assertEqual(provenance.redact({"1": "not a node"}), {"1": "not a node"})
        self.assertEqual(provenance.redact({"1": {"inputs": None}}), {"1": {"inputs": None}})

    def test_keeps_a_summary_when_the_workflow_does_not_fit(self):
        big = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
            "2": {"inputs": {"text": "x" * (provenance.MAX_BYTES + 1)}},
        }

        payload = provenance.build(big)

        self.assertNotIn("workflow", payload)
        self.assertEqual(payload["node_count"], 2)
        self.assertEqual(payload["models"], ["sdxl.safetensors"])
        self.assertLessEqual(len(json.dumps(payload)), provenance.MAX_BYTES)

    def test_keeps_the_workflow_when_it_fits(self):
        payload = provenance.build({"1": {"inputs": {"text": "small"}}})

        self.assertIn("workflow", payload)
        self.assertEqual(provenance.build(None)["source"], "comfyui")


class Credentials(unittest.TestCase):
    def setUp(self):
        credentials.clear()
        os.environ.pop("PLAYBOOK_API_KEY", None)
        os.environ.pop("PLAYBOOK_WORKSPACE", None)

    tearDown = setUp

    def test_environment_beats_stored_file(self):
        credentials.save("stored-key", "stored-slug")
        self.assertEqual(credentials.load()["api_key"], "stored-key")

        os.environ["PLAYBOOK_API_KEY"] = "env-key"
        self.assertEqual(credentials.load()["api_key"], "env-key")
        self.assertEqual(credentials.load()["slug"], "stored-slug")

    def test_file_is_owner_only(self):
        credentials.save("k", "s")
        mode = stat.S_IMODE(os.stat(credentials.CONFIG_FILE).st_mode)

        self.assertEqual(mode, 0o600)
        self.assertEqual(json.loads(credentials.CONFIG_FILE.read_text())["slug"], "s")

    def test_survives_a_corrupt_file(self):
        credentials.save("k", "s")
        credentials.CONFIG_FILE.write_text("{not json")

        self.assertIsNone(credentials.load()["api_key"])

    def test_mask_never_leaks_the_middle(self):
        self.assertEqual(credentials.mask("pb_live_abcdefghijkl"), "pb_liv...ijkl")
        self.assertEqual(credentials.mask("short"), "...")
        self.assertEqual(credentials.mask(None), "")


if __name__ == "__main__":
    unittest.main()
