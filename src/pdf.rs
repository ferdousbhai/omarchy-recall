//! PDFs through poppler: text 50 pages at a time (one pdftotext run each), pictures of pages one at a time, and which
//! pages are read as text, rendered for the vision encoder, or read by OCR.

use std::collections::HashMap;
use std::ops::Range;
use std::path::{Path, PathBuf};

use crate::image;
use crate::ocr;
use crate::text::{self, PageTextProblem, ARABIC};
use crate::tools::{self, ExtractError, Result};

pub struct PdfFile {
    path: PathBuf,
    pub page_count: usize,
    pub title: Option<String>,
    texts: HashMap<usize, String>,
}

const TEXT_STEP: usize = 50;

fn fields(text: &str) -> HashMap<String, String> {
    text.lines()
        .filter_map(|line| line.split_once(':'))
        .map(|(key, value)| (key.to_string(), value.trim().to_string()))
        .collect()
}

/// The page count alone (for estimates), or None when poppler can't open the file.
pub fn page_count(path: &Path) -> Option<usize> {
    let info = tools::run("pdfinfo", &[path.to_string_lossy().as_ref()]).ok()?;
    if !info.ok() {
        return None;
    }
    fields(&info.text()).get("Pages")?.parse().ok()
}

impl PdfFile {
    pub fn open(path: &Path) -> Result<Self> {
        let info = tools::run("pdfinfo", &["-enc", "UTF-8", &path.to_string_lossy()])?;
        if !info.ok() {
            if info.stderr.to_lowercase().contains("password") {
                return Err(ExtractError::Locked);
            }
            return Err(ExtractError::Unreadable(format!("can't open PDF: {}", info.complaint())));
        }
        let fields = fields(&info.text());
        Ok(Self {
            path: path.to_path_buf(),
            page_count: fields.get("Pages").and_then(|n| n.parse().ok()).unwrap_or(0),
            title: fields.get("Title").filter(|title| !title.is_empty()).cloned(),
            texts: HashMap::new(),
        })
    }

    /// Page `index`'s text (from 0).
    pub fn text(&mut self, index: usize) -> String {
        if let Some(text) = self.texts.get(&index) {
            return text.clone();
        }
        let (first, last) = (index, (index + TEXT_STEP).min(self.page_count).saturating_sub(1));
        self.texts.clear(); // only the stretch being read is kept
        let read = tools::run(
            "pdftotext",
            &["-enc", "UTF-8", "-f", &(first + 1).to_string(), "-l", &(last + 1).to_string(),
              &self.path.to_string_lossy(), "-"],
        );
        if let Some(read) = read.ok().filter(|read| read.ok()) {
            // A form feed ends each page.
            for (offset, page) in read.text().split('\u{0C}').enumerate() {
                if first + offset <= last {
                    self.texts.insert(first + offset, page.to_string());
                }
            }
        }
        self.texts.get(&index).cloned().unwrap_or_default()
    }

    /// Page `index` as a PNG in `folder`, fitted into a square of `pixels`.
    pub fn render(&self, index: usize, pixels: u32, folder: &Path) -> Option<PathBuf> {
        let base = folder.join(uuid::Uuid::new_v4().to_string());
        let page = (index + 1).to_string();
        let made = tools::run(
            "pdftoppm",
            &["-f", &page, "-l", &page, "-png", "-scale-to", &pixels.to_string(), "-singlefile",
              &self.path.to_string_lossy(), &base.to_string_lossy()],
        )
        .ok()?;
        let out = base.with_extension("png");
        (made.ok() && out.exists()).then_some(out)
    }
}

#[derive(Clone, Debug)]
pub struct Page {
    /// From 1.
    pub number: usize,
    /// May be empty for scans.
    pub text: String,
    /// Set when the page had too little text and was rendered for the vision encoder.
    pub image_path: Option<String>,
    /// The text came from OCR: a scan, or a text layer that was garbled (`LostLetters`).
    pub ocr: bool,
    /// Text read in glyph order (`GlyphOrder`): too garbled to show or match words against, but enough of its words
    /// survive for a meaning vector beside the page's picture.
    pub garbled: Option<String>,
}

pub struct Read {
    pub pages: Vec<Page>,
    pub total: usize,
    pub title: Option<String>,
}

/// Reads the pages in `range(page_count)` (from 0). Pages with real text become text; scans and slides are rendered,
/// and what's written on them comes from OCR, for search by words. Text layers read garbled are replaced: by OCR
/// where letters were lost (Arabic ligatures), by the rendered page where the words came out in glyph order (Bengali,
/// which the OCR can't read).
pub fn pages(path: &Path, range: impl FnOnce(usize) -> Range<usize>, min_text: usize, render_pixels: u32, folder: &Path)
    -> Result<Read> {
    let mut document = PdfFile::open(path)?;
    let total = document.page_count;
    let wanted = range(total);
    let wanted = wanted.start.min(total)..wanted.end.min(total);
    let mut pages = Vec::new();
    for index in wanted {
        let mut page_text = document.text(index).trim().to_string();
        let mut garbled = None;
        match PageTextProblem::check(&page_text) {
            Some(PageTextProblem::LostLetters) => {
                // OCR's reading replaces the text layer only when it's real Arabic.
                if ocr::reads_arabic() {
                    if let Some(image) = document.render(index, 2000, folder) {
                        let read = ocr::text(&image).trim().to_string();
                        let _ = std::fs::remove_file(&image);
                        if text::length_chars(&read) >= min_text
                            && PageTextProblem::check(&read).is_none()
                            && read.chars().any(|ch| ARABIC.contains(&(ch as u32)))
                        {
                            pages.push(Page { number: index + 1, text: read, image_path: None, ocr: true, garbled: None });
                            continue;
                        }
                    }
                }
                if text::length_chars(&page_text) >= min_text {
                    pages.push(Page { number: index + 1, text: page_text, image_path: None, ocr: false, garbled: None });
                    continue;
                }
            }
            Some(PageTextProblem::GlyphOrder) => {
                garbled = Some(std::mem::take(&mut page_text));
            }
            None => {
                if text::length_chars(&page_text) >= min_text {
                    pages.push(Page { number: index + 1, text: page_text, image_path: None, ocr: false, garbled: None });
                    continue;
                }
            }
        }
        let Some(image) = document.render(index, render_pixels, folder) else { continue };
        // A scan or a slide: its picture has the meaning, and what's written on it comes from OCR, for search by
        // words as in screenshots. Not for glyph-order pages: OCR can't read them.
        let mut read = false;
        if garbled.is_none() {
            let found = ocr::text(&image).trim().to_string();
            if text::length_chars(&found) > text::length_chars(&page_text) {
                page_text = found;
                read = true;
            }
        }
        let out = folder.join(format!("{}.png", uuid::Uuid::new_v4()));
        image::write(&image, &out, true)?;
        pages.push(Page {
            number: index + 1,
            text: page_text,
            image_path: Some(out.to_string_lossy().into_owned()),
            ocr: read,
            garbled,
        });
    }
    Ok(Read { pages, total, title: document.title })
}
