use std::cmp::Reverse;
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant};

use chrono::Local;
use eframe::egui::{self, ColorImage, RichText, TextureHandle};
use rand::RngExt;

use crate::api::MarktplaatsClient;
use crate::matching::{MatchResult, ReferenceImage, best_match, dimensions, load_rgba};
use crate::model::{Listing, SEARCH_RESULTS, SearchOptions, normalise_search_terms};
use crate::state::PersistentState;

const POLL_JITTER: f64 = 0.2;
const MAX_LOG_ENTRIES: usize = 500;

#[derive(Clone, Copy, PartialEq, Eq)]
enum SortMode {
    MatchRelevance,
    Distance,
    DateAdded,
    DateCreated,
}

impl SortMode {
    fn label(self) -> &'static str {
        match self {
            Self::MatchRelevance => "Match relevance",
            Self::Distance => "Distance (nearest)",
            Self::DateAdded => "Date added (newest)",
            Self::DateCreated => "Listing date (newest)",
        }
    }
}

struct ConditionChoice {
    label: &'static str,
    id: u32,
    selected: bool,
}

enum WorkerEvent {
    SearchFinished(Result<Vec<Listing>, String>),
    Thumbnail {
        listing_id: String,
        result: Result<Vec<u8>, String>,
    },
    MatchFinished {
        listing_id: String,
        generation: u64,
        result: Result<Option<MatchResult>, String>,
    },
}

pub struct MonitorApp {
    client: MarktplaatsClient,
    sender: Sender<WorkerEvent>,
    receiver: Receiver<WorkerEvent>,
    state: PersistentState,
    state_path: PathBuf,
    query: String,
    postcode: String,
    distance: String,
    price_from: String,
    price_to: String,
    maximum_listings: String,
    conditions: Vec<ConditionChoice>,
    poll_enabled: bool,
    poll_minutes: String,
    next_poll: Option<Instant>,
    show_crossed_off: bool,
    minimum_match: f32,
    sort_mode: SortMode,
    search_running: bool,
    active_options: Option<SearchOptions>,
    listings: HashMap<String, Listing>,
    listing_order: Vec<String>,
    selected: Option<String>,
    current_view: String,
    thumbnails: HashMap<String, TextureHandle>,
    thumbnail_pending: HashSet<String>,
    references: Vec<ReferenceImage>,
    reference_textures: HashMap<PathBuf, TextureHandle>,
    scores: HashMap<String, Option<MatchResult>>,
    match_pending: HashSet<(u64, String)>,
    reference_generation: u64,
    status: String,
    logs: VecDeque<String>,
    show_reference_stash: bool,
    show_new_list: bool,
    new_list_name: String,
    save_after_create: Option<String>,
    delete_list_confirmation: Option<String>,
}

impl MonitorApp {
    pub fn new(context: &eframe::CreationContext<'_>) -> Self {
        let (sender, receiver) = mpsc::channel();
        let (mut state, state_path) = PersistentState::load();
        let mut references = Vec::new();
        let mut reference_textures = HashMap::new();
        let mut valid_paths = Vec::new();
        for path in &state.reference_images {
            if let Ok(reference) = ReferenceImage::load(path) {
                if let Ok(texture) = texture_from_path(&context.egui_ctx, path) {
                    reference_textures.insert(path.clone(), texture);
                }
                valid_paths.push(path.clone());
                references.push(reference);
            }
        }
        state.reference_images = valid_paths;
        let mut app = Self {
            client: MarktplaatsClient::new().expect("HTTP client initialization failed"),
            sender,
            receiver,
            state,
            state_path,
            query: String::new(),
            postcode: String::new(),
            distance: "100".into(),
            price_from: String::new(),
            price_to: String::new(),
            maximum_listings: String::new(),
            conditions: vec![
                ConditionChoice {
                    label: "New",
                    id: 30,
                    selected: false,
                },
                ConditionChoice {
                    label: "Refurbished",
                    id: 14_050,
                    selected: false,
                },
                ConditionChoice {
                    label: "As good as new",
                    id: 31,
                    selected: false,
                },
                ConditionChoice {
                    label: "Used",
                    id: 32,
                    selected: false,
                },
                ConditionChoice {
                    label: "Not working",
                    id: 13_940,
                    selected: false,
                },
            ],
            poll_enabled: false,
            poll_minutes: "5".into(),
            next_poll: None,
            show_crossed_off: false,
            minimum_match: 0.0,
            sort_mode: SortMode::MatchRelevance,
            search_running: false,
            active_options: None,
            listings: HashMap::new(),
            listing_order: Vec::new(),
            selected: None,
            current_view: SEARCH_RESULTS.into(),
            thumbnails: HashMap::new(),
            thumbnail_pending: HashSet::new(),
            references,
            reference_textures,
            scores: HashMap::new(),
            match_pending: HashSet::new(),
            reference_generation: 0,
            status: "Ready".into(),
            logs: VecDeque::new(),
            show_reference_stash: false,
            show_new_list: false,
            new_list_name: String::new(),
            save_after_create: None,
            delete_list_confirmation: None,
        };
        app.log("INFO", "Monitor ready.");
        if !app.references.is_empty() {
            app.log(
                "INFO",
                format!("Loaded {} reference image(s).", app.references.len()),
            );
        }
        if !app.state.collections.is_empty() {
            app.log(
                "INFO",
                format!("Loaded {} saved list(s).", app.state.collections.len()),
            );
        }
        app
    }

    fn log(&mut self, level: &str, message: impl AsRef<str>) {
        let line = format!(
            "[{}] {level:<7} {}",
            Local::now().format("%H:%M:%S"),
            message.as_ref()
        );
        self.logs.push_back(line);
        while self.logs.len() > MAX_LOG_ENTRIES {
            self.logs.pop_front();
        }
    }

    fn set_status(&mut self, level: &str, message: impl Into<String>) {
        self.status = message.into();
        let status = self.status.clone();
        self.log(level, status);
    }

    fn save_state(&mut self) {
        if let Err(error) = self.state.save(&self.state_path) {
            self.set_status("ERROR", error);
        }
    }

    fn read_search_options(&self) -> Result<SearchOptions, String> {
        let query = normalise_search_terms(&self.query);
        if query.is_empty() {
            return Err("Enter one or more search terms.".into());
        }
        let distance_km = optional_number(&self.distance, "Radius")?;
        let price_from = optional_number(&self.price_from, "Minimum price")?;
        let price_to = optional_number(&self.price_to, "Maximum price")?;
        let maximum_listings = optional_number(&self.maximum_listings, "Maximum listings")?
            .map(|maximum| maximum as usize);
        if maximum_listings == Some(0) {
            return Err("Maximum listings must be at least one when specified.".into());
        }
        if price_from.zip(price_to).is_some_and(|(from, to)| from > to) {
            return Err("Minimum price cannot be greater than maximum price.".into());
        }
        Ok(SearchOptions {
            query,
            postcode: self.postcode.trim().into(),
            distance_km,
            price_from,
            price_to,
            conditions: self
                .conditions
                .iter()
                .filter(|condition| condition.selected)
                .map(|condition| condition.id)
                .collect(),
            maximum_listings,
        })
    }

    fn start_search(&mut self, automatic: bool) {
        if self.search_running {
            self.set_status("WARNING", "A search is already running…");
            return;
        }
        let options = match self.read_search_options() {
            Ok(options) => options,
            Err(error) => {
                self.set_status("ERROR", error);
                return;
            }
        };
        self.next_poll = None;
        self.log(
            "INFO",
            if automatic {
                "Starting scheduled automatic search."
            } else {
                "Starting manual search without waiting for the poll timer."
            },
        );
        if self
            .active_options
            .as_ref()
            .is_some_and(|active| active != &options)
        {
            self.listings.clear();
            self.listing_order.clear();
            self.selected = None;
            self.thumbnails.clear();
            self.thumbnail_pending.clear();
            self.scores.clear();
            self.match_pending.clear();
            self.log(
                "INFO",
                "Search filters changed; cleared accumulated results.",
            );
        }
        self.active_options = Some(options.clone());
        self.search_running = true;
        self.set_status(
            "INFO",
            format!("Searching Marktplaats for “{}”…", options.query),
        );
        let client = self.client.clone();
        let sender = self.sender.clone();
        rayon::spawn(move || {
            let _ = sender.send(WorkerEvent::SearchFinished(client.search(&options)));
        });
    }

    fn schedule_next_poll(&mut self) {
        if !self.poll_enabled {
            self.next_poll = None;
            return;
        }
        let Ok(minutes) = self.poll_minutes.trim().parse::<u64>() else {
            self.poll_enabled = false;
            self.set_status(
                "ERROR",
                "Polling interval must be a whole number of minutes.",
            );
            return;
        };
        if minutes < 1 {
            self.poll_enabled = false;
            self.set_status("ERROR", "Polling interval must be at least one minute.");
            return;
        }
        let factor = rand::rng().random_range((1.0 - POLL_JITTER)..=(1.0 + POLL_JITTER));
        let delay = Duration::from_secs_f64(minutes as f64 * 60.0 * factor);
        self.next_poll = Some(Instant::now() + delay);
        self.set_status(
            "INFO",
            format!("Next poll in {:.1} minutes.", delay.as_secs_f64() / 60.0),
        );
    }

    fn process_events(&mut self, context: &egui::Context) {
        while let Ok(event) = self.receiver.try_recv() {
            match event {
                WorkerEvent::SearchFinished(Ok(found)) => self.search_completed(found),
                WorkerEvent::SearchFinished(Err(error)) => {
                    self.search_running = false;
                    self.set_status("ERROR", error);
                    self.schedule_next_poll();
                }
                WorkerEvent::Thumbnail { listing_id, result } => {
                    self.thumbnail_pending.remove(&listing_id);
                    match result.and_then(|bytes| texture_from_bytes(context, &listing_id, &bytes))
                    {
                        Ok(texture) => {
                            self.thumbnails.insert(listing_id, texture);
                        }
                        Err(error) => self.log(
                            "WARNING",
                            format!("Thumbnail unavailable for {listing_id}: {error}"),
                        ),
                    }
                }
                WorkerEvent::MatchFinished {
                    listing_id,
                    generation,
                    result,
                } => {
                    self.match_pending.remove(&(generation, listing_id.clone()));
                    if generation != self.reference_generation {
                        continue;
                    }
                    match result {
                        Ok(result) => {
                            self.scores.insert(listing_id, result);
                        }
                        Err(error) => {
                            self.scores.insert(listing_id.clone(), None);
                            self.log(
                                "WARNING",
                                format!("Image matching failed for {listing_id}: {error}"),
                            );
                        }
                    }
                }
            }
        }
    }

    fn search_completed(&mut self, found: Vec<Listing>) {
        let previous: HashSet<String> = self.listings.keys().cloned().collect();
        let mut latest_order = Vec::with_capacity(found.len() + self.listing_order.len());
        for mut listing in found.iter().cloned() {
            if let Some(existing) = self.listings.get(&listing.id) {
                listing.added_sort = existing.added_sort;
            }
            latest_order.push(listing.id.clone());
            self.listings.insert(listing.id.clone(), listing);
        }
        let latest_ids: HashSet<String> = latest_order.iter().cloned().collect();
        latest_order.extend(
            self.listing_order
                .iter()
                .filter(|id| !latest_ids.contains(*id))
                .cloned(),
        );
        self.listing_order = latest_order;
        for listing in &found {
            self.request_thumbnail(listing);
            self.request_match(listing);
        }
        let new_count = self
            .listings
            .keys()
            .filter(|id| !previous.contains(*id))
            .count();
        self.search_running = false;
        self.set_status(
            "INFO",
            format!(
                "{} found, {new_count} new; {} accumulated.",
                found.len(),
                self.listings.len()
            ),
        );
        self.schedule_next_poll();
    }

    fn request_thumbnail(&mut self, listing: &Listing) {
        if self.thumbnails.contains_key(&listing.id) || self.thumbnail_pending.contains(&listing.id)
        {
            return;
        }
        let Some(url) = listing.image_url.clone() else {
            return;
        };
        self.thumbnail_pending.insert(listing.id.clone());
        let listing_id = listing.id.clone();
        let client = self.client.clone();
        let sender = self.sender.clone();
        rayon::spawn(move || {
            let result = client.download(&url);
            let _ = sender.send(WorkerEvent::Thumbnail { listing_id, result });
        });
    }

    fn request_match(&mut self, listing: &Listing) {
        if self.references.is_empty() {
            return;
        }
        let key = (self.reference_generation, listing.id.clone());
        if self.match_pending.contains(&key) || self.scores.contains_key(&listing.id) {
            return;
        }
        self.match_pending.insert(key.clone());
        let listing = listing.clone();
        let references = Arc::new(self.references.clone());
        let client = self.client.clone();
        let sender = self.sender.clone();
        rayon::spawn(move || {
            let mut best: Option<MatchResult> = None;
            let urls = client.listing_image_urls(&listing);
            let mut last_error = None;
            for url in urls {
                match client
                    .download(&url)
                    .and_then(|bytes| best_match(&bytes, &references))
                {
                    Ok(Some(candidate))
                        if best
                            .as_ref()
                            .is_none_or(|current| candidate.score > current.score) =>
                    {
                        best = Some(candidate);
                    }
                    Ok(_) => {}
                    Err(error) => last_error = Some(error),
                }
            }
            let result = if best.is_some() {
                Ok(best)
            } else if let Some(error) = last_error {
                Err(error)
            } else {
                Ok(None)
            };
            let _ = sender.send(WorkerEvent::MatchFinished {
                listing_id: listing.id,
                generation: key.0,
                result,
            });
        });
    }

    fn replace_references(&mut self, context: &egui::Context, paths: Vec<PathBuf>) {
        let mut references = Vec::new();
        let mut valid_paths = Vec::new();
        for path in paths {
            if valid_paths.contains(&path) {
                continue;
            }
            match ReferenceImage::load(&path) {
                Ok(reference) => {
                    if !self.reference_textures.contains_key(&path) {
                        match texture_from_path(context, &path) {
                            Ok(texture) => {
                                self.reference_textures.insert(path.clone(), texture);
                            }
                            Err(error) => self.log("WARNING", error),
                        }
                    }
                    valid_paths.push(path);
                    references.push(reference);
                }
                Err(error) => self.log("ERROR", error),
            }
        }
        self.references = references;
        self.state.reference_images = valid_paths;
        self.reference_generation += 1;
        self.scores.clear();
        self.match_pending.clear();
        self.save_state();
        let listings: Vec<Listing> = self.listings.values().cloned().collect();
        for listing in &listings {
            self.request_match(listing);
        }
        self.set_status(
            "INFO",
            format!(
                "Reference stash updated: {} image(s).",
                self.references.len()
            ),
        );
    }

    fn visible_listings(&self) -> Vec<Listing> {
        let mut listings = if self.current_view == SEARCH_RESULTS {
            self.listing_order
                .iter()
                .filter_map(|id| self.listings.get(id).cloned())
                .collect::<Vec<_>>()
        } else {
            self.state
                .collections
                .get(&self.current_view)
                .into_iter()
                .flatten()
                .map(|saved| {
                    self.listings
                        .get(&saved.id)
                        .cloned()
                        .unwrap_or_else(|| saved.into())
                })
                .collect()
        };
        match self.sort_mode {
            SortMode::MatchRelevance => listings.sort_by(|left, right| {
                score_for(&self.scores, &right.id).total_cmp(&score_for(&self.scores, &left.id))
            }),
            SortMode::Distance => listings.sort_by(|left, right| {
                left.distance_km
                    .unwrap_or(f32::INFINITY)
                    .total_cmp(&right.distance_km.unwrap_or(f32::INFINITY))
            }),
            SortMode::DateAdded => {
                listings.sort_by_key(|listing| Reverse(listing.added_sort));
            }
            SortMode::DateCreated => {
                listings.sort_by_key(|listing| Reverse(listing.created_sort));
            }
        }
        listings
            .into_iter()
            .filter(|listing| {
                let selected = self.selected.as_deref() == Some(&listing.id);
                let visible_by_viewed = self.current_view != SEARCH_RESULTS
                    || self.show_crossed_off
                    || !self.state.viewed.contains(&listing.id)
                    || selected;
                let visible_by_score = if self.references.is_empty() {
                    true
                } else {
                    match self.scores.get(&listing.id) {
                        Some(Some(result)) => result.score >= self.minimum_match,
                        Some(None) => self.minimum_match <= 0.0,
                        None => true,
                    }
                };
                visible_by_viewed && visible_by_score
            })
            .collect()
    }

    fn select_listing(&mut self, id: String) {
        self.selected = Some(id.clone());
        if self.state.viewed.insert(id.clone()) {
            self.save_state();
            self.log("INFO", format!("Reviewed and crossed off listing {id}."));
        }
    }

    fn save_listing_to(&mut self, listing_id: &str, collection: &str) {
        let Some(listing) = self.listings.get(listing_id).cloned().or_else(|| {
            self.state
                .collections
                .values()
                .flatten()
                .find(|listing| listing.id == listing_id)
                .map(Listing::from)
        }) else {
            self.set_status("ERROR", "The selected listing is no longer available.");
            return;
        };
        match self.state.save_listing(collection, &listing) {
            Ok(()) => {
                self.save_state();
                self.set_status(
                    "INFO",
                    format!("Saved listing {listing_id} to “{collection}”."),
                );
            }
            Err(error) => self.set_status("ERROR", error),
        }
    }

    fn create_list(&mut self) {
        match self.state.create_collection(&self.new_list_name) {
            Ok(name) => {
                if let Some(listing_id) = self.save_after_create.take() {
                    self.save_listing_to(&listing_id, &name);
                } else {
                    self.save_state();
                    self.current_view = name.clone();
                    self.set_status("INFO", format!("Created list “{name}”."));
                }
                self.new_list_name.clear();
                self.show_new_list = false;
            }
            Err(error) => self.set_status("ERROR", error),
        }
    }

    fn top_search_panel(&mut self, root: &mut egui::Ui) {
        egui::Panel::top("search-panel").show(root, |ui| {
            ui.add_space(6.0);
            ui.horizontal_wrapped(|ui| {
                field(ui, "Search terms", &mut self.query, 230.0);
                field(ui, "Postcode", &mut self.postcode, 85.0);
                field(ui, "Radius km", &mut self.distance, 70.0);
                field(ui, "Minimum €", &mut self.price_from, 70.0);
                field(ui, "Maximum €", &mut self.price_to, 70.0);
                field(
                    ui,
                    "Maximum listings (blank = all)",
                    &mut self.maximum_listings,
                    105.0,
                );
                if ui
                    .add_enabled(!self.search_running, egui::Button::new("Search now"))
                    .clicked()
                {
                    self.start_search(false);
                }
            });
            ui.horizontal_wrapped(|ui| {
                ui.label("Conditions:");
                for condition in &mut self.conditions {
                    ui.checkbox(&mut condition.selected, condition.label);
                }
                ui.separator();
                let changed = ui.checkbox(&mut self.poll_enabled, "Poll every").changed();
                ui.add(egui::TextEdit::singleline(&mut self.poll_minutes).desired_width(45.0));
                ui.label("minutes (±20%)");
                if changed {
                    if self.poll_enabled {
                        self.start_search(true);
                    } else {
                        self.next_poll = None;
                        self.set_status("INFO", "Automatic polling disabled.");
                    }
                }
            });
            ui.add_space(6.0);
        });
    }

    fn reference_panel(&mut self, root: &mut egui::Ui, context: &egui::Context) {
        let mut add_references = false;
        let mut clear_references = false;
        egui::Panel::top("reference-panel").show(root, |ui| {
            ui.horizontal_wrapped(|ui| {
                add_references = ui.button("Add reference images…").clicked();
                clear_references = ui
                    .add_enabled(
                        !self.references.is_empty(),
                        egui::Button::new("Clear images"),
                    )
                    .clicked();
                if ui.button("View stash…").clicked() {
                    self.show_reference_stash = true;
                }
                ui.label(format!("{} reference image(s)", self.references.len()));
                ui.separator();
                ui.label("Match ≥");
                ui.add(egui::Slider::new(&mut self.minimum_match, 0.0..=100.0).suffix("%"));
                ui.label("Sort:");
                egui::ComboBox::from_id_salt("result-sort")
                    .selected_text(self.sort_mode.label())
                    .show_ui(ui, |ui| {
                        for mode in [
                            SortMode::MatchRelevance,
                            SortMode::Distance,
                            SortMode::DateAdded,
                            SortMode::DateCreated,
                        ] {
                            ui.selectable_value(&mut self.sort_mode, mode, mode.label());
                        }
                    });
                ui.checkbox(&mut self.show_crossed_off, "Show crossed-off listings");
            });
        });
        if add_references
            && let Some(paths) = rfd::FileDialog::new()
                .set_title("Choose photos of the item")
                .add_filter(
                    "Images",
                    &["jpg", "jpeg", "png", "webp", "bmp", "gif", "tif", "tiff"],
                )
                .pick_files()
        {
            let mut combined = self.state.reference_images.clone();
            combined.extend(paths);
            self.replace_references(context, combined);
        }
        if clear_references {
            self.replace_references(context, Vec::new());
        }
    }

    fn collection_panel(&mut self, root: &mut egui::Ui) {
        egui::Panel::top("collection-panel").show(root, |ui| {
            ui.horizontal(|ui| {
                ui.label("View:");
                egui::ComboBox::from_id_salt("collection-view")
                    .selected_text(&self.current_view)
                    .show_ui(ui, |ui| {
                        ui.selectable_value(
                            &mut self.current_view,
                            SEARCH_RESULTS.into(),
                            SEARCH_RESULTS,
                        );
                        for name in self.state.collections.keys() {
                            ui.selectable_value(&mut self.current_view, name.clone(), name);
                        }
                    });
                if ui.button("New list…").clicked() {
                    self.new_list_name.clear();
                    self.save_after_create = None;
                    self.show_new_list = true;
                }
                if ui
                    .add_enabled(
                        self.current_view != SEARCH_RESULTS,
                        egui::Button::new("Delete list"),
                    )
                    .clicked()
                {
                    self.delete_list_confirmation = Some(self.current_view.clone());
                }
            });
        });
    }

    fn detail_panel(&mut self, root: &mut egui::Ui) {
        let selected = self.selected.as_ref().and_then(|id| {
            self.visible_listings()
                .into_iter()
                .find(|listing| &listing.id == id)
        });
        let mut mark_unreviewed = false;
        let mut remove_from_current = false;
        let mut save_to: Option<String> = None;
        let mut create_and_save = false;
        egui::Panel::right("detail-panel")
            .resizable(true)
            .default_size(410.0)
            .min_size(300.0)
            .show(root, |ui| {
                egui::ScrollArea::vertical()
                    .id_salt("detail-scroll")
                    .show(ui, |ui| {
                        let Some(listing) = selected.as_ref() else {
                            ui.heading("Select a listing");
                            ui.label("The full preview will appear here.");
                            return;
                        };
                        ui.heading(&listing.title);
                        ui.add_space(6.0);
                        ui.horizontal(|ui| {
                            image_or_placeholder(
                                ui,
                                self.thumbnails.get(&listing.id),
                                egui::vec2(190.0, 180.0),
                            );
                            let matched_path = self
                                .scores
                                .get(&listing.id)
                                .and_then(|result| result.as_ref())
                                .map(|result| &result.reference_path);
                            let texture =
                                matched_path.and_then(|path| self.reference_textures.get(path));
                            image_or_placeholder(ui, texture, egui::vec2(190.0, 180.0));
                        });
                        ui.horizontal(|ui| {
                            ui.label("Listing photo");
                            ui.separator();
                            let closest = self
                                .scores
                                .get(&listing.id)
                                .and_then(|result| result.as_ref())
                                .and_then(|result| result.reference_path.file_name())
                                .and_then(|name| name.to_str())
                                .unwrap_or("Closest reference");
                            ui.label(closest);
                        });
                        ui.separator();
                        ui.label(
                            RichText::new(format!("{} · {}", listing.price_text, listing.location))
                                .strong(),
                        );
                        ui.label(listing.distance_km.map_or_else(
                            || "Distance: unknown".into(),
                            |distance| format!("Distance: {distance:.1} km"),
                        ));
                        match self.scores.get(&listing.id) {
                            Some(Some(result)) => {
                                ui.label(format!("Visual match: {:.1}%", result.score))
                            }
                            Some(None) => ui.label("Visual match: N/A"),
                            None if !self.references.is_empty() => {
                                ui.label("Visual match: calculating…")
                            }
                            None => ui.label("Visual match: add reference images to compare"),
                        };
                        ui.label(format!("Listed: {}", listing.date_text));
                        ui.add_space(8.0);
                        ui.label(&listing.description);
                        ui.add_space(12.0);
                        ui.horizontal_wrapped(|ui| {
                            if ui.button("Mark unreviewed").clicked() {
                                mark_unreviewed = true;
                            }
                            ui.hyperlink_to("Open on Marktplaats", &listing.link);
                            ui.menu_button("Save to list", |ui| {
                                for name in self.state.collections.keys() {
                                    if ui.button(name).clicked() {
                                        save_to = Some(name.clone());
                                        ui.close();
                                    }
                                }
                                if !self.state.collections.is_empty() {
                                    ui.separator();
                                }
                                if ui.button("Create new list…").clicked() {
                                    create_and_save = true;
                                    ui.close();
                                }
                            });
                            if self.current_view != SEARCH_RESULTS
                                && ui.button("Remove from list").clicked()
                            {
                                remove_from_current = true;
                            }
                        });
                    });
            });
        let Some(listing) = selected else { return };
        if mark_unreviewed {
            self.state.viewed.remove(&listing.id);
            self.selected = None;
            self.save_state();
            self.set_status(
                "INFO",
                format!("Marked listing {} as unreviewed.", listing.id),
            );
        }
        if let Some(collection) = save_to {
            self.save_listing_to(&listing.id, &collection);
        }
        if create_and_save {
            self.new_list_name.clear();
            self.save_after_create = Some(listing.id.clone());
            self.show_new_list = true;
        }
        if remove_from_current
            && let Some(items) = self.state.collections.get_mut(&self.current_view)
        {
            items.retain(|item| item.id != listing.id);
            self.selected = None;
            self.save_state();
            self.set_status(
                "INFO",
                format!(
                    "Removed listing {} from “{}”.",
                    listing.id, self.current_view
                ),
            );
        }
    }

    fn results_panel(&mut self, root: &mut egui::Ui) {
        let visible = self.visible_listings();
        let mut selected = None;
        egui::CentralPanel::default().show(root, |ui| {
            ui.horizontal(|ui| {
                ui.strong("Listings");
                ui.label(format!("{} shown", visible.len()));
                if self.search_running {
                    ui.spinner();
                }
            });
            ui.separator();
            egui::ScrollArea::vertical().show(ui, |ui| {
                for listing in &visible {
                    let is_selected = self.selected.as_deref() == Some(&listing.id);
                    ui.horizontal(|ui| {
                        image_or_placeholder(
                            ui,
                            self.thumbnails.get(&listing.id),
                            egui::vec2(112.0, 82.0),
                        );
                        ui.vertical(|ui| {
                            let response = ui.selectable_label(
                                is_selected,
                                RichText::new(&listing.title).strong(),
                            );
                            if response.clicked() {
                                selected = Some(listing.id.clone());
                            }
                            ui.label(format!(
                                "{} · {} · {}",
                                listing.price_text,
                                listing.location,
                                listing.distance_km.map_or_else(
                                    || "distance unknown".into(),
                                    |distance| format!("{distance:.1} km")
                                )
                            ));
                            let match_text = match self.scores.get(&listing.id) {
                                Some(Some(result)) => format!("Match {:.1}%", result.score),
                                Some(None) => "Match N/A".into(),
                                None if !self.references.is_empty() => "Match …".into(),
                                None => "No reference match".into(),
                            };
                            let status = if self.state.viewed.contains(&listing.id) {
                                "Crossed off"
                            } else {
                                "New"
                            };
                            ui.label(format!(
                                "{} · {} · {}",
                                listing.date_text, match_text, status
                            ));
                        });
                    });
                    ui.separator();
                }
            });
        });
        if let Some(id) = selected {
            self.select_listing(id);
        }
    }

    fn status_panel(&mut self, root: &mut egui::Ui) {
        egui::Panel::bottom("status-log")
            .resizable(true)
            .default_size(125.0)
            .min_size(65.0)
            .show(root, |ui| {
                ui.horizontal(|ui| {
                    ui.strong("Status:");
                    ui.label(&self.status);
                });
                ui.separator();
                ui.set_width(ui.available_width());
                egui::ScrollArea::vertical()
                    .auto_shrink([false, false])
                    .stick_to_bottom(true)
                    .show(ui, |ui| {
                        for line in &self.logs {
                            let text = if line.contains(" ERROR ") {
                                RichText::new(line).color(egui::Color32::LIGHT_RED)
                            } else if line.contains(" WARNING ") {
                                RichText::new(line).color(egui::Color32::YELLOW)
                            } else {
                                RichText::new(line).monospace()
                            };
                            ui.label(text);
                        }
                    });
            });
    }

    fn auxiliary_windows(&mut self, context: &egui::Context) {
        if self.show_reference_stash {
            let mut open = true;
            let mut remove = None;
            egui::Window::new("Reference image stash")
                .open(&mut open)
                .resizable(true)
                .show(context, |ui| {
                    egui::ScrollArea::vertical().show(ui, |ui| {
                        for path in &self.state.reference_images {
                            ui.horizontal(|ui| {
                                image_or_placeholder(
                                    ui,
                                    self.reference_textures.get(path),
                                    egui::vec2(120.0, 100.0),
                                );
                                ui.vertical(|ui| {
                                    ui.label(
                                        path.file_name()
                                            .and_then(|name| name.to_str())
                                            .unwrap_or("Reference image"),
                                    );
                                    ui.small(path.display().to_string());
                                    if ui.button("Remove").clicked() {
                                        remove = Some(path.clone());
                                    }
                                });
                            });
                            ui.separator();
                        }
                    });
                });
            self.show_reference_stash = open;
            if let Some(path) = remove {
                let paths = self
                    .state
                    .reference_images
                    .iter()
                    .filter(|candidate| **candidate != path)
                    .cloned()
                    .collect();
                self.replace_references(context, paths);
            }
        }

        if self.show_new_list {
            let mut open = true;
            let mut create = false;
            egui::Window::new("Create list")
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .show(context, |ui| {
                    ui.label("New list name:");
                    let response = ui.text_edit_singleline(&mut self.new_list_name);
                    create = ui.button("Create").clicked()
                        || (response.lost_focus()
                            && ui.input(|input| input.key_pressed(egui::Key::Enter)));
                });
            self.show_new_list = open;
            if create {
                self.create_list();
            } else if !open {
                self.save_after_create = None;
            }
        }

        if let Some(name) = self.delete_list_confirmation.clone() {
            let mut open = true;
            let mut delete = false;
            egui::Window::new("Delete list")
                .open(&mut open)
                .collapsible(false)
                .resizable(false)
                .show(context, |ui| {
                    ui.label(format!("Delete “{name}” and its saved listing snapshots?"));
                    ui.horizontal(|ui| {
                        delete = ui.button("Delete").clicked();
                        if ui.button("Cancel").clicked() {
                            self.delete_list_confirmation = None;
                        }
                    });
                });
            if delete {
                self.state.collections.remove(&name);
                self.current_view = SEARCH_RESULTS.into();
                self.selected = None;
                self.delete_list_confirmation = None;
                self.save_state();
                self.set_status("INFO", format!("Deleted list “{name}”."));
            } else if !open {
                self.delete_list_confirmation = None;
            }
        }
    }
}

impl eframe::App for MonitorApp {
    fn ui(&mut self, root: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let context = root.ctx().clone();
        self.process_events(&context);
        if self
            .next_poll
            .is_some_and(|scheduled| Instant::now() >= scheduled)
            && !self.search_running
        {
            self.start_search(true);
        }
        self.top_search_panel(root);
        self.reference_panel(root, &context);
        self.collection_panel(root);
        self.status_panel(root);
        self.detail_panel(root);
        self.results_panel(root);
        self.auxiliary_windows(&context);
        context.request_repaint_after(Duration::from_millis(100));
    }
}

fn optional_number(value: &str, field: &str) -> Result<Option<u32>, String> {
    let value = value.trim();
    if value.is_empty() {
        return Ok(None);
    }
    value
        .parse::<u32>()
        .map(Some)
        .map_err(|_| format!("{field} must be a non-negative whole number."))
}

fn score_for(scores: &HashMap<String, Option<MatchResult>>, listing_id: &str) -> f32 {
    scores
        .get(listing_id)
        .and_then(|result| result.as_ref())
        .map_or(-1.0, |result| result.score)
}

fn texture_from_bytes(
    context: &egui::Context,
    name: &str,
    bytes: &[u8],
) -> Result<TextureHandle, String> {
    let (width, height, rgba) = dimensions(bytes)?;
    let image = ColorImage::from_rgba_unmultiplied([width, height], &rgba);
    Ok(context.load_texture(name, image, egui::TextureOptions::LINEAR))
}

fn texture_from_path(context: &egui::Context, path: &Path) -> Result<TextureHandle, String> {
    let (width, height, rgba) = load_rgba(path)?;
    let image = ColorImage::from_rgba_unmultiplied([width, height], &rgba);
    Ok(context.load_texture(
        path.display().to_string(),
        image,
        egui::TextureOptions::LINEAR,
    ))
}

fn image_or_placeholder(ui: &mut egui::Ui, texture: Option<&TextureHandle>, maximum: egui::Vec2) {
    if let Some(texture) = texture {
        let original = texture.size_vec2();
        let scale = (maximum.x / original.x)
            .min(maximum.y / original.y)
            .min(1.0);
        ui.add(egui::Image::new(texture).fit_to_exact_size(original * scale));
    } else {
        let (rectangle, _) = ui.allocate_exact_size(maximum, egui::Sense::hover());
        ui.painter()
            .rect_filled(rectangle, 4.0, egui::Color32::from_gray(45));
        ui.painter().text(
            rectangle.center(),
            egui::Align2::CENTER_CENTER,
            "No image",
            egui::FontId::default(),
            egui::Color32::GRAY,
        );
    }
}

fn field(ui: &mut egui::Ui, label: &str, value: &mut String, width: f32) {
    ui.vertical(|ui| {
        ui.small(label);
        ui.add(egui::TextEdit::singleline(value).desired_width(width));
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn optional_numbers_are_validated() {
        assert_eq!(optional_number("", "Radius").unwrap(), None);
        assert_eq!(optional_number("12", "Radius").unwrap(), Some(12));
        assert!(optional_number("-1", "Radius").is_err());
        assert!(optional_number("one", "Radius").is_err());
    }
}
