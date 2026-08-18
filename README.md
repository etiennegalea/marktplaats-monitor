# marktplaats-py
A Python package for requesting listings from marktplaats.nl, plus a native Rust
desktop monitor for reviewing and matching listings on Windows, macOS, and Linux.

## Native Rust desktop app

The desktop monitor is implemented in Rust and compiles to a standalone native
executable. It does not require Python, uv, or Tk at runtime.

To run it from source, install the current stable Rust toolchain and use:

```shell
cargo run --release
```

To build a distributable executable:

```shell
cargo build --locked --release
```

The resulting file is:

- Windows: `target/release/marktplaats-monitor.exe`
- macOS/Linux: `target/release/marktplaats-monitor`

Windows builds need the Visual Studio C++ Build Tools, macOS builds need the
Xcode Command Line Tools, and Linux builds need a compiler plus the Wayland/X11
development packages. The `rust-desktop.yml` GitHub Actions workflow builds and
packages all three platform executables automatically. Download the artifact for
your operating system from a completed workflow run and extract it before use.

The native app includes polling with randomized minute intervals, crossed-off
listing state, scrollable image previews, multiple conditions, reference-image
matching and ranking, saved lists, and an error/activity log. It can migrate the
existing Python GUI state from `~/.marktplaats-monitor/viewed.json` the first time
it starts.

**Search now** always starts a manual refresh immediately and does not wait for
the automatic poll timer. Leave **Maximum listings** blank to fetch every result
available from Marktplaats, or enter a limit; multi-page searches use randomized
delays between requests. Results can be sorted by visual-match relevance,
distance, the time the monitor first added them, or the listing creation date.

## Python library

The Python package supports Python 3.10+.

## Installing
```shell
pip install marktplaats
```

## Example
This is an example on how to use the library:
```py
from datetime import datetime, timedelta

from marktplaats import Condition, SearchQuery, SortBy, SortOrder, category_from_name

search = SearchQuery(
    query="gazelle",  # Search query. Can be left out, but then category must be specified.
    zip_code="1016LV",  # Zip code to base distance from
    distance_km=100,  # Max distance in kilometers from the zip code for listings
    price_from=0,  # Lowest price to search for
    price_to=100,  # Highest price to search for
    limit=5,  # Max listings (page size, max 100)
    offset=0,  # Offset for listings (page * limit)
    sort_by=SortBy.OPTIMIZED,  # DATE, PRICE, LOCATION, OPTIMIZED
    sort_order=SortOrder.ASC,  # ASCending or DESCending
    condition=Condition.NEW,  # NEW, AS_GOOD_AS_NEW, USED or category-specific
    offered_since=datetime.now() - timedelta(days=7),  # Filter listings since a point in time
    category=category_from_name("Fietsen en Brommers"),  # Filter in specific category (L1) or subcategory (L2)
)

listings = search.get_listings()

for listing in listings:
    print(listing.title)
    print(listing.description)
    print(listing.price)
    print(listing.price_as_string(lang="nl"))
    print(listing.price_type)
    print(listing.link)

    # the location object
    print(listing.location)

    # the seller object
    print(listing.seller)

    # the date object
    print(listing.date)

    # the full seller object (another request)
    print(listing.seller.get_seller())

    # medium-sized cover image
    print(listing.first_image.medium)

    # image urls for all the listing's image
    # (this sends another HTTP request)
    for image in listing.get_images():
        print(image)

    print("-----------------------------")
```

## Listing monitor GUI

An optional desktop monitor can repeatedly run a search, accumulate newly found
listings, and show image previews. Search terms can be separated with spaces or
commas, and multiple item conditions can be selected at once.

You can keep a persistent stash of reference photos. The monitor compares every
reference with up to ten photos from each listing, ranks results by the best
visual-similarity percentage, and shows the closest reference beside the listing
preview.

Install the GUI extra and start the monitor:

```shell
pip install "marktplaats[gui]"
marktplaats-monitor
```

Enter the search filters, use **Search now** for a single refresh, or enable
polling and choose an interval in whole minutes. To avoid synchronized request
bursts, every scheduled refresh is randomly jittered by 20% around that interval.
Results from later polls are added to the current result set.

Opening a preview automatically crosses that listing off. The selected row stays
visible while you review it, then is hidden when you move on unless **Show
crossed-off listings** is enabled. Listings can also be saved into persistent,
user-created lists. Crossed-off IDs, reference paths, saved lists, and listing
snapshots are stored in `~/.marktplaats-monitor/viewed.json`.
The **Save to list** dropdown shows every existing list and includes an option to
create a new list while saving.

A timestamped activity log at the bottom of the window reports searches,
scheduled polls, matching progress, saved-list actions, warnings, and errors.

The image percentage is a perceptual similarity hint, not an identification or
proof of ownership. Cropping, a different viewpoint, or a busy background can
lower the score, so review lower-scoring results too. Please use considerate
polling intervals and report suspected stolen goods through the appropriate
marketplace and local-authority channels.

## Seller
Query a seller by their ID. This allows fetching the seller's details and
all their listings.
The seller ID can be obtained from the Marktplaats website: copy it from a
seller's profile URL, e.g. `https://www.marktplaats.nl/u/johndoe/12345678/`.

```python
import pprint
from marktplaats import SellerQuery

seller = SellerQuery(seller_id=12345678)

details = seller.fetch_details()
pprint.pprint(details)

listings = seller.fetch_listings()
for listing in listings["items"]:
    pprint.pprint(listing)
    print("-" * 80)
```

## Categories
Filtering by Marktplaats category is possible. Please refer to the categories index at [CATEGORIES.md](./CATEGORIES.md)

The categories can also be used programmatically. Some usage examples:

```python
from marktplaats import L1Category, category_from_name, get_l1_categories, get_l2_categories, get_l2_categories_by_parent, get_subcategories

# List all level 1 categories.
for cat in get_l1_categories():
    print(cat.name, cat.id)  # E.g. `Antiek en Kunst 1`

# List all level 2 categories.
for cat in get_l2_categories():
    print(cat.name, cat.id, cat.parent)  # E.g. `Antiek | Bestek 2 Antiek en Kunst`

# Get a level 1 or 2 category by name.
vacation = category_from_name("Vakantie")
print(vacation.name, vacation.id)  # E.g. `Vakantie 856`

# List level 2 categories for a specific level 1 category.
books = L1Category(201, "Boeken")
for cat in get_subcategories(books):
    print(cat.name, cat.id, cat.parent)  # E.g. `Biografieën 205 Boeken`

# Map level 1 categories to their level 2 subcategories.
l1_to_l2_mapping = get_l2_categories_by_parent()
for l1_cat in l1_to_l2_mapping:
    print(l1_cat.name, l1_cat.id, "-" * 60)  # E.g. `Diversen 428 ------`
    for l2_cat in l1_to_l2_mapping[l1_cat]:
        print(l2_cat.name, l2_cat.id, l2_cat.parent)  # E.g. `Kerst 436 Diversen`
```
