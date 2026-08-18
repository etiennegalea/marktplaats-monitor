use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use directories::{BaseDirs, ProjectDirs};
use serde::{Deserialize, Serialize};

use crate::model::{Listing, SavedListing};

#[derive(Default, Serialize, Deserialize)]
pub struct PersistentState {
    #[serde(default)]
    pub viewed: BTreeSet<String>,
    #[serde(default)]
    pub reference_images: Vec<PathBuf>,
    #[serde(default)]
    pub collections: BTreeMap<String, Vec<SavedListing>>,
}

impl PersistentState {
    pub fn load() -> (Self, PathBuf) {
        let path = state_path();
        let legacy =
            BaseDirs::new().map(|dirs| dirs.home_dir().join(".marktplaats-monitor/viewed.json"));
        let source = if path.exists() {
            path.clone()
        } else if legacy.as_ref().is_some_and(|legacy| legacy.exists()) {
            legacy.expect("legacy path was checked")
        } else {
            return (Self::default(), path);
        };
        let state = fs::read_to_string(source)
            .ok()
            .and_then(|contents| serde_json::from_str(&contents).ok())
            .unwrap_or_default();
        (state, path)
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("Could not create state directory: {error}"))?;
        }
        let temporary = path.with_extension("json.tmp");
        let contents = serde_json::to_string_pretty(self)
            .map_err(|error| format!("Could not encode application state: {error}"))?;
        fs::write(&temporary, contents)
            .map_err(|error| format!("Could not write application state: {error}"))?;
        #[cfg(target_os = "windows")]
        if path.exists() {
            fs::remove_file(path)
                .map_err(|error| format!("Could not replace application state: {error}"))?;
        }
        fs::rename(&temporary, path)
            .map_err(|error| format!("Could not replace application state: {error}"))
    }

    pub fn create_collection(&mut self, name: &str) -> Result<String, String> {
        let cleaned = name.trim();
        if cleaned.is_empty() {
            return Err("List name cannot be empty.".into());
        }
        self.collections.entry(cleaned.to_owned()).or_default();
        Ok(cleaned.to_owned())
    }

    pub fn save_listing(&mut self, collection: &str, listing: &Listing) -> Result<(), String> {
        let items = self
            .collections
            .get_mut(collection)
            .ok_or_else(|| format!("Unknown list: {collection}"))?;
        let snapshot = listing.snapshot();
        if let Some(existing) = items.iter_mut().find(|item| item.id == listing.id) {
            *existing = snapshot;
        } else {
            items.push(snapshot);
        }
        Ok(())
    }
}

fn state_path() -> PathBuf {
    ProjectDirs::from("nl", "marktplaats", "Marktplaats Monitor")
        .map(|dirs| dirs.data_local_dir().join("state.json"))
        .or_else(|| {
            BaseDirs::new().map(|dirs| dirs.home_dir().join(".marktplaats-monitor/state.json"))
        })
        .unwrap_or_else(|| PathBuf::from("marktplaats-monitor-state.json"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn listing() -> Listing {
        Listing {
            id: "m123".into(),
            title: "Blue bicycle".into(),
            description: "Distinctive frame".into(),
            price_text: "€ 100.00".into(),
            location: "Amsterdam".into(),
            date_text: "Vandaag".into(),
            link: "https://link.marktplaats.nl/m123".into(),
            image_url: None,
            extra_large_image_url: None,
            distance_km: None,
            created_sort: 0,
            added_sort: 0,
        }
    }

    #[test]
    fn named_lists_update_existing_snapshots() {
        let mut state = PersistentState::default();
        state.create_collection("Possible matches").unwrap();
        state.save_listing("Possible matches", &listing()).unwrap();
        state.save_listing("Possible matches", &listing()).unwrap();
        assert_eq!(state.collections["Possible matches"].len(), 1);
    }
}
