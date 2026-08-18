use std::path::{Path, PathBuf};

use image::{DynamicImage, imageops::FilterType};

const HASH_SIZE: u32 = 8;
const PHASH_SIZE: u32 = 32;
const HISTOGRAM_BUCKETS: usize = 8;

#[derive(Clone)]
pub struct ReferenceImage {
    pub path: PathBuf,
    fingerprint: Fingerprint,
}

#[derive(Clone, Debug)]
pub struct MatchResult {
    pub score: f32,
    pub reference_path: PathBuf,
}

#[derive(Clone)]
struct Fingerprint {
    perceptual_hash: u64,
    difference_hash: u64,
    colour_histogram: [f32; 24],
}

impl ReferenceImage {
    pub fn load(path: &Path) -> Result<Self, String> {
        let image = image::open(path)
            .map_err(|error| format!("{} could not be decoded: {error}", path.display()))?;
        Ok(Self {
            path: path.to_owned(),
            fingerprint: Fingerprint::from_image(&image),
        })
    }
}

pub fn best_match(
    bytes: &[u8],
    references: &[ReferenceImage],
) -> Result<Option<MatchResult>, String> {
    if references.is_empty() {
        return Ok(None);
    }
    let image = image::load_from_memory(bytes)
        .map_err(|error| format!("Downloaded image could not be decoded: {error}"))?;
    let candidate = Fingerprint::from_image(&image);
    Ok(references
        .iter()
        .map(|reference| MatchResult {
            score: reference.fingerprint.similarity(&candidate),
            reference_path: reference.path.clone(),
        })
        .max_by(|left, right| left.score.total_cmp(&right.score)))
}

impl Fingerprint {
    fn from_image(image: &DynamicImage) -> Self {
        Self {
            perceptual_hash: perceptual_hash(image),
            difference_hash: difference_hash(image),
            colour_histogram: colour_histogram(image),
        }
    }

    fn similarity(&self, other: &Self) -> f32 {
        let perceptual =
            1.0 - ((self.perceptual_hash ^ other.perceptual_hash).count_ones() as f32 / 63.0);
        let difference =
            1.0 - ((self.difference_hash ^ other.difference_hash).count_ones() as f32 / 64.0);
        let colour = self
            .colour_histogram
            .iter()
            .zip(other.colour_histogram.iter())
            .map(|(left, right)| left.min(*right))
            .sum::<f32>()
            / 3.0;
        let structure = 0.7 * perceptual + 0.3 * difference;
        (structure.max(0.0) * (0.15 + 0.85 * colour))
            .sqrt()
            .clamp(0.0, 1.0)
            * 100.0
    }
}

fn perceptual_hash(image: &DynamicImage) -> u64 {
    let pixels = image
        .resize_exact(PHASH_SIZE, PHASH_SIZE, FilterType::Lanczos3)
        .to_luma8();
    let mut coefficients = Vec::with_capacity(64);
    let factor = std::f64::consts::PI / (2.0 * f64::from(PHASH_SIZE));
    for vertical_frequency in 0..HASH_SIZE {
        for horizontal_frequency in 0..HASH_SIZE {
            let mut coefficient = 0.0;
            for y in 0..PHASH_SIZE {
                let vertical = ((2 * y + 1) as f64 * f64::from(vertical_frequency) * factor).cos();
                for x in 0..PHASH_SIZE {
                    coefficient += f64::from(pixels.get_pixel(x, y)[0])
                        * ((2 * x + 1) as f64 * f64::from(horizontal_frequency) * factor).cos()
                        * vertical;
                }
            }
            coefficients.push(coefficient);
        }
    }
    let mut comparison_values = coefficients[1..].to_vec();
    comparison_values.sort_by(f64::total_cmp);
    let median = comparison_values[comparison_values.len() / 2];
    coefficients[1..].iter().fold(0, |hash, coefficient| {
        (hash << 1) | u64::from(*coefficient > median)
    })
}

fn difference_hash(image: &DynamicImage) -> u64 {
    let pixels = image
        .resize_exact(HASH_SIZE + 1, HASH_SIZE, FilterType::Lanczos3)
        .to_luma8();
    let mut hash = 0;
    for y in 0..HASH_SIZE {
        for x in 0..HASH_SIZE {
            hash =
                (hash << 1) | u64::from(pixels.get_pixel(x, y)[0] > pixels.get_pixel(x + 1, y)[0]);
        }
    }
    hash
}

fn colour_histogram(image: &DynamicImage) -> [f32; 24] {
    let resized = image.resize_exact(64, 64, FilterType::Triangle).to_rgb8();
    let mut buckets = [0.0_f32; 24];
    for pixel in resized.pixels() {
        for channel in 0..3 {
            let bucket = usize::from(pixel[channel]) * HISTOGRAM_BUCKETS / 256;
            buckets[channel * HISTOGRAM_BUCKETS + bucket] += 1.0;
        }
    }
    for value in &mut buckets {
        *value /= 64.0 * 64.0;
    }
    buckets
}

pub fn dimensions(bytes: &[u8]) -> Result<(usize, usize, Vec<u8>), String> {
    let image = image::load_from_memory(bytes)
        .map_err(|error| format!("Image could not be decoded: {error}"))?
        .to_rgba8();
    let (width, height) = image.dimensions();
    Ok((width as usize, height as usize, image.into_raw()))
}

pub fn load_rgba(path: &Path) -> Result<(usize, usize, Vec<u8>), String> {
    let image = image::open(path)
        .map_err(|error| format!("{} could not be decoded: {error}", path.display()))?
        .to_rgba8();
    let (width, height) = image.dimensions();
    Ok((width as usize, height as usize, image.into_raw()))
}

#[cfg(test)]
mod tests {
    use image::{ImageBuffer, Rgb};

    use super::*;

    fn sample(colour: Rgb<u8>) -> DynamicImage {
        let mut image = ImageBuffer::from_pixel(320, 240, Rgb([255, 255, 255]));
        for x in 50..270 {
            for y in 40..200 {
                image.put_pixel(x, y, colour);
            }
        }
        DynamicImage::ImageRgb8(image)
    }

    #[test]
    fn identical_fingerprints_score_one_hundred() {
        let fingerprint = Fingerprint::from_image(&sample(Rgb([0, 0, 128])));
        assert!((fingerprint.similarity(&fingerprint) - 100.0).abs() < 0.01);
    }

    #[test]
    fn colour_changes_reduce_similarity() {
        let navy = Fingerprint::from_image(&sample(Rgb([0, 0, 128])));
        let lime = Fingerprint::from_image(&sample(Rgb([0, 255, 0])));
        assert!(navy.similarity(&lime) < 90.0);
    }
}
