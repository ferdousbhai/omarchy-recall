//! Where Recall keeps its files: `$XDG_DATA_HOME/recall` (`~/.local/share/recall`), as the XDG base directory spec
//! has it.

use std::env;
use std::path::{Path, PathBuf};

pub fn home() -> PathBuf {
    env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

pub fn support() -> PathBuf {
    let data = env::var("XDG_DATA_HOME")
        .ok()
        .filter(|path| path.starts_with('/'))
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".local/share"));
    data.join("recall")
}

/// The index folder.
pub fn default_index() -> PathBuf {
    support().join("index")
}

/// A path with `~` for the home folder, for messages.
pub fn tilde(path: &Path) -> String {
    let home = home();
    match path.strip_prefix(&home) {
        Ok(rest) if rest.as_os_str().is_empty() => "~".into(),
        Ok(rest) => format!("~/{}", rest.display()),
        Err(_) => path.display().to_string(),
    }
}

/// `~/…` as the home folder.
pub fn expand(path: &str) -> PathBuf {
    if path == "~" {
        home()
    } else if let Some(rest) = path.strip_prefix("~/") {
        home().join(rest)
    } else {
        PathBuf::from(path)
    }
}

/// A path made absolute and tidied (".", "..", doubled slashes), without resolving symlinks.
pub fn standardize(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() { path.to_path_buf() } else { env::current_dir().unwrap_or_default().join(path) };
    let mut out = PathBuf::from("/");
    for part in absolute.components() {
        match part {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::Normal(name) => out.push(name),
            _ => {}
        }
    }
    out
}
