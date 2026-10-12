//! Which folders are searched: the folders chosen, minus the folders skipped inside them. A skip applies inside the
//! chosen folders that contain it, and a chosen folder is searched whole even inside a skipped one.

use std::ffi::{CStr, CString};

/// Whether `path` (a folder or a file) is searched.
pub fn is_searched(path: &str, folders: &[String], skipped: &[String]) -> bool {
    folders.iter().any(|folder| {
        (path == folder || is_inside(path, folder))
            && !skipped.iter().any(|skip| is_inside(skip, folder) && (path == skip || is_inside(path, skip)))
    })
}

pub fn is_inside(path: &str, folder: &str) -> bool {
    if folder.ends_with('/') {
        path.starts_with(folder)
    } else {
        path.len() > folder.len() && path.starts_with(folder) && path.as_bytes()[folder.len()] == b'/'
    }
}

/// The folder a path is in.
pub fn parent(path: &str) -> &str {
    match path.trim_end_matches('/').rfind('/') {
        Some(0) => "/",
        Some(at) => &path[..at],
        None => "",
    }
}

/// The path as a walk of the folder reports it (symlinks resolved).
pub fn real_path(path: &str) -> String {
    let Ok(text) = CString::new(path) else { return path.to_string() };
    // SAFETY: realpath gets a NUL-terminated path and returns a malloc'ed one (freed here), or null.
    unsafe {
        let real = libc::realpath(text.as_ptr(), std::ptr::null_mut());
        if real.is_null() {
            return path.to_string();
        }
        let out = CStr::from_ptr(real).to_string_lossy().into_owned();
        libc::free(real.cast());
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn skips_apply_inside_chosen_folders() {
        let folders = strings(&["/h/Downloads", "/h/Downloads/Media/Courses"]);
        let skipped = strings(&["/h/Downloads/Media"]);
        assert!(is_searched("/h/Downloads/a.pdf", &folders, &skipped));
        assert!(!is_searched("/h/Downloads/Media/b.mp4", &folders, &skipped));
        assert!(is_searched("/h/Downloads/Media/Courses/c.mp4", &folders, &skipped));
        assert!(!is_searched("/h/Documents/d.txt", &folders, &skipped));
        assert!(!is_inside("/h/Downloads2/x", "/h/Downloads"));
        assert_eq!(parent("/h/Downloads/a.pdf"), "/h/Downloads");
    }
}
