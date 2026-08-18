use chrono::{Datelike, Local, NaiveDate};
use serde::{Deserialize, Serialize};

pub const SEARCH_RESULTS: &str = "Search results";
pub const SEARCH_PAGE_SIZE: usize = 100;

#[derive(Clone, Debug, PartialEq)]
pub struct SearchOptions {
    pub query: String,
    pub postcode: String,
    pub distance_km: Option<u32>,
    pub price_from: Option<u32>,
    pub price_to: Option<u32>,
    pub conditions: Vec<u32>,
    pub maximum_listings: Option<usize>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Listing {
    pub id: String,
    pub title: String,
    pub description: String,
    pub price_text: String,
    pub location: String,
    pub date_text: String,
    pub link: String,
    pub image_url: Option<String>,
    #[serde(default)]
    pub extra_large_image_url: Option<String>,
    #[serde(default)]
    pub distance_km: Option<f32>,
    #[serde(default)]
    pub created_sort: i64,
    #[serde(default)]
    pub added_sort: i64,
}

impl Listing {
    pub fn snapshot(&self) -> SavedListing {
        SavedListing {
            id: self.id.clone(),
            title: self.title.clone(),
            description: self.description.clone(),
            price_text: self.price_text.clone(),
            location: self.location.clone(),
            date_text: self.date_text.clone(),
            link: self.link.clone(),
            image_url: self.image_url.clone(),
            distance_km: self.distance_km,
            created_sort: self.created_sort,
            added_sort: self.added_sort,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SavedListing {
    pub id: String,
    pub title: String,
    pub description: String,
    pub price_text: String,
    pub location: String,
    pub date_text: String,
    pub link: String,
    pub image_url: Option<String>,
    #[serde(default)]
    pub distance_km: Option<f32>,
    #[serde(default)]
    pub created_sort: i64,
    #[serde(default)]
    pub added_sort: i64,
}

impl From<&SavedListing> for Listing {
    fn from(value: &SavedListing) -> Self {
        Self {
            id: value.id.clone(),
            title: value.title.clone(),
            description: value.description.clone(),
            price_text: value.price_text.clone(),
            location: value.location.clone(),
            date_text: value.date_text.clone(),
            link: value.link.clone(),
            image_url: value.image_url.clone(),
            extra_large_image_url: value.image_url.clone(),
            distance_km: value.distance_km,
            created_sort: value.created_sort,
            added_sort: value.added_sort,
        }
    }
}

pub fn date_rank(value: &str) -> i64 {
    let today = Local::now().date_naive();
    let date = match value {
        "Vandaag" => Some(today),
        "Gisteren" => today.pred_opt(),
        "Eergisteren" => today.pred_opt().and_then(|date| date.pred_opt()),
        value => {
            let translated = [
                ("jan", "Jan"),
                ("feb", "Feb"),
                ("mrt", "Mar"),
                ("apr", "Apr"),
                ("mei", "May"),
                ("jun", "Jun"),
                ("jul", "Jul"),
                ("aug", "Aug"),
                ("sep", "Sep"),
                ("okt", "Oct"),
                ("nov", "Nov"),
                ("dec", "Dec"),
            ]
            .into_iter()
            .fold(value.to_owned(), |text, (dutch, english)| {
                text.replace(dutch, english)
            });
            NaiveDate::parse_from_str(&translated, "%d %b %y").ok()
        }
    };
    date.map_or(0, |date| i64::from(date.num_days_from_ce()))
}

pub fn normalise_search_terms(value: &str) -> String {
    value
        .split(|character: char| character.is_whitespace() || character == ',')
        .filter(|term| !term.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn format_price(price_cents: i64, price_type: &str) -> String {
    match price_type {
        "FREE" => "Gratis".into(),
        "FAST_BID" => "Bieden".into(),
        "RESERVED" => "Gereserveerd".into(),
        "SEE_DESCRIPTION" => "Zie omschrijving".into(),
        "NOTK" => "N.o.t.k.".into(),
        "ON_REQUEST" => "Op aanvraag".into(),
        "EXCHANGE" => "Ruilen".into(),
        "FIXED" | "MIN_BID" => format!("€ {:.2}", price_cents as f64 / 100.0),
        _ => "Onbekend".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_terms_accept_commas_and_whitespace() {
        assert_eq!(
            normalise_search_terms("  gazelle, fiets  blauw,heren "),
            "gazelle fiets blauw heren"
        );
    }

    #[test]
    fn price_types_are_presented_in_dutch() {
        assert_eq!(format_price(12_345, "FIXED"), "€ 123.45");
        assert_eq!(format_price(0, "FREE"), "Gratis");
    }

    #[test]
    fn dutch_listing_dates_have_a_sortable_rank() {
        assert!(date_rank("Vandaag") > date_rank("Gisteren"));
        assert!(date_rank("10 mrt 24") > 0);
        assert_eq!(date_rank("unknown"), 0);
    }
}
