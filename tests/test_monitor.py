from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from marktplaats.monitor import (
    ImageFingerprint,
    ReferenceImages,
    SavedListing,
    ViewedListingStore,
    jittered_poll_delay_ms,
    normalise_search_terms,
)


if TYPE_CHECKING:
    from pathlib import Path


def _test_image(
    *, foreground: str = "navy", size: tuple[int, int] = (320, 240)
) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((50, 40, 270, 200), fill=foreground)
    draw.ellipse((110, 70, 210, 170), fill="orange")
    return image


def _as_png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_identical_image_scores_one_hundred() -> None:
    fingerprint = ImageFingerprint.from_image(_test_image())

    assert fingerprint.similarity(fingerprint) == 100


def test_resized_image_remains_a_strong_match() -> None:
    image = _test_image()
    original = ImageFingerprint.from_image(image)
    resized = ImageFingerprint.from_image(image.resize((640, 480)))

    assert original.similarity(resized) >= 95


def test_visually_different_image_scores_lower() -> None:
    original = ImageFingerprint.from_image(_test_image())
    different = ImageFingerprint.from_image(Image.new("RGB", (320, 240), "lime"))

    assert original.similarity(different) < 60


def test_reference_images_returns_best_match(tmp_path: Path) -> None:
    reference_path = tmp_path / "reference.png"
    image = _test_image()
    image.save(reference_path)
    references = ReferenceImages.from_paths([reference_path])

    content = _as_png(image.resize((160, 120)))
    assert references.score_bytes(content) >= 95
    match = references.match_bytes(content)
    assert match is not None
    assert match.reference_path == reference_path


def test_search_terms_accept_spaces_and_commas() -> None:
    assert normalise_search_terms("  gazelle, fiets  blauw,heren ") == (
        "gazelle fiets blauw heren"
    )


def test_poll_delay_adds_bounded_jitter() -> None:
    assert jittered_poll_delay_ms(5, 0.0) == 240_000
    assert jittered_poll_delay_ms(5, 0.5) == 300_000
    assert jittered_poll_delay_ms(5, 1.0) == 360_000


def test_viewed_store_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "viewed.json"
    store = ViewedListingStore(state_path)

    store.set_viewed("m123", viewed=True)
    assert store.contains("m123")
    assert ViewedListingStore(state_path).contains("m123")

    store.set_viewed("m123", viewed=False)
    assert not ViewedListingStore(state_path).contains("m123")


def test_viewed_store_ignores_invalid_state(tmp_path: Path) -> None:
    state_path = tmp_path / "viewed.json"
    state_path.write_text("not json", encoding="utf-8")

    assert not ViewedListingStore(state_path).contains("m123")


def test_reference_stash_and_saved_lists_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    reference_path = tmp_path / "reference.png"
    listing = SavedListing(
        id="m123",
        title="Blue bicycle",
        description="Distinctive frame",
        price_text="€ 100.00",
        location="Amsterdam",
        date_text="2026-08-18",
        link="https://link.marktplaats.nl/m123",
        image_url="https://images.marktplaats.com/example",
    )
    store = ViewedListingStore(state_path)
    store.set_reference_paths([reference_path])
    store.create_collection("Possible matches")
    store.save_listing("Possible matches", listing)

    reloaded = ViewedListingStore(state_path)
    assert reloaded.reference_paths() == (reference_path,)
    assert reloaded.collection_names() == ("Possible matches",)
    assert reloaded.collection_items("Possible matches") == (listing,)

    reloaded.remove_listing("Possible matches", listing.id)
    assert reloaded.collection_items("Possible matches") == ()
    reloaded.delete_collection("Possible matches")
    assert reloaded.collection_names() == ()
