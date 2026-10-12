//! Walks the chosen folders and applies `rules`. Reads metadata only, never file contents (except the 600-byte sniff
//! that tells a real MPEG-TS video from a TypeScript file).
//!
//! For the code index (`IndexOptions::code`) it walks the code folders instead: code files only
//! (`rules::classify_code`), in a git repo only those git keeps (`GitRepos`), and none in vendored or built folders.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::folders;
use crate::model::{FileKind, IndexOptions};
use crate::rules::{self, Classification};

/// A file the crawler decided to index.
#[derive(Clone, Debug)]
pub struct Candidate {
    pub path: String,
    pub kind: FileKind,
    pub size: i64,
    pub mtime: f64,
    pub inode: u64,
    pub device: u64,
}

#[derive(Default, Debug)]
pub struct CrawlReport {
    pub candidates: Vec<Candidate>,
    /// Reason → files.
    pub skipped: BTreeMap<String, usize>,
    /// Reason → folders not walked into.
    pub skipped_folders: BTreeMap<String, usize>,
    /// Folder → files skipped as a dataset.
    pub datasets: BTreeMap<String, usize>,
}

pub fn crawl(roots: &[PathBuf], options: &IndexOptions) -> CrawlReport {
    let mut report = CrawlReport::default();
    let mut project_roots: HashSet<String> = HashSet::new();
    let mut repos = GitRepos::default();
    let mut found: Vec<Candidate> = Vec::new();
    let mut found_paths: HashSet<String> = HashSet::new(); // a folder inside another root would be walked twice
    let root_paths: HashSet<String> = roots.iter().map(|root| root.to_string_lossy().into_owned()).collect();

    for root in roots {
        let root_text = root.to_string_lossy().into_owned();
        if options.code {
            repos.enter_root(&root_text);
        } else if rules::is_project_root(root) {
            project_roots.insert(root_text.clone());
        }
        // A skip applies inside the chosen folders that contain it: a folder chosen inside a skipped one is walked
        // whole. The walk reports real paths (where a root goes through a symlink).
        let real = folders::real_path(&root_text);
        let skips: Vec<String> = options
            .excluded_folders
            .iter()
            .filter(|skip| folders::is_inside(skip, &root_text) || folders::is_inside(skip, &real))
            .flat_map(|skip| [skip.clone(), folders::real_path(skip)])
            .collect();
        let mut stack = vec![root.clone()];
        while let Some(folder) = stack.pop() {
            let Ok(entries) = fs::read_dir(&folder) else { continue };
            let mut children: Vec<_> = entries.flatten().collect();
            children.sort_by_key(|entry| entry.file_name());
            let mut subfolders = Vec::new();
            for entry in children {
                let name = entry.file_name();
                if name.to_string_lossy().starts_with('.') {
                    continue; // hidden: never walked
                }
                let path = entry.path();
                let Ok(file_type) = entry.file_type() else { continue };
                if file_type.is_dir() {
                    let reason = rules::skip_reason(&path, &skips)
                        .or_else(|| if options.code { rules::code_skip_reason(&path) } else { None });
                    if let Some(reason) = reason {
                        *report.skipped_folders.entry(reason.into()).or_default() += 1;
                        continue;
                    }
                    let text = path.to_string_lossy().into_owned();
                    if options.code {
                        repos.enter(&text);
                    } else if rules::is_project_root(&path) {
                        project_roots.insert(text);
                    }
                    subfolders.push(path);
                    continue;
                }
                let classification =
                    if options.code { rules::classify_code(&path, options) } else { rules::classify(&path, options) };
                let kind = match classification {
                    Classification::Ignore => {
                        *report.skipped.entry("other type".into()).or_default() += 1;
                        continue;
                    }
                    Classification::Skip(reason) => {
                        *report.skipped.entry(reason).or_default() += 1;
                        continue;
                    }
                    Classification::Index(kind) => kind,
                };
                let Ok(info) = fs::symlink_metadata(&path) else { continue };
                if !info.file_type().is_file() {
                    *report.skipped.entry("link or special file".into()).or_default() += 1;
                    continue;
                }
                let text = path.to_string_lossy().into_owned();
                if options.code {
                    // Too big to be written by hand; a notebook's file holds its outputs too.
                    let limit = if rules::extension(&path) == "ipynb" {
                        options.max_notebook_bytes
                    } else {
                        options.max_code_bytes
                    };
                    if info.len() as i64 > limit {
                        *report.skipped.entry("too big to be written by hand".into()).or_default() += 1;
                        continue;
                    }
                    if repos.ignores(&text) {
                        *report.skipped.entry("ignored by git".into()).or_default() += 1;
                        continue;
                    }
                }
                if !found_paths.insert(text.clone()) {
                    continue;
                }
                found.push(Candidate {
                    path: text,
                    kind,
                    size: info.len() as i64,
                    mtime: info.mtime() as f64 + info.mtime_nsec() as f64 / 1e9,
                    inode: info.ino(),
                    device: info.dev(),
                });
            }
            // Depth first, in name order.
            stack.extend(subfolders.into_iter().rev());
        }
    }

    // Inside a code project only screenshots count (assets, fixtures and datasets don't).
    let mut in_project: HashMap<String, bool> = HashMap::new();
    fn is_in_project(folder: &str, roots: &HashSet<String>, memo: &mut HashMap<String, bool>) -> bool {
        if let Some(known) = memo.get(folder) {
            return *known;
        }
        let parent = folders::parent(folder);
        let answer = roots.contains(folder) || (parent != folder && parent.len() > 1 && is_in_project(parent, roots, memo));
        memo.insert(folder.to_string(), answer);
        answer
    }
    let mut kept = Vec::new();
    for file in found {
        if !options.code
            && file.kind != FileKind::Screenshot
            && is_in_project(folders::parent(&file.path), &project_roots, &mut in_project)
        {
            *report.skipped.entry("inside a code project".into()).or_default() += 1;
        } else {
            kept.push(file);
        }
    }

    if !options.include_datasets && !options.code {
        let files: Vec<(String, String, FileKind)> = kept
            .iter()
            .map(|file| {
                let path = Path::new(&file.path);
                let stem = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                (folders::parent(&file.path).to_string(), stem, file.kind)
            })
            .collect();
        let datasets = rules::dataset_folders(&files, &root_paths, 300);
        if !datasets.is_empty() {
            kept.retain(|file| {
                let skip = datasets.contains_key(folders::parent(&file.path))
                    && matches!(file.kind, FileKind::Image | FileKind::Audio | FileKind::Video);
                if skip {
                    *report.skipped.entry("dataset".into()).or_default() += 1;
                }
                !skip
            });
            report.datasets = datasets.into_iter().collect();
        }
    }
    report.candidates = kept;
    report
}

/// The git repos a code crawl walks through, and the files each keeps (`git_files`): a file git ignores (a build, an
/// environment, data, a secret listed in .gitignore) isn't code search's to read. A folder that isn't in a repo, or a
/// system without git, has no list: everything there counts.
#[derive(Default)]
struct GitRepos {
    /// Repo folder → the files git keeps in it (paths relative to it), or None when git couldn't say.
    files: HashMap<String, Option<HashSet<String>>>,
    /// Folder → the repo it's in (None: none), as the walk found them.
    repo_of: HashMap<String, Option<String>>,
}

impl GitRepos {
    /// A chosen folder: a repo itself, or inside one (a repo's subfolder chosen on its own).
    fn enter_root(&mut self, path: &str) {
        let mut folder = path.to_string();
        loop {
            if Path::new(&folder).join(".git").exists() {
                self.files.insert(folder.clone(), git_files(&folder));
                self.repo_of.insert(path.to_string(), Some(folder));
                return;
            }
            let parent = folders::parent(&folder).to_string();
            if parent == folder || parent.len() <= 1 {
                break;
            }
            folder = parent;
        }
        self.repo_of.insert(path.to_string(), None);
    }

    /// A folder the walk goes into: a repo of its own when it has a .git folder.
    fn enter(&mut self, path: &str) {
        if Path::new(path).join(".git").is_dir() {
            self.files.insert(path.to_string(), git_files(path));
            self.repo_of.insert(path.to_string(), Some(path.to_string()));
        }
    }

    /// The repo `folder` is in, from the closest folder the walk has seen.
    fn repo(&mut self, folder: &str) -> Option<String> {
        if let Some(known) = self.repo_of.get(folder) {
            return known.clone();
        }
        let parent = folders::parent(folder).to_string();
        let answer = if parent != folder && parent.len() > 1 { self.repo(&parent) } else { None };
        self.repo_of.insert(folder.to_string(), answer.clone());
        answer
    }

    /// Whether git ignores the file at `path` (in a repo whose files git listed).
    fn ignores(&mut self, path: &str) -> bool {
        let Some(repo) = self.repo(folders::parent(path)) else { return false };
        let Some(Some(kept)) = self.files.get(&repo) else { return false };
        !kept.contains(&path[repo.len() + 1..])
    }
}

/// The files git keeps in a repo: tracked ones, and new ones its .gitignore doesn't leave out. None when git can't
/// say (no git, not a repo, an error).
fn git_files(repo: &str) -> Option<HashSet<String>> {
    let git = ["/usr/bin/git", "/usr/local/bin/git", "/bin/git"].into_iter().find(|path| Path::new(path).exists())?;
    let output = Command::new(git)
        .args(["-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        // Reading only: never wait for, or take, the repo's lock (an editor's git may be busy in it).
        .env("GIT_OPTIONAL_LOCKS", "0")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(
        output
            .stdout
            .split(|byte| *byte == 0)
            .filter(|path| !path.is_empty())
            .filter_map(|path| String::from_utf8(path.to_vec()).ok())
            .collect(),
    )
}
