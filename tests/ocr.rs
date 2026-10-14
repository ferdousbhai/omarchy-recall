//! System OCR coverage: use the same Tesseract engine supplied by Omarchy.
//! ocr.png is synthetic English text rendered in Liberation Sans on a plain white background.
use std::path::Path;

#[test]
fn system_ocr_recovers_after_invalid_image_and_handles_path_spaces() {
    // OCR is optional for engine-only development. Release CI installs the English language data.
    if recall::tools::path("tesseract").is_none() {
        eprintln!("Skipping system OCR test: Tesseract not installed");
        return;
    }
    let folder = tempfile::tempdir().unwrap();
    let invalid = folder.path().join("invalid.png");
    std::fs::write(&invalid, "not an image").unwrap();
    assert!(recall::ocr::text(&invalid).is_empty());
    let image = folder.path().join("image with spaces.png");
    std::fs::copy(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/reference/ocr.png"),
        &image,
    )
    .unwrap();
    let text = recall::ocr::text(&image);
    assert_eq!(text, "Recall for Omarchy\nPayment declined: retry with exponential backoff.\nRelease deadline: 14 October 2026.");
}

#[test]
fn cli_ocr_reports_languages_without_service() {
    if recall::tools::path("tesseract").is_none() {
        return;
    }
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_recall"))
        .arg("ocr")
        .env("XDG_RUNTIME_DIR", "/nonexistent-recall-runtime")
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stdout)
    );
    let reply: serde_json::Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(reply["engine"], "tesseract");
    assert!(reply["languages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|lang| lang == "eng"));
}
