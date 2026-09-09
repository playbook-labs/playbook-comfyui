# Playbook for ComfyUI

Save renders straight into a [Playbook](https://www.playbook.com) board, and pull assets from
your workspace back into the graph.

## Nodes

**Save to Playbook** — takes an `IMAGE` batch, uploads each frame to a board, applies tags and a
status, and returns the asset tokens. Every upload is marked as AI generated and records how it
was made: the node count and the checkpoints and LoRAs used, plus the whole workflow when it fits
inside Playbook's 4 KB provenance limit. Anything in the graph that looks like a credential is
stripped before upload.

**Load from Playbook** — pulls images from a board, a keyword search, or a semantic search, and
returns an `IMAGE` batch, the matching `MASK` batch (inverted alpha, same as `LoadImage`), and the
asset tokens. Filter by board (with or without sub-boards), tags, and status, so a graph can be
fed only what the team has approved. Results are cached like any other node; bump **refresh** to
re-fetch a board that has changed.

## Setup

1. Install from ComfyUI-Manager (search for *Playbook*) or
   `comfy node registry-install playbook-creative`.
2. In the Playbook app, open **Developer → SDK** and create an API token.
3. Add a Playbook node to the graph and press **Connect Playbook**, then paste the token.

That is all — the key is validated before it is stored, you pick a workspace if your account has
more than one, and **Pick board** lists the boards you can write to.

### Headless installs

For Docker, RunPod and other setups with no browser, set the environment instead. Environment
variables win over the stored key.

```
PLAYBOOK_API_KEY=pb_...
PLAYBOOK_WORKSPACE=your-workspace-slug
```

## Where the key is kept

The key is never a widget on a node. Widget values are serialised into `workflow.json` and into
the `prompt` and `workflow` text chunks of every PNG ComfyUI saves, so a key typed onto a node
would travel with every render you share.

It is not a ComfyUI setting either: those live in `comfy.settings.json`, which every other
extension can read through the settings API. It is stored in ComfyUI's user directory
(`user/playbook/credentials.json`, mode `0600`), outside this folder, so updating the node never
wipes it.

That directory is shared between profiles, so on a `--multi-user` server the node refuses to store
a key at all — set `PLAYBOOK_API_KEY` in the environment there, or wire one into `api_key`. The browser only ever receives
a masked version. If you keep secrets in a manager already, wire your own string into the
`api_key` input — it is input-only for the same reason.

Tokens created on plans without full API access expire after 30 days; the node says so plainly
when that happens. Revoke a key any time in Developer → SDK.

## Licence

MIT
