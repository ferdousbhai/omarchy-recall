//! How long a first pass takes, from metadata alone: counts, PDF page counts (poppler) and media durations (ffprobe),
//! so nothing is decoded.

use std::collections::{BTreeMap, HashMap};
use std::ops::Add;
use std::path::Path;

use crate::crawler::Candidate;
use crate::docs;
use crate::folders;
use crate::media;
use crate::model::{FileKind, IndexOptions};
use crate::pdf;
use crate::rules;

/// Seconds of indexing per unit with llama.cpp (Q8_0) on a fast GPU, reading files while the model embeds.
#[derive(Clone, Debug)]
pub struct Costs {
    /// 140 tokens, decode included.
    pub image: f64,
    /// 280 tokens; its OCR runs alongside and sets the pace.
    pub screenshot: f64,
    /// Text pages are cheap, rendered scans cost an image.
    pub pdf_page: f64,
    pub doc_chunk: f64,
    /// A 30 s window every 25 s.
    pub audio_second: f64,
    /// A 70-token keyframe per shot (at most one per 3 s) and the soundtrack.
    pub video_second: f64,
    pub code_stretch: f64,
}

impl Default for Costs {
    fn default() -> Self {
        Self {
            image: 0.12,
            screenshot: 0.32,
            pdf_page: 0.09,
            doc_chunk: 0.095,
            audio_second: 0.0089,
            video_second: 0.019,
            code_stretch: 0.037,
        }
    }
}

/// How big the code index gets: its words (~2.4 bytes of index a character of code) and a vector row a stretch.
const PER_CHARACTER: f64 = 2.4;
const PER_STRETCH: f64 = 1600.0;
/// Characters a stretch holds on average, its lines cut where the code breaks.
const CHARACTERS_PER_STRETCH: i64 = 1400;

#[derive(Clone, Debug, Default)]
pub struct Estimate {
    pub files: BTreeMap<FileKind, usize>,
    /// Pages the first pass will read.
    pub pdf_pages: usize,
    pub pdf_pages_total: usize,
    pub audio_seconds: f64,
    pub video_seconds: f64,
    /// Chunks the first pass will read.
    pub doc_chunks: usize,
    /// The first pass.
    pub seconds: f64,
    /// Long PDFs and documents, which get their first step in the first pass, and how long the rest takes later.
    pub long_files: usize,
    pub later_seconds: f64,
    /// The code index: its stretches of code, and about how big it gets on disk.
    pub code_stretches: usize,
    pub bytes: f64,
}

impl Add for Estimate {
    type Output = Estimate;

    fn add(mut self, other: Estimate) -> Estimate {
        for (kind, count) in other.files {
            *self.files.entry(kind).or_default() += count;
        }
        self.pdf_pages += other.pdf_pages;
        self.pdf_pages_total += other.pdf_pages_total;
        self.audio_seconds += other.audio_seconds;
        self.video_seconds += other.video_seconds;
        self.doc_chunks += other.doc_chunks;
        self.seconds += other.seconds;
        self.long_files += other.long_files;
        self.later_seconds += other.later_seconds;
        self.code_stretches += other.code_stretches;
        self.bytes += other.bytes;
        self
    }
}

pub fn estimate(candidates: &[Candidate], options: &IndexOptions, costs: &Costs) -> Estimate {
    let mut estimate = Estimate::default();
    for file in candidates {
        *estimate.files.entry(file.kind).or_default() += 1;
        match file.kind {
            FileKind::Image => estimate.seconds += costs.image,
            FileKind::Screenshot => estimate.seconds += costs.screenshot,
            FileKind::Pdf => {
                let total = pdf::page_count(Path::new(&file.path)).unwrap_or(1);
                let all = total.min(options.max_pages);
                let first = if total > options.big_pdf_pages { options.pdf_step.min(all) } else { all };
                estimate.pdf_pages_total += total;
                estimate.pdf_pages += first;
                estimate.seconds += first as f64 * costs.pdf_page;
                if all > first {
                    estimate.long_files += 1;
                    estimate.later_seconds += (all - first) as f64 * costs.pdf_page;
                }
            }
            FileKind::Doc => {
                // A zipped document is mostly pictures and markup: its text says how long it is.
                let zipped = matches!(rules::extension(Path::new(&file.path)).as_str(), "docx" | "odt" | "pptx" | "xlsx");
                let characters = if zipped { docs::zipped_text_size(Path::new(&file.path)).unwrap_or(0) as i64 } else { file.size };
                let all = ((characters / 2000) as usize).clamp(1, options.max_chunks.max(1));
                let first = all.min(options.doc_step);
                estimate.doc_chunks += first;
                estimate.seconds += first as f64 * costs.doc_chunk;
                if all > first {
                    estimate.long_files += 1;
                    estimate.later_seconds += (all - first) as f64 * costs.doc_chunk;
                }
            }
            FileKind::Audio => {
                let seconds = media::duration(Path::new(&file.path)).unwrap_or(0.0);
                estimate.audio_seconds += seconds;
                estimate.seconds += seconds * costs.audio_second;
            }
            FileKind::Video => {
                let seconds = media::duration(Path::new(&file.path)).unwrap_or(0.0);
                estimate.video_seconds += seconds;
                estimate.seconds += seconds * costs.video_second;
            }
            FileKind::Code => {
                // A notebook's file is mostly its outputs, which aren't read: a guess at its cells' share.
                let characters = if file.path.to_lowercase().ends_with(".ipynb") { file.size / 8 } else { file.size };
                let stretches = ((characters + CHARACTERS_PER_STRETCH - 1) / CHARACTERS_PER_STRETCH).max(1) as usize;
                estimate.code_stretches += stretches;
                estimate.seconds += stretches as f64 * costs.code_stretch;
                estimate.bytes += characters as f64 * PER_CHARACTER + stretches as f64 * PER_STRETCH;
            }
        }
    }
    estimate
}

/// Estimates for each subfolder one level down, and for the files right in `folder` (under its own path), from the
/// same look at each file: the folder lists show where a big first pass comes from. Subfolders are named under
/// `folder` as given, though the walk reports real paths.
pub fn estimates_by_subfolder(candidates: &[Candidate], folder: &str, options: &IndexOptions, costs: &Costs)
    -> HashMap<String, Estimate> {
    let prefix = if folder.ends_with('/') { folder.to_string() } else { format!("{folder}/") };
    let real = format!("{}/", folders::real_path(folder));
    let mut groups: HashMap<String, Vec<Candidate>> = HashMap::new();
    for file in candidates {
        let rest =
            file.path.strip_prefix(prefix.as_str()).or_else(|| file.path.strip_prefix(real.as_str()));
        let key = rest
            .and_then(|rest| rest.find('/').map(|slash| format!("{prefix}{}", &rest[..slash])))
            .unwrap_or_else(|| folder.to_string());
        groups.entry(key).or_default().push(file.clone());
    }
    groups.into_iter().map(|(key, files)| (key, estimate(&files, options, costs))).collect()
}
