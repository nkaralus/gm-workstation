"""Background token-portrait generation (Imagen 3 via google-genai).

Set GEMINI_API_KEY (or GOOGLE_API_KEY). Without a key, or without the package,
every call is a quiet no-op and the UI shows initials in the token instead.
Output: assets/media/<node_id>.png (1:1, cropped to a circle by the UI).
"""
from __future__ import annotations

import asyncio
import os

import database as dbm

MODEL = os.environ.get("IMAGEN_MODEL", "imagen-3.0-generate-002")
STYLE = (
    "Circular token portrait, head and shoulders, centered, painterly fantasy illustration, "
    "warm candlelight, muted earthy palette, dark plain background, no text, no border"
)
_sem = asyncio.Semaphore(2)
on_update = None            # optional async callable(node_id), set by the engine: lets players see a finished portrait


def _api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def available() -> bool:
    if not _api_key():
        return False
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False
    return True


async def generate_portrait(node_id: int, prompt: str) -> bool:
    if not available():
        return False
    from google import genai
    from google.genai import types

    full_prompt = f"{prompt.strip() or 'Fantasy tavern character'}. {STYLE}"
    await asyncio.to_thread(dbm.set_media, node_id, "pending", None, full_prompt)
    try:
        async with _sem:
            client = genai.Client(api_key=_api_key())
            resp = await client.aio.models.generate_images(
                model=MODEL,
                prompt=full_prompt,
                config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="1:1"),
            )
        data = resp.generated_images[0].image.image_bytes
        path = dbm.MEDIA_DIR / f"{node_id}.png"
        await asyncio.to_thread(path.write_bytes, data)
        await asyncio.to_thread(dbm.set_media, node_id, "ready", str(path), None)
        if on_update:
            try:
                await on_update(node_id)
            except Exception:
                pass
        return True
    except Exception as exc:
        print(f"[gemini] portrait {node_id} failed: {exc}")
        await asyncio.to_thread(dbm.set_media, node_id, "failed", None, None)
        return False


async def backfill() -> None:
    """Generate portraits for any token that has none yet."""
    if not available():
        return
    for node_id, name, prompt in await asyncio.to_thread(dbm.nodes_needing_portrait):
        await generate_portrait(node_id, prompt or name)
