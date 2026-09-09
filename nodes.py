import io

import numpy as np
import torch
from comfy.utils import ProgressBar
from PIL import Image, ImageOps, UnidentifiedImageError

from .client import Playbook, PlaybookError
from .provenance import build as build_provenance

# Carried by assets that are images to Playbook but not rasters to Pillow.
UNREADABLE = {"image/svg+xml", "image/vnd.adobe.photoshop"}


def _to_png(tensor):
    array = np.clip(255.0 * tensor.cpu().numpy(), 0, 255).astype(np.uint8)
    image = Image.fromarray(array)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=4)
    return buffer.getvalue(), image.width, image.height


def _to_tensors(image):
    """IMAGE plus MASK, following LoadImage: the mask is the inverted alpha."""
    rgb = torch.from_numpy(np.array(image.convert("RGB")).astype(np.float32) / 255.0)

    if "A" in image.getbands():
        alpha = np.array(image.getchannel("A")).astype(np.float32) / 255.0
        mask = 1.0 - torch.from_numpy(alpha)
    else:
        mask = torch.zeros((image.height, image.width), dtype=torch.float32)

    return rgb, mask


class SaveToPlaybook:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "title": ("STRING", {"default": "ComfyUI render"}),
            },
            "optional": {
                "board": ("STRING", {"default": "", "tooltip": "Board token. Use 'Pick board' to fill it."}),
                "new_board_name": ("STRING", {"default": "", "tooltip":
                                              "Create a board with this name, or reuse the one that "
                                              "already has it. Nested under 'board' when that is set."}),
                "tags": ("STRING", {"default": "", "tooltip": "Comma separated."}),
                "status": ("STRING", {"default": "", "tooltip":
                                      "Asset status name, e.g. Approved. Matched by name, and a "
                                      "name that does not exist is CREATED in the workspace -- "
                                      "so a typo here adds a status, it does not fail."}),
                "description": ("STRING", {"default": "", "multiline": True}),
                "api_key": ("STRING", {"forceInput": True}),
                "workspace": ("STRING", {"forceInput": True,
                                         "tooltip": "Workspace slug. Required alongside api_key."}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("asset_tokens",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Playbook"
    DESCRIPTION = "Upload rendered images to a Playbook board."

    def save(self, images, title, board="", new_board_name="", tags="", status="", description="",
             api_key=None, workspace=None, prompt=None):
        client = Playbook(api_key=api_key or None, slug=workspace or None)
        # The key itself goes in as a known secret: it reaches this node through forceInput
        # from some other pack's widget, whose name we cannot predict but whose value we know.
        payload = build_provenance(prompt, secrets=[client.api_key])
        tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
        progress = ProgressBar(len(images))

        # Resolved once per run, not per image: every image of this batch, and every later
        # run naming the same board, lands in the one board rather than a new one each time.
        destination = board.strip() or None
        if new_board_name.strip():
            destination = client.ensure_board(new_board_name, parent=destination)

        # An emptied title would make the filename ".png", which has no extension as far as
        # mimetypes is concerned -- the asset would arrive as an untyped binary with no preview.
        stem = title.strip() or "ComfyUI render"

        tokens, failures, warnings = [], [], []
        for index, image in enumerate(images):
            name = f"{stem}.png" if len(images) == 1 else f"{stem} {index + 1:02d}.png"

            try:
                data, width, height = _to_png(image)
                asset = client.upload(
                    data,
                    title=name,
                    board=destination,
                    description=description or None,
                    width=width,
                    height=height,
                    agent_payload=payload,
                )

                token = asset["token"]
                tokens.append(token)
            except PlaybookError as error:
                failures.append(f"{name}: {error}")
                progress.update(1)
                continue

            # Separate from the upload: the asset exists either way, so a failure here is a
            # warning about metadata, not a lost render.
            try:
                if tag_list:
                    client.add_tags(token, tag_list)
                if status.strip():
                    client.set_status(token, status.strip())
            except PlaybookError as error:
                warnings.append(f"{name}: {error}")

            progress.update(1)

        if failures and not tokens:
            raise PlaybookError("; ".join(failures))
        if failures:
            print(f"[Playbook] uploaded {len(tokens)} of {len(images)}. Failed: {'; '.join(failures)}")
        if warnings:
            print(f"[Playbook] uploaded, but tags or status did not apply: {'; '.join(warnings)}")

        summary = f"Saved {len(tokens)} of {len(images)} to Playbook"
        if failures:
            summary += f" ({len(failures)} failed)"
        elif warnings:
            summary += f" ({len(warnings)} without tags or status)"

        return {"ui": {"playbook": [{"summary": summary, "tokens": tokens,
                                     "errors": failures + warnings}]},
                "result": (",".join(tokens),)}


class LoadFromPlaybook:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (["board", "search", "semantic search"],),
                "limit": ("INT", {"default": 4, "min": 1, "max": 64}),
            },
            "optional": {
                "query": ("STRING", {"default": ""}),
                "board": ("STRING", {"default": "", "tooltip": "Board token. Use 'Pick board' to fill it."}),
                "include_subboards": ("BOOLEAN", {"default": True}),
                "tags": ("STRING", {"default": "", "tooltip": "Comma separated."}),
                "statuses": ("STRING", {"default": "", "tooltip": "Comma separated, e.g. Approved."}),
                "match_size": ("BOOLEAN", {"default": True, "tooltip": "Resize everything to the first image so it batches."}),
                "originals": ("BOOLEAN", {"default": True, "tooltip":
                                          "Load the stored file rather than Playbook's WebP render. "
                                          "Costs one extra API request per asset; turn it off to "
                                          "save quota when exact pixels do not matter."}),
                "refresh": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF,
                                    "tooltip": "Bump to re-fetch. The board is a live source, but "
                                               "invalidating every run would re-run the whole graph."}),
                "api_key": ("STRING", {"forceInput": True}),
                "workspace": ("STRING", {"forceInput": True,
                                         "tooltip": "Workspace slug. Required alongside api_key."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "MASK", "STRING")
    RETURN_NAMES = ("images", "masks", "asset_tokens")
    FUNCTION = "load"
    CATEGORY = "Playbook"
    DESCRIPTION = "Pull assets from a Playbook board or search into the graph."

    def load(self, mode, limit, query="", board="", include_subboards=True, tags="", statuses="",
             match_size=True, originals=True, refresh=0, api_key=None, workspace=None):
        if mode == "board" and not board.strip():
            raise PlaybookError("Pick a board, or switch mode to search.")

        client = Playbook(api_key=api_key or None, slug=workspace or None)

        results = client.search(
            query="" if mode == "board" else query,
            board=board.strip() or None,
            recursive=include_subboards,
            tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
            statuses=[status.strip() for status in statuses.split(",") if status.strip()],
            limit=limit,
            semantic=(mode == "semantic search"),
        )

        images, masks, tokens, size = [], [], [], None
        progress = ProgressBar(min(limit, len(results)) or 1)

        skipped = []
        for item in results:
            media_type = item.get("media_type") or ""
            if not media_type.startswith("image/") or media_type in UNREADABLE:
                continue
            # The render is the fallback, not the default: it is a WebP transcode of the file.
            url = (client.original_url(item["token"]) if originals else None) or item.get("display_url")
            if not url:
                continue

            try:
                image = Image.open(io.BytesIO(client.fetch(url)))
                # Before anything measures or resizes it: a phone photo stores landscape
                # pixels with an orientation tag, and core LoadImage applies it too, so
                # without this the same file arrives sideways here and upright there.
                image = ImageOps.exif_transpose(image)
            except UnidentifiedImageError:
                skipped.append(item.get("title") or item["token"])
                continue
            if size is None:
                size = image.size
            elif image.size != size:
                if not match_size:
                    raise PlaybookError(
                        f"'{item.get('title')}' is {image.size[0]}x{image.size[1]}, the batch is "
                        f"{size[0]}x{size[1]}. Turn on match_size or narrow the search."
                    )
                image = image.resize(size, Image.LANCZOS)

            rgb, mask = _to_tensors(image)
            images.append(rgb)
            masks.append(mask)
            tokens.append(item["token"])
            progress.update(1)

            if len(images) >= limit:
                break

        if skipped:
            print(f"[Playbook] skipped {len(skipped)} asset(s) Pillow cannot read: {', '.join(skipped[:5])}")

        if not images:
            raise PlaybookError("Playbook returned no images for that board or query.")

        return (torch.stack(images), torch.stack(masks), ",".join(tokens))


NODE_CLASS_MAPPINGS = {
    "PlaybookSave": SaveToPlaybook,
    "PlaybookLoad": LoadFromPlaybook,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PlaybookSave": "Save to Playbook",
    "PlaybookLoad": "Load from Playbook",
}
