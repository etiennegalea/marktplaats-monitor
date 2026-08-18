from __future__ import annotations

import queue
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from io import BytesIO
from pathlib import Path
from random import SystemRandom
from tkinter import (
    BooleanVar,
    Canvas,
    DoubleVar,
    Event,
    Listbox,
    Menu,
    Misc,
    StringVar,
    TclError,
    Text,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    simpledialog,
    ttk,
)
from typing import TYPE_CHECKING


try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError as error:  # pragma: no cover - depends on the local installation
    msg = (
        "The monitor GUI needs Pillow. Install it with "
        "`pip install 'marktplaats[gui]'`."
    )
    raise SystemExit(msg) from error

from marktplaats.monitor import (
    ImageFingerprint,
    InvalidImageError,
    ReferenceImages,
    ReferenceMatch,
    SavedListing,
    ViewedListingStore,
    jittered_poll_delay_ms,
    normalise_search_terms,
)
from marktplaats.query import Condition, SearchQuery, SortBy, SortOrder
from marktplaats.utils import get_request


if TYPE_CHECKING:
    from types import TracebackType

    from PIL.ImageTk import PhotoImage

    from marktplaats.models import Listing


_THUMBNAIL_SIZE = (112, 82)
_DETAIL_IMAGE_SIZE = (200, 190)
_DETAIL_WRAP_LENGTH = 420
_MAX_MATCH_IMAGES = 10
_RESULTS_PER_POLL = 50
_MINIMUM_POLL_MINUTES = 1
_POLL_JITTER = 0.2
_SEARCH_RESULTS = "Search results"
_SCROLL_UP_BUTTON = 4
_SCROLL_DOWN_BUTTON = 5

_CONDITIONS = {
    "New": Condition.NEW,
    "Refurbished": Condition.REFURBISHED,
    "As good as new": Condition.AS_GOOD_AS_NEW,
    "Used": Condition.USED,
    "Not working": Condition.NOT_WORKING,
}


@dataclass(frozen=True)
class _SearchOptions:
    query: str
    zip_code: str
    distance_km: int | None
    price_from: int | None
    price_to: int | None
    conditions: tuple[Condition, ...]


@dataclass(frozen=True)
class _SearchCompleted:
    listings: list[Listing]
    finished_at: datetime


@dataclass(frozen=True)
class _SearchFailed:
    message: str


@dataclass(frozen=True)
class _ThumbnailLoaded:
    listing_id: str
    content: bytes


@dataclass(frozen=True)
class _MatchCompleted:
    listing_id: str
    generation: int
    score: float | None
    reference_path: Path | None


@dataclass(frozen=True)
class _LogMessage:
    message: str
    level: str = "INFO"


_UiEvent = (
    _SearchCompleted | _SearchFailed | _ThumbnailLoaded | _MatchCompleted | _LogMessage
)


class MarktplaatsMonitorApp:
    """Tk desktop application for accumulating and reviewing search results."""

    def __init__(self, root: Tk) -> None:  # ruff: ignore[too-many-statements]
        self.root = root
        self.root.title("Marktplaats listing monitor")
        self.root.geometry("1220x860")
        self.root.minsize(960, 620)

        self.query_var = StringVar()
        self.zip_code_var = StringVar()
        self.distance_var = StringVar(value="100")
        self.price_from_var = StringVar()
        self.price_to_var = StringVar()
        self.condition_vars = {
            condition: BooleanVar(value=False) for condition in _CONDITIONS.values()
        }
        self.poll_enabled_var = BooleanVar(value=False)
        self.poll_interval_var = StringVar(value="5")
        self.show_crossed_off_var = BooleanVar(value=False)
        self.minimum_match_var = DoubleVar(value=0)
        self.status_var = StringVar(value="Ready")
        self.collection_var = StringVar(value=_SEARCH_RESULTS)
        self.selected_title_var = StringVar(value="Select a listing")
        self.selected_details_var = StringVar()
        self.matched_reference_var = StringVar(value="Closest reference")

        self._state = ViewedListingStore()
        self._references = self._load_saved_references()
        self.reference_label_var = StringVar(value=self._reference_label())
        self._random = SystemRandom()

        self._events: queue.Queue[_UiEvent] = queue.Queue()
        self._search_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="marktplaats-search",
        )
        self._media_executor = ThreadPoolExecutor(
            max_workers=6,
            thread_name_prefix="marktplaats-media",
        )
        self._listings: dict[str, Listing] = {}
        self._current_items: dict[str, Listing | SavedListing] = {}
        self._scores: dict[str, float | None] = {}
        self._matched_references: dict[str, Path | None] = {}
        self._score_requests: set[tuple[int, str]] = set()
        self._pending_matches: set[tuple[int, str]] = set()
        self._thumbnail_requests: set[str] = set()
        self._thumbnail_images: dict[str, PhotoImage] = {}
        self._detail_images: dict[str, PhotoImage] = {}
        self._reference_preview_images: dict[Path, PhotoImage] = {}
        self._reference_generation = 0
        self._search_running = False
        self._active_options: _SearchOptions | None = None
        self._previewed_listing_id: str | None = None
        self._poll_job: str | None = None
        self._closed = False

        self._configure_style()
        self._build_interface()
        self._placeholder_thumbnail = self._make_placeholder(_THUMBNAIL_SIZE)
        self._placeholder_detail = self._make_placeholder(_DETAIL_IMAGE_SIZE)
        self.listing_image_label.configure(image=self._placeholder_detail)
        self.reference_image_label.configure(image=self._placeholder_detail)
        self._refresh_reference_previews()
        self._refresh_collection_choices()
        self._append_log("Monitor ready.")
        if self._references.paths:
            self._append_log(
                f"Loaded {len(self._references.paths)} reference image(s) from state."
            )
        if self._state.collection_names():
            self._append_log(
                f"Loaded {len(self._state.collection_names())} saved list(s)."
            )
        self.root.report_callback_exception = self._report_callback_exception
        self.root.bind_all("<MouseWheel>", self._scroll_detail_preview, add="+")
        self.root.bind_all("<Button-4>", self._scroll_detail_preview, add="+")
        self.root.bind_all("<Button-5>", self._scroll_detail_preview, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(100, self._drain_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=_THUMBNAIL_SIZE[1] + 8)
        style.configure("Heading.TLabel", font=("TkDefaultFont", 16, "bold"))
        style.configure("Muted.TLabel", foreground="#666666")

    def _append_log(self, message: str, *, level: str = "INFO") -> None:
        log_level = level if level in {"INFO", "WARNING", "ERROR"} else "INFO"
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.status_log.configure(state="normal")
        self.status_log.insert(
            "end",
            f"[{timestamp}] {log_level:<7} {message}\n",
            log_level,
        )
        self.status_log.configure(state="disabled")
        self.status_log.see("end")

    def _set_status(self, message: str, *, level: str = "INFO") -> None:
        self.status_var.set(message)
        self._append_log(message, level=level)

    def _queue_log(self, message: str, *, level: str = "INFO") -> None:
        self._events.put(_LogMessage(message=message, level=level))

    def _show_error(self, title: str, error: object) -> None:
        message = str(error)
        self._set_status(message, level="ERROR")
        messagebox.showerror(title, message, parent=self.root)

    def _report_callback_exception(
        self,
        exception_type: type[BaseException],
        exception: BaseException,
        _traceback: TracebackType | None,
    ) -> object:
        self._show_error(
            "Unexpected application error",
            f"{exception_type.__name__}: {exception}",
        )
        return None

    def _load_saved_references(self) -> ReferenceImages:
        valid_paths = []
        fingerprints: list[ImageFingerprint] = []
        for path in self._state.reference_paths():
            if not path.is_file():
                continue
            try:
                reference = ReferenceImages.from_paths([path])
            except InvalidImageError:
                continue
            valid_paths.append(path)
            fingerprints.extend(reference.fingerprints)
        references = ReferenceImages(tuple(valid_paths), tuple(fingerprints))
        if tuple(self._state.reference_paths()) != references.paths:
            self._state.set_reference_paths(references.paths)
        return references

    def _reference_label(self) -> str:
        count = len(self._references.paths)
        if count == 0:
            return "No reference images"
        return f"{count} reference image{'s' if count != 1 else ''}"

    def _build_interface(  # ruff: ignore[too-many-locals, too-many-statements]
        self,
    ) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        search = ttk.LabelFrame(outer, text="Search and polling", padding=10)
        search.pack(fill="x")
        for column in range(10):
            search.columnconfigure(column, weight=1 if column in {0, 1} else 0)

        self._labelled_entry(
            search,
            "Search terms (spaces or commas)",
            self.query_var,
            0,
            0,
            width=28,
        )
        self._labelled_entry(search, "Postcode", self.zip_code_var, 0, 2, width=10)
        self._labelled_entry(search, "Radius (km)", self.distance_var, 0, 4, width=8)
        self._labelled_entry(search, "Minimum €", self.price_from_var, 0, 6, width=8)
        self._labelled_entry(search, "Maximum €", self.price_to_var, 0, 8, width=8)

        ttk.Label(search, text="Conditions (select any)").grid(
            row=2,
            column=0,
            columnspan=5,
            sticky="w",
            pady=(8, 2),
        )
        condition_frame = ttk.Frame(search)
        condition_frame.grid(row=3, column=0, columnspan=6, sticky="w")
        for label, condition in _CONDITIONS.items():
            ttk.Checkbutton(
                condition_frame,
                text=label,
                variable=self.condition_vars[condition],
            ).pack(side="left", padx=(0, 8))

        ttk.Checkbutton(
            search,
            text="Poll every",
            variable=self.poll_enabled_var,
            command=self._poll_setting_changed,
        ).grid(row=3, column=6, sticky="w")
        ttk.Entry(search, textvariable=self.poll_interval_var, width=7).grid(
            row=3,
            column=7,
            sticky="w",
        )
        ttk.Label(search, text="minutes (±20%)").grid(row=3, column=8, sticky="w")
        self.search_button = ttk.Button(search, text="Search now", command=self.refresh)
        self.search_button.grid(row=3, column=9, sticky="e")

        reference = ttk.Frame(outer, padding=(0, 10))
        reference.pack(fill="x")
        ttk.Button(
            reference,
            text="Add reference images…",
            command=self._choose_reference_images,
        ).pack(side="left")
        ttk.Button(reference, text="Clear images", command=self._clear_references).pack(
            side="left",
            padx=6,
        )
        ttk.Button(
            reference,
            text="View stash…",
            command=self._show_reference_stash,
        ).pack(
            side="left",
            padx=(0, 12),
        )
        ttk.Label(reference, textvariable=self.reference_label_var).pack(side="left")
        ttk.Label(reference, text="Match ≥").pack(side="left", padx=(24, 4))
        ttk.Spinbox(
            reference,
            from_=0,
            to=100,
            increment=5,
            textvariable=self.minimum_match_var,
            width=5,
            command=self._render_results,
        ).pack(side="left")
        ttk.Label(reference, text="%").pack(side="left")
        ttk.Button(reference, text="Apply", command=self._render_results).pack(
            side="left",
            padx=5,
        )
        ttk.Checkbutton(
            reference,
            text="Show crossed-off listings",
            variable=self.show_crossed_off_var,
            command=self._render_results,
        ).pack(side="right")

        collections = ttk.Frame(outer, padding=(0, 0, 0, 10))
        collections.pack(fill="x")
        ttk.Label(collections, text="View:").pack(side="left")
        self.collection_combo = ttk.Combobox(
            collections,
            textvariable=self.collection_var,
            state="readonly",
            width=28,
        )
        self.collection_combo.pack(side="left", padx=6)
        self.collection_combo.bind("<<ComboboxSelected>>", self._collection_changed)
        ttk.Button(
            collections,
            text="New list…",
            command=self._new_collection,
        ).pack(side="left")
        ttk.Button(
            collections,
            text="Delete list",
            command=self._delete_current_collection,
        ).pack(side="left", padx=6)

        content = ttk.Panedwindow(outer, orient="horizontal")
        content.pack(fill="both", expand=True)

        results_frame = ttk.Frame(content)
        details_host = ttk.Frame(content)
        content.add(results_frame, weight=3)
        content.add(details_host, weight=2)

        self.detail_canvas = Canvas(
            details_host,
            highlightthickness=0,
            borderwidth=0,
        )
        detail_scrollbar = ttk.Scrollbar(
            details_host,
            orient="vertical",
            command=self.detail_canvas.yview,
        )
        self.detail_canvas.configure(yscrollcommand=detail_scrollbar.set)
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        detail_scrollbar.pack(side="right", fill="y")
        details_frame = ttk.Frame(self.detail_canvas, padding=(14, 6))
        self._detail_window = self.detail_canvas.create_window(
            (0, 0),
            window=details_frame,
            anchor="nw",
        )
        details_frame.bind("<Configure>", self._update_detail_scrollregion)
        self.detail_canvas.bind("<Configure>", self._resize_detail_content)

        columns = ("title", "price", "location", "match", "date", "status")
        self.results = ttk.Treeview(
            results_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
        )
        self.results.heading("#0", text="Photo")
        self.results.column("#0", width=125, minwidth=125, stretch=False)
        headings = {
            "title": ("Listing", 260),
            "price": ("Price", 90),
            "location": ("Location", 120),
            "match": ("Match", 75),
            "date": ("Date", 90),
            "status": ("Status", 90),
        }
        for name, (label, width) in headings.items():
            self.results.heading(name, text=label)
            self.results.column(name, width=width, minwidth=60)
        self.results.tag_configure("crossed", foreground="#888888")
        self.results.bind("<<TreeviewSelect>>", self._selection_changed)
        scrollbar = ttk.Scrollbar(
            results_frame,
            orient="vertical",
            command=self.results.yview,
        )
        self.results.configure(yscrollcommand=scrollbar.set)
        self.results.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(
            details_frame,
            textvariable=self.selected_title_var,
            style="Heading.TLabel",
            wraplength=_DETAIL_WRAP_LENGTH,
        ).pack(anchor="w", pady=(0, 10))
        image_comparison = ttk.Frame(details_frame)
        image_comparison.pack(fill="x")
        listing_preview = ttk.Frame(image_comparison)
        listing_preview.pack(side="left", anchor="n")
        ttk.Label(listing_preview, text="Listing photo").pack(anchor="w")
        self.listing_image_label = ttk.Label(listing_preview)
        self.listing_image_label.pack(anchor="w")
        reference_preview = ttk.Frame(image_comparison)
        reference_preview.pack(side="left", anchor="n", padx=(10, 0))
        ttk.Label(reference_preview, textvariable=self.matched_reference_var).pack(
            anchor="w"
        )
        self.reference_image_label = ttk.Label(reference_preview)
        self.reference_image_label.pack(anchor="w")
        ttk.Label(
            details_frame,
            textvariable=self.selected_details_var,
            wraplength=_DETAIL_WRAP_LENGTH,
            justify="left",
        ).pack(anchor="w", fill="x", pady=10)
        buttons = ttk.Frame(details_frame)
        buttons.pack(anchor="w")
        self.cross_off_button = ttk.Button(
            buttons,
            text="Cross off",
            command=self._toggle_selected_viewed,
            state="disabled",
        )
        self.cross_off_button.pack(side="left")
        self.open_button = ttk.Button(
            buttons,
            text="Open on Marktplaats",
            command=self._open_selected,
            state="disabled",
        )
        self.open_button.pack(side="left", padx=8)
        self.save_button = ttk.Menubutton(
            buttons,
            text="Save to list",
            state="disabled",
        )
        self.save_menu = Menu(self.save_button, tearoff=False)
        self.save_button.configure(menu=self.save_menu)
        self.save_button.pack(side="left")
        self.remove_saved_button = ttk.Button(
            buttons,
            text="Remove from list",
            command=self._remove_selected_from_collection,
            state="disabled",
        )
        self.remove_saved_button.pack(side="left", padx=8)

        status = ttk.Frame(outer, padding=(0, 8, 0, 0))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(
            side="left"
        )

        log_frame = ttk.LabelFrame(outer, text="Activity log", padding=6)
        log_frame.pack(fill="x", pady=(6, 0))
        self.status_log = Text(
            log_frame,
            height=6,
            wrap="word",
            state="disabled",
            font=("TkFixedFont", 10),
        )
        log_scrollbar = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.status_log.yview,
        )
        self.status_log.configure(yscrollcommand=log_scrollbar.set)
        self.status_log.tag_configure("INFO", foreground="#333333")
        self.status_log.tag_configure("WARNING", foreground="#9a6700")
        self.status_log.tag_configure("ERROR", foreground="#b42318")
        self.status_log.pack(side="left", fill="both", expand=True)
        log_scrollbar.pack(side="right", fill="y")

    @staticmethod
    def _labelled_entry(  # ruff: ignore[too-many-arguments]
        parent: ttk.Widget,
        label: str,
        variable: StringVar,
        row: int,
        column: int,
        *,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=column,
            columnspan=2,
            sticky="w",
            pady=(0 if row == 0 else 8, 2),
        )
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row + 1,
            column=column,
            columnspan=2,
            sticky="ew",
            padx=(0, 10),
        )

    @staticmethod
    def _make_placeholder(size: tuple[int, int]) -> PhotoImage:
        image = Image.new("RGB", size, "#e7e9ec")
        draw = ImageDraw.Draw(image)
        draw.line((0, 0, *size), fill="#c0c4c8", width=2)
        draw.line((0, size[1], size[0], 0), fill="#c0c4c8", width=2)
        return ImageTk.PhotoImage(image)

    def _update_detail_scrollregion(self, _event: object | None = None) -> None:
        bounds = self.detail_canvas.bbox("all")
        if bounds is not None:
            self.detail_canvas.configure(scrollregion=bounds)

    def _resize_detail_content(self, _event: object | None = None) -> None:
        self.detail_canvas.itemconfigure(
            self._detail_window,
            width=self.detail_canvas.winfo_width(),
        )

    def _scroll_detail_preview(self, event: Event[Misc]) -> str | None:
        pointer_x = event.x_root
        pointer_y = event.y_root
        left = self.detail_canvas.winfo_rootx()
        top = self.detail_canvas.winfo_rooty()
        if not (
            left <= pointer_x <= left + self.detail_canvas.winfo_width()
            and top <= pointer_y <= top + self.detail_canvas.winfo_height()
        ):
            return None

        if event.num == _SCROLL_UP_BUTTON:
            direction = -1
        elif event.num == _SCROLL_DOWN_BUTTON:
            direction = 1
        elif event.delta:
            direction = -1 if event.delta > 0 else 1
        else:
            return None
        self.detail_canvas.yview_scroll(direction, "units")
        return "break"

    def _read_options(self) -> _SearchOptions:
        query = normalise_search_terms(self.query_var.get())
        if not query:
            msg = "Enter one or more search terms."
            raise ValueError(msg)

        distance = self._optional_integer(self.distance_var.get(), "Radius")
        price_from = self._optional_integer(self.price_from_var.get(), "Minimum price")
        price_to = self._optional_integer(self.price_to_var.get(), "Maximum price")
        if distance is not None and distance < 0:
            msg = "Radius cannot be negative."
            raise ValueError(msg)
        if price_from is not None and price_to is not None and price_from > price_to:
            msg = "Minimum price cannot be greater than maximum price."
            raise ValueError(msg)

        return _SearchOptions(
            query=query,
            zip_code=self.zip_code_var.get().strip(),
            distance_km=distance,
            price_from=price_from,
            price_to=price_to,
            conditions=tuple(
                condition
                for condition, variable in self.condition_vars.items()
                if variable.get()
            ),
        )

    @staticmethod
    def _optional_integer(value: str, field: str) -> int | None:
        if not value.strip():
            return None
        try:
            return int(value)
        except ValueError as error:
            msg = f"{field} must be a whole number."
            raise ValueError(msg) from error

    def refresh(self) -> None:
        if self._search_running:
            self._set_status("A search is already running…", level="WARNING")
            return
        try:
            options = self._read_options()
        except ValueError as error:
            self._show_error("Invalid search", error)
            return

        if self._active_options is not None and options != self._active_options:
            self._listings.clear()
            self._scores.clear()
            self._matched_references.clear()
            self._score_requests.clear()
            self._pending_matches.clear()
            self._reference_generation += 1
            self._thumbnail_requests.clear()
            self._thumbnail_images.clear()
            self._detail_images.clear()
            self._render_results()
            self._append_log("Search filters changed; cleared accumulated results.")
        self._active_options = options
        self._cancel_poll_job()
        self._search_running = True
        self.search_button.configure(state="disabled")
        self._set_status(f"Searching Marktplaats for “{options.query}”…")
        self._search_executor.submit(self._search_worker, options)

    def _search_worker(self, options: _SearchOptions) -> None:
        try:
            query = SearchQuery(
                options.query,
                zip_code=options.zip_code,
                distance_km=options.distance_km,
                price_from=options.price_from,
                price_to=options.price_to,
                limit=_RESULTS_PER_POLL,
                extra_attributes=[condition.value for condition in options.conditions]
                or None,
                sort_by=SortBy.DATE,
                sort_order=SortOrder.DESC,
            )
            event: _UiEvent = _SearchCompleted(
                listings=query.get_listings(),
                finished_at=datetime.now(),
            )
        except Exception as error:  # ruff: ignore[blind-except] reported in GUI
            event = _SearchFailed(str(error))
        self._events.put(event)

    def _handle_search_completed(self, event: _SearchCompleted) -> None:
        previous_ids = set(self._listings)
        current_results = {listing.id: listing for listing in event.listings}
        current_results.update(
            (listing_id, listing)
            for listing_id, listing in self._listings.items()
            if listing_id not in current_results
        )
        self._listings = current_results
        queued_matches = 0
        for listing in event.listings:
            self._request_thumbnail(listing)
            queued_matches += self._request_match(listing)
        new_count = len(set(self._listings) - previous_ids)
        self._search_running = False
        self.search_button.configure(state="normal")
        self._set_status(
            f"{len(event.listings)} found, {new_count} new; "
            f"{len(self._listings)} accumulated — updated {event.finished_at:%H:%M:%S}"
        )
        if queued_matches:
            self._append_log(
                f"Queued {queued_matches} new listings for reference-image matching."
            )
        self._render_results()
        self._schedule_next_poll()

    def _request_thumbnail(self, listing: Listing) -> None:
        image = listing.first_image
        if image is None or listing.id in self._thumbnail_requests:
            return
        self._thumbnail_requests.add(listing.id)
        self._media_executor.submit(
            self._download_thumbnail,
            listing.id,
            image.large,
        )

    def _download_thumbnail(self, listing_id: str, url: str) -> None:
        try:
            response = get_request(url)
            response.raise_for_status()
        except Exception as error:  # ruff: ignore[blind-except]
            self._queue_log(
                f"Thumbnail unavailable for {listing_id}: {error}",
                level="WARNING",
            )
            return
        self._events.put(_ThumbnailLoaded(listing_id, response.content))

    def _request_match(self, listing: Listing) -> bool:
        if not self._references.fingerprints:
            return False
        request_key = (self._reference_generation, listing.id)
        if request_key in self._score_requests:
            return False
        self._score_requests.add(request_key)
        self._pending_matches.add(request_key)
        self._media_executor.submit(
            self._score_listing,
            listing,
            self._references,
            self._reference_generation,
        )
        return True

    def _score_listing(
        self,
        listing: Listing,
        references: ReferenceImages,
        generation: int,
    ) -> None:
        page_error: Exception | None = None
        try:
            urls = listing.get_images()
        except Exception as error:  # ruff: ignore[blind-except]
            urls = []
            page_error = error
        if not urls and listing.first_image is not None:
            urls = [listing.first_image.extra_large]

        matches: list[ReferenceMatch] = []
        failed_images = 0
        for url in list(dict.fromkeys(urls))[:_MAX_MATCH_IMAGES]:
            try:
                response = get_request(url)
                response.raise_for_status()
                match = references.match_bytes(response.content)
            except Exception:  # ruff: ignore[blind-except]
                failed_images += 1
                continue
            if match is not None:
                matches.append(match)
        best_match = max(matches, key=lambda match: match.score, default=None)
        self._events.put(
            _MatchCompleted(
                listing_id=listing.id,
                generation=generation,
                score=best_match.score if best_match is not None else None,
                reference_path=(
                    best_match.reference_path if best_match is not None else None
                ),
            )
        )
        if page_error is not None:
            self._queue_log(
                f"Could not load all photos for {listing.id}; used its preview: "
                f"{page_error}",
                level="WARNING",
            )
        if failed_images:
            self._queue_log(
                f"Could not compare {failed_images} photo(s) for {listing.id}.",
                level="WARNING",
            )

    def _drain_events(self) -> None:
        if self._closed:
            return
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, _SearchCompleted):
                self._handle_search_completed(event)
            elif isinstance(event, _SearchFailed):
                self._search_running = False
                self.search_button.configure(state="normal")
                self._set_status(f"Search failed: {event.message}", level="ERROR")
                self._schedule_next_poll()
            elif isinstance(event, _ThumbnailLoaded):
                self._apply_thumbnail(event)
            elif isinstance(event, _LogMessage):
                self._append_log(event.message, level=event.level)
            else:
                self._pending_matches.discard((event.generation, event.listing_id))
                if event.generation == self._reference_generation:
                    self._scores[event.listing_id] = event.score
                    self._matched_references[event.listing_id] = event.reference_path
                    self._render_results()
                    if not any(
                        generation == self._reference_generation
                        for generation, _listing_id in self._pending_matches
                    ):
                        self._append_log("Reference-image matching complete.")
        self.root.after(100, self._drain_events)

    def _apply_thumbnail(self, event: _ThumbnailLoaded) -> None:
        try:
            with Image.open(BytesIO(event.content)) as image:
                image.load()
                prepared = ImageOps.fit(
                    ImageOps.exif_transpose(image).convert("RGB"),
                    _THUMBNAIL_SIZE,
                    Image.Resampling.LANCZOS,
                )
                thumbnail = ImageTk.PhotoImage(prepared)
                detail = ImageTk.PhotoImage(
                    ImageOps.contain(
                        ImageOps.exif_transpose(image).convert("RGB"),
                        _DETAIL_IMAGE_SIZE,
                        Image.Resampling.LANCZOS,
                    )
                )
        except (OSError, InvalidImageError) as error:
            self._append_log(
                f"Could not decode thumbnail for {event.listing_id}: {error}",
                level="WARNING",
            )
            return
        self._thumbnail_images[event.listing_id] = thumbnail
        self._detail_images[event.listing_id] = detail
        if self.results.exists(event.listing_id):
            self.results.item(event.listing_id, image=thumbnail)
        selected = self._selected_listing_id()
        if selected == event.listing_id:
            self.listing_image_label.configure(image=detail)

    def _items_for_current_view(self) -> list[Listing | SavedListing]:
        collection = self.collection_var.get()
        if collection == _SEARCH_RESULTS:
            return list(self._listings.values())
        return [
            self._listings.get(saved.id, saved)
            for saved in self._state.collection_items(collection)
        ]

    def _request_saved_thumbnail(self, listing: SavedListing) -> None:
        if listing.image_url is None or listing.id in self._thumbnail_requests:
            return
        self._thumbnail_requests.add(listing.id)
        self._media_executor.submit(
            self._download_thumbnail,
            listing.id,
            listing.image_url,
        )

    def _render_results(self) -> None:
        selected = self._selected_listing_id()
        try:
            minimum_match = max(0.0, min(100.0, self.minimum_match_var.get()))
        except TclError:
            minimum_match = 0.0
        items = self._items_for_current_view()
        if self._references.fingerprints:
            items.sort(
                key=lambda item: self._scores.get(item.id) or -1,
                reverse=True,
            )

        self._current_items = {item.id: item for item in items}
        self.results.delete(*self.results.get_children())
        for listing in items:
            listing_id = listing.id
            crossed_off = self._state.contains(listing_id)
            if (
                self.collection_var.get() == _SEARCH_RESULTS
                and crossed_off
                and not self.show_crossed_off_var.get()
                and listing_id != selected
            ):
                continue
            score = self._scores.get(listing_id)
            if score is not None and score < minimum_match:
                continue
            if minimum_match > 0 and listing_id in self._scores and score is None:
                continue
            match_text = (
                f"{score:.1f}%"
                if score is not None
                else "N/A"
                if listing_id in self._scores
                else "…"
                if self._references.fingerprints
                else "—"
            )
            if isinstance(listing, SavedListing):
                price_text = listing.price_text
                location = listing.location
                date_text = listing.date_text
                self._request_saved_thumbnail(listing)
            else:
                price_text = listing.price_as_string(lang="nl")
                location = listing.location.city or listing.location.country or "—"
                date_text = (
                    listing.date.isoformat() if listing.date is not None else "—"
                )
            self.results.insert(
                "",
                "end",
                iid=listing_id,
                image=self._thumbnail_images.get(
                    listing_id,
                    self._placeholder_thumbnail,
                ),
                values=(
                    listing.title,
                    price_text,
                    location,
                    match_text,
                    date_text,
                    "Crossed off" if crossed_off else "New",
                ),
                tags=("crossed",) if crossed_off else (),
            )
        if selected is not None and self.results.exists(selected):
            self.results.selection_set(selected)
            self.results.see(selected)
            self._selection_changed()
        else:
            self._clear_details()

    def _selection_changed(self, _event: object | None = None) -> None:
        listing_id = self._selected_listing_id()
        if listing_id is None:
            self._clear_details()
            return
        if listing_id != self._previewed_listing_id:
            self.detail_canvas.yview_moveto(0)
            self._previewed_listing_id = listing_id
        listing = self._current_items[listing_id]
        was_viewed = self._state.contains(listing_id)
        if not was_viewed:
            self._state.set_viewed(listing_id, viewed=True)
            self._append_log(f"Reviewed and crossed off: {listing.title}")
        score = self._scores.get(listing_id)
        if isinstance(listing, SavedListing):
            price_text = listing.price_text
            location = listing.location
        else:
            price_text = listing.price_as_string(lang="nl")
            location = (
                listing.location.city or listing.location.country or "Unknown location"
            )
        match = f"\nVisual match: {score:.1f}%" if score is not None else ""
        self.selected_title_var.set(listing.title)
        self.selected_details_var.set(
            f"{price_text} · {location}{match}\n\n{listing.description}"
        )
        detail = self._detail_images.get(listing_id)
        self.listing_image_label.configure(image=detail or self._placeholder_detail)
        reference_path = self._matched_references.get(listing_id)
        reference_image = (
            self._reference_preview_images.get(reference_path)
            if reference_path is not None
            else None
        )
        self.reference_image_label.configure(
            image=reference_image or self._placeholder_detail
        )
        self.matched_reference_var.set(
            f"Closest reference: {reference_path.name}"
            if reference_path is not None
            else "Closest reference"
        )
        self.cross_off_button.configure(
            text="Mark unreviewed",
            state="normal",
        )
        self.open_button.configure(state="normal")
        self.save_button.configure(state="normal")
        self.remove_saved_button.configure(
            state=(
                "normal" if self.collection_var.get() != _SEARCH_RESULTS else "disabled"
            )
        )
        if not was_viewed:
            self.root.after_idle(self._render_results)

    def _clear_details(self) -> None:
        self._previewed_listing_id = None
        self.detail_canvas.yview_moveto(0)
        self.selected_title_var.set("Select a listing")
        self.selected_details_var.set("")
        self.listing_image_label.configure(image=self._placeholder_detail)
        self.reference_image_label.configure(image=self._placeholder_detail)
        self.matched_reference_var.set("Closest reference")
        self.cross_off_button.configure(text="Cross off", state="disabled")
        self.open_button.configure(state="disabled")
        self.save_button.configure(state="disabled")
        self.remove_saved_button.configure(state="disabled")

    def _selected_listing_id(self) -> str | None:
        selection = self.results.selection()
        return selection[0] if selection else None

    def _toggle_selected_viewed(self) -> None:
        listing_id = self._selected_listing_id()
        if listing_id is None:
            return
        if self._state.contains(listing_id):
            self._state.set_viewed(listing_id, viewed=False)
            self.results.selection_remove(listing_id)
            self._clear_details()
            self._render_results()
            self._set_status(f"Marked listing {listing_id} as unreviewed.")
            return
        self._state.set_viewed(listing_id, viewed=True)
        self._set_status(f"Crossed off listing {listing_id}.")
        self._render_results()
        if self.results.exists(listing_id):
            self.results.selection_set(listing_id)
            self._selection_changed()
        else:
            self._clear_details()

    def _open_selected(self) -> None:
        listing_id = self._selected_listing_id()
        if listing_id is not None:
            self._append_log(f"Opening listing {listing_id} in the browser.")
            webbrowser.open(self._current_items[listing_id].link)

    def _refresh_collection_choices(self) -> None:
        choices = (_SEARCH_RESULTS, *self._state.collection_names())
        self.collection_combo.configure(values=choices)
        if self.collection_var.get() not in choices:
            self.collection_var.set(_SEARCH_RESULTS)
        self._refresh_save_menu()

    def _refresh_save_menu(self) -> None:
        collection_names = self._state.collection_names()
        self.save_menu.delete(0, "end")
        for collection in collection_names:
            self.save_menu.add_command(
                label=collection,
                command=partial(self._save_selected_to, collection),
            )
        if collection_names:
            self.save_menu.add_separator()
        self.save_menu.add_command(
            label="Create new list…",
            command=self._save_selected_to_new_collection,
        )

    def _collection_changed(self, _event: object | None = None) -> None:
        self._render_results()

    def _new_collection(self) -> None:
        name = simpledialog.askstring(
            "New list",
            "List name:",
            parent=self.root,
        )
        if name is None:
            return
        try:
            cleaned = self._state.create_collection(name)
        except ValueError as error:
            self._show_error("Invalid list name", error)
            return
        self._refresh_collection_choices()
        self.collection_var.set(cleaned)
        self._render_results()
        self._set_status(f"Created list “{cleaned}”.")

    def _delete_current_collection(self) -> None:
        collection = self.collection_var.get()
        if collection == _SEARCH_RESULTS:
            messagebox.showinfo(
                "Select a list",
                "Choose a saved list before deleting it.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Delete list",
            f"Delete the list “{collection}”? "
            "Saved listing snapshots in it will be removed.",
            parent=self.root,
        ):
            return
        self._state.delete_collection(collection)
        self.collection_var.set(_SEARCH_RESULTS)
        self._refresh_collection_choices()
        self._render_results()
        self._set_status(f"Deleted list “{collection}”.")

    def _save_selected_to(self, collection: str) -> None:
        listing_id = self._selected_listing_id()
        if listing_id is None:
            return
        try:
            self._state.save_listing(collection, self._current_items[listing_id])
        except ValueError as error:
            self._show_error("Could not save listing", error)
            return
        self._set_status(f"Saved listing {listing_id} to “{collection}”.")

    def _save_selected_to_new_collection(self) -> None:
        listing_id = self._selected_listing_id()
        if listing_id is None:
            return
        name = simpledialog.askstring(
            "Create list",
            "New list name:",
            parent=self.root,
        )
        if name is None:
            return
        try:
            collection = self._state.create_collection(name)
            self._state.save_listing(collection, self._current_items[listing_id])
        except ValueError as error:
            self._show_error("Could not save listing", error)
            return
        self._refresh_collection_choices()
        self._set_status(f"Saved listing {listing_id} to “{collection}”.")

    def _remove_selected_from_collection(self) -> None:
        listing_id = self._selected_listing_id()
        collection = self.collection_var.get()
        if listing_id is None or collection == _SEARCH_RESULTS:
            return
        self._state.remove_listing(collection, listing_id)
        self._render_results()
        self._set_status(f"Removed listing {listing_id} from “{collection}”.")

    def _set_references(self, paths: tuple[Path, ...]) -> bool:
        try:
            references = ReferenceImages.from_paths(paths)
        except InvalidImageError as error:
            self._show_error("Invalid image", error)
            return False
        self._references = references
        self._state.set_reference_paths(paths)
        self._reference_generation += 1
        self._scores.clear()
        self._matched_references.clear()
        self._score_requests.clear()
        self._pending_matches.clear()
        self.reference_label_var.set(self._reference_label())
        self._refresh_reference_previews()
        if references.paths:
            self._set_status(
                f"Reference stash updated: {len(references.paths)} image(s)."
            )
            queued_matches = 0
            for listing in self._listings.values():
                queued_matches += self._request_match(listing)
            if queued_matches:
                self._append_log(
                    f"Queued {queued_matches} accumulated listings for matching."
                )
        else:
            self._set_status("Reference image stash cleared.")
        self._render_results()
        return True

    def _choose_reference_images(self) -> None:
        chosen = filedialog.askopenfilenames(
            parent=self.root,
            title="Choose photos of the item",
            filetypes=(
                ("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff"),
                ("All files", "*"),
            ),
        )
        if not chosen:
            return
        paths = tuple(
            dict.fromkeys((*self._references.paths, *(Path(path) for path in chosen)))
        )
        self._set_references(paths)

    def _clear_references(self) -> None:
        self._set_references(())

    def _refresh_reference_previews(self) -> None:
        self._reference_preview_images.clear()
        for path in self._references.paths:
            try:
                with Image.open(path) as image:
                    image.load()
                    preview = ImageTk.PhotoImage(
                        ImageOps.contain(
                            ImageOps.exif_transpose(image).convert("RGB"),
                            _DETAIL_IMAGE_SIZE,
                            Image.Resampling.LANCZOS,
                        )
                    )
            except OSError:
                continue
            self._reference_preview_images[path] = preview

    def _show_reference_stash(self) -> None:
        window = Toplevel(self.root)
        window.title("Reference image stash")
        window.geometry("680x430")
        window.transient(self.root)
        body = ttk.Frame(window, padding=12)
        body.pack(fill="both", expand=True)
        paths = self._references.paths
        path_list = Listbox(body, width=55, exportselection=False)
        path_list.pack(side="left", fill="both", expand=True)
        for path in paths:
            path_list.insert("end", path.name)
        preview = ttk.Label(body, image=self._placeholder_detail)
        preview.pack(side="left", anchor="n", padx=(12, 0))

        def show_selected(_event: object | None = None) -> None:
            selection = path_list.curselection()  # type: ignore[no-untyped-call]
            if not selection:
                preview.configure(image=self._placeholder_detail)
                return
            image = self._reference_preview_images.get(paths[selection[0]])
            preview.configure(image=image or self._placeholder_detail)

        def remove_selected() -> None:
            selection = path_list.curselection()  # type: ignore[no-untyped-call]
            if not selection:
                return
            remaining = tuple(
                path for index, path in enumerate(paths) if index != selection[0]
            )
            if self._set_references(remaining):
                window.destroy()
                self._show_reference_stash()

        path_list.bind("<<ListboxSelect>>", show_selected)
        controls = ttk.Frame(window, padding=(12, 0, 12, 12))
        controls.pack(fill="x")
        ttk.Button(
            controls,
            text="Remove selected",
            command=remove_selected,
        ).pack(side="left")
        ttk.Button(controls, text="Close", command=window.destroy).pack(side="right")

    def _poll_setting_changed(self) -> None:
        self._cancel_poll_job()
        if not self.poll_enabled_var.get():
            self._set_status("Automatic polling disabled.")
            return
        try:
            self._poll_delay_ms()
        except ValueError as error:
            self.poll_enabled_var.set(False)
            self._show_error("Invalid polling interval", error)
            return
        self._append_log("Automatic polling enabled.")
        if not self._search_running:
            self.refresh()

    def _poll_delay_ms(self) -> int:
        try:
            minutes = int(self.poll_interval_var.get())
        except ValueError as error:
            msg = "Polling interval must be a whole number of minutes."
            raise ValueError(msg) from error
        if minutes < _MINIMUM_POLL_MINUTES:
            msg = f"Polling interval must be at least {_MINIMUM_POLL_MINUTES} minute."
            raise ValueError(msg)
        return jittered_poll_delay_ms(
            minutes,
            self._random.random(),
            jitter=_POLL_JITTER,
        )

    def _schedule_next_poll(self) -> None:
        self._cancel_poll_job()
        if not self.poll_enabled_var.get():
            return
        try:
            delay = self._poll_delay_ms()
        except ValueError as error:
            self.poll_enabled_var.set(False)
            self._set_status(str(error), level="ERROR")
            return
        self._poll_job = self.root.after(delay, self.refresh)
        self._set_status(f"Next poll in {delay / 60_000:.1f} minutes.")

    def _cancel_poll_job(self) -> None:
        if self._poll_job is not None:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None

    def _close(self) -> None:
        self._closed = True
        self._cancel_poll_job()
        self._search_executor.shutdown(wait=False, cancel_futures=True)
        self._media_executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main() -> None:
    """Start the Marktplaats listing monitor GUI."""
    root = Tk()
    MarktplaatsMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
