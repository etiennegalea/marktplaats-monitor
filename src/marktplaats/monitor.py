from __future__ import annotations

import json
import math
import re
import threading
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

from PIL import Image, ImageOps, UnidentifiedImageError


if TYPE_CHECKING:
    from collections.abc import Iterable

    from marktplaats.models import Listing


_HASH_SIZE = 8
_PHASH_IMAGE_SIZE = 32
_HISTOGRAM_BUCKETS = 8


class InvalidImageError(ValueError):
    """Raised when an uploaded or downloaded file cannot be decoded as an image."""


def normalise_search_terms(value: str) -> str:
    """
    Normalise comma- or whitespace-separated search terms for Marktplaats.

    Returns:
        A single space-separated search string.

    """
    return " ".join(term for term in re.split(r"[\s,]+", value.strip()) if term)


def jittered_poll_delay_ms(
    minutes: int,
    unit_interval: float,
    *,
    jitter: float = 0.2,
) -> int:
    """
    Apply symmetric jitter to a polling interval.

    Returns:
        The jittered interval in milliseconds.

    """
    bounded_fraction = max(0.0, min(1.0, unit_interval))
    factor = (1 - jitter) + (2 * jitter * bounded_fraction)
    return round(minutes * 60_000 * factor)


@dataclass(frozen=True)
class ImageFingerprint:
    """A compact fingerprint combining structure, edges, and coarse colour."""

    perceptual_hash: int
    difference_hash: int
    colour_histogram: tuple[float, ...]

    @classmethod
    def from_image(cls, image: Image.Image) -> ImageFingerprint:
        prepared = ImageOps.exif_transpose(image).convert("RGB")
        return cls(
            perceptual_hash=_perceptual_hash(prepared),
            difference_hash=_difference_hash(prepared),
            colour_histogram=_colour_histogram(prepared),
        )

    @classmethod
    def from_bytes(cls, content: bytes) -> ImageFingerprint:
        try:
            with Image.open(BytesIO(content)) as image:
                image.load()
                return cls.from_image(image)
        except (OSError, UnidentifiedImageError) as error:
            msg = "The file could not be decoded as an image."
            raise InvalidImageError(msg) from error

    @classmethod
    def from_path(cls, path: str | Path) -> ImageFingerprint:
        try:
            with Image.open(path) as image:
                image.load()
                return cls.from_image(image)
        except (OSError, UnidentifiedImageError) as error:
            msg = f"{path!s} could not be decoded as an image."
            raise InvalidImageError(msg) from error

    def similarity(self, other: ImageFingerprint) -> float:
        """
        Compare two image fingerprints.

        Returns:
            A heuristic visual similarity score between 0 and 100.

        """
        perceptual = 1 - (
            (self.perceptual_hash ^ other.perceptual_hash).bit_count()
            / (_HASH_SIZE * _HASH_SIZE - 1)
        )
        difference = 1 - (
            (self.difference_hash ^ other.difference_hash).bit_count()
            / (_HASH_SIZE * _HASH_SIZE)
        )
        colour = 0.0
        for left, right in zip(
            self.colour_histogram,
            other.colour_histogram,
            strict=True,
        ):
            colour += min(left, right)
        colour /= 3

        structure = (0.7 * perceptual) + (0.3 * difference)
        # A colour penalty prevents structurally empty images with entirely different
        # colours from appearing identical, while still tolerating lighting changes.
        combined = math.sqrt(max(0.0, structure) * (0.15 + (0.85 * colour)))
        return round(max(0.0, min(1.0, combined)) * 100, 1)


@dataclass(frozen=True)
class ReferenceImages:
    """Uploaded reference images and their reusable fingerprints."""

    paths: tuple[Path, ...]
    fingerprints: tuple[ImageFingerprint, ...]

    @classmethod
    def from_paths(cls, paths: Iterable[str | Path]) -> ReferenceImages:
        resolved = tuple(Path(path) for path in paths)
        return cls(
            paths=resolved,
            fingerprints=tuple(ImageFingerprint.from_path(path) for path in resolved),
        )

    def score_bytes(self, content: bytes) -> float | None:
        match = self.match_bytes(content)
        return match.score if match is not None else None

    def match_bytes(self, content: bytes) -> ReferenceMatch | None:
        """
        Find the closest reference image for downloaded image data.

        Returns:
            The highest-scoring reference match, or ``None`` for an empty stash.

        """
        if not self.fingerprints:
            return None
        candidate = ImageFingerprint.from_bytes(content)
        return max(
            (
                ReferenceMatch(
                    score=reference.similarity(candidate),
                    reference_path=path,
                )
                for path, reference in zip(
                    self.paths,
                    self.fingerprints,
                    strict=True,
                )
            ),
            key=lambda match: match.score,
        )


@dataclass(frozen=True)
class ReferenceMatch:
    """The closest uploaded reference for one candidate image."""

    score: float
    reference_path: Path


@dataclass(frozen=True)
class SavedListing:
    """A persistent snapshot of the listing fields needed by the monitor UI."""

    id: str
    title: str
    description: str
    price_text: str
    location: str
    date_text: str
    link: str
    image_url: str | None

    @classmethod
    def from_listing(cls, listing: Listing) -> SavedListing:
        image = listing.first_image
        return cls(
            id=listing.id,
            title=listing.title,
            description=listing.description,
            price_text=listing.price_as_string(lang="nl"),
            location=listing.location.city or listing.location.country or "—",
            date_text=listing.date.isoformat() if listing.date is not None else "—",
            link=listing.link,
            image_url=image.large if image is not None else None,
        )

    @classmethod
    def parse(cls, value: object) -> SavedListing | None:
        if not isinstance(value, dict):
            return None
        required = (
            "id",
            "title",
            "description",
            "price_text",
            "location",
            "date_text",
            "link",
        )
        if not all(isinstance(value.get(field), str) for field in required):
            return None
        image_url = value.get("image_url")
        if image_url is not None and not isinstance(image_url, str):
            return None
        return cls(
            id=value["id"],
            title=value["title"],
            description=value["description"],
            price_text=value["price_text"],
            location=value["location"],
            date_text=value["date_text"],
            link=value["link"],
            image_url=image_url,
        )


class ViewedListingStore:
    """Persist crossed-off IDs, reference paths, and named saved-item lists."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else Path.home() / ".marktplaats-monitor" / "viewed.json"
        )
        self._lock = threading.Lock()
        data = self._load()
        raw_viewed = data.get("viewed")
        self._viewed = (
            {value for value in raw_viewed if isinstance(value, str)}
            if isinstance(raw_viewed, list)
            else set()
        )
        raw_references = data.get("reference_images")
        self._reference_paths = (
            [Path(value) for value in raw_references if isinstance(value, str)]
            if isinstance(raw_references, list)
            else []
        )
        self._collections = self._parse_collections(data.get("collections"))

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_collections(value: object) -> dict[str, dict[str, SavedListing]]:
        if not isinstance(value, dict):
            return {}
        collections: dict[str, dict[str, SavedListing]] = {}
        for name, raw_items in value.items():
            if not isinstance(name, str) or not isinstance(raw_items, list):
                continue
            items: dict[str, SavedListing] = {}
            for raw_item in raw_items:
                item = SavedListing.parse(raw_item)
                if item is not None:
                    items[item.id] = item
            collections[name] = items
        return collections

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "viewed": sorted(self._viewed),
                    "reference_images": [str(path) for path in self._reference_paths],
                    "collections": {
                        name: [asdict(item) for item in items.values()]
                        for name, items in self._collections.items()
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary_path.replace(self.path)

    def contains(self, listing_id: str) -> bool:
        with self._lock:
            return listing_id in self._viewed

    def set_viewed(self, listing_id: str, *, viewed: bool) -> None:
        with self._lock:
            if viewed:
                self._viewed.add(listing_id)
            else:
                self._viewed.discard(listing_id)
            self._save()

    def reference_paths(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(self._reference_paths)

    def set_reference_paths(self, paths: Iterable[str | Path]) -> None:
        with self._lock:
            self._reference_paths = [Path(path) for path in paths]
            self._save()

    def collection_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._collections)

    def create_collection(self, name: str) -> str:
        cleaned = name.strip()
        if not cleaned:
            msg = "List name cannot be empty."
            raise ValueError(msg)
        with self._lock:
            self._collections.setdefault(cleaned, {})
            self._save()
        return cleaned

    def delete_collection(self, name: str) -> None:
        with self._lock:
            self._collections.pop(name, None)
            self._save()

    def save_listing(self, collection: str, listing: Listing | SavedListing) -> None:
        saved = (
            listing
            if isinstance(listing, SavedListing)
            else SavedListing.from_listing(listing)
        )
        with self._lock:
            if collection not in self._collections:
                msg = f"Unknown list: {collection}"
                raise ValueError(msg)
            self._collections[collection][saved.id] = saved
            self._save()

    def remove_listing(self, collection: str, listing_id: str) -> None:
        with self._lock:
            items = self._collections.get(collection)
            if items is not None:
                items.pop(listing_id, None)
                self._save()

    def collection_items(self, name: str) -> tuple[SavedListing, ...]:
        with self._lock:
            return tuple(self._collections.get(name, {}).values())


def _perceptual_hash(image: Image.Image) -> int:
    pixels = list(
        image.convert("L")
        .resize((_PHASH_IMAGE_SIZE, _PHASH_IMAGE_SIZE), Image.Resampling.LANCZOS)
        .tobytes()
    )
    coefficients: list[float] = []
    factor = math.pi / (2 * _PHASH_IMAGE_SIZE)
    for vertical_frequency in range(_HASH_SIZE):
        for horizontal_frequency in range(_HASH_SIZE):
            coefficient = 0.0
            for y_position in range(_PHASH_IMAGE_SIZE):
                vertical_cosine = math.cos(
                    (2 * y_position + 1) * vertical_frequency * factor
                )
                row_offset = y_position * _PHASH_IMAGE_SIZE
                for x_position in range(_PHASH_IMAGE_SIZE):
                    coefficient += (
                        pixels[row_offset + x_position]
                        * math.cos((2 * x_position + 1) * horizontal_frequency * factor)
                        * vertical_cosine
                    )
            coefficients.append(coefficient)

    comparison = median(coefficients[1:])
    result = 0
    for coefficient in coefficients[1:]:
        result = (result << 1) | int(coefficient > comparison)
    return result


def _difference_hash(image: Image.Image) -> int:
    pixels = list(
        image.convert("L")
        .resize((_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS)
        .tobytes()
    )
    result = 0
    for row in range(_HASH_SIZE):
        offset = row * (_HASH_SIZE + 1)
        for column in range(_HASH_SIZE):
            result = (result << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return result


def _colour_histogram(image: Image.Image) -> tuple[float, ...]:
    histogram = image.resize((64, 64), Image.Resampling.BILINEAR).histogram()
    pixels_per_channel = 64 * 64
    bucket_width = 256 // _HISTOGRAM_BUCKETS
    buckets: list[float] = []
    for channel in range(3):
        channel_offset = channel * 256
        for bucket in range(_HISTOGRAM_BUCKETS):
            start = channel_offset + (bucket * bucket_width)
            buckets.append(
                sum(histogram[start : start + bucket_width]) / pixels_per_channel
            )
    return tuple(buckets)
