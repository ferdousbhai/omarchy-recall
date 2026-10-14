//! Linux inotify with debounced sync and periodic recovery for overflow/watch exhaustion.
use super::{folders, Value};
use std::collections::HashSet;
use std::ffi::CString;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::Path;
use std::time::{Duration, Instant};
const NOISE: &[&str] = &[
    "node_modules",
    "bower_components",
    "venv",
    "site-packages",
    "__pycache__",
    "DerivedData",
    "Pods",
    "Carthage",
    "vendor",
    "build",
    "dist",
    "target",
    "out",
    "Vendor",
    "third_party",
];
pub(super) struct Watcher {
    fd: Option<OwnedFd>,
    watched: HashSet<(std::path::PathBuf, bool)>,
    changed: Option<Instant>,
    first: Option<Instant>,
    recovery: Instant,
}
impl Watcher {
    pub fn new(config: &Value) -> Self {
        let fd = unsafe { libc::inotify_init1(libc::IN_NONBLOCK | libc::IN_CLOEXEC) };
        let mut result = Self {
            fd: if fd < 0 {
                None
            } else {
                Some(unsafe { OwnedFd::from_raw_fd(fd) })
            },
            watched: HashSet::new(),
            changed: None,
            first: None,
            recovery: Instant::now(),
        };
        result.add_roots(config);
        result
    }
    fn add_roots(&mut self, config: &Value) {
        for (key, excluded_key, code) in [
            ("roots", "excluded_folders", false),
            ("code_roots", "code_excluded", true),
        ] {
            let mut excluded = folders(config, excluded_key);
            for key in ["index_directory", "models_directory"] {
                if let Some(path) = config[key].as_str() {
                    excluded.push(path.to_string());
                }
            }
            for root in folders(config, key) {
                self.add(Path::new(&root), &excluded, code);
            }
        }
    }
    fn add(&mut self, path: &Path, excluded: &[String], code: bool) {
        if self.watched.contains(&(path.to_path_buf(), code))
            || excluded.iter().any(|p| path.starts_with(p))
        {
            return;
        }
        let Some(fd) = &self.fd else { return };
        use std::os::unix::ffi::OsStrExt;
        let Ok(name) = CString::new(path.as_os_str().as_bytes()) else {
            return;
        };
        let mask = libc::IN_CLOSE_WRITE
            | libc::IN_ATTRIB
            | libc::IN_MOVED_FROM
            | libc::IN_MOVED_TO
            | libc::IN_CREATE
            | libc::IN_DELETE
            | libc::IN_DELETE_SELF
            | libc::IN_MOVE_SELF
            | libc::IN_ONLYDIR;
        if unsafe { libc::inotify_add_watch(fd.as_raw_fd(), name.as_ptr(), mask) } < 0 {
            return;
        }
        self.watched.insert((path.to_path_buf(), code));
        if let Ok(entries) = std::fs::read_dir(path) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                if !name.starts_with('.')
                    && !NOISE.contains(&name.as_ref())
                    && !name.ends_with(".noindex")
                    && entry.file_type().is_ok_and(|t| t.is_dir())
                {
                    self.add(&entry.path(), excluded, code);
                }
            }
        }
    }
    pub fn tick(&mut self, config: &Value) -> bool {
        let mut buffer = [0u8; 65536];
        let mut activity = false;
        let mut rebuild = false;
        if let Some(fd) = &self.fd {
            loop {
                let size =
                    unsafe { libc::read(fd.as_raw_fd(), buffer.as_mut_ptr().cast(), buffer.len()) };
                if size <= 0 {
                    break;
                }
                let mut offset = 0;
                while offset + 16 <= size as usize {
                    let mask =
                        u32::from_ne_bytes(buffer[offset + 4..offset + 8].try_into().unwrap());
                    let len =
                        u32::from_ne_bytes(buffer[offset + 12..offset + 16].try_into().unwrap())
                            as usize;
                    if offset + 16 + len > size as usize {
                        break;
                    }
                    let name = &buffer[offset + 16..offset + 16 + len];
                    if name.first() != Some(&b'.') {
                        activity = true;
                    }
                    rebuild |= mask
                        & (libc::IN_Q_OVERFLOW
                            | libc::IN_IGNORED
                            | libc::IN_DELETE_SELF
                            | libc::IN_MOVE_SELF)
                        != 0
                        || mask & libc::IN_ISDIR != 0;
                    offset += 16 + len;
                }
            }
        }
        if rebuild {
            // Rebuild after directory topology changes; event bursts still produce one sync.
            let first = self.first;
            let changed = self.changed;
            let recovery = self.recovery;
            *self = Self::new(config);
            self.first = first;
            self.changed = changed;
            self.recovery = recovery;
        }
        if activity {
            let now = Instant::now();
            self.changed = Some(now);
            self.first.get_or_insert(now);
        }
        let periodic = self.recovery.elapsed() >= Duration::from_secs(300);
        let settled = self
            .changed
            .is_some_and(|t| t.elapsed() >= Duration::from_millis(750))
            || self
                .first
                .is_some_and(|t| t.elapsed() >= Duration::from_secs(5));
        if periodic || settled {
            self.changed = None;
            self.first = None;
            self.recovery = Instant::now();
            if periodic {
                *self = Self::new(config);
            }
            true
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn observes_new_and_nested_files_after_debounce() {
        let directory = tempfile::tempdir().unwrap();
        let config = serde_json::json!({"roots":[directory.path()],"code_roots":[],"excluded_folders":[],"code_excluded":[]});
        let mut watcher = Watcher::new(&config);
        std::fs::write(directory.path().join("file"), "one").unwrap();
        assert!(!watcher.tick(&config));
        std::thread::sleep(Duration::from_millis(800));
        assert!(watcher.tick(&config));
        std::fs::create_dir(directory.path().join("nested")).unwrap();
        watcher.tick(&config);
        std::fs::write(directory.path().join("nested/file"), "two").unwrap();
        watcher.tick(&config);
        std::thread::sleep(Duration::from_millis(800));
        assert!(watcher.tick(&config));
    }
}
