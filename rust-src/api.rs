use std::collections::HashSet;
use std::thread;
use std::time::Duration;

use rand::RngExt;
use reqwest::blocking::Client;
use reqwest::header::{ACCEPT, HeaderMap, HeaderValue};
use scraper::{Html, Selector};
use serde::Deserialize;
use url::Url;

use crate::model::{Listing, SEARCH_PAGE_SIZE, SearchOptions, date_rank, format_price};

const SEARCH_URL: &str = "https://www.marktplaats.nl/lrp/api/search";
const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

#[derive(Clone)]
pub struct MarktplaatsClient {
    client: Client,
}

impl MarktplaatsClient {
    pub fn new() -> Result<Self, String> {
        let mut headers = HeaderMap::new();
        headers.insert(ACCEPT, HeaderValue::from_static("application/json"));
        let client = Client::builder()
            .default_headers(headers)
            .user_agent(USER_AGENT)
            .timeout(Duration::from_secs(15))
            .build()
            .map_err(|error| format!("Could not initialize HTTP client: {error}"))?;
        Ok(Self { client })
    }

    pub fn search(&self, options: &SearchOptions) -> Result<Vec<Listing>, String> {
        let mut listings = Vec::new();
        let mut listing_ids = HashSet::new();
        let mut offset = 0;
        let mut page_number = 0;
        loop {
            let remaining = options
                .maximum_listings
                .map_or(SEARCH_PAGE_SIZE, |maximum| {
                    maximum.saturating_sub(listings.len()).min(SEARCH_PAGE_SIZE)
                });
            if remaining == 0 {
                break;
            }
            if page_number > 0 {
                let delay = rand::rng().random_range(400..=1_200);
                thread::sleep(Duration::from_millis(delay));
            }
            let page = self.search_page(options, remaining, offset)?;
            let total = page.total_result_count;
            let maximum_page = page.max_allowed_page_number;
            let returned = page.listings.len();
            for listing in page.listings.into_iter().take(remaining).map(Listing::from) {
                if listing_ids.insert(listing.id.clone()) {
                    listings.push(listing);
                }
            }
            offset += remaining;
            page_number += 1;
            if options
                .maximum_listings
                .is_some_and(|maximum| listings.len() >= maximum)
                || total.is_some_and(|total| listings.len() >= total)
                || maximum_page.is_some_and(|maximum_page| page_number > maximum_page)
                || returned < remaining
                || returned == 0
            {
                break;
            }
        }
        Ok(listings)
    }

    fn search_page(
        &self,
        options: &SearchOptions,
        limit: usize,
        offset: usize,
    ) -> Result<SearchResponse, String> {
        let distance = options.distance_km.unwrap_or(1_000) * 1_000;
        let mut parameters = vec![
            ("limit", limit.to_string()),
            ("offset", offset.to_string()),
            ("query", options.query.clone()),
            ("searchInTitleAndDescription", "true".into()),
            ("viewOptions", "list-view".into()),
            ("distanceMeters", distance.to_string()),
            ("postcode", options.postcode.clone()),
            ("sortBy", "SORT_INDEX".into()),
            ("sortOrder", "DECREASING".into()),
        ];
        if options.price_from.is_some() || options.price_to.is_some() {
            let lower = options
                .price_from
                .map_or_else(|| "null".into(), |price| (price * 100).to_string());
            let upper = options
                .price_to
                .map_or_else(|| "null".into(), |price| (price * 100).to_string());
            parameters.push(("attributeRanges[]", format!("PriceCents:{lower}:{upper}")));
        }
        for condition in &options.conditions {
            parameters.push(("attributesById[]", condition.to_string()));
        }

        let response = self
            .client
            .get(SEARCH_URL)
            .query(&parameters)
            .send()
            .map_err(|error| format!("Search request failed: {error}"))?
            .error_for_status()
            .map_err(|error| format!("Marktplaats returned an error: {error}"))?;
        response
            .json()
            .map_err(|error| format!("Could not decode search results: {error}"))
    }

    pub fn download(&self, url: &str) -> Result<Vec<u8>, String> {
        self.client
            .get(url)
            .send()
            .map_err(|error| format!("Download failed: {error}"))?
            .error_for_status()
            .map_err(|error| format!("Download returned an error: {error}"))?
            .bytes()
            .map(|bytes| bytes.to_vec())
            .map_err(|error| format!("Could not read download: {error}"))
    }

    pub fn listing_image_urls(&self, listing: &Listing) -> Vec<String> {
        let mut urls = self
            .scrape_listing_image_urls(&listing.id)
            .unwrap_or_default();
        if urls.is_empty()
            && let Some(fallback) = listing
                .extra_large_image_url
                .as_ref()
                .or(listing.image_url.as_ref())
        {
            urls.push(fallback.clone());
        }
        urls.truncate(10);
        urls
    }

    fn scrape_listing_image_urls(&self, listing_id: &str) -> Result<Vec<String>, String> {
        let html = self
            .client
            .get(format!("https://link.marktplaats.nl/{listing_id}"))
            .send()
            .map_err(|error| format!("Listing page request failed: {error}"))?
            .error_for_status()
            .map_err(|error| format!("Listing page returned an error: {error}"))?
            .text()
            .map_err(|error| format!("Could not read listing page: {error}"))?;
        let document = Html::parse_document(&html);
        let selector = Selector::parse(r#"script[type="application/ld+json"]"#)
            .map_err(|error| format!("Could not prepare page parser: {error}"))?;
        for node in document.select(&selector) {
            let raw = node.text().collect::<String>();
            let Ok(value) = serde_json::from_str::<serde_json::Value>(&raw) else {
                continue;
            };
            if value.get("@type").and_then(|kind| kind.as_str()) != Some("Product") {
                continue;
            }
            let mut urls = Vec::new();
            match value.get("image") {
                Some(serde_json::Value::String(url)) => urls.push(url.clone()),
                Some(serde_json::Value::Array(values)) => {
                    urls.extend(
                        values
                            .iter()
                            .filter_map(|value| value.as_str().map(str::to_owned)),
                    );
                }
                _ => {}
            }
            return Ok(urls.into_iter().filter_map(normalise_image_url).collect());
        }
        Ok(Vec::new())
    }
}

fn normalise_image_url(value: String) -> Option<String> {
    let absolute = if value.starts_with("//") {
        format!("https:{value}")
    } else {
        value
    };
    let parsed = Url::parse(&absolute).ok()?;
    (parsed.scheme() == "https" && parsed.host_str() == Some("images.marktplaats.com"))
        .then_some(absolute)
}

#[derive(Deserialize)]
struct SearchResponse {
    listings: Vec<ApiListing>,
    #[serde(default, rename = "totalResultCount")]
    total_result_count: Option<usize>,
    #[serde(default, rename = "maxAllowedPageNumber")]
    max_allowed_page_number: Option<usize>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ApiListing {
    item_id: String,
    title: String,
    description: String,
    date: String,
    price_info: PriceInfo,
    location: Location,
    #[serde(default)]
    pictures: Vec<Picture>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PriceInfo {
    price_cents: i64,
    price_type: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Location {
    city_name: Option<String>,
    country_name: Option<String>,
    distance_meters: Option<i64>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Picture {
    large_url: String,
    extra_extra_large_url: String,
}

impl From<ApiListing> for Listing {
    fn from(value: ApiListing) -> Self {
        let first_picture = value.pictures.first();
        let created_sort = date_rank(&value.date);
        Self {
            id: value.item_id.clone(),
            title: value.title,
            description: value.description,
            price_text: format_price(value.price_info.price_cents, &value.price_info.price_type),
            location: value
                .location
                .city_name
                .or(value.location.country_name)
                .unwrap_or_else(|| "—".into()),
            date_text: value.date,
            link: format!("https://link.marktplaats.nl/{}", value.item_id),
            image_url: first_picture.map(|picture| picture.large_url.clone()),
            extra_large_image_url: first_picture
                .map(|picture| picture.extra_extra_large_url.clone()),
            distance_km: value
                .location
                .distance_meters
                .filter(|distance| *distance >= 0)
                .map(|distance| distance as f32 / 1_000.0),
            created_sort,
            added_sort: chrono::Local::now().timestamp_millis(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_fixture_is_compatible_with_rust_parser() {
        let response: SearchResponse =
            serde_json::from_str(include_str!("../tests/mock/query_response.json")).unwrap();
        let listing = Listing::from(response.listings.into_iter().next().unwrap());
        assert_eq!(listing.id, "m2064554806");
        assert_eq!(listing.price_text, "€ 75.00");
        assert_eq!(listing.location, "Nieuwerkerk aan den IJssel");
        assert!(listing.image_url.is_some());
    }

    #[test]
    #[ignore = "contacts the live Marktplaats API"]
    fn live_search_returns_listings() {
        let client = MarktplaatsClient::new().unwrap();
        let listings = client
            .search(&SearchOptions {
                query: "fiets".into(),
                postcode: String::new(),
                distance_km: None,
                price_from: None,
                price_to: None,
                conditions: Vec::new(),
                maximum_listings: Some(5),
            })
            .unwrap();
        assert!(!listings.is_empty());
    }
}
