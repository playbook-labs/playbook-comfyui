"""Thin wrapper over the Playbook v1 REST API."""

import mimetypes
import os
import threading
import time

import requests

from .credentials import API_BASE, load

TIMEOUT = 120
RETRY_STATUS = {500, 502, 503, 504}
ATTEMPTS = 3

# Board name -> token, resolved once per process. See Playbook.ensure_board.
_BOARDS = {}
_BOARD_LOCK = threading.Lock()


class PlaybookError(RuntimeError):
    pass


def _send(request):
    """Three attempts with backoff, for transport failures and 5xx only.

    A render can take minutes; losing it to one dropped connection on the final PUT
    is the difference between a node people keep and one they uninstall.
    """
    delay = 1.0
    last = ""
    for attempt in range(ATTEMPTS):
        response = None
        try:
            response = request()
            if response.status_code not in RETRY_STATUS:
                return response
            last = f"HTTP {response.status_code}"
        except (requests.Timeout, requests.ConnectionError) as error:
            last = str(error)

        if attempt == ATTEMPTS - 1:
            if response is not None:
                return response
            raise PlaybookError(f"Playbook unreachable after {ATTEMPTS} attempts: {last}")

        time.sleep(delay)
        delay *= 2


def _meta(headers, name, value):
    """Blank metadata headers are omitted, never sent empty.

    The server signs this header set through `.compact` (playbook/server/lib/
    storage_locations/*.rb), so an unsigned or empty one invalidates the signature and
    the upload fails with an opaque 403.
    """
    if isinstance(value, str) and value:
        headers[name] = value


def _transfer(target, data, media_type):
    """Move the bytes to storage, per the provider's choreography.

    Not guessed and not taken from the public docs, which describe a plain PUT: this
    mirrors playbook-mcp/src/uploads/uploadRecipe.ts, which is the shipped description
    of the same endpoint, itself taken from the web uploader and the desktop daemon.

    No Authorization header on any leg -- the signed URL is the credential, and sending
    the Playbook bearer would hand our API token to Google or Backblaze.
    """
    provider = target.get("storage_provider")
    url = target.get("upload_url")

    if not provider:
        raise PlaybookError("Playbook did not say which storage provider to upload to.")

    if not url:
        raise PlaybookError(
            "Playbook returned a multipart upload, which this node does not implement. "
            "Save a file under 5 MB, or upload it through the app."
        )

    if provider == "backblaze":
        headers = {"Content-Type": media_type}
        _meta(headers, "x-amz-meta-extension", target.get("file_extension"))
        _meta(headers, "x-amz-meta-encrypted-organization-metadata",
              target.get("encrypted_organization_metadata"))

        sent = _send(lambda: requests.put(url, data=data, headers=headers, timeout=TIMEOUT))
        if not sent.ok:
            raise PlaybookError(f"Storage rejected the transfer (HTTP {sent.status_code}).")
        return

    if provider != "gcs":
        raise PlaybookError(f"Unsupported storage provider '{provider}'.")

    # GCS is a resumable upload: a POST opens a session, the bytes go to its Location.
    start = {"x-goog-resumable": "start", "Content-Type": media_type}
    _meta(start, "x-goog-meta-extension", target.get("file_extension"))
    _meta(start, "x-goog-meta-encrypted-organization-metadata",
          target.get("encrypted_organization_metadata"))

    size = len(data)
    # text/plain, not the file's media type: both shipped uploaders send it and the
    # session signature expects it. A zero-length body still needs a valid range.
    headers = {
        "Content-Type": "text/plain",
        "Content-Range": f"bytes 0-{size - 1}/{size}" if size else "bytes */0",
    }

    for attempt in range(ATTEMPTS):
        opened = _send(lambda: requests.post(url, data=b"", headers=start, timeout=TIMEOUT))
        if not opened.ok:
            raise PlaybookError(f"Storage refused to start the upload (HTTP {opened.status_code}).")

        session = opened.headers.get("location")
        if not session:
            raise PlaybookError("Storage started no upload session (no Location header).")

        try:
            sent = requests.put(session, data=data, headers=headers, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as error:
            if attempt == ATTEMPTS - 1:
                raise PlaybookError(f"Lost the connection sending the file: {error}") from error
            time.sleep(2 ** attempt)
            continue

        if sent.status_code in (200, 201):
            return

        retryable = sent.status_code == 308 or sent.status_code in RETRY_STATUS
        if not retryable or attempt == ATTEMPTS - 1:
            raise PlaybookError(f"Storage rejected the transfer (HTTP {sent.status_code}).")

        time.sleep(2 ** attempt)


class Playbook:
    def __init__(self, api_key=None, slug=None):
        creds = load()
        self.api_key = api_key or creds["api_key"]

        if api_key:
            self.slug = slug or os.environ.get("PLAYBOOK_WORKSPACE")
        else:
            self.slug = slug or creds["slug"]

        if not self.api_key:
            raise PlaybookError(
                "No Playbook API key. Press Connect Playbook on the node and paste a token "
                "from Developer -> SDK in the Playbook app, or set PLAYBOOK_API_KEY."
            )

        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    # -- plumbing ---------------------------------------------------------

    def _call(self, method, path, retry=True, **kwargs):
        def once():
            return self.session.request(method, f"{API_BASE}{path}", timeout=TIMEOUT, **kwargs)

        if retry:
            response = _send(once)
        else:
            try:
                response = once()
            except (requests.Timeout, requests.ConnectionError) as error:
                # Deliberately not retried, so the outcome is genuinely unknown: the
                # asset may already exist. Say so rather than implying it failed.
                raise PlaybookError(
                    f"The connection dropped during {path}, which is not safe to repeat. "
                    f"Check the board before running it again. ({error})"
                ) from error

        if response.status_code == 401:
            raise PlaybookError(
                "Playbook rejected the API key. On plans without full API access tokens "
                "expire after 30 days -- create a new one in Developer -> SDK."
            )
        if response.status_code == 429:
            raise PlaybookError(
                "Playbook API limit reached for this workspace. Wait for the quota to reset "
                "or upgrade the plan."
            )
        if response.status_code == 403:
            raise PlaybookError(f"Playbook refused the request: {response.text[:200]}")
        if not response.ok:
            raise PlaybookError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")

        if not response.content:
            return None
        return response.json().get("data")

    def _org(self, path):
        if not self.slug:
            raise PlaybookError(
                "No Playbook workspace selected. Press Connect Playbook on the node -- or, if "
                "you are wiring api_key yourself, wire the workspace input too."
            )
        return f"/{self.slug}{path}"

    # -- reads ------------------------------------------------------------

    def organizations(self):
        return self._call("GET", "/organizations")

    def boards(self, query=None, pages=10, per_page=100):
        """Every board, not just the first page.

        depth=all matters too: the endpoint defaults to DEFAULT_DEPTH = 1, i.e. top-level
        boards only, so without it a picker silently hides most of the workspace.
        """
        found, page = [], 1
        while page <= pages:
            params = [("depth", "all"), ("per_page", str(per_page)), ("page", str(page))]
            if query:
                params.append(("query", query))

            batch = self._call("GET", self._org("/boards"), params=params) or []
            found += batch
            if len(batch) < per_page:
                break
            page += 1

        return found

    def board(self, token):
        return self._call("GET", self._org(f"/boards/{token}"))

    def ensure_board(self, title, parent=None):
        """Find a board by name, or make one. Never a second board with the same name.

        Boards carry no uniqueness constraint (collections_controller.rb#create is a plain
        save!), so a generation loop that names its destination would otherwise leave one
        board per run. Resolution is cached per process and guarded by a lock, so a batch
        and the runs after it all land in the board the first one resolved.
        """
        title = title.strip()
        key = (self.slug, parent or "", title.lower())

        with _BOARD_LOCK:
            cached = _BOARDS.get(key)
            if cached:
                try:
                    current = self.board(cached)
                except PlaybookError:
                    current = None

                if current and (current.get("title") or "").strip().lower() == title.lower():
                    return cached
                _BOARDS.pop(key, None)

            parent_id = self.board(parent)["id"] if parent else None

            def same_board(board):
                if (board.get("title") or "").strip().lower() != title.lower():
                    return False
                if parent_id:
                    return board.get("parent_id") == parent_id
                return board.get("tree_depth") == 1

            match = next((board for board in self.boards(query=title) if same_board(board)), None)

            if match is None:
                body = {"collection": {"title": title}}
                if parent:
                    body["collection"]["parent_collection_token"] = parent
                # Not retried: a replayed create is exactly how duplicates appear.
                match = self._call("POST", self._org("/boards"), retry=False, json=body)

            if not isinstance(match, dict) or not match.get("token"):
                raise PlaybookError(f"Playbook did not return a board for '{title}'.")

            _BOARDS[key] = match["token"]
            return match["token"]

    def search(self, query="", board=None, recursive=True, tags=None, statuses=None,
               limit=20, semantic=False):
        params = [("query", query or "")]
        if not semantic:
            params += [("page", "1"), ("per_page", str(min(int(limit), 100)))]
        if board:
            params.append((f"filters[{'recursive_boards' if recursive else 'boards'}][]", board))
        for tag in tags or []:
            params.append(("filters[tags][]", tag))
        for status in statuses or []:
            params.append(("filters[statuses][]", status))

        path = "/ai_search" if semantic else "/search"
        return self._call("GET", self._org(path), params=params) or []

    def original_url(self, token):
        """The stored file, not a render.

        `display_url` is imgproxy output — Assets::GenerateURL defaults to WebP — so feeding
        it back into a graph silently re-encodes every asset. The only field carrying the
        original is `raw_url` from this endpoint: `download_url` on the asset itself is
        hardcoded to nil, and `source_url` is the ingest link, not our copy. The endpoint is
        deprecated and costs one API request per asset, which is why this is optional.
        """
        found = self._call("GET", self._org(f"/assets/{token}/download"))
        return (found or {}).get("raw_url")

    def fetch(self, url):
        response = _send(lambda: requests.get(url, timeout=TIMEOUT))
        if not response.ok:
            raise PlaybookError(f"Could not download asset: {response.status_code}")
        return response.content

    # -- writes -----------------------------------------------------------

    def upload(self, data, title, board=None, description=None, width=None, height=None,
               agent_payload=None):
        """Single-asset two-step upload: prepare -> transfer -> complete.

        Deliberately not the batch endpoints: `batch_upload_complete` is not atomic and
        must not be retried (a retry duplicates assets), so per-image calls give us
        independently reportable failures for the handful of images a graph produces.
        """
        media_type = mimetypes.guess_type(title)[0] or "application/octet-stream"
        size = len(data)

        prepared = self._call("POST", self._org("/assets/upload_prepare"), json={
            "asset": {"title": title, "media_type": media_type, "size": size},
        })

        _transfer(prepared, data, media_type)

        asset = {
            "signed_gcs_id": prepared["signed_gcs_id"],
            "title": title,
            "media_type": media_type,
            "size": size,
            "ai_generated": True,
        }
        if board:
            asset["collection_token"] = board
        if description:
            asset["description"] = description
        if width and height:
            asset["width"], asset["height"] = width, height
        if agent_payload:
            asset["ai_agent_payload"] = agent_payload

        created = self._call("POST", self._org("/assets/upload_complete"), retry=False,
                             json={"asset": asset})

        landed = created.get("collection_token") if isinstance(created, dict) else None
        if board and landed and landed != board:
            print(f"[Playbook] '{title}' went to board {landed}, not {board} -- that board "
                  "token was not recognised.")

        return created

    def add_tags(self, token, tags):
        return self._call("POST", self._org(f"/assets/{token}/change_tags"), json={"add_tags": tags})

    def set_status(self, token, status):
        return self._call("POST", self._org(f"/assets/{token}/update_status"), json={"status": status})
