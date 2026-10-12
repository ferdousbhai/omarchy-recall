//! `recall index-service`: the app's indexing helper, one process per stretch of work.
//!
//! Extraction (OCR, pictures, PDFs, sound and video) runs here and not in the app, so the model's and the readers'
//! memory goes back to the system when this process exits.
//!
//! Protocol: one command per line on stdin, one JSON event per line on stdout.
//!   stdin:  sync | sync code | stop | backfill on | backfill off      (EOF = stop)
//!   stdout: {"event": "missing" | "pruned" | "sync" | "file" | "rest" | "summary" | "waiting" | "idle" | "error" |
//!            "exit", ...}; events about the code index say "code": true
//!
//! The loop: sync, index what the sync just queued (if it's a few files: new files go first, on any power source),
//! then the backfill if allowed; then the code index the same way (`--code-root`: its own sync, asked for with
//! "sync code"); then the rest of long files (`Indexer::finish_long_files`) under the same rules. Between batches it
//! yields to a new sync, to stop, and to "backfill off". When it's done it keeps a warm worker for `linger` seconds
//! (screenshots come in bursts), then exits.
//!
//! Before the model has downloaded it only syncs (file names are searchable at once), says it's "waiting", and exits.
//! Folders that aren't there (an unplugged drive) or can't be read are skipped, and what was indexed from them stays.
//! That's checked before every sync, not once: a drive ejected while this runs must not crawl as "all deleted".

use std::collections::HashSet;
use std::io::BufRead;
use std::path::PathBuf;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use serde_json::{json, Map, Value};

use recall::embed::{Embedder, Embedding, WorkerError, WorkerInfo};
use recall::indexer::Indexer;
use recall::llama::LlamaModels;
use recall::model::IndexOptions;
use recall::store::{self, Store};

use super::{emit, embedder_source, folder_path, is_folder, is_readable, make_embedder, Arguments};

/// One model for both indexes, started when the first file needs it and closed when the helper is done. The indexers
/// get it as an `Embedder` whose `close` leaves it be, since the other may be using it.
#[derive(Clone)]
struct SharedEmbedder {
    log: PathBuf,
    model: Arc<Mutex<Option<Box<dyn Embedder>>>>,
}

impl SharedEmbedder {
    fn is_running(&self) -> bool {
        self.model.lock().unwrap().is_some()
    }

    fn embedder(&self) -> Result<Box<dyn Embedder>, WorkerError> {
        let mut model = self.model.lock().unwrap();
        if model.is_none() {
            *model = Some(make_embedder(false, &self.log)?);
        }
        let info = model.as_ref().unwrap().info().clone();
        Ok(Box::new(Kept { model: self.model.clone(), info }))
    }

    fn close(&self) {
        if let Some(mut model) = self.model.lock().unwrap().take() {
            model.close();
        }
    }
}

struct Kept {
    model: Arc<Mutex<Option<Box<dyn Embedder>>>>,
    info: WorkerInfo,
}

impl Kept {
    fn with<T>(&self, body: impl FnOnce(&mut Box<dyn Embedder>) -> Result<T, WorkerError>) -> Result<T, WorkerError> {
        let mut model = self.model.lock().unwrap();
        match model.as_mut() {
            Some(model) => body(model),
            None => Err(WorkerError::Died("the model was closed".into())),
        }
    }
}

impl Embedder for Kept {
    fn info(&self) -> &WorkerInfo {
        &self.info
    }
    fn embed_texts(&mut self, texts: &[String]) -> Result<Vec<Vec<f32>>, WorkerError> {
        self.with(|model| model.embed_texts(texts))
    }
    fn embed_images(&mut self, paths: &[String], budget: u32) -> Result<Vec<Embedding>, WorkerError> {
        self.with(|model| model.embed_images(paths, budget))
    }
    fn embed_audio(&mut self, paths: &[String]) -> Result<Vec<Embedding>, WorkerError> {
        self.with(|model| model.embed_audio(paths))
    }
    fn close(&mut self) {}
}

/// One index: its indexer (and its store), its folders.
struct Part {
    indexer: Indexer,
    /// The chosen folders, there or not.
    roots: Vec<PathBuf>,
    /// When set, files under none of these folders leave the index (folders taken out of the app).
    keep_only: Option<Vec<PathBuf>>,
    code: bool,
    /// The last report of folders that aren't there or can't be read (said again only when it changes).
    unavailable: Option<(Vec<String>, Vec<String>)>,
}

impl Part {
    fn store(&self) -> &Store {
        &self.indexer.store
    }

    fn pending(&self) -> Option<usize> {
        self.store().pending_count().ok()
    }
}

#[derive(Default)]
struct Requests {
    sync: bool,
    code_sync: bool,
    stop: bool,
    backfill: bool,
    /// Commands read since the waiting loop last looked.
    pokes: u64,
}

type Shared = Arc<(Mutex<Requests>, Condvar)>;

fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64()
}

pub fn serve(args: &Arguments) -> Result<i32> {
    let index = args.index_folder();
    let model = SharedEmbedder { log: index.join("worker.log"), model: Arc::new(Mutex::new(None)) };
    let prune = args.flag("--prune");
    let part = |file: &str, options: IndexOptions, roots: Vec<PathBuf>| -> Result<Part> {
        let store = Store::open(&index, file)?;
        let shared = model.clone();
        let code = options.code;
        let mut indexer = Indexer::new(store, options, Box::new(move || shared.embedder()))?;
        // A new app version may bring a new model: the index starts over by itself (keyword search on names goes on).
        indexer.embedder_source = embedder_source();
        indexer.reset_on_model_change = Some(Box::new(move |indexed: &str, now: &str| {
            emit(json!({"event": "reindex", "from": indexed, "to": now, "code": code}));
        }));
        // A chosen folder that isn't there now (an unplugged drive) is skipped, not an error, and its files stay
        // indexed. With --prune, files under none of the given folders leave the index (the app passes all its
        // folders).
        Ok(Part { indexer, keep_only: prune.then(|| roots.clone()), roots, code, unavailable: None })
    };
    let mut code_options = args.options();
    code_options.code = true;
    code_options.excluded_folders =
        args.list("--code-exclude").iter().map(|path| folder_path(path).to_string_lossy().into_owned()).collect();
    let code_roots: Vec<PathBuf> = args.list("--code-root").iter().map(|path| folder_path(path)).collect();
    let main = part(store::MAIN_FILE, args.options(), args.positional.iter().map(|path| folder_path(path)).collect())?;
    let code = if code_roots.is_empty() { None } else { Some(part(store::CODE_FILE, code_options, code_roots)?) };
    let mut service = Service {
        main,
        code,
        model,
        new_files_limit: args.number("--new-limit").unwrap_or(50),
        linger: args.number("--linger").unwrap_or(60.0),
        shared: Arc::new((
            Mutex::new(Requests {
                sync: true, // the first thing it does
                code_sync: true,
                backfill: args.flag("--backfill"),
                ..Requests::default()
            }),
            Condvar::new(),
        )),
    };
    Ok(service.serve())
}

struct Service {
    main: Part,
    /// The code index (`--code-root`), when there are code folders.
    code: Option<Part>,
    model: SharedEmbedder,
    new_files_limit: usize,
    linger: f64,
    shared: Shared,
}

impl Service {
    fn requests<T>(&self, body: impl FnOnce(&mut Requests) -> T) -> T {
        body(&mut self.shared.0.lock().unwrap())
    }

    fn model_ready(&self) -> bool {
        embedder_source().is_none() || LlamaModels::complete()
    }

    fn serve(&mut self) -> i32 {
        remove_stale_scratch_folders();
        let mut ready: Result<()> = Ok(());
        for part in std::iter::once(&mut self.main).chain(self.code.as_mut()) {
            ready = ready.and_then(|_| {
                part.indexer.check_model()?; // a new model: everything is queued again
                if let Some(keep_only) = &part.keep_only {
                    let roots: Vec<String> = keep_only.iter().map(|root| root.to_string_lossy().into_owned()).collect();
                    let pruned = part.indexer.store.prune(&roots)?;
                    if pruned > 0 {
                        emit(json!({"event": "pruned", "files": pruned, "code": part.code}));
                    }
                }
                Ok(())
            });
        }
        if let Err(error) = ready {
            emit(json!({"event": "error", "message": error.to_string(), "fatal": true}));
            self.finish();
            return 1;
        }
        let shared = self.shared.clone();
        std::thread::spawn(move || read_commands(shared));
        let mut new_files: HashSet<i64> = HashSet::new();
        let mut new_code: HashSet<i64> = HashSet::new();
        loop {
            if self.requests(|r| std::mem::take(&mut r.sync)) {
                self.sync(false, &mut new_files);
            }
            if self.requests(|r| r.stop) {
                break;
            }
            if !self.model_ready() {
                // Nothing to embed with yet; the app starts another helper once the download is done.
                self.emit_counts("waiting", json!({"for": "model"}));
                break;
            }
            if !new_files.is_empty() {
                if !self.run(false, Some(&new_files)) {
                    break;
                }
                new_files = self.left(false, &new_files);
                continue;
            }
            if self.main.pending().unwrap_or(0) > 0 && self.requests(|r| r.backfill) {
                if !self.run(false, None) {
                    break;
                }
                if self.requests(|r| r.sync) {
                    continue;
                }
            }
            // The code index once the main one has nothing waiting: its own sync (code changes come in a while after
            // they settle), what it just queued at once, the rest under the backfill's rules.
            if self.code.is_some() && (self.main.pending().unwrap_or(1) == 0 || !self.requests(|r| r.backfill)) {
                if self.requests(|r| std::mem::take(&mut r.code_sync)) {
                    self.sync(true, &mut new_code);
                }
                if !new_code.is_empty() {
                    if !self.run(true, Some(&new_code)) {
                        break;
                    }
                    new_code = self.left(true, &new_code);
                    continue;
                }
                let code_pending = self.code.as_ref().and_then(Part::pending).unwrap_or(0);
                if code_pending > 0 && self.requests(|r| r.backfill) {
                    if !self.run(true, None) {
                        break;
                    }
                    if self.requests(|r| r.sync || r.code_sync) {
                        continue;
                    }
                }
            }
            // Once nothing is waiting: the rest of long PDFs and documents, the same way.
            let code_pending = match &self.code {
                Some(code) => code.pending().unwrap_or(1),
                None => 0,
            };
            if self.requests(|r| r.backfill)
                && self.main.pending().unwrap_or(1) == 0
                && code_pending == 0
                && self.main.store().unfinished_count().map(|(files, _)| files).unwrap_or(0) > 0
            {
                if !self.finish_long_files() {
                    break;
                }
                if self.requests(|r| r.sync || r.code_sync) {
                    continue;
                }
            }
            if self.code.is_some() && self.requests(|r| r.code_sync) {
                continue;
            }
            self.emit_counts("idle", json!({}));
            // Nothing warm to keep around: leave now. Else wait a little for more work.
            if !self.model.is_running() || !self.wait_for_work() {
                break;
            }
        }
        self.finish();
        emit(json!({"event": "exit"}));
        0
    }

    fn finish(&mut self) {
        self.main.indexer.finish();
        if let Some(code) = &mut self.code {
            code.indexer.finish();
        }
        self.model.close();
    }

    fn part(&mut self, code: bool) -> &mut Part {
        if code { self.code.as_mut().expect("code part") } else { &mut self.main }
    }

    fn sync(&mut self, code: bool, new_files: &mut HashSet<i64>) {
        let started = Instant::now();
        let started_at = now();
        let limit = self.new_files_limit;
        let part = self.part(code);
        let roots = available_roots(part);
        match part.indexer.sync(&roots) {
            Ok(summary) => {
                if summary.queued.len() <= limit {
                    new_files.extend(summary.queued.iter().copied());
                }
                self.emit_counts("sync", json!({
                    "code": code,
                    "new": new_files.len(), // about to be indexed, whatever the power source
                    "added": summary.added, "changed": summary.changed, "moved": summary.moved,
                    "removed": summary.removed, "unchanged": summary.unchanged,
                    "ms": started.elapsed().as_millis() as u64,
                    "started": started_at, // the app checks its sync request came before this
                }));
            }
            Err(error) => {
                emit(json!({"event": "error", "message": format!("sync failed: {error}"), "code": code}));
            }
        }
    }

    /// New files a run stopped before (a sync or stop came first): kept for the next round. Done: anything still
    /// pending joins the backfill.
    fn left(&mut self, code: bool, files: &HashSet<i64>) -> HashSet<i64> {
        let yielded = self.requests(|r| r.sync || r.stop || (code && r.code_sync));
        if !yielded {
            return HashSet::new();
        }
        let ids: Vec<i64> = files.iter().copied().collect();
        self.part(code).store().pending(None, Some(&ids)).map(|files| files.iter().map(|file| file.id).collect()).unwrap_or_default()
    }

    /// Embeds a part's pending files (`only` some). False when indexing has to stop (the worker died, or the index was
    /// built with other settings).
    fn run(&mut self, code: bool, only: Option<&HashSet<i64>>) -> bool {
        let started = Instant::now();
        let is_new = only.is_some();
        let ids: Option<Vec<i64>> = only.map(|ids| ids.iter().copied().collect());
        let shared = self.shared.clone();
        let mut should_stop = || {
            let r = shared.0.lock().unwrap();
            r.sync || r.stop || (!is_new && !r.backfill) || (code && r.code_sync)
        };
        let mut on_event = |event: &recall::indexer::Event| {
            emit(json!({
                "event": "file", "new": is_new, "done": event.done, "total": event.total, "path": event.path,
                "kind": event.kind.as_str(), "state": event.state, "seconds": event.seconds,
                "note": event.note.clone().unwrap_or_default(), "code": code,
            }));
        };
        let part = self.part(code);
        match part.indexer.run(None, ids.as_deref(), &mut should_stop, &mut on_event) {
            Ok(states) => {
                if !states.is_empty() {
                    emit(json!({"event": "summary", "new": is_new, "states": states, "code": code,
                                "ms": started.elapsed().as_millis() as u64}));
                }
                true
            }
            Err(error) => {
                emit(json!({"event": "error", "message": error.to_string(), "fatal": true, "code": code}));
                false
            }
        }
    }

    /// The rest of long files (`Indexer::finish_long_files`), yielding as the backfill does. False when indexing has
    /// to stop.
    fn finish_long_files(&mut self) -> bool {
        let started = Instant::now();
        let shared = self.shared.clone();
        let has_code = self.code.is_some();
        let mut should_stop = || {
            let r = shared.0.lock().unwrap();
            r.sync || (r.code_sync && has_code) || r.stop || !r.backfill
        };
        let mut on_step = |step: &recall::indexer::Step| {
            emit(json!({
                "event": "rest", "done": step.done, "total": step.total, "files": step.files, "path": step.path,
                "kind": step.kind.as_str(), "state": step.state, "seconds": step.seconds,
                "note": step.note.clone().unwrap_or_default(),
            }));
        };
        match self.main.indexer.finish_long_files(&mut should_stop, &mut on_step) {
            Ok(states) => {
                if !states.is_empty() {
                    emit(json!({"event": "summary", "rest": true, "states": states,
                                "ms": started.elapsed().as_millis() as u64}));
                }
                true
            }
            Err(error) => {
                emit(json!({"event": "error", "message": error.to_string(), "fatal": true}));
                false
            }
        }
    }

    /// Waits up to `linger` seconds for something to do. False: time's up, or told to stop.
    fn wait_for_work(&self) -> bool {
        let deadline = Instant::now() + Duration::from_secs_f64(self.linger);
        let (lock, condition) = &*self.shared;
        let mut requests = lock.lock().unwrap();
        let mut seen = requests.pokes;
        loop {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            requests = condition.wait_timeout(requests, left).unwrap().0;
            if requests.pokes == seen {
                continue;
            }
            seen = requests.pokes;
            if requests.stop {
                return false;
            }
            if requests.sync || (requests.code_sync && self.code.is_some()) {
                return true;
            }
            if requests.backfill {
                drop(requests);
                let pending = std::iter::once(&self.main).chain(self.code.as_ref()).any(|part| part.pending().unwrap_or(0) > 0);
                let unfinished = self.main.store().unfinished_count().map(|(files, _)| files).unwrap_or(0) > 0;
                if pending || unfinished {
                    return true;
                }
                requests = lock.lock().unwrap();
            }
        }
    }

    /// An event with both indexes' counts: the main one's as they are, the code index's prefixed "code_".
    fn emit_counts(&self, event: &str, extra: Value) {
        let mut fields: Map<String, Value> = extra.as_object().cloned().unwrap_or_default();
        add_counts(&mut fields, &self.main);
        if let Some(code) = &self.code {
            add_counts(&mut fields, code);
        }
        fields.insert("event".into(), event.into());
        emit(Value::Object(fields));
    }
}

fn add_counts(fields: &mut Map<String, Value>, part: &Part) {
    let Ok(counts) = part.store().counts() else { return };
    let prefix = if part.code { "code_" } else { "" };
    let (mut files, mut searchable, mut pending) = (0, 0, 0);
    for states in counts.by_kind_and_state.values() {
        for (state, count) in states {
            files += count;
            if state == "done" || state == "partial" {
                searchable += count;
            }
            if state == "pending" {
                pending += count;
            }
        }
    }
    fields.insert(format!("{prefix}files"), files.into());
    fields.insert(format!("{prefix}searchable"), searchable.into());
    fields.insert(format!("{prefix}pending"), pending.into());
    fields.insert(format!("{prefix}vectors"), counts.vectors.into());
    if !part.code {
        fields.insert("unfinished".into(), counts.unfinished.into()); // long files with pages or chunks left to read
    }
}

/// A part's chosen folders that are there and readable right now; also what `pending` and the counts look at.
fn available_roots(part: &mut Part) -> Vec<PathBuf> {
    let missing: Vec<&PathBuf> = part.roots.iter().filter(|root| !is_folder(root)).collect();
    let unreadable: Vec<&PathBuf> =
        part.roots.iter().filter(|root| !missing.contains(root) && !is_readable(root)).collect();
    let text = |list: &[&PathBuf]| list.iter().map(|root| root.to_string_lossy().into_owned()).collect::<Vec<_>>();
    let report = (text(&missing), text(&unreadable));
    let changed = match &part.unavailable {
        Some(last) => *last != report,
        None => !(report.0.is_empty() && report.1.is_empty()),
    };
    if changed {
        emit(json!({"event": "missing", "folders": report.0, "unreadable": report.1, "code": part.code}));
    }
    let available: Vec<PathBuf> =
        part.roots.iter().filter(|root| !missing.contains(root) && !unreadable.contains(root)).cloned().collect();
    part.unavailable = Some(report);
    part.indexer.store.set_scope(Some(available.iter().map(|root| root.to_string_lossy().into_owned()).collect()));
    available
}

fn read_commands(shared: Shared) {
    let (lock, condition) = &*shared;
    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let mut requests = lock.lock().unwrap();
        match line.trim() {
            "sync" => requests.sync = true,
            "sync code" => requests.code_sync = true,
            "stop" => requests.stop = true,
            "backfill on" => requests.backfill = true,
            "backfill off" => requests.backfill = false,
            _ => {}
        }
        requests.pokes += 1;
        condition.notify_all();
    }
    let mut requests = lock.lock().unwrap();
    requests.stop = true; // the app went away
    requests.pokes += 1;
    condition.notify_all();
}

/// Scratch folders ($TMPDIR/Recall-<pid>) of indexing processes that are gone (killed mid-batch).
fn remove_stale_scratch_folders() {
    let temp = std::env::temp_dir();
    let Ok(entries) = std::fs::read_dir(&temp) else { return };
    let me = std::process::id() as i32;
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        let Some(pid) = name.strip_prefix("Recall-").and_then(|pid| pid.parse::<i32>().ok()) else { continue };
        if pid == me {
            continue;
        }
        let gone = unsafe { libc::kill(pid, 0) } != 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
        if gone {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
}
