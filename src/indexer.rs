//! Keeps the index in step with the chosen folders, then extracts and embeds whatever is pending.
//!
//! Model work goes to an `Embedder` (made on first use, freed by `close_worker`). Extraction (vips, OCR, poppler,
//! ffmpeg) runs on a second thread one job ahead, so reading files and embedding them overlap (see `process`).

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::ops::Range;
use std::path::{Path, PathBuf};
use std::thread::JoinHandle;
use std::time::Instant;

use anyhow::Result;

use crate::code;
use crate::crawler::{self, CrawlReport};
use crate::docs;
use crate::embed::{Embedder, Embedding, WorkerError};
use crate::image;
use crate::media::{self, Piece};
use crate::model::{prompts, FileKind, IndexOptions, Modality, SegmentKind};
use crate::ocr;
use crate::pdf;
use crate::store::{FileRecord, Info, Store};
use crate::text::{self, excerpt};
use crate::tools::ExtractError;

// A committed batch updates progress and persists extraction/embedding outcomes.
type CommitCallback<'a> = dyn FnMut(usize, &Job, &[Outcome], f64) + 'a;

#[derive(Debug)]
pub enum IndexerError {
    FingerprintMismatch { index: String, now: String },
}

impl fmt::Display for IndexerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::FingerprintMismatch { index, now } => write!(
                f,
                "this index was built with different settings or model:\n  index: {index}\n  now:   {now}\n\
                 re-run with --reindex (vectors from different settings can't be compared)"
            ),
        }
    }
}

impl std::error::Error for IndexerError {}

/// Errors that stop a run (the files stay pending), rather than fail one file.
fn stops_the_run(error: &anyhow::Error) -> bool {
    error.downcast_ref::<WorkerError>().is_some() || error.downcast_ref::<IndexerError>().is_some()
}

#[derive(Debug, Default)]
pub struct SyncSummary {
    pub added: usize,
    pub changed: usize,
    pub moved: usize,
    pub removed: usize,
    pub unchanged: usize,
    pub crawl: CrawlReport,
    /// Files this sync queued for embedding (new or changed), so a watcher can index exactly those first.
    pub queued: Vec<i64>,
}

#[derive(Clone, Debug)]
pub struct Event {
    pub done: usize,
    pub total: usize,
    pub path: String,
    pub kind: FileKind,
    pub state: String,
    pub seconds: f64,
    pub note: Option<String>,
}

/// One step of the later pass: how far it has got (`done` of the `total` pages and chunks it set out to read, in
/// `files` files), and the file it just read more of.
#[derive(Clone, Debug)]
pub struct Step {
    pub done: usize,
    pub total: usize,
    /// Long files not finished yet, this one included until its last step.
    pub files: usize,
    pub path: String,
    pub kind: FileKind,
    /// "partial" until the file's last step, then "done".
    pub state: String,
    pub seconds: f64,
    pub note: Option<String>,
}

pub type States = BTreeMap<String, usize>;
type Outcome = (String, Option<String>);
pub type MakeEmbedder = Box<dyn FnMut() -> std::result::Result<Box<dyn Embedder>, WorkerError> + Send>;
pub type ResetHook = Box<dyn FnMut(&str, &str) + Send>;

/// Files read and embedded together (images 8 at a time, other files one by one), or one step of a long file (`step`:
/// the pages of a PDF or the chunks of a document to read, from 0). The first step comes with the file.
#[derive(Clone)]
struct Job {
    files: Vec<FileRecord>,
    step: Option<Range<usize>>,
}

struct ReadyImage {
    slot: usize,
    image: image::Prepared,
    ocr: String,
}

enum Payload {
    Nothing,
    Images { screenshot: bool, ready: Vec<ReadyImage> },
    /// `read`: the pages read (from 0) of the PDF's `total`.
    Pdf { pages: Vec<pdf::Page>, read: Range<usize>, total: usize, title: Option<String> },
    /// `chunks`: the ones in `read` (from 0) of the document's `total` (counted up to `max_chunks` + 1).
    Doc { chunks: Vec<String>, read: Range<usize>, total: usize, characters: usize },
    Audio { pieces: Vec<Piece>, duration: f64 },
    Video { frames: Vec<Piece>, sound: Vec<Piece>, duration: f64 },
    Code(code::File),
}

/// A job read from disk and decoded, ready for the model. Its scratch files live in `folder`.
struct Prepared {
    batch: Vec<FileRecord>,
    step: Option<Range<usize>>,
    folder: PathBuf,
    /// Files already settled (skipped or failed while reading).
    outcomes: BTreeMap<usize, (String, Option<String>)>,
    payload: Payload,
}

fn clamped(range: Range<usize>, end: usize) -> Range<usize> {
    range.start.min(end)..range.end.min(end)
}

fn stem(path: &str) -> String {
    Path::new(path).file_stem().map(|stem| stem.to_string_lossy().into_owned()).unwrap_or_default()
}

pub struct Indexer {
    pub store: Store,
    pub options: IndexOptions,
    make_embedder: MakeEmbedder,
    worker: Option<Box<dyn Embedder>>,
    scratch: PathBuf,
    jobs_prepared: usize,
    /// When set, an index built with another model or other settings is emptied and embedded again (the app: a new
    /// version may bring a new model), and this hears about it. When None, indexing stops with `FingerprintMismatch`.
    pub reset_on_model_change: Option<ResetHook>,
    /// The vector source of the embedders `make_embedder` makes, when it's known without starting one (llama.cpp's
    /// is): then `check_model()` can notice a new model before anything needs embedding.
    pub embedder_source: Option<String>,
}

impl Indexer {
    pub fn new(store: Store, options: IndexOptions, make_embedder: MakeEmbedder) -> Result<Self> {
        // One folder per process ($TMPDIR/Recall-<pid>, which the helper clears up after a process that died), and one
        // in it per indexer: the helper runs two (the main index and the code index), and each clears its own away.
        let scratch = std::env::temp_dir()
            .join(format!("Recall-{}", std::process::id()))
            .join(uuid::Uuid::new_v4().to_string());
        std::fs::create_dir_all(&scratch)?;
        Ok(Self {
            store,
            options,
            make_embedder,
            worker: None,
            scratch,
            jobs_prepared: 0,
            reset_on_model_change: None,
            embedder_source: None,
        })
    }

    /// Frees the embedding model (about 1 GB) until the next file needs it.
    pub fn close_worker(&mut self) {
        if let Some(mut worker) = self.worker.take() {
            worker.close();
        }
    }

    /// Stops the worker and removes the scratch folder.
    pub fn finish(&mut self) {
        self.close_worker();
        let _ = std::fs::remove_dir_all(&self.scratch);
    }

    pub fn worker_is_running(&self) -> bool {
        self.worker.is_some()
    }

    // MARK: Sync

    /// Crawls `roots` and reconciles the index: new files are queued, changed ones re-queued, moved ones re-pathed (no
    /// re-embedding), and files that are gone or newly excluded are removed.
    pub fn sync(&mut self, roots: &[PathBuf]) -> Result<SyncSummary> {
        let crawl = crawler::crawl(roots, &self.options);
        let root_paths: Vec<String> =
            roots.iter().map(|root| crate::paths::standardize(root).to_string_lossy().into_owned()).collect();
        let existing = self.store.files_under(&root_paths)?;
        let mut by_path: HashMap<&str, &FileRecord> = HashMap::new();
        let mut by_inode: HashMap<(u64, u64), &FileRecord> = HashMap::new();
        for file in &existing {
            by_path.insert(&file.path, file);
            by_inode.insert((file.device, file.inode), file);
        }
        let seen: HashSet<&str> = crawl.candidates.iter().map(|file| file.path.as_str()).collect();
        let mut moved_from: HashSet<String> = HashSet::new();
        let mut summary = SyncSummary::default();
        let store = &self.store;
        store.transaction(|| {
            for file in &crawl.candidates {
                if let Some(row) = by_path.get(file.path.as_str()) {
                    if row.size != file.size || (row.mtime - file.mtime).abs() > 0.001 || row.kind != file.kind {
                        store.requeue_as(row.id, file)?;
                        summary.changed += 1;
                        summary.queued.push(row.id);
                    } else {
                        summary.unchanged += 1;
                    }
                } else if let Some(row) = by_inode.get(&(file.device, file.inode)).filter(|row| {
                    !seen.contains(row.path.as_str())
                        && !moved_from.contains(&row.path)
                        && row.size == file.size
                        && (row.mtime - file.mtime).abs() < 0.001
                }) {
                    store.move_to(row.id, &file.path)?;
                    moved_from.insert(row.path.clone());
                    summary.moved += 1;
                } else {
                    summary.queued.push(store.insert(file)?);
                    summary.added += 1;
                }
            }
            // A root that went away while it was being walked (a drive ejected mid-crawl) looked emptier than it is:
            // keep its rows rather than take that for "deleted".
            let vanished: Vec<String> = root_paths
                .iter()
                .filter(|root| !Path::new(root).exists())
                .map(|root| if root.ends_with('/') { root.clone() } else { format!("{root}/") })
                .collect();
            for row in &existing {
                if !seen.contains(row.path.as_str())
                    && !moved_from.contains(&row.path)
                    && !vanished.iter().any(|root| row.path.starts_with(root))
                {
                    store.delete(row.id)?;
                    summary.removed += 1;
                }
            }
            Ok(())
        })?;
        summary.crawl = crawl;
        Ok(summary)
    }

    // MARK: Run

    /// Processes pending files, screenshots first, newest first (`only`: just these files). Long PDFs and documents get
    /// their first step (`pdf_step` pages, `doc_step` chunks) and stay `partial` for `finish_long_files`. Per-file
    /// problems are recorded on the file; a dead worker or a settings mismatch stops the run (pending files stay
    /// pending, so the next run resumes). `should_stop` is asked between batches.
    pub fn run(&mut self, limit: Option<usize>, only: Option<&[i64]>, should_stop: &mut dyn FnMut() -> bool,
               on_event: &mut dyn FnMut(&Event)) -> Result<States> {
        let queue = self.store.pending(limit, only)?;
        let total = queue.len();
        let jobs: Vec<Job> = batches(&queue).into_iter().map(|files| Job { files, step: None }).collect();
        let mut states = States::new();
        let mut done = 0;
        self.process(jobs, should_stop, &mut |_, job, results, seconds| {
            for (offset, file) in job.files.iter().enumerate() {
                let (state, note) = &results[offset];
                *states.entry(state.clone()).or_insert(0) += 1;
                on_event(&Event {
                    done: done + offset + 1,
                    total,
                    path: file.path.clone(),
                    kind: file.kind,
                    state: state.clone(),
                    seconds,
                    note: note.clone(),
                });
            }
            done += job.files.len();
        }, &mut |_| false)?;
        Ok(states)
    }

    /// The later pass: reads the rest of long PDFs and documents (`partial` after their first step), in indexing order,
    /// a step at a time. Each step is saved as one transaction, so stopping between steps loses nothing, and the next
    /// pass goes on from there. It runs once nothing is pending: a book's last pages matter less than everything
    /// else's first ones.
    pub fn finish_long_files(&mut self, should_stop: &mut dyn FnMut() -> bool, on_step: &mut dyn FnMut(&Step))
        -> Result<States> {
        let mut jobs = Vec::new();
        for file in self.store.unfinished()? {
            let info = self.store.info(file.id)?;
            let size = if file.kind == FileKind::Pdf { self.options.pdf_step } else { self.options.doc_step };
            // Where it stopped (after the last page or chunk stored, should its info not say; nothing stored: the
            // start).
            let mut start = match info.get("read_to").and_then(|n| n.parse().ok()) {
                Some(start) => start,
                None => self.store.last_place(file.id)?.unwrap_or(0),
            };
            // At least one step, which settles a file whose length was only guessed.
            let end = info.get("read_end").and_then(|n| n.parse::<usize>().ok()).unwrap_or(0).max(start + 1);
            while start < end {
                jobs.push(Job { files: vec![file.clone()], step: Some(start..(start + size).min(end)) });
                start += size;
            }
        }
        // Progress counts pages and chunks: what's read once each job is through.
        let mut through = Vec::new();
        for job in &jobs {
            through.push(through.last().copied().unwrap_or(0) + job.step.as_ref().map_or(0, |step| step.len()));
        }
        let total = through.last().copied().unwrap_or(0);
        let mut files = jobs.iter().map(|job| job.files[0].id).collect::<HashSet<_>>().len();
        let mut states = States::new();
        let finished = std::cell::RefCell::new(HashSet::new());
        self.process(jobs, should_stop, &mut |index, job, results, seconds| {
            let file = &job.files[0];
            let (state, note) = &results[0];
            if state != "partial" && finished.borrow_mut().insert(file.id) {
                *states.entry(state.clone()).or_insert(0) += 1;
                files -= 1;
            }
            on_step(&Step {
                done: through[index],
                total,
                files,
                path: file.path.clone(),
                kind: file.kind,
                state: state.clone(),
                seconds,
                note: note.clone(),
            });
        }, &mut |job| {
            // Finished sooner than its steps said: a document whose length was guessed, or a file that went unreadable.
            finished.borrow().contains(&job.files[0].id)
        })?;
        Ok(states)
    }

    /// Runs `jobs` in order until `should_stop` (asked between jobs), passing each one's results to `committed` (with
    /// its place in `jobs`). Jobs that `skip` turns down when their turn comes are left out.
    ///
    /// Two stages overlap: while the model embeds one job, the next one is read and decoded on another thread (images
    /// scaled, screenshots OCR'd, PDF pages rendered, audio and video decoded).
    fn process(&mut self, jobs: Vec<Job>, should_stop: &mut dyn FnMut() -> bool,
               committed: &mut CommitCallback<'_>, skip: &mut dyn FnMut(&Job) -> bool)
        -> Result<()> {
        let mut ahead: Option<JoinHandle<Prepared>> = None;
        let mut result = Ok(());
        for (index, job) in jobs.iter().enumerate() {
            if should_stop() {
                break;
            }
            let started = Instant::now();
            let prepared = match ahead.take() {
                Some(handle) => handle.join().expect("prepare thread"),
                None => self.prepare(job.clone()).join().expect("prepare thread"),
            };
            if index + 1 < jobs.len() {
                ahead = Some(self.prepare(jobs[index + 1].clone()));
            }
            let folder = prepared.folder.clone();
            if !skip(job) {
                match self.commit(prepared) {
                    Ok(results) => {
                        let seconds = started.elapsed().as_secs_f64() / job.files.len() as f64;
                        committed(index, job, &results, seconds);
                    }
                    Err(error) => result = Err(error),
                }
            }
            let _ = std::fs::remove_dir_all(&folder);
            if result.is_err() {
                break;
            }
        }
        // A job prepared for a run that stopped early: throw its files away.
        if let Some(handle) = ahead {
            if let Ok(prepared) = handle.join() {
                let _ = std::fs::remove_dir_all(prepared.folder);
            }
        }
        result
    }

    /// Compares the index with the current model and settings (`embedder_source`), without starting the model; a
    /// change empties the index (see `reset_on_model_change`) or fails with `FingerprintMismatch`. Without a known
    /// source the check waits for the first file to embed.
    pub fn check_model(&mut self) -> Result<()> {
        let Some(source) = &self.embedder_source else { return Ok(()) };
        let fingerprint = format!("{source} | {}", self.options.fingerprint());
        self.adopt(&fingerprint)
    }

    fn adopt(&mut self, fingerprint: &str) -> Result<()> {
        let Some(indexed) = self.store.meta("fingerprint")? else { return Ok(()) };
        if indexed == fingerprint {
            return Ok(());
        }
        let Some(hook) = self.reset_on_model_change.as_mut() else {
            return Err(IndexerError::FingerprintMismatch { index: indexed, now: fingerprint.to_string() }.into());
        };
        // Everything is queued again: a run that's under way goes on with its batch, and later runs take the rest.
        self.store.reset_all()?;
        hook(&indexed, fingerprint);
        Ok(())
    }

    fn full_worker(&mut self) -> Result<&mut Box<dyn Embedder>> {
        if self.worker.is_none() {
            let mut started = (self.make_embedder)()?;
            let source = started.info().vector_source();
            let fingerprint = format!("{source} | {}", self.options.fingerprint());
            if let Err(error) = self.adopt(&fingerprint) {
                started.close();
                return Err(error);
            }
            self.store.set_meta("fingerprint", Some(&fingerprint))?;
            self.store.set_meta("vector_source", Some(&source))?;
            self.worker = Some(started);
        }
        Ok(self.worker.as_mut().expect("started"))
    }

    // MARK: Prepare (another thread: files only, no database, no model)

    fn prepare(&mut self, job: Job) -> JoinHandle<Prepared> {
        self.jobs_prepared += 1;
        let options = self.options.clone();
        let folder = self.scratch.join(format!("batch-{}", self.jobs_prepared));
        std::thread::Builder::new()
            .name("recall-prepare".into())
            .spawn(move || prepare(job, &options, folder))
            .expect("thread")
    }

    // MARK: Commit (the indexing thread: the model and the database)

    fn commit(&mut self, mut prepared: Prepared) -> Result<Vec<Outcome>> {
        let batch = std::mem::take(&mut prepared.batch);
        let step = prepared.step.clone();
        let later = step.is_some();
        if let (Some(step), Some((state, detail))) = (&step, prepared.outcomes.get(&0)) {
            let why = detail.clone().unwrap_or_else(|| state.clone());
            return Ok(vec![self.stop_reading(&batch[0], step.start, &why)?]);
        }
        let mut results: Vec<Outcome> = vec![("failed".into(), None); batch.len()];
        for (slot, (state, detail)) in &prepared.outcomes {
            self.store.finish(batch[*slot].id, state, detail.as_deref(), &Info::new())?;
            results[*slot] = (state.clone(), detail.clone());
        }
        let payload = std::mem::replace(&mut prepared.payload, Payload::Nothing);
        let outcome = match payload {
            Payload::Nothing => Ok(()),
            Payload::Images { screenshot, ready } => self.images(&batch, screenshot, ready, &mut results),
            Payload::Pdf { pages, read, total, title } => {
                self.pdf(&batch[0], &pages, read, total, title, later).map(|result| results[0] = result)
            }
            Payload::Doc { chunks, read, total, characters } => {
                self.doc(&batch[0], &chunks, read, total, characters, later).map(|result| results[0] = result)
            }
            Payload::Audio { pieces, duration } => self.audio(&batch[0], &pieces, duration).map(|r| results[0] = r),
            Payload::Video { frames, sound, duration } => {
                self.video(&batch[0], &frames, &sound, duration).map(|result| results[0] = result)
            }
            Payload::Code(file) => self.code(&batch[0], &file).map(|result| results[0] = result),
        };
        if let Err(error) = outcome {
            if stops_the_run(&error) {
                return Err(error);
            }
            let why = error.to_string();
            if let Some(step) = &step {
                return Ok(vec![self.stop_reading(&batch[0], step.start, &why)?]);
            }
            for (slot, file) in batch.iter().enumerate() {
                if !prepared.outcomes.contains_key(&slot) {
                    self.store.finish(file.id, "failed", Some(&why), &Info::new())?;
                    results[slot] = ("failed".into(), Some(why.clone()));
                }
            }
        }
        Ok(results)
    }

    /// A long file that can't be read any further: it keeps the pages or chunks read so far, which stay searchable,
    /// and counts as done rather than failed or waiting to be tried again and again.
    fn stop_reading(&self, file: &FileRecord, place: usize, problem: &str) -> Result<Outcome> {
        let mut info = self.store.info(file.id)?;
        info.remove("read_to");
        info.remove("read_end");
        let unit = if file.kind == FileKind::Pdf { "page" } else { "chunk" };
        self.store.finish(file.id, "done", Some(&format!("read to {unit} {place}, not after: {problem}")), &info)?;
        Ok(("done".into(), Some(format!("stopped before {unit} {}: {problem}", place + 1))))
    }

    fn images(&mut self, batch: &[FileRecord], screenshot: bool, ready: Vec<ReadyImage>, results: &mut [Outcome])
        -> Result<()> {
        if ready.is_empty() {
            return Ok(());
        }
        let budget = if screenshot { self.options.screenshot_budget } else { self.options.photo_budget };
        let paths: Vec<String> = ready.iter().map(|item| item.image.path.clone()).collect();
        let vectors = self.full_worker()?.embed_images(&paths, budget)?;
        let store = &self.store;
        store.transaction(|| {
            for (position, item) in ready.iter().enumerate() {
                let file = &batch[item.slot];
                let Some(vector) = &vectors[position].vector else {
                    store.finish(file.id, "failed", vectors[position].error.as_deref(), &Info::new())?;
                    results[item.slot] = ("failed".into(), vectors[position].error.clone());
                    continue;
                };
                store.add_segment(file.id, SegmentKind::Image, Modality::Image, None, None, None, None, Some(vector))?;
                // The words on screen are for keyword search: codes, names and errors typed exactly. Meaning comes from
                // the picture alone; a vector of the words made screenshots outrank better matches.
                if !item.ocr.is_empty() {
                    store.add_segment(file.id, SegmentKind::Ocr, Modality::Text, None, None,
                                      Some(&excerpt(&item.ocr, 240)), Some(&item.ocr), None)?;
                }
                let mut info = Info::new();
                info.insert("width".into(), item.image.width.to_string());
                info.insert("height".into(), item.image.height.to_string());
                if let Some(taken) = &item.image.taken {
                    info.insert("taken".into(), taken.clone());
                }
                let characters = text::length_chars(&item.ocr);
                if screenshot {
                    info.insert("ocr_chars".into(), characters.to_string());
                }
                store.finish(file.id, "done", None, &info)?;
                results[item.slot] = ("done".into(), screenshot.then(|| format!("ocr {characters} chars")));
            }
            Ok(())
        })
    }

    /// Pages `read` of a PDF (all of a short one, a step of a long one). A long one stays `partial` with `read_to` and
    /// `read_end` (pages, from 0) in its info until its last step.
    fn pdf(&mut self, file: &FileRecord, pages: &[pdf::Page], read: Range<usize>, total: usize, title: Option<String>,
           later: bool) -> Result<Outcome> {
        let end = total.min(self.options.max_pages);
        let unread = read.end < end;
        if pages.is_empty() && !later && !unread {
            self.store.finish(file.id, "skipped", Some("no pages"), &Info::new())?;
            return Ok(("skipped".into(), Some("no pages".into())));
        }
        let title = title.unwrap_or_else(|| stem(&file.path));
        let text_pages: Vec<&pdf::Page> = pages.iter().filter(|page| page.image_path.is_none()).collect();
        // A page that's mostly digits (a table, a data listing) gets keyword search only.
        let word_pages: Vec<&pdf::Page> = text_pages.iter().copied().filter(|page| text::has_words(&page.text)).collect();
        let image_pages: Vec<&pdf::Page> = pages.iter().filter(|page| page.image_path.is_some()).collect();
        let garbled_pages: Vec<&pdf::Page> = pages.iter().filter(|page| page.garbled.is_some()).collect();
        let mut text_vectors: HashMap<usize, Vec<f32>> = HashMap::new(); // by page number
        let mut image_vectors: Vec<Embedding> = Vec::new();
        let mut garbled_vectors: Vec<Vec<f32>> = Vec::new();
        if !word_pages.is_empty() || !image_pages.is_empty() {
            let page_budget = self.options.page_budget;
            let worker = self.full_worker()?;
            let inputs: Vec<String> = word_pages
                .iter()
                .map(|page| prompts::document(Some(&title), text::prefix(&page.text, 4000)))
                .collect();
            let vectors = worker.embed_texts(&inputs)?;
            text_vectors = word_pages.iter().map(|page| page.number).zip(vectors).collect();
            let images: Vec<String> = image_pages.iter().filter_map(|page| page.image_path.clone()).collect();
            image_vectors = worker.embed_images(&images, page_budget)?;
            let inputs: Vec<String> = garbled_pages
                .iter()
                .map(|page| prompts::document(Some(&title), text::prefix(page.garbled.as_deref().unwrap_or(""), 4000)))
                .collect();
            garbled_vectors = worker.embed_texts(&inputs)?;
        }
        let store = &self.store;
        store.transaction(|| {
            // Meaning only: the garbled words are never shown or matched against what you type.
            for (page, vector) in garbled_pages.iter().zip(&garbled_vectors) {
                store.add_segment(file.id, SegmentKind::Page, Modality::Text, Some(page.number as f64), None, None,
                                  None, Some(vector))?;
            }
            for page in &text_pages {
                store.add_segment(file.id, SegmentKind::Page, Modality::Text, Some(page.number as f64), None,
                                  Some(&excerpt(&page.text, 240)), Some(&page.text),
                                  text_vectors.get(&page.number).map(Vec::as_slice))?;
            }
            for (page, embedding) in image_pages.iter().zip(&image_vectors) {
                let page_excerpt = (!page.text.is_empty()).then(|| excerpt(&page.text, 240));
                store.add_segment(file.id, SegmentKind::PageImage, Modality::Image, Some(page.number as f64), None,
                                  page_excerpt.as_deref(), Some(&page.text), embedding.vector.as_deref())?;
            }
            let mut info = if later { store.info(file.id)? } else { Info::new() };
            info.insert("pages".into(), total.to_string());
            let scanned = info.get("scanned_pages").and_then(|n| n.parse::<usize>().ok()).unwrap_or(0);
            info.insert("scanned_pages".into(), (scanned + image_pages.len()).to_string());
            if unread {
                info.insert("read_to".into(), read.end.to_string());
                info.insert("read_end".into(), end.to_string());
            } else {
                info.remove("read_to");
                info.remove("read_end");
            }
            let detail = if unread {
                Some(format!("read to page {} of {total}", read.end))
            } else if end < total {
                Some(format!("the first {end} of {total} pages"))
            } else {
                None
            };
            store.finish(file.id, if unread { "partial" } else { "done" }, detail.as_deref(), &info)
        })?;
        let ocr = pages.iter().filter(|page| page.ocr).count();
        let span = if later {
            format!("pages {}–{} of {total}", read.start + 1, read.end)
        } else {
            format!("{}/{total} pages", pages.len())
        };
        let note = format!("{span}, {} as images", image_pages.len())
            + &if ocr > 0 { format!(", {ocr} read by OCR") } else { String::new() };
        Ok((if unread { "partial" } else { "done" }.into(), Some(note)))
    }

    /// Chunks `read` of a document (all of a short one, a step of a long one), the way `pdf` reads pages.
    fn doc(&mut self, file: &FileRecord, chunks: &[String], read: Range<usize>, total: usize, characters: usize,
           later: bool) -> Result<Outcome> {
        if total == 0 {
            self.store.finish(file.id, "skipped", Some("no text"), &Info::new())?;
            return Ok(("skipped".into(), Some("no text".into())));
        }
        let title = stem(&file.path);
        // Text that's mostly digits (a data dump saved as .txt) gets keyword search only.
        let worded: Vec<usize> = (0..chunks.len()).filter(|index| text::has_words(&chunks[*index])).collect();
        let vectors = if worded.is_empty() {
            Vec::new()
        } else {
            let inputs: Vec<String> =
                worded.iter().map(|index| prompts::document(Some(&title), &chunks[*index])).collect();
            self.full_worker()?.embed_texts(&inputs)?
        };
        let vector: HashMap<usize, Vec<f32>> = worded.iter().copied().zip(vectors).collect();
        let end = total.min(self.options.max_chunks);
        let unread = read.end < end;
        let store = &self.store;
        store.transaction(|| {
            for (offset, chunk) in chunks.iter().enumerate() {
                store.add_segment(file.id, SegmentKind::Chunk, Modality::Text, Some((read.start + offset + 1) as f64),
                                  None, Some(&excerpt(chunk, 240)), Some(chunk),
                                  vector.get(&offset).map(Vec::as_slice))?;
            }
            let mut info = Info::new();
            info.insert("chunks".into(), end.to_string());
            info.insert("chars".into(), characters.to_string());
            if unread {
                info.insert("read_to".into(), read.end.to_string());
                info.insert("read_end".into(), end.to_string());
            }
            let detail = if unread {
                Some(format!("read to chunk {} of {end}", read.end))
            } else if end < total {
                Some(format!("the first {end} chunks"))
            } else {
                None
            };
            store.finish(file.id, if unread { "partial" } else { "done" }, detail.as_deref(), &info)
        })?;
        let span = if later {
            format!("chunks {}–{} of {end}", read.start + 1, read.end)
        } else if unread {
            format!("{} of {end} chunks", chunks.len())
        } else {
            format!("{} chunks", chunks.len())
        };
        let keywords_only = chunks.len() - worded.len();
        let note = span + &if keywords_only > 0 { format!(", {keywords_only} for keywords only") } else { String::new() };
        Ok((if unread { "partial" } else { "done" }.into(), Some(note)))
    }

    /// A code file's stretches of lines (a notebook's of cells), each embedded with the file's path in its repo as its
    /// title, and kept with its lines for keyword search and for opening at the place.
    fn code(&mut self, file: &FileRecord, code: &code::File) -> Result<Outcome> {
        if code.pieces.is_empty() {
            self.store.finish(file.id, "skipped", Some("no code"), &Info::new())?;
            return Ok(("skipped".into(), Some("no code".into())));
        }
        // Lines that are mostly numbers and symbols (a table of constants) get keyword search only.
        let worded: Vec<usize> = (0..code.pieces.len()).filter(|index| text::has_words(&code.pieces[*index].text)).collect();
        let vectors = if worded.is_empty() {
            Vec::new()
        } else {
            let inputs: Vec<String> =
                worded.iter().map(|index| prompts::document(Some(&code.title), &code.pieces[*index].text)).collect();
            self.full_worker()?.embed_texts(&inputs)?
        };
        let vector: HashMap<usize, Vec<f32>> = worded.iter().copied().zip(vectors).collect();
        let kind = if code.cells.is_none() { SegmentKind::Lines } else { SegmentKind::Cells };
        let store = &self.store;
        store.transaction(|| {
            for (offset, piece) in code.pieces.iter().enumerate() {
                store.add_segment(file.id, kind, Modality::Text, Some(piece.first as f64), Some(piece.last as f64),
                                  Some(&code::summary(&piece.text)), Some(&piece.text),
                                  vector.get(&offset).map(Vec::as_slice))?;
            }
            let mut info = Info::new();
            info.insert("lines".into(), code.lines.to_string());
            if let Some(cells) = code.cells {
                info.insert("cells".into(), cells.to_string());
            }
            if let Some(repo) = &code.repo {
                info.insert("repo".into(), repo.clone());
            }
            store.finish(file.id, "done", None, &info)
        })?;
        let size = match code.cells {
            Some(cells) => format!("{cells} cells"),
            None => format!("{} lines", code.lines),
        };
        let keywords_only = code.pieces.len() - worded.len();
        let note = format!("{} stretches, {size}", code.pieces.len())
            + &if keywords_only > 0 { format!(", {keywords_only} for keywords only") } else { String::new() };
        Ok(("done".into(), Some(note)))
    }

    fn audio(&mut self, file: &FileRecord, pieces: &[Piece], duration: f64) -> Result<Outcome> {
        if pieces.is_empty() {
            self.store.finish(file.id, "skipped", Some("silent or shorter than 2 s"), &Info::new())?;
            return Ok(("skipped".into(), Some("silent or too short".into())));
        }
        let paths: Vec<String> = pieces.iter().map(|piece| piece.path.clone()).collect();
        let vectors = self.full_worker()?.embed_audio(&paths)?;
        let store = &self.store;
        store.transaction(|| {
            for (piece, embedding) in pieces.iter().zip(&vectors) {
                store.add_segment(file.id, SegmentKind::Audio, Modality::Audio, Some(piece.start), Some(piece.end),
                                  None, None, embedding.vector.as_deref())?;
            }
            let mut info = Info::new();
            info.insert("duration".into(), format!("{duration:.1}"));
            store.finish(file.id, "done", None, &info)
        })?;
        Ok(("done".into(), Some(format!("{} windows, {} s", pieces.len(), duration as i64))))
    }

    fn video(&mut self, file: &FileRecord, frames: &[Piece], sound: &[Piece], duration: f64) -> Result<Outcome> {
        if frames.is_empty() && sound.is_empty() {
            self.store.finish(file.id, "skipped", Some("no frames or sound"), &Info::new())?;
            return Ok(("skipped".into(), Some("no frames or sound".into())));
        }
        let budget = self.options.frame_budget;
        let worker = self.full_worker()?;
        let frame_paths: Vec<String> = frames.iter().map(|piece| piece.path.clone()).collect();
        let frame_vectors = worker.embed_images(&frame_paths, budget)?;
        let sound_paths: Vec<String> = sound.iter().map(|piece| piece.path.clone()).collect();
        let sound_vectors = worker.embed_audio(&sound_paths)?;
        let store = &self.store;
        store.transaction(|| {
            for (frame, embedding) in frames.iter().zip(&frame_vectors) {
                store.add_segment(file.id, SegmentKind::Frame, Modality::Image, Some(frame.start), Some(frame.end),
                                  None, None, embedding.vector.as_deref())?;
            }
            for (piece, embedding) in sound.iter().zip(&sound_vectors) {
                store.add_segment(file.id, SegmentKind::Audio, Modality::Audio, Some(piece.start), Some(piece.end),
                                  None, None, embedding.vector.as_deref())?;
            }
            let mut info = Info::new();
            info.insert("duration".into(), format!("{duration:.1}"));
            info.insert("frames".into(), frames.len().to_string());
            info.insert("sound_windows".into(), sound.len().to_string());
            store.finish(file.id, "done", None, &info)
        })?;
        Ok((
            "done".into(),
            Some(format!("{} frames, {} sound windows, {} s", frames.len(), sound.len(), duration as i64)),
        ))
    }
}

impl Drop for Indexer {
    fn drop(&mut self) {
        self.finish();
    }
}

/// Images and screenshots go to the model 8 at a time; other files one by one.
fn batches(queue: &[FileRecord]) -> Vec<Vec<FileRecord>> {
    let mut batches = Vec::new();
    let mut index = 0;
    while index < queue.len() {
        let mut batch = vec![queue[index].clone()];
        if matches!(batch[0].kind, FileKind::Image | FileKind::Screenshot) {
            while index + batch.len() < queue.len()
                && batch.len() < 8
                && queue[index + batch.len()].kind == batch[0].kind
            {
                batch.push(queue[index + batch.len()].clone());
            }
        }
        index += batch.len();
        batches.push(batch);
    }
    batches
}

fn prepare(job: Job, options: &IndexOptions, folder: PathBuf) -> Prepared {
    let batch = job.files.clone();
    let mut prepared =
        Prepared { batch: batch.clone(), step: job.step.clone(), folder, outcomes: BTreeMap::new(), payload: Payload::Nothing };
    let path = PathBuf::from(&batch[0].path);
    let read = (|| -> std::result::Result<Payload, ExtractError> {
        std::fs::create_dir_all(&prepared.folder).map_err(|error| ExtractError::Unreadable(error.to_string()))?;
        let folder = &prepared.folder;
        Ok(match batch[0].kind {
            FileKind::Image | FileKind::Screenshot => {
                let screenshot = batch[0].kind == FileKind::Screenshot;
                let mut ready = Vec::new();
                for (slot, file) in batch.iter().enumerate() {
                    let path = Path::new(&file.path);
                    let max_pixel = if screenshot { 1536 } else { 1024 };
                    match image::prepare(path, max_pixel, options.min_image_side, folder, screenshot) {
                        Ok(image) => {
                            let ocr = if screenshot { ocr::text(path) } else { String::new() };
                            ready.push(ReadyImage { slot, image, ocr });
                        }
                        Err(error @ ExtractError::TooSmall(..)) => {
                            prepared.outcomes.insert(slot, ("skipped".into(), Some(error.to_string())));
                        }
                        Err(error) => {
                            prepared.outcomes.insert(slot, ("failed".into(), Some(error.to_string())));
                        }
                    }
                }
                Payload::Images { screenshot, ready }
            }
            FileKind::Pdf => {
                // Long PDFs (books) get their first pages now and the rest after everything else.
                let mut wanted = 0..0;
                let read = pdf::pages(&path, |total| {
                    let range = job.step.clone().unwrap_or(if total > options.big_pdf_pages {
                        0..options.pdf_step
                    } else {
                        0..options.max_pages
                    });
                    wanted = clamped(range, total.min(options.max_pages));
                    wanted.clone()
                }, options.min_page_text, 1400, folder)?;
                Payload::Pdf { pages: read.pages, read: wanted, total: read.total, title: read.title }
            }
            FileKind::Doc => {
                // The chunks are cut the same way every time, so a later step goes on where the last one stopped.
                let text = docs::text_of(&path)?;
                let chunks = docs::chunks(&text, 1800, 200, options.max_chunks + 1);
                let read = clamped(job.step.clone().unwrap_or(0..options.doc_step), chunks.len().min(options.max_chunks));
                Payload::Doc {
                    chunks: chunks[read.clone()].to_vec(),
                    read,
                    total: chunks.len(),
                    characters: text::length_chars(&text),
                }
            }
            FileKind::Audio => {
                let (pieces, duration) = media::audio_windows(&path, options.audio_window, options.audio_hop, folder)?;
                Payload::Audio { pieces, duration }
            }
            FileKind::Video => {
                let keyframes = media::keyframes(&path, options.frame_interval, 1024, 1200, folder)?;
                let sound = if keyframes.has_audio {
                    media::audio_windows(&path, options.audio_window, options.audio_hop, folder)?.0
                } else {
                    Vec::new()
                };
                Payload::Video { frames: keyframes.frames, sound, duration: keyframes.duration }
            }
            FileKind::Code => Payload::Code(code::read(&path, options)?),
        })
    })();
    match read {
        Ok(payload) => prepared.payload = payload,
        Err(error @ ExtractError::LeftOut(_)) => {
            prepared.outcomes.insert(0, ("skipped".into(), Some(error.to_string())));
        }
        Err(error) => {
            for slot in 0..batch.len() {
                prepared.outcomes.entry(slot).or_insert_with(|| ("failed".into(), Some(error.to_string())));
            }
        }
    }
    prepared
}
