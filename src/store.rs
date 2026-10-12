//! The on-disk index: one SQLite file with files, their segments (with fp16 vectors), and an FTS5 table.
//!
//! Vectors live in the same database as everything else, so a crash can never leave them out of step with the rows
//! that describe them. At this scale (~20k vectors, ~30 MB) brute-force search over an in-memory copy takes a few
//! milliseconds; no vector database needed.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{bail, Result};
use rusqlite::types::Value;
use rusqlite::{params, params_from_iter, Connection, OptionalExtension};

use crate::crawler::Candidate;
use crate::folders;
use crate::model::{FileKind, Modality, SegmentKind};
use crate::paths;
use crate::text;
use crate::vectors;

/// The main index's file in the index folder.
pub const MAIN_FILE: &str = "index.sqlite";
/// The code index is a file of its own beside it: code search never moves the rest (its meaning scale, its keyword
/// results, the vectors search keeps in memory), and turning it off deletes one file.
pub const CODE_FILE: &str = "code.sqlite";
pub const SCHEMA_VERSION: i64 = 5;

/// Keyword search's tokenizer. Marks (M*) count as letters, so Bengali vowel signs and Arabic harakat stay inside
/// their words: by default SQLite splits on them ("কলকাতায়" became "কলক", "ত", "য").
const TOKENIZER: &str = "porter unicode61 remove_diacritics 2 categories 'L* N* Co M*'";

/// A file as the index knows it.
#[derive(Clone, Debug)]
pub struct FileRecord {
    pub id: i64,
    pub path: String,
    pub kind: FileKind,
    pub size: i64,
    pub mtime: f64,
    pub inode: u64,
    pub device: u64,
    pub state: String,
}

pub type Info = BTreeMap<String, String>;

pub struct Store {
    pub directory: PathBuf,
    pub file: PathBuf,
    pub db: Connection,
    /// When set, `pending`, `pending_count` and `counts` see only files under these folders: the app's helper sets the
    /// folders that are connected right now, so files on an unplugged drive neither fail nor count as waiting.
    scope: Option<Vec<String>>,
}

const FILE_COLUMNS: &str = "id, path, kind, size, mtime, inode, device, state";

fn record(row: &rusqlite::Row) -> rusqlite::Result<FileRecord> {
    Ok(FileRecord {
        id: row.get(0)?,
        path: row.get(1)?,
        kind: FileKind::parse(&row.get::<_, String>(2)?).unwrap_or(FileKind::Doc),
        size: row.get(3)?,
        mtime: row.get(4)?,
        inode: row.get::<_, i64>(5)? as u64,
        device: row.get::<_, i64>(6)? as u64,
        state: row.get(7)?,
    })
}

/// "everything that starts with `root`/" as a range on the UNIQUE path index.
fn range(root: &str) -> (String, String) {
    let prefix = if root.ends_with('/') { root.to_string() } else { format!("{root}/") };
    let end = format!("{prefix}\u{10FFFF}");
    (prefix, end)
}

pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or(Duration::ZERO).as_secs_f64()
}

impl Store {
    pub fn open(directory: &Path, name: &str) -> Result<Self> {
        std::fs::create_dir_all(directory)?;
        let file = directory.join(name);
        let db = Connection::open(&file)?;
        db.busy_timeout(Duration::from_secs(5))?;
        db.execute_batch("PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;")?;
        let mut store = Self { directory: directory.to_path_buf(), file, db, scope: None };
        store.migrate()?;
        Ok(store)
    }

    pub fn is_code(&self) -> bool {
        self.file.file_name().and_then(|n| n.to_str()) == Some(CODE_FILE)
    }

    /// Bytes on disk: the database and its write-ahead log.
    pub fn size_of(file: &Path) -> u64 {
        let wal = PathBuf::from(format!("{}-wal", file.display()));
        [file.to_path_buf(), wal].iter().filter_map(|path| path.metadata().ok()).map(|m| m.len()).sum()
    }

    fn migrate(&mut self) -> Result<()> {
        let version: i64 = self.db.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version > SCHEMA_VERSION {
            bail!("this index was made by a newer Recall (schema {version})");
        }
        if version == SCHEMA_VERSION {
            return Ok(());
        }
        // The app and its helper can open the index at the same moment: one makes it while the other waits.
        self.db.busy_timeout(Duration::from_secs(60))?;
        let tx = self.db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let version: i64 = tx.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version < SCHEMA_VERSION {
            // Older layouts came before this engine: start over (the next sync reads everything again).
            tx.execute_batch(
                "DROP TABLE IF EXISTS fts; DROP TABLE IF EXISTS segments; DROP TABLE IF EXISTS files; \
                 DROP TABLE IF EXISTS meta;",
            )?;
            tx.execute_batch(&format!(
                "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE files(
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    inode INTEGER NOT NULL,
                    device INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',   -- pending | done | partial | skipped | failed
                    detail TEXT,                              -- why skipped/failed, or how far a long file has been read
                    info TEXT,                                -- JSON: pixels, pages, duration, date taken, read_to/read_end
                    indexed_at REAL
                );
                CREATE INDEX files_inode ON files(device, inode);
                CREATE INDEX files_state ON files(state, kind);
                CREATE TABLE segments(
                    id INTEGER PRIMARY KEY,
                    file INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,       -- SegmentKind
                    modality TEXT NOT NULL,   -- the vector's Modality ('none' without one)
                    loc REAL,                 -- PDF page, chunk, start second, or first line or cell
                    loc_end REAL,
                    excerpt TEXT,
                    vec BLOB                  -- 768 × float16, L2-normalized
                );
                CREATE INDEX segments_file ON segments(file);
                CREATE VIRTUAL TABLE fts USING fts5(name, body, original UNINDEXED, tokenize = \"{TOKENIZER}\");
                PRAGMA user_version = {SCHEMA_VERSION};"
            ))?;
        }
        tx.commit()?;
        self.db.busy_timeout(Duration::from_secs(5))?;
        Ok(())
    }

    /// Runs `body` in one transaction (BEGIN IMMEDIATE), rolled back if it fails.
    pub fn transaction<T>(&self, body: impl FnOnce() -> Result<T>) -> Result<T> {
        self.db.execute_batch("BEGIN IMMEDIATE")?;
        match body() {
            Ok(value) => {
                self.db.execute_batch("COMMIT")?;
                Ok(value)
            }
            Err(error) => {
                let _ = self.db.execute_batch("ROLLBACK");
                Err(error)
            }
        }
    }

    pub fn set_scope(&mut self, scope: Option<Vec<String>>) {
        self.scope = scope;
    }

    /// The scope as SQL and its values.
    fn scope_sql(&self) -> (String, Vec<Value>) {
        match &self.scope {
            None => (String::new(), Vec::new()),
            Some(roots) if roots.is_empty() => (" AND 0".into(), Vec::new()),
            Some(roots) => {
                let sql = roots.iter().map(|_| "(path >= ? AND path < ?)").collect::<Vec<_>>().join(" OR ");
                let values = roots.iter().flat_map(|root| {
                    let (from, to) = range(root);
                    [Value::Text(from), Value::Text(to)]
                });
                (format!(" AND ({sql})"), values.collect())
            }
        }
    }

    // MARK: Meta

    pub fn meta(&self, key: &str) -> Result<Option<String>> {
        Ok(self.db.query_row("SELECT value FROM meta WHERE key = ?", [key], |row| row.get(0)).optional()?)
    }

    pub fn set_meta(&self, key: &str, value: Option<&str>) -> Result<()> {
        match value {
            Some(value) => self.db.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [key, value],
            )?,
            None => self.db.execute("DELETE FROM meta WHERE key = ?", [key])?,
        };
        Ok(())
    }

    // MARK: Files

    /// Every file under one of `roots`.
    pub fn files_under(&self, roots: &[String]) -> Result<Vec<FileRecord>> {
        let mut out = Vec::new();
        let mut statement = self.db.prepare(&format!("SELECT {FILE_COLUMNS} FROM files WHERE path >= ? AND path < ?"))?;
        // A folder inside another one listed is already covered (each file once).
        for root in roots.iter().filter(|root| !roots.iter().any(|other| folders::is_inside(root, other))) {
            let (from, to) = range(root);
            for file in statement.query_map([from, to], record)? {
                out.push(file?);
            }
        }
        Ok(out)
    }

    /// Adds a file (pending) and returns its id.
    pub fn insert(&self, file: &Candidate) -> Result<i64> {
        self.db.execute(
            "INSERT INTO files(path, kind, size, mtime, inode, device) VALUES(?, ?, ?, ?, ?, ?)",
            params![file.path, file.kind.as_str(), file.size, file.mtime, file.inode as i64, file.device as i64],
        )?;
        let id = self.db.last_insert_rowid();
        self.add_name_row(id, &file.path)?;
        Ok(id)
    }

    /// The file changed on disk: drop what was extracted from it and queue it again.
    pub fn requeue_as(&self, id: i64, file: &Candidate) -> Result<()> {
        self.delete_segments(id, true)?;
        self.db.execute(
            "UPDATE files SET kind = ?, size = ?, mtime = ?, inode = ?, device = ?, state = 'pending', detail = NULL, \
             info = NULL, indexed_at = NULL WHERE id = ?",
            params![file.kind.as_str(), file.size, file.mtime, file.inode as i64, file.device as i64, id],
        )?;
        Ok(())
    }

    /// Same inode, size and date at a new path: a move or rename. Only the path changes; nothing is re-embedded.
    pub fn move_to(&self, id: i64, path: &str) -> Result<()> {
        self.db.execute("UPDATE files SET path = ? WHERE id = ?", params![path, id])?;
        self.db.execute(
            "UPDATE fts SET name = ?, body = ? WHERE rowid IN (SELECT id FROM segments WHERE file = ? AND kind = 'name')",
            params![text::normalized(&name_text(path)), text::normalized(&folder_text(path)), id],
        )?;
        Ok(())
    }

    pub fn delete(&self, id: i64) -> Result<()> {
        self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE file = ?)", [id])?;
        self.db.execute("DELETE FROM files WHERE id = ?", [id])?;
        Ok(())
    }

    /// Removes every file that isn't under one of `roots` (folders taken out of the app). Returns how many went.
    pub fn prune(&self, roots: &[String]) -> Result<usize> {
        let prefixes: Vec<String> =
            roots.iter().map(|root| if root.ends_with('/') { root.clone() } else { format!("{root}/") }).collect();
        let mut gone = Vec::new();
        {
            let mut statement = self.db.prepare("SELECT id, path FROM files")?;
            let rows = statement.query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)))?;
            for row in rows {
                let (id, path) = row?;
                if !prefixes.iter().any(|prefix| path.starts_with(prefix)) {
                    gone.push(id);
                }
            }
        }
        if !gone.is_empty() {
            self.transaction(|| {
                for id in &gone {
                    self.delete(*id)?;
                }
                Ok(())
            })?;
        }
        Ok(gone.len())
    }

    /// Queues every file again and forgets the fingerprint (after the model or settings changed).
    pub fn reset_all(&self) -> Result<()> {
        self.transaction(|| {
            self.db.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE kind != 'name')", [])?;
            self.db.execute("DELETE FROM segments WHERE kind != 'name'", [])?;
            self.db.execute("UPDATE files SET state = 'pending', detail = NULL, info = NULL, indexed_at = NULL", [])?;
            self.set_meta("fingerprint", None)
        })
    }

    /// Files waiting to be embedded, in indexing order; `only` narrows it to some files (just-added ones).
    pub fn pending(&self, limit: Option<usize>, only: Option<&[i64]>) -> Result<Vec<FileRecord>> {
        if only.is_some_and(|ids| ids.is_empty()) {
            return Ok(Vec::new());
        }
        let filter = only
            .map(|ids| format!(" AND id IN ({})", ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",")))
            .unwrap_or_default();
        let (scope, mut values) = self.scope_sql();
        values.push(Value::Integer(limit.map(|n| n as i64).unwrap_or(-1)));
        let sql = format!(
            "SELECT {FILE_COLUMNS} FROM files WHERE state = 'pending'{filter}{scope} ORDER BY {}, mtime DESC LIMIT ?",
            FileKind::INDEXING_ORDER_SQL
        );
        let mut statement = self.db.prepare(&sql)?;
        let rows = statement.query_map(params_from_iter(values.iter()), record)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    pub fn pending_count(&self) -> Result<usize> {
        let (scope, values) = self.scope_sql();
        let sql = format!("SELECT COUNT(*) FROM files WHERE state = 'pending'{scope}");
        Ok(self.db.query_row(&sql, params_from_iter(values.iter()), |row| row.get::<_, i64>(0))? as usize)
    }

    /// Long files with pages or chunks still to read after their first step, in indexing order.
    pub fn unfinished(&self) -> Result<Vec<FileRecord>> {
        let (scope, values) = self.scope_sql();
        let sql = format!(
            "SELECT {FILE_COLUMNS} FROM files WHERE state = 'partial'{scope} ORDER BY {}, mtime DESC",
            FileKind::INDEXING_ORDER_SQL
        );
        let mut statement = self.db.prepare(&sql)?;
        let rows = statement.query_map(params_from_iter(values.iter()), record)?;
        Ok(rows.collect::<rusqlite::Result<_>>()?)
    }

    /// How many long files are unfinished, and how many of their pages and chunks are left to read.
    pub fn unfinished_count(&self) -> Result<(usize, usize)> {
        let (scope, values) = self.scope_sql();
        let sql = format!(
            "SELECT COUNT(*), SUM(MAX(0, CAST(json_extract(info, '$.read_end') AS INTEGER) \
             - CAST(json_extract(info, '$.read_to') AS INTEGER))) FROM files WHERE state = 'partial'{scope}"
        );
        let (files, left): (i64, Option<i64>) =
            self.db.query_row(&sql, params_from_iter(values.iter()), |row| Ok((row.get(0)?, row.get(1)?)))?;
        Ok((files as usize, left.unwrap_or(0) as usize))
    }

    /// What indexing learned about a file (`finish`'s `info`).
    pub fn info(&self, id: i64) -> Result<Info> {
        let json: Option<String> =
            self.db.query_row("SELECT info FROM files WHERE id = ?", [id], |row| row.get(0)).optional()?.flatten();
        Ok(json.and_then(|json| parse_info(&json)).unwrap_or_default())
    }

    /// The last page or chunk stored for a file (where a long file's reading got to, when its info doesn't say).
    pub fn last_place(&self, id: i64) -> Result<Option<usize>> {
        let last: Option<f64> = self.db.query_row(
            "SELECT MAX(loc) FROM segments WHERE file = ? AND kind IN ('page', 'page_image', 'chunk')",
            [id],
            |row| row.get(0),
        )?;
        Ok(last.map(|place| place as usize))
    }

    pub fn finish(&self, id: i64, state: &str, detail: Option<&str>, info: &Info) -> Result<()> {
        let json = if info.is_empty() { None } else { Some(serde_json::to_string(info)?) };
        self.db.execute(
            "UPDATE files SET state = ?, detail = ?, info = ?, indexed_at = ? WHERE id = ?",
            params![state, detail, json, now(), id],
        )?;
        Ok(())
    }

    // MARK: Segments

    /// Adds a segment; `text` goes into keyword search (normalized, see `text`), `vector` into meaning search.
    #[allow(clippy::too_many_arguments)]
    pub fn add_segment(&self, file: i64, kind: SegmentKind, modality: Modality, loc: Option<f64>,
                       loc_end: Option<f64>, excerpt: Option<&str>, text: Option<&str>, vector: Option<&[f32]>)
        -> Result<()> {
        let modality = if vector.is_some() { modality } else { Modality::None };
        let blob = vector.map(vectors::half);
        self.db.execute(
            "INSERT INTO segments(file, kind, modality, loc, loc_end, excerpt, vec) VALUES(?, ?, ?, ?, ?, ?, ?)",
            params![file, kind.as_str(), modality.as_str(), loc, loc_end, excerpt, blob],
        )?;
        if let Some(text) = text.filter(|text| !text.is_empty()) {
            let (body, original) = keyword_text(text);
            self.db.execute(
                "INSERT INTO fts(rowid, name, body, original) VALUES(?, '', ?, ?)",
                params![self.db.last_insert_rowid(), body, original],
            )?;
        }
        Ok(())
    }

    fn add_name_row(&self, file: i64, path: &str) -> Result<()> {
        self.db.execute("INSERT INTO segments(file, kind, modality) VALUES(?, 'name', 'none')", [file])?;
        self.db.execute(
            "INSERT INTO fts(rowid, name, body) VALUES(?, ?, ?)",
            params![self.db.last_insert_rowid(), text::normalized(&name_text(path)), text::normalized(&folder_text(path))],
        )?;
        Ok(())
    }

    fn delete_segments(&self, file: i64, keeping_name: bool) -> Result<()> {
        let filter = if keeping_name { "AND kind != 'name'" } else { "" };
        self.db
            .execute(&format!("DELETE FROM fts WHERE rowid IN (SELECT id FROM segments WHERE file = ? {filter})"), [file])?;
        self.db.execute(&format!("DELETE FROM segments WHERE file = ? {filter}"), [file])?;
        Ok(())
    }

    // MARK: Stats

    pub fn counts(&self) -> Result<Counts> {
        let mut counts = Counts::default();
        let (scope, values) = self.scope_sql();
        {
            let sql = format!("SELECT kind, state, COUNT(*) FROM files WHERE 1{scope} GROUP BY kind, state");
            let mut statement = self.db.prepare(&sql)?;
            let rows = statement.query_map(params_from_iter(values.iter()), |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, i64>(2)?))
            })?;
            for row in rows {
                let (kind, state, count) = row?;
                counts.by_kind_and_state.entry(kind).or_default().insert(state, count as usize);
            }
        }
        (counts.unfinished, counts.left) = self.unfinished_count()?;
        let in_scope =
            if self.scope.is_none() { String::new() } else { format!(" AND file IN (SELECT id FROM files WHERE 1{scope})") };
        let sql = format!("SELECT COUNT(*), COUNT(vec) FROM segments WHERE kind != 'name'{in_scope}");
        let (segments, vectors): (i64, i64) =
            self.db.query_row(&sql, params_from_iter(values.iter()), |row| Ok((row.get(0)?, row.get(1)?)))?;
        counts.segments = segments as usize;
        counts.vectors = vectors as usize;
        Ok(counts)
    }
}

#[derive(Default, Debug)]
pub struct Counts {
    pub by_kind_and_state: BTreeMap<String, BTreeMap<String, usize>>,
    pub vectors: usize,
    pub segments: usize,
    /// Long files still being read, and their pages and chunks left.
    pub unfinished: usize,
    pub left: usize,
}

/// `info` as stored: a JSON object of strings.
pub fn parse_info(json: &str) -> Option<Info> {
    let value: serde_json::Value = serde_json::from_str(json).ok()?;
    let object = value.as_object()?;
    Some(
        object
            .iter()
            .map(|(key, value)| (key.clone(), value.as_str().map(String::from).unwrap_or_else(|| value.to_string())))
            .collect(),
    )
}

/// `body` and `original` for keyword search: the normalized text, and the text as written if that differs.
pub fn keyword_text(text: &str) -> (String, Option<String>) {
    let normalized = text::normalized(text);
    let original = (normalized != text).then(|| text.to_string());
    (normalized, original)
}

/// "Screenshot 2026-10-01 at 9.41.07.png" → searchable words.
pub fn name_text(path: &str) -> String {
    Path::new(path).file_name().map(|name| name.to_string_lossy().into_owned()).unwrap_or_default()
}

/// The folders a file sits in, relative to the home folder ("Downloads Invoices"), as low-weight keywords.
pub fn folder_text(path: &str) -> String {
    let home = paths::home().to_string_lossy().into_owned();
    let mut folder = folders::parent(path).to_string();
    if folder.starts_with(&home) {
        folder = folder[home.len()..].to_string();
    }
    folder.split('/').filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" ")
}
