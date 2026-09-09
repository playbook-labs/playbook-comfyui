"""HTTP endpoints behind the Connect Playbook and Pick board dialogs.

The key is posted here and stored server side; the browser only ever gets a mask back.
Nothing is written into ComfyUI's shared settings file, which any other extension can
read through GET /settings/{id}.

Like every custom node's routes these are unauthenticated and local to this ComfyUI, so
a ComfyUI exposed to a network exposes them too. That is the server's decision to make,
not this pack's -- which is why the key itself is never readable back through them.
"""

import asyncio
import os

from aiohttp import web
from server import PromptServer

from . import credentials
from .client import Playbook, PlaybookError

routes = PromptServer.instance.routes


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _fail(error):
    return web.json_response({"error": str(error)}, status=400)


def _is_local(request):
    forwarded = any(request.headers.get(header)
                    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"))
    return not forwarded and (request.remote or "") in LOOPBACK


@routes.get("/playbook/status")
async def status(request):
    creds = credentials.load()
    if not creds["api_key"]:
        return web.json_response({"connected": False})

    body = {
        "connected": True,
        "masked": credentials.mask(creds["api_key"]),
        "slug": creds["slug"],
        "from_env": bool(os.environ.get("PLAYBOOK_API_KEY")),
        "multi_user": credentials.multi_user(),
    }
    try:
        body["workspaces"] = await asyncio.to_thread(Playbook().organizations)
    except PlaybookError as error:
        body["error"] = str(error)
    return web.json_response(body)


@routes.post("/playbook/credentials")
async def connect(request):
    if not _is_local(request):
        return web.json_response(
            {"error": "Connect Playbook only works from the machine running ComfyUI. "
                      "On a remote server, set PLAYBOOK_API_KEY in the environment."},
            status=403,
        )

    body = await request.json()
    api_key = (body.get("api_key") or "").strip()
    slug = (body.get("slug") or "").strip() or None

    if not api_key:
        credentials.clear()
        return web.json_response({"connected": False})

    try:
        workspaces = await asyncio.to_thread(Playbook(api_key=api_key, slug=slug).organizations)
    except PlaybookError as error:
        return _fail(error)

    if not slug and len(workspaces or []) == 1:
        slug = workspaces[0].get("slug")

    if not slug:
        return web.json_response({"connected": False, "workspaces": workspaces})

    try:
        credentials.save(api_key, slug)
    except (PermissionError, OSError) as error:
        return _fail(error)
    return web.json_response({
        "connected": True,
        "masked": credentials.mask(api_key),
        "slug": slug,
        "workspaces": workspaces,
    })


@routes.get("/playbook/board")
async def board(request):
    """One board's name, so a node loaded from a saved workflow can show where it points
    without fetching every board in the workspace."""
    token = request.query.get("token") or ""
    if not token:
        return _fail("No board token given.")

    try:
        found = await asyncio.to_thread(Playbook().board, token)
    except PlaybookError as error:
        return _fail(error)

    return web.json_response({"token": token, "title": found.get("title") or "Untitled board"})


@routes.get("/playbook/boards")
async def boards(request):
    try:
        found = await asyncio.to_thread(Playbook().boards)
    except PlaybookError as error:
        return _fail(error)

    return web.json_response({
        "boards": [{"token": board["token"], "title": board.get("title") or "Untitled board"}
                   for board in found],
    })
