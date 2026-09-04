from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import io
import json
from typing import Dict, TypedDict, NewType, TYPE_CHECKING

import aiohttp

from utils import json_load, json_save
from constants import URLType, CACHE_PATH, CACHE_DB

from PIL import Image as Image_module
from PIL.ImageTk import PhotoImage


if TYPE_CHECKING:
    from gui import GUIManager
    from PIL.Image import Image
    from typing_extensions import TypeAlias


ImageHash = NewType("ImageHash", str)
ImageSize: TypeAlias = "tuple[int, int]"


class ExpiringHash(TypedDict):
    hash: ImageHash
    expires: datetime


Hashes = Dict[URLType, ExpiringHash]
default_database: Hashes = {}


class ImageCache:
    LIFETIME = timedelta(days=7)
    FETCH_TIMEOUT = 10  # seconds; images are non-critical, fail fast to a blank placeholder

    def __init__(self, manager: GUIManager) -> None:
        self._root = manager._root
        self._twitch = manager._twitch
        cleanup: bool = False
        CACHE_PATH.mkdir(parents=True, exist_ok=True)
        try:
            self._hashes: Hashes = json_load(CACHE_DB, default_database, merge=False)
        except json.JSONDecodeError:
            # if we can't load the mapping file, delete all existing files,
            # then reinitialize the image cache anew
            cleanup = True
            self._hashes = default_database.copy()
        self._images: OrderedDict[ImageHash, Image] = OrderedDict()
        self._photos: OrderedDict[tuple[ImageHash, ImageSize], PhotoImage] = OrderedDict()
        # RAM bound: the on-disk cache is fine to keep for days, but keeping every decoded
        # image and every resized PhotoImage in memory for a whole 24h+ session adds up.
        # Simple LRU-style cap: evict oldest entries once these grow past a small ceiling.
        self._MAX_IMAGES = 150
        self._MAX_PHOTOS = 150
        self._lock = asyncio.Lock()
        self._altered: bool = False
        # cleanup the URLs
        hash_counts: dict[ImageHash, int] = {}
        now = datetime.now(timezone.utc)
        for url, hash_dict in list(self._hashes.items()):
            img_hash = hash_dict["hash"]
            if img_hash not in hash_counts:
                hash_counts[img_hash] = 0
            if now >= hash_dict["expires"]:
                del self._hashes[url]
                self._altered = True
            else:
                hash_counts[img_hash] += 1
        for img_hash, count in hash_counts.items():
            if count == 0:
                # hashes come with an extension already
                CACHE_PATH.joinpath(img_hash).unlink(missing_ok=True)
                # NOTE: The hashes are deleted from self._hashes above
        if cleanup:
            # This cleanups the cache folder from unused PNG files
            orphans = [
                file.name for file in CACHE_PATH.glob("*.png") if file.name not in hash_counts
            ]
            for filename in orphans:
                CACHE_PATH.joinpath(filename).unlink(missing_ok=True)

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(CACHE_DB, self._hashes, sort=True)

    def _new_expires(self) -> datetime:
        return datetime.now(timezone.utc) + self.LIFETIME

    def _hash(self, image: Image) -> ImageHash:
        pixel_data = list(
            image.resize((10, 10), Image_module.Resampling.LANCZOS).convert('L').getdata()
        )
        avg_pixel = sum(pixel_data) / len(pixel_data)
        bits = ''.join('1' if px >= avg_pixel else '0' for px in pixel_data)
        return ImageHash(f"{int(bits, 2):x}.png")

    async def get(self, url: URLType, size: ImageSize | None = None) -> PhotoImage:
        # bug fix: image downloads used to go through Twitch.request(), which retries
        # forever (capped backoff, unlimited attempts) on connection errors, all while
        # holding self._lock for the whole download. One stalled image during a network
        # blip would then block every other image fetch behind it - freezing the whole
        # "adding campaigns" step, since it fires off one task per campaign concurrently.
        # Images aren't critical (a blank placeholder is an acceptable fallback), so they
        # get a short, bounded timeout instead, and the lock is released during the fetch.
        async with self._lock:
            image: Image | None = None
            if url in self._hashes:
                img_hash = self._hashes[url]["hash"]
                self._hashes[url]["expires"] = self._new_expires()
                if img_hash in self._images:
                    image = self._images[img_hash]
                    self._images.move_to_end(img_hash)
                else:
                    try:
                        loaded = Image_module.open(CACHE_PATH / img_hash)
                        loaded.load()  # force full decode so broken data is caught here
                        self._images[img_hash] = image = loaded
                        self._evict_if_needed(self._images, self._MAX_IMAGES)
                    except (FileNotFoundError, Image_module.UnidentifiedImageError, OSError):
                        pass
        if image is None:
            try:
                session = await self._twitch.get_session()
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=self.FETCH_TIMEOUT)
                ) as response:
                    if response.status != 404:
                        image = Image_module.open(io.BytesIO(await response.read()))
            except Exception:
                pass
            if image is None:
                # use a blank white image as a fallback
                image = Image_module.new("RGB", (10, 10), (255, 255, 255))
            async with self._lock:
                img_hash = self._hash(image)
                self._images[img_hash] = image
                self._evict_if_needed(self._images, self._MAX_IMAGES)
                image.save(CACHE_PATH / img_hash)
                self._hashes[url] = {
                    "hash": img_hash,
                    "expires": self._new_expires()
                }
        # NOTE: If self._hashes ever stops being updated in both above if cases,
        # this will need to be moved
        self._altered = True
        if size is None:
            size = image.size
        photo_key = (img_hash, size)
        async with self._lock:
            if photo_key in self._photos:
                self._photos.move_to_end(photo_key)
                return self._photos[photo_key]
            if image.size != size:
                try:
                    image = image.resize(size, Image_module.Resampling.LANCZOS)
                except OSError:
                    # broken image data surfaced during resize; fall back to blank placeholder
                    image = Image_module.new("RGB", size, (255, 255, 255))
            self._photos[photo_key] = photo = PhotoImage(master=self._root, image=image)
            self._evict_if_needed(self._photos, self._MAX_PHOTOS)
            return photo

    @staticmethod
    def _evict_if_needed(mapping: OrderedDict, max_size: int) -> None:
        # drops the least-recently-used entries once the cache grows past its cap;
        # explicitly closes PIL images so their decoded pixel buffer is freed right away
        # instead of waiting for a gc pass to notice they're unreferenced
        while len(mapping) > max_size:
            _key, value = mapping.popitem(last=False)
            close = getattr(value, "close", None)
            if close is not None:
                close()

    def trim(self, *, keep: int = 0) -> None:
        # drop almost everything from RAM (decoded images + resized PhotoImages);
        # the on-disk cache is untouched, so images just get reloaded/redecoded
        # next time they're needed instead of staying loaded for nothing
        self._evict_if_needed(self._images, keep)
        self._evict_if_needed(self._photos, keep)
