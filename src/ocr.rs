//! Text in pictures: PaddleOCR's models (PP-OCR through RapidOCR on ONNX Runtime) in a helper process, `recall-ocr`,
//! one JSON line per picture each way. It's set up by scripts/setup-ocr.sh into ~/.local/share/recall/ocr;
//! `$RECALL_OCR` names another. Without it, screenshots are still searched by what they show, only not by their words.

use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{LazyLock, Mutex};

use serde::Deserialize;

use crate::paths;
use crate::tools;

#[derive(Deserialize)]
struct Reply {
    ready: Option<bool>,
    languages: Option<Vec<String>>,
    text: Option<String>,
}

struct Helper {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
    languages: Vec<String>,
}

impl Helper {
    fn start(executable: &Path) -> Option<Self> {
        let mut child = Command::new(executable)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .ok()?;
        let input = child.stdin.take()?;
        let output = BufReader::new(child.stdout.take()?);
        let mut helper = Self { child, input, output, languages: Vec::new() };
        let ready = helper.reply()?;
        if ready.ready != Some(true) {
            let _ = helper.child.kill();
            return None;
        }
        helper.languages = ready.languages.unwrap_or_default();
        Some(helper)
    }

    fn reply(&mut self) -> Option<Reply> {
        let mut line = String::new();
        loop {
            line.clear();
            if self.output.read_line(&mut line).ok()? == 0 {
                return None; // it exited
            }
            if let Ok(reply) = serde_json::from_str::<Reply>(line.trim()) {
                return Some(reply);
            } // a stray line (a library's print): skip it
        }
    }

    fn read(&mut self, path: &str) -> Option<String> {
        let request = serde_json::json!({ "path": path }).to_string();
        writeln!(self.input, "{request}").ok()?;
        self.input.flush().ok()?;
        self.reply()?.text
    }
}

fn command() -> Option<PathBuf> {
    if let Ok(named) = std::env::var("RECALL_OCR") {
        if !named.is_empty() {
            return Some(PathBuf::from(named));
        }
    }
    let here = std::env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf));
    here.map(|dir| dir.join("recall-ocr"))
        .into_iter()
        .chain([paths::support().join("ocr/bin/recall-ocr")])
        .find(|candidate| tools::is_executable(candidate))
}

/// Started the first time a picture needs reading, then kept for this process (None when it isn't set up).
static HELPER: LazyLock<Mutex<Option<Helper>>> = LazyLock::new(|| {
    let helper = match command() {
        None => {
            eprintln!("OCR isn't set up (scripts/setup-ocr.sh): no text from screenshots");
            None
        }
        Some(executable) => {
            let helper = Helper::start(&executable);
            if helper.is_none() {
                eprintln!("OCR didn't start: {}", executable.display());
            }
            helper
        }
    };
    Mutex::new(helper)
});

/// The text in a picture, top to bottom, a line each; "" without OCR.
pub fn text(image: &Path) -> String {
    let mut helper = HELPER.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    helper.as_mut().and_then(|helper| helper.read(&image.to_string_lossy())).unwrap_or_default()
}

/// Whether the OCR models read Arabic (the helper says which scripts it reads when it starts).
pub fn reads_arabic() -> bool {
    let helper = HELPER.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    helper.as_ref().is_some_and(|helper| helper.languages.iter().any(|language| language == "ar"))
}
