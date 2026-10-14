//! OCR through Omarchy's system Tesseract. No helper environment or downloaded models.
use std::ffi::OsStr;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::thread;
use std::time::{Duration, Instant};

use crate::tools;

const LIMIT: usize = 1024 * 1024;
const TIMEOUT: Duration = Duration::from_secs(30);

/// Separate invocations recover naturally after an image fails. Bound time and both output pipes,
/// and always reap the child, including on timeout or an OS error.
fn run(executable: &Path, arguments: &[&OsStr], timeout: Duration) -> Option<String> {
    let mut child = Command::new(executable)
        .args(arguments)
        .env("OMP_THREAD_LIMIT", "2")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;
    let stdout = child.stdout.take().expect("piped");
    let stderr = child.stderr.take().expect("piped");
    let out = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take((LIMIT + 1) as u64)
            .read_to_end(&mut bytes)
            .ok()?;
        (bytes.len() <= LIMIT).then_some(bytes)
    });
    let err = thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take((LIMIT + 1) as u64)
            .read_to_end(&mut bytes)
            .is_ok()
            && bytes.len() <= LIMIT
    });
    let started = Instant::now();
    let success = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status.success(),
            Ok(None) if started.elapsed() < timeout => thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                break false;
            }
        }
    };
    let bytes = out.join().ok().flatten();
    let errors_ok = err.join().unwrap_or(false);
    if success && errors_ok {
        bytes.map(|bytes| String::from_utf8_lossy(&bytes).trim().to_string())
    } else {
        None
    }
}

static LANGUAGES: LazyLock<Vec<String>> = LazyLock::new(|| {
    tools::path("tesseract")
        .and_then(|exe| run(&exe, &[OsStr::new("--list-langs")], TIMEOUT))
        .map(|text| {
            text.lines()
                .skip(1)
                .map(str::to_owned)
                .filter(|lang| lang != "osd")
                .collect()
        })
        .unwrap_or_default()
});

/// Recognize all installed languages, English first when available. Additional system language
/// packs work without a Recall-specific configuration or download step.
pub fn text(image: &Path) -> String {
    let Some(executable) = tools::path("tesseract") else {
        return String::new();
    };
    let mut languages = LANGUAGES.clone();
    languages.sort_by_key(|language| language != "eng");
    if languages.is_empty() {
        return String::new();
    }
    let languages = languages.join("+");
    let result = run(
        &executable,
        &[
            image.as_os_str(),
            OsStr::new("stdout"),
            OsStr::new("-l"),
            OsStr::new(&languages),
        ],
        TIMEOUT,
    );
    if result.is_none() {
        eprintln!("OCR failed or timed out: {}", image.display());
    }
    result.unwrap_or_default()
}

pub fn reads_arabic() -> bool {
    LANGUAGES
        .iter()
        .any(|language| language == "ara" || language == "script/Arabic")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn script(body: &str) -> (tempfile::TempDir, std::path::PathBuf) {
        let directory = tempfile::tempdir().unwrap();
        let file = directory.path().join("ocr");
        std::fs::write(&file, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o700)).unwrap();
        (directory, file)
    }

    #[test]
    fn bounds_runtime_and_recovers() {
        let (_slow_dir, slow) = script("exec sleep 5");
        let started = Instant::now();
        assert!(run(&slow, &[], Duration::from_millis(50)).is_none());
        assert!(started.elapsed() < Duration::from_secs(2));
        let (_good_dir, good) = script("printf 'recognized text\\n'");
        assert_eq!(run(&good, &[], TIMEOUT).as_deref(), Some("recognized text"));
    }

    #[test]
    fn rejects_failure_and_excessive_output() {
        let (_bad_dir, bad) = script("printf partial; exit 1");
        assert!(run(&bad, &[], TIMEOUT).is_none());
        let (_large_dir, large) = script("exec head -c 2097152 /dev/zero");
        assert!(run(&large, &[], TIMEOUT).is_none());
        let (_errors_dir, errors) = script("head -c 2097152 /dev/zero >&2");
        assert!(run(&errors, &[], TIMEOUT).is_none());
    }
}
