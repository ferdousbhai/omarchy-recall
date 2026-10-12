//! Indexing without a model (a fake embedder): long PDFs and documents read in steps, digits left to keyword search,
//! caps, the store's scope and pruning, a folder that went away, and float16 scoring across blocks.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use recall::crawler::Candidate;
use recall::docs;
use recall::embed::{Embedder, Embedding, WorkerError, WorkerInfo};
use recall::estimator::{self, Costs};
use recall::indexer::{Indexer, Step};
use recall::model::{FileKind, IndexOptions, Modality, SegmentKind};
use recall::searcher::Searcher;
use recall::store::{Info, Store, MAIN_FILE};
use recall::text;
use recall::vectors::{VectorStorage, DIMENSION};

/// Vectors without a model: the same text always gets the same unit vector. Counts the texts it was asked to embed.
struct FakeEmbedder {
    info: WorkerInfo,
    texts: Arc<AtomicUsize>,
}

fn fake_vector(text: &str) -> Vec<f32> {
    let mut state: u64 = 5381;
    for byte in text.bytes() {
        state = state.wrapping_mul(33).wrapping_add(u64::from(byte));
    }
    let mut values: Vec<f32> = (0..DIMENSION)
        .map(|_| {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            ((state % 2001) as i64 - 1000) as f32 / 1000.0
        })
        .collect();
    let norm = values.iter().map(|v| v * v).sum::<f32>().sqrt();
    values.iter_mut().for_each(|v| *v /= norm);
    values
}

impl Embedder for FakeEmbedder {
    fn info(&self) -> &WorkerInfo {
        &self.info
    }
    fn embed_texts(&mut self, texts: &[String]) -> Result<Vec<Vec<f32>>, WorkerError> {
        self.texts.fetch_add(texts.len(), Ordering::SeqCst);
        Ok(texts.iter().map(|text| fake_vector(text)).collect())
    }
    fn embed_images(&mut self, paths: &[String], _budget: u32) -> Result<Vec<Embedding>, WorkerError> {
        Ok(paths.iter().map(|path| Embedding { vector: Some(fake_vector(path)), error: None }).collect())
    }
    fn embed_audio(&mut self, paths: &[String]) -> Result<Vec<Embedding>, WorkerError> {
        Ok(paths.iter().map(|path| Embedding { vector: Some(fake_vector(path)), error: None }).collect())
    }
    fn close(&mut self) {}
}

struct Setup {
    _dir: tempfile::TempDir,
    folder: PathBuf,
    texts: Arc<AtomicUsize>,
}

impl Setup {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        // The real path, as the crawler reports it.
        let folder = dir.path().canonicalize().unwrap();
        Self { _dir: dir, folder, texts: Arc::new(AtomicUsize::new(0)) }
    }

    fn files(&self) -> PathBuf {
        let files = self.folder.join("files");
        std::fs::create_dir_all(&files).unwrap();
        files
    }

    fn store(&self) -> Store {
        Store::open(&self.folder.join("index"), MAIN_FILE).unwrap()
    }

    fn indexer(&self, options: IndexOptions) -> Indexer {
        let texts = self.texts.clone();
        Indexer::new(self.store(), options, Box::new(move || {
            let info = WorkerInfo {
                model: "fake".into(),
                revision: "0000000".into(),
                runtime: "test".into(),
                dtype: "f32".into(),
                dim: DIMENSION,
                text_only: false,
                load_seconds: None,
            };
            Ok(Box::new(FakeEmbedder { info, texts: texts.clone() }) as Box<dyn Embedder>)
        }))
        .unwrap()
    }

    /// A text PDF with `pages` pages of a few sentences each; `special` puts a word on a page (from 1).
    fn make_pdf(&self, name: &str, pages: usize, special: &[(usize, &str)]) -> String {
        let path = self.files().join(name);
        let texts: Vec<String> = (1..=pages)
            .map(|page| {
                let extra = special.iter().find(|(at, _)| *at == page).map(|(_, word)| format!(" The {word} lives here."));
                format!("Page {page}. {}{}", prose(600, page as u64, None), extra.unwrap_or_default())
            })
            .collect();
        std::fs::write(&path, simple_pdf(&texts)).unwrap();
        path.to_string_lossy().into_owned()
    }
}

fn run(indexer: &mut Indexer) -> recall::indexer::States {
    indexer.run(None, None, &mut || false, &mut |_| {}).unwrap()
}

fn finish(indexer: &mut Indexer) -> recall::indexer::States {
    indexer.finish_long_files(&mut || false, &mut |_| {}).unwrap()
}

/// Pages (from 1) of a file's segments of the page and chunk kinds, in order.
fn places(store: &Store, path: &str) -> Vec<i64> {
    let mut statement = store
        .db
        .prepare(
            "SELECT s.loc FROM segments s JOIN files f ON f.id = s.file \
             WHERE f.path = ? AND s.kind IN ('page', 'page_image', 'chunk') ORDER BY s.loc",
        )
        .unwrap();
    statement.query_map([path], |row| row.get::<_, f64>(0)).unwrap().map(|loc| loc.unwrap() as i64).collect()
}

fn state(store: &Store, path: &str) -> (String, Info) {
    let (id, state): (i64, String) =
        store.db.query_row("SELECT id, state FROM files WHERE path = ?", [path], |row| Ok((row.get(0)?, row.get(1)?))).unwrap();
    (state, store.info(id).unwrap())
}

/// Where keyword search finds `word`: the page or chunk numbers.
fn found(store: &Store, word: &str) -> Vec<i64> {
    let mut statement = store
        .db
        .prepare("SELECT s.loc FROM fts JOIN segments s ON s.id = fts.rowid WHERE fts MATCH ? ORDER BY s.loc")
        .unwrap();
    statement.query_map([format!("\"{word}\"")], |row| row.get::<_, f64>(0)).unwrap().map(|loc| loc.unwrap() as i64).collect()
}

fn range(from: i64, to: i64) -> Vec<i64> {
    (from..=to).collect()
}

#[test]
fn long_pdf_is_read_in_steps() {
    let setup = Setup::new();
    let pdf = setup.make_pdf("Field guide.pdf", 120, &[(100, "quokka")]);
    let mut indexer = setup.indexer(IndexOptions::default());
    indexer.sync(&[setup.files()]).unwrap();
    assert_eq!(run(&mut indexer).get("partial"), Some(&1));
    let store = &indexer.store;
    assert_eq!(places(store, &pdf), range(1, 30));
    assert_eq!(state(store, &pdf).0, "partial");
    assert_eq!(state(store, &pdf).1.get("read_to").map(String::as_str), Some("30"));
    assert_eq!(store.unfinished_count().unwrap(), (1, 90));
    assert!(found(store, "quokka").is_empty());

    let mut steps: Vec<Step> = Vec::new();
    let states = indexer.finish_long_files(&mut || false, &mut |step| steps.push(step.clone())).unwrap();
    assert_eq!(states.get("done"), Some(&1));
    assert_eq!(steps.iter().map(|s| s.done).collect::<Vec<_>>(), [30, 60, 90]);
    assert_eq!(steps.iter().map(|s| s.total).collect::<Vec<_>>(), [90, 90, 90]);
    assert_eq!(steps.iter().map(|s| s.state.as_str()).collect::<Vec<_>>(), ["partial", "partial", "done"]);
    assert_eq!(steps.iter().map(|s| s.files).collect::<Vec<_>>(), [1, 1, 0]);
    let store = &indexer.store;
    assert_eq!(places(store, &pdf), range(1, 120));
    assert_eq!(found(store, "quokka"), [100]);
    let (done, info) = state(store, &pdf);
    assert_eq!(done, "done");
    assert!(!info.contains_key("read_to") && !info.contains_key("read_end"));
    assert_eq!(info.get("pages").map(String::as_str), Some("120"));
    assert_eq!(store.unfinished_count().unwrap(), (0, 0));
    assert!(finish(&mut indexer).is_empty()); // nothing left
    assert_eq!(setup.texts.load(Ordering::SeqCst), 120); // every page embedded once
}

#[test]
fn stopping_between_steps_loses_nothing() {
    let setup = Setup::new();
    let pdf = setup.make_pdf("Book.pdf", 100, &[]);
    let mut indexer = setup.indexer(IndexOptions::default());
    indexer.sync(&[setup.files()]).unwrap();
    run(&mut indexer);
    let steps = std::cell::Cell::new(0);
    indexer.finish_long_files(&mut || steps.get() >= 1, &mut |_| steps.set(steps.get() + 1)).unwrap();
    assert_eq!(state(&indexer.store, &pdf).1.get("read_to").map(String::as_str), Some("60"));
    assert_eq!(indexer.store.unfinished_count().unwrap(), (1, 40));
    finish(&mut indexer);
    assert_eq!(places(&indexer.store, &pdf), range(1, 100));
    assert_eq!(state(&indexer.store, &pdf).0, "done");
}

#[test]
fn a_changed_file_starts_over() {
    let setup = Setup::new();
    let pdf = setup.make_pdf("Book.pdf", 80, &[]);
    let mut indexer = setup.indexer(IndexOptions::default());
    indexer.sync(&[setup.files()]).unwrap();
    run(&mut indexer);
    std::thread::sleep(std::time::Duration::from_millis(20));
    setup.make_pdf("Book.pdf", 90, &[]); // a new edition, same name
    assert_eq!(indexer.sync(&[setup.files()]).unwrap().changed, 1);
    assert_eq!(state(&indexer.store, &pdf).0, "pending");
    assert!(places(&indexer.store, &pdf).is_empty());
    run(&mut indexer);
    finish(&mut indexer);
    assert_eq!(places(&indexer.store, &pdf), range(1, 90));
}

#[test]
fn long_document_goes_on_where_it_stopped() {
    let setup = Setup::new();
    let text = prose(150_000, 1, Some((120_000, "axolotl")));
    let path = setup.files().join("Notes.txt");
    std::fs::write(&path, &text).unwrap();
    let path = path.to_string_lossy().into_owned();
    let chunks = docs::chunks(&text, 1800, 200, 10_000);
    assert!(chunks.len() > 80);
    let mut indexer = setup.indexer(IndexOptions::default());
    indexer.sync(&[setup.files()]).unwrap();
    run(&mut indexer);
    assert_eq!(places(&indexer.store, &path), range(1, 40));
    assert_eq!(state(&indexer.store, &path).1.get("read_end"), Some(&chunks.len().to_string()));
    assert!(found(&indexer.store, "axolotl").is_empty());
    finish(&mut indexer);
    let store = &indexer.store;
    assert_eq!(places(store, &path), range(1, chunks.len() as i64));
    // The same chunks as one pass would cut, none twice: what the later steps added fits on.
    let mut statement = store
        .db
        .prepare(
            "SELECT coalesce(fts.original, fts.body) FROM fts JOIN segments s ON s.id = fts.rowid \
             WHERE s.kind = 'chunk' ORDER BY s.loc",
        )
        .unwrap();
    let stored: Vec<String> = statement.query_map([], |row| row.get(0)).unwrap().map(Result::unwrap).collect();
    assert_eq!(stored, chunks);
    let places = found(store, "axolotl"); // in one chunk, or two where they overlap
    assert!(!places.is_empty() && places.iter().all(|place| *place > 40));
    assert_eq!(state(store, &path).0, "done");
}

#[test]
fn digits_get_keyword_search_only() {
    let setup = Setup::new();
    let mut dump = String::new();
    for row in 0..4_000u64 {
        dump += &format!("{row} {} {}\n", row * 7919 % 100_003, row * 31 % 977);
    }
    dump += "4815162342\n";
    std::fs::write(setup.files().join("sensor log.txt"), &dump).unwrap();
    assert!(!text::has_words(&dump));
    assert!(text::has_words("Revenue 1,234,567 · Net income 234,567 · Margin 19% in the third quarter"));
    assert!(text::has_words("কলকাতায় বৃষ্টি হচ্ছে"));
    let mut indexer = setup.indexer(IndexOptions::default());
    indexer.sync(&[setup.files()]).unwrap();
    run(&mut indexer);
    finish(&mut indexer);
    assert_eq!(setup.texts.load(Ordering::SeqCst), 0);
    let vectors: i64 = indexer.store.db.query_row("SELECT COUNT(vec) FROM segments", [], |row| row.get(0)).unwrap();
    assert_eq!(vectors, 0);
    assert_eq!(found(&indexer.store, "4815162342").len(), 1);
}

#[test]
fn machine_made_giants_stop_at_the_cap() {
    let setup = Setup::new();
    let pdf = setup.make_pdf("Export.pdf", 120, &[]);
    let mut indexer = setup.indexer(IndexOptions { max_pages: 45, ..IndexOptions::default() });
    indexer.sync(&[setup.files()]).unwrap();
    run(&mut indexer);
    assert_eq!(indexer.store.unfinished_count().unwrap(), (1, 15));
    finish(&mut indexer);
    assert_eq!(places(&indexer.store, &pdf), range(1, 45));
    assert_eq!(state(&indexer.store, &pdf).0, "done");
    let detail: String = indexer.store.db.query_row("SELECT detail FROM files", [], |row| row.get(0)).unwrap();
    assert_eq!(detail, "the first 45 of 120 pages");
}

fn candidate(path: &str, kind: FileKind, inode: u64) -> Candidate {
    Candidate { path: path.into(), kind, size: 10, mtime: 1.0, inode, device: 1 }
}

#[test]
fn estimates_count_the_rest() {
    let setup = Setup::new();
    let book = setup.make_pdf("Book.pdf", 120, &[]);
    let short = setup.make_pdf("Leaflet.pdf", 3, &[]);
    let candidates = [candidate(&book, FileKind::Pdf, 1), candidate(&short, FileKind::Pdf, 2)];
    let costs = Costs::default();
    let estimate = estimator::estimate(&candidates, &IndexOptions::default(), &costs);
    assert_eq!(estimate.pdf_pages, 33);
    assert_eq!(estimate.pdf_pages_total, 123);
    assert_eq!(estimate.long_files, 1);
    assert!((estimate.seconds - 33.0 * costs.pdf_page).abs() < 1e-9);
    assert!((estimate.later_seconds - 90.0 * costs.pdf_page).abs() < 1e-9);
}

#[test]
fn float16_vectors_score_across_blocks() {
    // More vectors than one block: every one must be scored, the last ones too.
    let setup = Setup::new();
    let store = setup.store();
    let rows = VectorStorage::BLOCK_ROWS * 2 + 300;
    let mut target = Vec::new();
    store
        .transaction(|| {
            for index in 0..rows {
                let id = store.insert(&candidate(&format!("/v/{index}.png"), FileKind::Image, index as u64 + 1))?;
                let vector = fake_vector(&index.to_string());
                if index == rows - 5 {
                    target = vector.clone();
                }
                store.add_segment(id, SegmentKind::Image, Modality::Image, None, None, None, None, Some(&vector))?;
            }
            Ok(())
        })
        .unwrap();
    let mut searcher = Searcher::new(store, true).unwrap();
    assert_eq!(searcher.vector_count(), rows);
    let hits = searcher.search("", Some(&target), None, 3, 2.0, None, false).unwrap();
    assert_eq!(hits[0].path, format!("/v/{}.png", rows - 5));
    assert!((hits[0].cosine.unwrap() - 1.0).abs() < 0.002); // float16 rounding only
}

#[test]
fn prune_keeps_only_chosen_folders() {
    let setup = Setup::new();
    let store = setup.store();
    store.insert(&candidate("/a/x.png", FileKind::Image, 1)).unwrap();
    store.insert(&candidate("/a/sub/y.png", FileKind::Image, 2)).unwrap();
    store.insert(&candidate("/ab/z.png", FileKind::Image, 3)).unwrap(); // a sibling whose name starts like "/a"
    store.insert(&candidate("/b/w.png", FileKind::Image, 4)).unwrap();
    assert_eq!(store.prune(&["/a".into()]).unwrap(), 2);
    assert_eq!(store.files_under(&["/a".into()]).unwrap().len(), 2);
    assert!(store.files_under(&["/ab".into(), "/b".into()]).unwrap().is_empty());
    let keyword_rows: i64 = store.db.query_row("SELECT COUNT(*) FROM fts", [], |row| row.get(0)).unwrap();
    assert_eq!(keyword_rows, 2); // the pruned files' names left keyword search too
    assert_eq!(store.prune(&[]).unwrap(), 2);
}

#[test]
fn scope_limits_what_is_waiting() {
    let setup = Setup::new();
    let mut store = setup.store();
    store.insert(&candidate("/a/x.png", FileKind::Image, 1)).unwrap();
    store.insert(&candidate("/drive/y.png", FileKind::Image, 2)).unwrap(); // on a drive that's unplugged
    store.set_scope(Some(vec!["/a".into()]));
    assert_eq!(store.pending_count().unwrap(), 1);
    assert_eq!(store.pending(None, None).unwrap().iter().map(|f| f.path.as_str()).collect::<Vec<_>>(), ["/a/x.png"]);
    assert_eq!(store.counts().unwrap().by_kind_and_state["image"].get("pending"), Some(&1));
    store.set_scope(Some(Vec::new()));
    assert_eq!(store.pending_count().unwrap(), 0);
    store.set_scope(None);
    assert_eq!(store.pending_count().unwrap(), 2);
}

#[test]
fn sync_keeps_the_rows_of_a_folder_that_went_away() {
    let setup = Setup::new();
    let drive = setup.folder.join("drive");
    std::fs::create_dir_all(&drive).unwrap();
    std::fs::write(drive.join("notes.txt"), "notes from the trip").unwrap();
    let mut indexer = Indexer::new(setup.store(), IndexOptions::default(), Box::new(|| {
        Err(WorkerError::NotFound("no model needed to sync".into()))
    }))
    .unwrap();
    assert_eq!(indexer.sync(&[drive.clone()]).unwrap().added, 1);
    std::fs::rename(&drive, setup.folder.join("ejected")).unwrap();
    assert_eq!(indexer.sync(&[drive.clone()]).unwrap().removed, 0); // an ejected drive is not "everything deleted"
    assert_eq!(indexer.store.files_under(&[drive.to_string_lossy().into_owned()]).unwrap().len(), 1);
}

// MARK: Files

/// A PDF with one page per text, in Helvetica, wrapped at 80 characters: enough for poppler to read back.
fn simple_pdf(texts: &[String]) -> Vec<u8> {
    let mut objects: Vec<String> = vec![
        "<< /Type /Catalog /Pages 2 0 R >>".into(),
        String::new(),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".into(),
    ];
    let mut kids = Vec::new();
    for text in texts {
        let (mut lines, mut line) = (Vec::new(), String::new());
        for word in text.split(' ') {
            if line.len() + word.len() > 80 {
                lines.push(std::mem::take(&mut line));
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line += word;
        }
        lines.push(line);
        let body = format!(
            "BT /F1 12 Tf 60 730 Td 15 TL {} ET",
            lines.iter().map(|line| format!("({line}) Tj T*")).collect::<Vec<_>>().join(" ")
        );
        objects.push(format!("<< /Length {} >>\nstream\n{body}\nendstream", body.len()));
        objects.push(format!(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {} 0 R \
             /Resources << /Font << /F1 3 0 R >> >> >>",
            objects.len()
        ));
        kids.push(objects.len());
    }
    objects[1] = format!(
        "<< /Type /Pages /Kids [{}] /Count {} >>",
        kids.iter().map(|kid| format!("{kid} 0 R")).collect::<Vec<_>>().join(" "),
        kids.len()
    );
    let mut pdf = String::from("%PDF-1.4\n");
    let mut offsets = Vec::new();
    for (index, object) in objects.iter().enumerate() {
        offsets.push(pdf.len());
        pdf += &format!("{} 0 obj\n{object}\nendobj\n", index + 1);
    }
    let xref = pdf.len();
    pdf += &format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1);
    for offset in offsets {
        pdf += &format!("{offset:010} 00000 n \n");
    }
    pdf += &format!("trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n", objects.len() + 1);
    pdf.into_bytes()
}

/// Plain English-looking sentences, the same for the same seed; `special`'s word goes in once, past `at`.
fn prose(characters: usize, seed: u64, special: Option<(usize, &str)>) -> String {
    const WORDS: [&str; 18] = ["river", "garden", "morning", "letter", "window", "harbour", "kitchen", "mountain",
        "station", "market", "evening", "bridge", "orchard", "lantern", "meadow", "village", "library", "summer"];
    let (mut pieces, mut count, mut index, mut placed) = (Vec::new(), 0, seed as i64, special.is_none());
    while count < characters {
        index = (index.wrapping_mul(1103515245).wrapping_add(12345)) & 0x7fffffff;
        let mut piece = WORDS[index as usize % WORDS.len()].to_string() + if index % 7 == 0 { "." } else { "" };
        if let Some((at, word)) = special {
            if !placed && count >= at {
                piece = format!("the {word} swims. {piece}");
                placed = true;
            }
        }
        count += piece.chars().count() + 1;
        pieces.push(piece);
    }
    pieces.join(" ")
}

