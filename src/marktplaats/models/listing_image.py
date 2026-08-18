from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from typing_extensions import Self

from marktplaats.utils import get_request


if TYPE_CHECKING:
    from marktplaats.api_types import Picture


@dataclass
class ListingFirstImage:
    """
    Data format for the listing image that marktplaats responds with when doing a search query.

    The get_images() method will not use this format, but instead return a list of URLs.
    """  # ruff:ignore[line-too-long] Line too long, we can't easily wrap it in the docstring

    extra_small: str
    medium: str
    large: str
    extra_large: str

    @classmethod
    def parse(cls, data: list[Picture] | None) -> list[Self]:
        if data is None:
            return []
        return [
            cls(
                image_data["extraSmallUrl"],
                image_data["mediumUrl"],
                image_data["largeUrl"],
                image_data["extraExtraLargeUrl"],
            )
            for image_data in data
        ]


def fetch_listing_images(listing_id: str) -> list[str]:
    """
    Return a list of image URLs for a given listing.

    It scrapes the listing page and parses the ld+json objects on that page.
    Returns an empty list if the listing has no photos.
    :param listing_id: The listing ID to get images for.
    :return: A list of image URLs (https).
    """  # ruff:ignore[docstring-missing-returns] TODO: all the docstrings are a bit inconsistent
    r = get_request(f"https://link.marktplaats.nl/{listing_id}")
    r.raise_for_status()  # raises so we can stop the fetching on a higher level

    soup = BeautifulSoup(r.text, "html.parser")

    images: list[str] = []

    # get the data objects from the HTML response
    for data in soup.select('script[type="application/ld+json"]'):
        parsed = json.loads(data.text)
        # the list of image URLs is hidden within the product object
        if type(parsed) is dict and parsed.get("@type") == "Product":
            raw_images = parsed.get("image", [])
            if isinstance(raw_images, str):
                raw_images = [raw_images]
            images.extend(
                image_url
                for image in raw_images
                if isinstance(image, str)
                and (image_url := _normalise_listing_image_url(image)) is not None
            )
            break

    return images


def _normalise_listing_image_url(url: str) -> str | None:
    """
    Accept real Marktplaats photo URLs while excluding placeholder images.

    Returns:
        An absolute image URL, or ``None`` when the URL is not a listing photo.

    """
    absolute_url = f"https:{url}" if url.startswith("//") else url
    parsed = urlparse(absolute_url)
    if parsed.scheme == "https" and parsed.hostname == "images.marktplaats.com":
        return absolute_url
    return None
