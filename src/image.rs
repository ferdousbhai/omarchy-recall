//! Pictures for the model: a downscaled copy, with the EXIF orientation applied, and what's known about the original.

use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;
use serde::Deserialize;

use crate::tools::{self, ExtractError, Result};

pub struct Prepared {
    pub path: String,
    pub width: u32,
    pub height: u32,
    /// EXIF DateTimeOriginal, as written by the camera.
    pub taken: Option<String>,
}

static VIPS_SIZE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r": (\d+)x(\d+)\s").unwrap());
static EXIF_DATE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d{4}:\d\d:\d\d \d\d:\d\d:\d\d").unwrap());

/// An ffmpeg filter that fits a picture into a square of `side`, never enlarging it.
pub fn fit(side: u32) -> String {
    format!("scale=w='min({side},iw)':h='min({side},ih)':force_original_aspect_ratio=decrease")
}

/// Writes a downscaled copy for the model into `folder` (PNG for screenshots, JPEG for photos). vips decodes straight
/// to the thumbnail size and applies the EXIF orientation (and reads HEIC through libheif); without it, ffmpeg does.
pub fn prepare(path: &Path, max_pixel: u32, min_side: u32, folder: &Path, png: bool) -> Result<Prepared> {
    let out = folder.join(format!("{}.{}", uuid::Uuid::new_v4(), if png { "png" } else { "jpg" }));
    let out_text = out.to_string_lossy().into_owned();
    let source = path.to_string_lossy().into_owned();
    if tools::path("vipsthumbnail").is_some() && tools::path("vipsheader").is_some() {
        // "photo.jpg: 4032x3024 uchar, 3 bands, srgb, jpegload"
        let header = tools::run("vipsheader", &[&source])?;
        let size = header.ok().then(|| header.text()).and_then(|text| {
            let captures = VIPS_SIZE.captures(&text)?;
            Some((captures[1].parse::<u32>().ok()?, captures[2].parse::<u32>().ok()?))
        });
        let (width, height) = size.ok_or_else(|| ExtractError::Unreadable("not an image".into()))?;
        if width.max(height) < min_side {
            return Err(ExtractError::TooSmall(width, height));
        }
        let target = if png { out_text.clone() } else { format!("{out_text}[Q=90]") };
        let made = tools::run("vipsthumbnail", &[source.as_str(), "--size", &format!("{max_pixel}x{max_pixel}>"), "-o", &target])?;
        if !made.ok() {
            return Err(ExtractError::Unreadable(format!("can't decode image: {}", made.complaint())));
        }
        // "2024:06:01 18:22:05 (2024:06:01 18:22:05, ASCII, 20 components, 20 bytes)"
        let taken = tools::run("vipsheader", &["-f", "exif-ifd2-DateTimeOriginal", source.as_str()])
            .ok()
            .filter(|output| output.ok())
            .and_then(|output| EXIF_DATE.find(&output.text()).map(|found| found.as_str().to_string()));
        return Ok(Prepared { path: out_text, width, height, taken });
    }
    #[derive(Deserialize)]
    struct Stream {
        width: Option<u32>,
        height: Option<u32>,
        tags: Option<std::collections::HashMap<String, String>>,
    }
    #[derive(Deserialize)]
    struct Probe {
        streams: Option<Vec<Stream>>,
    }
    let probe = tools::run(
        "ffprobe",
        &["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_tags=DateTimeOriginal",
          "-of", "json", &source],
    )?;
    let stream = probe
        .ok()
        .then(|| serde_json::from_slice::<Probe>(&probe.stdout).ok())
        .flatten()
        .and_then(|probe| probe.streams)
        .and_then(|streams| streams.into_iter().next())
        .ok_or_else(|| ExtractError::Unreadable("not an image".into()))?;
    let (Some(width), Some(height)) = (stream.width, stream.height) else {
        return Err(ExtractError::Unreadable("not an image".into()));
    };
    if width.max(height) < min_side {
        return Err(ExtractError::TooSmall(width, height));
    }
    let made = tools::run(
        "ffmpeg",
        &["-nostdin", "-v", "error", "-y", "-i", &source, "-frames:v", "1", "-vf", &fit(max_pixel), "-q:v", "2",
          "-update", "1", &out_text],
    )?;
    if !made.ok() {
        return Err(ExtractError::Unreadable(format!("can't decode image: {}", made.complaint())));
    }
    let taken = stream.tags.and_then(|tags| tags.get("DateTimeOriginal").cloned());
    Ok(Prepared { path: out_text, width, height, taken })
}

/// Puts a picture made in the scratch folder (a rendered PDF page) where it's wanted, as PNG or JPEG.
pub fn write(image: &Path, to: &Path, png: bool) -> Result<()> {
    let is_png = image.extension().is_some_and(|ext| ext == "png");
    if png == is_png {
        return std::fs::rename(image, to).map_err(|error| ExtractError::Unreadable(error.to_string()));
    }
    let made = tools::run(
        "ffmpeg",
        &["-nostdin", "-v", "error", "-y", "-i", &image.to_string_lossy(), "-q:v", "2", "-update", "1",
          &to.to_string_lossy()],
    )?;
    if !made.ok() {
        return Err(ExtractError::Unreadable(format!("can't encode image: {}", made.complaint())));
    }
    Ok(())
}
