"""Pins the storage choreography.

The first version of this node PUT the bytes straight at the URL `upload_prepare`
returns, which is what the public docs describe and which cannot work: that URL is
signed for a resumable POST. Nothing caught it because nothing here was tested.
"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

folder_paths = ModuleType("folder_paths")
folder_paths.get_user_directory = lambda: tempfile.mkdtemp()
sys.modules["folder_paths"] = folder_paths

package = ModuleType("playbook_comfyui")
package.__path__ = [str(ROOT)]
sys.modules["playbook_comfyui"] = package

client = importlib.import_module("playbook_comfyui.client")


def response(status=200, headers=None):
    return SimpleNamespace(status_code=status, ok=200 <= status < 300, headers=headers or {},
                           content=b"", text="")


class FakeRequests:
    """Stands in for the `requests` module, recording every call."""

    Timeout = client.requests.Timeout
    ConnectionError = client.requests.ConnectionError

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _record(self, method, url, kwargs):
        self.calls.append(SimpleNamespace(method=method, url=url,
                                          headers=kwargs.get("headers") or {},
                                          data=kwargs.get("data")))
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)

    def put(self, url, **kwargs):
        return self._record("PUT", url, kwargs)


class Transfer(unittest.TestCase):
    def setUp(self):
        self.real = client.requests
        self.addCleanup(lambda: setattr(client, "requests", self.real))

    def use(self, *responses):
        fake = FakeRequests(*responses)
        client.requests = fake
        return fake

    def test_gcs_opens_a_session_then_sends_the_bytes_to_it(self):
        fake = self.use(response(200, {"location": "https://storage/session?id=1"}), response(200))

        client._transfer(
            {"storage_provider": "gcs", "upload_url": "https://upload/1",
             "file_extension": "png", "encrypted_organization_metadata": "enc"},
            b"xy", "image/png",
        )

        start, send = fake.calls
        self.assertEqual((start.method, start.url), ("POST", "https://upload/1"))
        self.assertEqual(start.headers["x-goog-resumable"], "start")
        self.assertEqual(start.headers["Content-Type"], "image/png")
        self.assertEqual(start.headers["x-goog-meta-extension"], "png")
        self.assertEqual(start.headers["x-goog-meta-encrypted-organization-metadata"], "enc")
        self.assertIn(start.data, (b"", None))

        self.assertEqual((send.method, send.url), ("PUT", "https://storage/session?id=1"))
        # text/plain, not the media type -- the session signature expects it.
        self.assertEqual(send.headers["Content-Type"], "text/plain")
        self.assertEqual(send.headers["Content-Range"], "bytes 0-1/2")
        self.assertEqual(send.data, b"xy")

    def test_a_dropped_transfer_opens_a_fresh_session_rather_than_replaying_the_put(self):
        # Resuming a GCS upload is not "send the same range again": once part of it is
        # committed, a resume starts by asking what landed. A new session is the honest retry.
        fake = self.use(
            response(200, {"location": "https://storage/session-1"}),
            client.requests.ConnectionError("reset"),
            response(200, {"location": "https://storage/session-2"}),
            response(200),
        )

        client._transfer({"storage_provider": "gcs", "upload_url": "https://upload/1"},
                         b"xy", "image/png")

        self.assertEqual([call.method for call in fake.calls], ["POST", "PUT", "POST", "PUT"])
        self.assertEqual(fake.calls[3].url, "https://storage/session-2")

    def test_a_308_is_not_a_finished_upload(self):
        # 308 Resume Incomplete means GCS kept part of the range and wants the rest.
        # requests treats it as ok(), so accepting it would tell the API about an object
        # that is not there.
        fake = self.use(
            response(200, {"location": "https://storage/session-1"}),
            response(308),
            response(200, {"location": "https://storage/session-2"}),
            response(200),
        )

        client._transfer({"storage_provider": "gcs", "upload_url": "https://upload/1"},
                         b"xy", "image/png")

        self.assertEqual([call.method for call in fake.calls], ["POST", "PUT", "POST", "PUT"])

    def test_blank_metadata_headers_are_omitted_not_sent_empty(self):
        fake = self.use(response(200, {"location": "https://storage/session"}), response(200))

        client._transfer(
            {"storage_provider": "gcs", "upload_url": "https://upload/1",
             "file_extension": None, "encrypted_organization_metadata": ""},
            b"x", "image/png",
        )

        start = fake.calls[0]
        self.assertNotIn("x-goog-meta-extension", start.headers)
        self.assertNotIn("x-goog-meta-encrypted-organization-metadata", start.headers)

    def test_never_sends_the_playbook_token_to_storage(self):
        fake = self.use(response(200, {"location": "https://storage/session"}), response(200))

        client._transfer({"storage_provider": "gcs", "upload_url": "https://upload/1"},
                         b"x", "image/png")

        for call in fake.calls:
            self.assertNotIn("Authorization", call.headers)

    def test_backblaze_is_a_single_put_with_amz_metadata(self):
        fake = self.use(response(200))

        client._transfer(
            {"storage_provider": "backblaze", "upload_url": "https://b2/1",
             "file_extension": "png", "encrypted_organization_metadata": "enc"},
            b"x", "image/png",
        )

        self.assertEqual(len(fake.calls), 1)
        only = fake.calls[0]
        self.assertEqual((only.method, only.url), ("PUT", "https://b2/1"))
        self.assertEqual(only.headers["x-amz-meta-extension"], "png")
        self.assertEqual(only.headers["Content-Type"], "image/png")

    def test_multipart_is_refused_clearly_instead_of_crashing(self):
        fake = self.use()

        with self.assertRaises(client.PlaybookError) as caught:
            client._transfer({"storage_provider": "backblaze", "upload_url": None},
                             b"x", "image/png")

        self.assertIn("multipart", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_a_session_without_a_location_is_an_error_not_a_put_to_none(self):
        self.use(response(200, {}))

        with self.assertRaises(client.PlaybookError):
            client._transfer({"storage_provider": "gcs", "upload_url": "https://upload/1"},
                             b"x", "image/png")

    def test_unknown_provider_is_refused(self):
        self.use()

        with self.assertRaises(client.PlaybookError):
            client._transfer({"storage_provider": "s3", "upload_url": "https://s3/1"},
                             b"x", "image/png")


class RetryPolicy(unittest.TestCase):
    def playbook(self, session):
        instance = client.Playbook(api_key="k", slug="s")
        instance.session = session
        return instance

    def test_upload_complete_is_never_replayed(self):
        calls = []

        def request(*args, **kwargs):
            calls.append(args)
            raise client.requests.ConnectionError("reset")

        with self.assertRaises(client.PlaybookError):
            self.playbook(SimpleNamespace(request=request, headers={}))._call(
                "POST", "/s/assets/upload_complete", retry=False
            )

        self.assertEqual(len(calls), 1)


class BoardByName(unittest.TestCase):
    """One board per name, however many runs write to it."""

    def setUp(self):
        client._BOARDS.clear()
        self.addCleanup(client._BOARDS.clear)
        self.calls = []

        real = client.load
        client.load = lambda: {"api_key": "k", "slug": "s"}
        self.addCleanup(lambda: setattr(client, "load", real))

    def playbook(self, listings, created=None, boards=None):
        """`boards` answers the by-token reads used to verify a cached resolution."""
        instance = client.Playbook()
        listings = list(listings)
        boards = boards or {}

        def call(method, path, retry=True, **kwargs):
            self.calls.append((method, path))
            if method == "GET" and path.startswith("/s/boards/"):
                return boards.get(path.rsplit("/", 1)[-1])
            if method == "GET":
                return listings.pop(0) if listings else []
            return created

        instance._call = call
        return instance

    def test_reuses_a_board_that_already_exists(self):
        instance = self.playbook([[{"token": "tok-1", "title": "Renders", "tree_depth": 1}]])

        self.assertEqual(instance.ensure_board("renders"), "tok-1")
        self.assertNotIn("POST", [method for method, _ in self.calls])

    def test_creates_it_only_when_nothing_matches(self):
        instance = self.playbook([[{"token": "other", "title": "Something else"}]],
                                 created={"token": "tok-new", "title": "Renders"})

        self.assertEqual(instance.ensure_board("Renders"), "tok-new")
        self.assertIn(("POST", "/s/boards"), self.calls)

    def test_the_second_run_verifies_the_cached_board_rather_than_searching_again(self):
        board = {"token": "tok-1", "title": "Renders", "tree_depth": 1}
        instance = self.playbook([[board]], boards={"tok-1": board})
        instance.ensure_board("Renders")
        self.calls.clear()

        self.assertEqual(instance.ensure_board("Renders"), "tok-1")
        self.assertEqual(self.calls, [("GET", "/s/boards/tok-1")])

    def test_a_renamed_board_is_resolved_again_instead_of_reused(self):
        # Cached "Renders" was renamed to "Archive" in the app. Writing into it because the
        # token still resolves would put this run somewhere nobody asked for.
        original = {"token": "tok-1", "title": "Renders", "tree_depth": 1}
        instance = self.playbook(
            [[original], []],
            created={"token": "tok-new", "title": "Renders", "tree_depth": 1},
            boards={"tok-1": {"token": "tok-1", "title": "Archive", "tree_depth": 1}},
        )
        instance.ensure_board("Renders")

        self.assertEqual(instance.ensure_board("Renders"), "tok-new")

    def test_a_same_named_board_in_another_project_is_not_reused(self):
        # "Renders" exists under project 1; this run asked for one under project 2.
        instance = client.Playbook()
        calls = self.calls

        def call(method, path, retry=True, **kwargs):
            calls.append((method, path))
            if path == "/s/boards/parent-2":
                return {"id": 2}
            if method == "GET":
                return [{"token": "tok-1", "title": "Renders", "parent_id": 1}]
            return {"token": "tok-new", "title": "Renders", "parent_id": 2}

        instance._call = call

        self.assertEqual(instance.ensure_board("Renders", parent="parent-2"), "tok-new")

    def test_with_no_parent_a_nested_board_of_that_name_is_not_reused(self):
        # "Client A / Renders" exists; this run asked for a root-level "Renders". Creating
        # is right -- reusing would file the output under someone else's project.
        instance = self.playbook(
            [[{"token": "nested", "title": "Renders", "parent_id": 7, "tree_depth": 2}]],
            created={"token": "tok-root", "title": "Renders", "tree_depth": 1},
        )

        self.assertEqual(instance.ensure_board("Renders"), "tok-root")

    def test_with_no_parent_a_root_board_of_that_name_is_reused(self):
        instance = self.playbook(
            [[{"token": "root", "title": "Renders", "parent_id": None, "tree_depth": 1}]]
        )

        self.assertEqual(instance.ensure_board("Renders"), "root")

    def test_a_partial_title_match_is_not_the_same_board(self):
        # The listing endpoint matches with ILIKE %query%, so "Renders" also returns
        # "Renders 2024". Creating is right here; reusing would be wrong.
        instance = self.playbook([[{"token": "tok-2", "title": "Renders 2024"}]],
                                 created={"token": "tok-new", "title": "Renders"})

        self.assertEqual(instance.ensure_board("Renders"), "tok-new")


class WorkspaceScoping(unittest.TestCase):
    """A wired key must not borrow the stored workspace -- but must still be usable
    before any workspace is known, which is exactly what the connect dialog does."""

    def setUp(self):
        import os

        os.environ.pop("PLAYBOOK_WORKSPACE", None)
        real = client.load
        client.load = lambda: {"api_key": "stored", "slug": "stored-workspace"}
        self.addCleanup(lambda: setattr(client, "load", real))

    def test_stored_key_uses_the_stored_workspace(self):
        self.assertEqual(client.Playbook().slug, "stored-workspace")

    def test_a_passed_key_does_not_inherit_it(self):
        self.assertIsNone(client.Playbook(api_key="wired").slug)

    def test_a_passed_key_with_no_workspace_still_constructs(self):
        # Regression: raising here broke Connect Playbook, whose whole job is to ask
        # /organizations which workspaces a fresh key can see.
        instance = client.Playbook(api_key="wired")

        self.assertEqual(instance.api_key, "wired")

    def test_workspace_scoped_paths_refuse_without_one(self):
        with self.assertRaises(client.PlaybookError):
            client.Playbook(api_key="wired")._org("/assets")


if __name__ == "__main__":
    unittest.main()
