//! The engine's shared types: what a file is, the searchable pieces of one, indexing settings, and the model's prompts.

use std::collections::HashSet;
use std::fmt;

/// What a file is, for indexing and for grouping results.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum FileKind {
    Screenshot,
    Image,
    Pdf,
    Doc,
    Audio,
    Video,
    /// Source code, a repo's docs and notebooks: only in the code index (`IndexOptions::code`).
    Code,
}

impl FileKind {
    pub const ALL: [FileKind; 7] =
        [Self::Screenshot, Self::Image, Self::Pdf, Self::Doc, Self::Audio, Self::Video, Self::Code];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Screenshot => "screenshot",
            Self::Image => "image",
            Self::Pdf => "pdf",
            Self::Doc => "doc",
            Self::Audio => "audio",
            Self::Video => "video",
            Self::Code => "code",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.as_str() == text)
    }

    /// The order of the first indexing pass: what people look for most comes first.
    pub const INDEXING_ORDER_SQL: &'static str = "CASE kind WHEN 'screenshot' THEN 0 WHEN 'image' THEN 1 \
        WHEN 'pdf' THEN 2 WHEN 'doc' THEN 3 WHEN 'audio' THEN 4 WHEN 'video' THEN 5 ELSE 6 END";
}

impl fmt::Display for FileKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One searchable piece of a file.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum SegmentKind {
    /// File name and folder names (keyword search only).
    Name,
    /// The image itself.
    Image,
    /// Text OCR read from a screenshot or a scanned page.
    Ocr,
    /// A PDF page's text.
    Page,
    /// A PDF page rendered as an image (scans, slides).
    PageImage,
    /// A piece of a document's text.
    Chunk,
    /// A window of audio (a file or a video's soundtrack).
    Audio,
    /// A video keyframe.
    Frame,
    /// A stretch of a code file: loc to loc_end are its first and last lines.
    Lines,
    /// A stretch of a notebook: loc to loc_end are its first and last cells, from 1.
    Cells,
}

impl SegmentKind {
    pub const ALL: [SegmentKind; 10] = [
        Self::Name, Self::Image, Self::Ocr, Self::Page, Self::PageImage, Self::Chunk, Self::Audio, Self::Frame,
        Self::Lines, Self::Cells,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Name => "name",
            Self::Image => "image",
            Self::Ocr => "ocr",
            Self::Page => "page",
            Self::PageImage => "page_image",
            Self::Chunk => "chunk",
            Self::Audio => "audio",
            Self::Frame => "frame",
            Self::Lines => "lines",
            Self::Cells => "cells",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.as_str() == text)
    }
}

/// What produced a segment's vector. Scores are normalized per modality because text vectors score higher than image
/// or audio vectors for the same topic (measured: 0.75–0.86 against 0.67–0.78).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Modality {
    None,
    Text,
    Image,
    Audio,
}

impl Modality {
    pub const ALL: [Modality; 4] = [Self::None, Self::Text, Self::Image, Self::Audio];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Text => "text",
            Self::Image => "image",
            Self::Audio => "audio",
        }
    }

    pub fn parse(text: &str) -> Self {
        Self::ALL.into_iter().find(|modality| modality.as_str() == text).unwrap_or(Self::None)
    }
}

/// Indexing settings. Anything that changes vectors is part of `fingerprint`; an index never mixes settings.
#[derive(Clone, Debug)]
pub struct IndexOptions {
    /// This index is the code index: the code folders' source files, their repos' docs and their notebooks, read as
    /// stretches of whole lines (`code`), and nothing else (`rules::classify_code`).
    pub code: bool,
    /// Characters a stretch of code holds at most (whole lines; a longer line is a stretch of its own). On a real-code
    /// eval, 1,200 and 3,600 both lost to it: shorter cut functions apart, longer blurred them together.
    pub code_stretch: usize,
    /// A bigger source file was made by a program (an amalgamation, data written as code): left out.
    pub max_code_bytes: i64,
    /// A notebook's file holds its outputs too (pictures, tables): bigger than this, it isn't read.
    pub max_notebook_bytes: i64,
    pub include_code: bool,
    pub include_datasets: bool,
    pub min_image_side: u32,
    /// Long files are read a step at a time: the first step in the first pass (every file gets searchable sooner),
    /// the rest after everything else (`Indexer::finish_long_files`). PDFs up to `big_pdf_pages` are read whole.
    pub big_pdf_pages: usize,
    /// Pages.
    pub pdf_step: usize,
    /// Chunks, ~64,000 characters.
    pub doc_step: usize,
    /// Only machine-made giants get this far: a file's pages or chunks past these are left out.
    pub max_pages: usize,
    pub max_chunks: usize,
    /// Fewer characters than this on a PDF page: the rendered page is embedded instead.
    pub min_page_text: usize,
    /// Vision soft tokens: 70 / 140 / 280 / 560 / 1120.
    pub photo_budget: u32,
    pub screenshot_budget: u32,
    pub page_budget: u32,
    pub frame_budget: u32,
    /// Seconds between sampled video frames (near-duplicates are dropped).
    pub frame_interval: f64,
    /// The model ignores audio past 30 s of one input.
    pub audio_window: f64,
    pub audio_hop: f64,
    pub excluded_folders: Vec<String>,
    pub excluded_extensions: HashSet<String>,
}

impl Default for IndexOptions {
    fn default() -> Self {
        Self {
            code: false,
            code_stretch: 1800,
            max_code_bytes: 1 << 20,
            max_notebook_bytes: 20 << 20,
            include_code: false,
            include_datasets: false,
            min_image_side: 256,
            big_pdf_pages: 50,
            pdf_step: 30,
            doc_step: 40,
            max_pages: 5_000,
            max_chunks: 5_000,
            min_page_text: 200,
            photo_budget: 140,
            screenshot_budget: 280,
            page_budget: 280,
            frame_budget: 70,
            frame_interval: 3.0,
            audio_window: 30.0,
            audio_hop: 25.0,
            excluded_folders: Vec::new(),
            excluded_extensions: HashSet::new(),
        }
    }
}

impl IndexOptions {
    /// File extensions as people write them ("HEIC, .txt *.md") → {"heic", "txt", "md"}.
    pub fn extensions(text: &str) -> HashSet<String> {
        text.split(|c: char| c == ',' || c.is_whitespace())
            .map(|word| word.trim_start_matches(['*', '.']).to_lowercase())
            .filter(|word| !word.is_empty())
            .collect()
    }

    /// The settings that shape vectors, as stored in each index ("photo140 shot280 … audio30.0/25.0s"); numbers keep
    /// the decimal point an index written by the first version of the engine has.
    pub fn fingerprint(&self) -> String {
        // "code1": how code is read (stretches of lines, the path in its repo as the title). A change to it is a new
        // number, and every code index is read again.
        if self.code {
            return format!("code1 lines{}", self.code_stretch);
        }
        format!(
            "photo{} shot{} page{} frame{}/{:?}s audio{:?}/{:?}s",
            self.photo_budget, self.screenshot_budget, self.page_budget, self.frame_budget, self.frame_interval,
            self.audio_window, self.audio_hop
        )
    }
}

/// EmbeddingGemma 2's task prompts (model card). Media inputs take no prompt.
pub mod prompts {
    pub fn query(text: &str) -> String {
        format!("task: search result | query: {text}")
    }

    /// A search of the code index (`code_query`): the model card's prompt for finding code by what it does. Code is
    /// embedded as any document is (`document`, its path in its repo as the title). Against the search prompt on a
    /// real-code eval (27 queries): 24 against 22 in the top 5, MRR 0.79 against 0.75.
    pub fn code_query(text: &str) -> String {
        format!("task: code retrieval | query: {text}")
    }

    pub fn document(title: Option<&str>, text: &str) -> String {
        let title = title.filter(|title| !title.is_empty()).unwrap_or("none");
        format!("title: {title} | text: {text}")
    }
}

/// "code: retry with backoff": a search of the code index, and nothing else. Without the prefix a search never sees
/// code (the code index is a file of its own: `store::CODE_FILE`).
pub mod code_query {
    pub const PREFIX: &str = "code:";

    /// What's typed after "code:" ("" for "code:" alone), or None for a search that isn't one of code.
    pub fn text(query: &str) -> Option<String> {
        let trimmed = query.trim_start();
        let head: String = trimmed.chars().take(PREFIX.chars().count()).collect();
        if head.to_lowercase() != PREFIX {
            return None;
        }
        Some(trimmed[head.len()..].trim().to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprints_match_existing_indexes() {
        assert_eq!(IndexOptions::default().fingerprint(), "photo140 shot280 page280 frame70/3.0s audio30.0/25.0s");
        let code = IndexOptions { code: true, ..Default::default() };
        assert_eq!(code.fingerprint(), "code1 lines1800");
    }

    #[test]
    fn code_queries() {
        assert_eq!(code_query::text("code: retry with backoff").as_deref(), Some("retry with backoff"));
        assert_eq!(code_query::text("  CODE:").as_deref(), Some(""));
        assert_eq!(code_query::text("decode: x"), None);
    }

    #[test]
    fn extensions_as_typed() {
        let set = IndexOptions::extensions("HEIC, .txt *.md");
        assert_eq!(set, ["heic", "txt", "md"].into_iter().map(String::from).collect());
    }
}
