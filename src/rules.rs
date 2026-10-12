//! What gets indexed. The defaults come from a real developer's computer: 57k TypeScript files that an extension-based
//! type check calls movies, a repo holding a 55k-clip dataset, and screenshot tools saving into code projects.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Read;
use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;

use crate::model::{FileKind, IndexOptions};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Classification {
    Index(FileKind),
    /// A reason worth reporting ("code or data file").
    Skip(String),
    /// A type we don't handle at all.
    Ignore,
}

fn set(items: &[&'static str]) -> HashSet<&'static str> {
    items.iter().copied().collect()
}

/// Folders never worth walking into. Hidden folders are skipped by the walk itself.
static SKIPPED_FOLDER_NAMES: LazyLock<HashSet<&str>> = LazyLock::new(|| {
    set(&["node_modules", "bower_components", "venv", "site-packages", "__pycache__", "DerivedData", "Pods", "Carthage"])
});

/// Any of these in a folder makes it a code project, where only screenshots are indexed.
const PROJECT_MARKERS: [&str; 14] = [
    ".git", "package.json", "Package.swift", "pyproject.toml", "setup.py", "requirements.txt", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "CMakeLists.txt",
];

static DOC_EXTENSIONS: LazyLock<HashSet<&str>> =
    LazyLock::new(|| set(&["txt", "text", "md", "markdown", "rtf", "docx", "doc", "odt", "html", "htm", "pptx", "xlsx"]));

/// Source code and data files: off by default (an editor and ripgrep search them better, and they flood an index).
static CODE_EXTENSIONS: LazyLock<HashSet<&str>> = LazyLock::new(|| {
    set(&[
        "py", "ipynb", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "m", "mm", "swift", "tsx", "cts", "js", "jsx", "mjs",
        "cjs", "go", "rs", "java", "kt", "kts", "scala", "rb", "php", "cs", "fs", "sh", "bash", "zsh", "fish", "ps1",
        "lua", "pl", "r", "jl", "dart", "sql", "vue", "svelte", "css", "scss", "sass", "less", "json", "jsonl",
        "yaml", "yml", "toml", "xml", "plist", "gradle", "cmake", "mk", "lock", "log", "csv", "tsv", "ini", "cfg",
        "conf", "proto", "graphql", "tf", "nix", "zig", "hs", "ex", "exs", "erl", "clj", "elm", "ml", "vim", "el",
        "asm", "metal", "glsl", "hlsl", "wgsl", "cu", "pyi", "pyc",
    ])
});

/// GNOME's ("Screenshot from …" and its translations), KDE Spectacle's ("Screenshot_…"), and other tools'.
const SCREENSHOT_PREFIXES: [&str; 9] = [
    "Screenshot", "Screen Shot", "CleanShot", "SCR-", "Capture d’écran", "Capture d'écran", "Captura de pantalla",
    "Bildschirmfoto", "Schermata",
];

// MARK: Code (the code index, `IndexOptions::code`)

/// Source files code search reads.
static SOURCE_EXTENSIONS: LazyLock<HashSet<&str>> = LazyLock::new(|| {
    set(&[
        "py", "pyi", "pyx", "c", "h", "cc", "cpp", "cxx", "hpp", "hh", "hxx", "m", "mm", "swift", "ts", "tsx", "mts",
        "cts", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt", "kts", "scala", "groovy", "rb", "php", "cs", "fs",
        "sh", "bash", "zsh", "fish", "ps1", "lua", "pl", "pm", "r", "jl", "dart", "sql", "vue", "svelte", "astro",
        "html", "htm", "css", "scss", "sass", "less", "gradle", "cmake", "mk", "proto", "graphql", "gql", "tf", "hcl",
        "nix", "zig", "hs", "ex", "exs", "erl", "clj", "cljs", "elm", "ml", "mli", "vim", "el", "lisp", "asm", "s",
        "metal", "glsl", "hlsl", "wgsl", "cu", "cuh", "ino", "v", "sv", "tex",
    ])
});
/// What goes with code: a repo's docs and its settings.
static CODE_TEXT_EXTENSIONS: LazyLock<HashSet<&str>> =
    LazyLock::new(|| set(&["md", "markdown", "rst", "adoc", "org", "txt", "yaml", "yml", "toml"]));
/// Code known by its name alone.
static CODE_FILE_NAMES: LazyLock<HashSet<&str>> = LazyLock::new(|| {
    set(&[
        "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile", "Justfile", "justfile", "Rakefile",
        "Gemfile", "Podfile", "Brewfile", "Procfile", "Vagrantfile", "Jenkinsfile",
    ])
});
/// Folders code search doesn't go into, besides what git ignores: other people's code kept in a repo, and what builds
/// and runs leave, for repos whose .gitignore doesn't list them and folders that aren't in git at all.
static CODE_SKIPPED_FOLDER_NAMES: LazyLock<HashSet<&str>> = LazyLock::new(|| {
    set(&[
        "vendor", "Vendor", "vendored", "third_party", "third-party", "thirdparty", "3rdparty", "external", "extern",
        "deps", "build", "Build", "dist", "out", "target", "obj", "coverage", "htmlcov", "generated", "__generated__",
        "wandb", "mlruns", "lightning_logs",
    ])
});

pub fn is_source_extension(ext: &str) -> bool {
    SOURCE_EXTENSIONS.contains(ext)
}

pub fn is_code_file_name(name: &str) -> bool {
    CODE_FILE_NAMES.contains(name)
}

pub fn name(path: &Path) -> &str {
    path.file_name().and_then(|name| name.to_str()).unwrap_or("")
}

pub fn extension(path: &Path) -> String {
    path.extension().and_then(|ext| ext.to_str()).unwrap_or("").to_lowercase()
}

/// What code search reads in the code folders: source files, a repo's docs and settings, and notebooks. Never data
/// (json, csv, lockfiles, logs), minified bundles, licenses, or anything that looks like a key or a password file.
pub fn classify_code(path: &Path, options: &IndexOptions) -> Classification {
    let name = name(path);
    let ext = extension(path);
    if options.excluded_extensions.contains(&ext) {
        return Classification::Skip("excluded type".into());
    }
    if is_secret(name) {
        return Classification::Skip("key or password file".into());
    }
    let lower = name.to_lowercase();
    if lower.ends_with(".min.js") || lower.ends_with(".min.mjs") || lower.ends_with(".min.css") {
        return Classification::Skip("minified".into());
    }
    if ["license", "licence", "copying", "notice"].iter().any(|prefix| lower.starts_with(prefix)) {
        return Classification::Skip("license".into());
    }
    if (ext == "ts" || ext == "mts") && is_mpeg_transport_stream(path) {
        return Classification::Ignore; // a video
    }
    if SOURCE_EXTENSIONS.contains(ext.as_str())
        || CODE_TEXT_EXTENSIONS.contains(ext.as_str())
        || ext == "ipynb"
        || CODE_FILE_NAMES.contains(name)
        || name.starts_with("Dockerfile")
    {
        return Classification::Index(FileKind::Code);
    }
    if CODE_EXTENSIONS.contains(ext.as_str()) {
        return Classification::Skip("data file".into()); // json, csv, lockfiles, logs
    }
    Classification::Ignore
}

/// Keys, certificates, password and credential files: never read, wherever they are.
pub fn is_secret(name: &str) -> bool {
    let lower = name.to_lowercase();
    const TYPES: [&str; 15] =
        ["pem", "key", "p12", "pfx", "keystore", "jks", "ppk", "kdbx", "gpg", "asc", "crt", "cer", "der", "p8", "mobileprovision"];
    if let Some(ext) = Path::new(&lower).extension().and_then(|e| e.to_str()) {
        if TYPES.contains(&ext) {
            return true;
        }
    }
    [
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".env", "secret", "credential", ".netrc", ".npmrc", ".pypirc",
        "htpasswd", ".htpasswd",
    ]
    .iter()
    .any(|prefix| lower.starts_with(prefix))
}

/// Why code search doesn't go into a folder, or None.
pub fn code_skip_reason(folder: &Path) -> Option<&'static str> {
    if CODE_SKIPPED_FOLDER_NAMES.contains(name(folder)) {
        return Some("vendored or built code");
    }
    // A submodule or a worktree (".git" is a file there): someone else's code, or a copy of a repo's own.
    if folder.join(".git").is_file() {
        return Some("submodule or worktree");
    }
    None
}

/// What kind of media a file is, as far as `classify` cares, from its extension (more dependable than content
/// sniffing for camera RAW and the many audio formats).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MediaType {
    SourceCode,
    Pdf,
    Image,
    Movie,
    Audio,
}

impl MediaType {
    pub fn from_extension(ext: &str) -> Option<Self> {
        const IMAGES: [&str; 31] = [
            "jpg", "jpeg", "jpe", "jfif", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif", "avif", "jxl",
            "ico", "dng", "cr2", "cr3", "nef", "nrw", "arw", "srf", "sr2", "orf", "rw2", "raf", "pef", "srw", "x3f",
            "3fr", "iiq",
        ];
        const MOVIES: [&str; 19] = [
            "mp4", "m4v", "mov", "qt", "mkv", "webm", "avi", "wmv", "flv", "mpg", "mpeg", "m2v", "3gp", "3g2", "ogv",
            "vob", "mts", "m2ts", "ts",
        ];
        const SOUNDS: [&str; 18] = [
            "mp3", "m4a", "m4b", "aac", "wav", "wave", "aif", "aiff", "aifc", "flac", "ogg", "oga", "opus", "wma",
            "caf", "amr", "mka", "alac",
        ];
        const SCRIPTS: [&str; 22] = [
            "pm", "tcl", "awk", "sed", "bat", "cmd", "vbs", "applescript", "scpt", "f90", "f", "for", "pas", "d", "nim",
            "cr", "v", "ada", "adb", "ads", "s", "groovy",
        ];
        let ext = ext.to_lowercase();
        let ext = ext.as_str();
        if ext == "pdf" {
            Some(Self::Pdf)
        } else if IMAGES.contains(&ext) {
            Some(Self::Image)
        } else if MOVIES.contains(&ext) {
            Some(Self::Movie)
        } else if SOUNDS.contains(&ext) {
            Some(Self::Audio)
        } else if SCRIPTS.contains(&ext) {
            Some(Self::SourceCode)
        } else {
            None
        }
    }
}

pub fn classify(path: &Path, options: &IndexOptions) -> Classification {
    let ext = extension(path);
    if options.excluded_extensions.contains(&ext) {
        return Classification::Skip("excluded type".into());
    }
    let code = || {
        if options.include_code {
            Classification::Index(FileKind::Doc)
        } else {
            Classification::Skip("code or data file".into())
        }
    };
    // .ts / .mts are usually TypeScript, though the extension says MPEG-2 video. Only a real transport stream is.
    if ext == "ts" || ext == "mts" || ext == "m2ts" {
        return if is_mpeg_transport_stream(path) { Classification::Index(FileKind::Video) } else { code() };
    }
    if DOC_EXTENSIONS.contains(ext.as_str()) {
        return Classification::Index(FileKind::Doc);
    }
    if CODE_EXTENSIONS.contains(ext.as_str()) {
        return code();
    }
    match MediaType::from_extension(&ext) {
        Some(MediaType::SourceCode) => code(),
        Some(MediaType::Pdf) => Classification::Index(FileKind::Pdf),
        Some(MediaType::Image) => {
            Classification::Index(if is_screenshot(path) { FileKind::Screenshot } else { FileKind::Image })
        }
        Some(MediaType::Movie) => Classification::Index(FileKind::Video),
        Some(MediaType::Audio) => Classification::Index(FileKind::Audio),
        None => Classification::Ignore,
    }
}

/// MPEG-TS packets start with 0x47 every 188 bytes (or every 192 with a 4-byte timecode, as in .m2ts / AVCHD .mts).
pub fn is_mpeg_transport_stream(path: &Path) -> bool {
    let mut bytes = [0u8; 600];
    let Ok(mut file) = fs::File::open(path) else { return false };
    let mut read = 0;
    while read < bytes.len() {
        match file.read(&mut bytes[read..]) {
            Ok(0) | Err(_) => break,
            Ok(n) => read += n,
        }
    }
    if read < 400 {
        return false;
    }
    (bytes[0] == 0x47 && bytes[188] == 0x47 && bytes[376] == 0x47)
        || (bytes[4] == 0x47 && bytes[196] == 0x47 && bytes[388] == 0x47)
}

/// Screenshots by their names, or by the folder GNOME and KDE save them to (~/Pictures/Screenshots).
pub fn is_screenshot(path: &Path) -> bool {
    if path.parent().map(name) == Some("Screenshots") {
        return true;
    }
    let name = name(path);
    SCREENSHOT_PREFIXES.iter().any(|prefix| name.starts_with(prefix))
}

pub fn skip_reason(folder: &Path, excluded: &[String]) -> Option<&'static str> {
    let name = name(folder);
    if SKIPPED_FOLDER_NAMES.contains(name) {
        return Some("developer folder");
    }
    if name.ends_with(".noindex") {
        return Some(".noindex folder");
    }
    let path = folder.to_string_lossy();
    if excluded.iter().any(|skip| path == skip.as_str() || path.starts_with(&format!("{skip}/"))) {
        return Some("excluded folder");
    }
    None
}

pub fn is_project_root(folder: &Path) -> bool {
    PROJECT_MARKERS.iter().any(|marker| folder.join(marker).exists())
}

const CAMERA_PREFIXES: [&str; 10] =
    ["img", "dsc", "pxl", "mvimg", "vid", "gopr", "dji", "photo", "whatsapp", "screenshot"];
static HEX_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9a-f]{8,}([-_][0-9a-f]{4,})*$").unwrap());
static NUMBERED_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z]{0,16}[-_. ]?\d{3,}([-_.]\d+)*$").unwrap());

/// Machine-made names like "clip_000123", "frame-0042" or "3f9a1c0e-…", but not camera names like "IMG_1234".
pub fn looks_machine_named(stem: &str) -> bool {
    let name = stem.to_lowercase();
    if CAMERA_PREFIXES.iter().any(|prefix| name.starts_with(prefix)) {
        return false;
    }
    HEX_NAME.is_match(&name) || NUMBERED_NAME.is_match(&name)
}

/// Folders with hundreds of same-kind, machine-named media files: training data, not someone's own things. They're
/// skipped (and reported) instead of being indexed silently. Folders chosen themselves are never datasets.
pub fn dataset_folders(files: &[(String, String, FileKind)], roots: &HashSet<String>, minimum: usize)
    -> HashMap<String, usize> {
    let mut groups: HashMap<(String, FileKind), Vec<&str>> = HashMap::new();
    for (folder, stem, kind) in files {
        if matches!(kind, FileKind::Image | FileKind::Audio | FileKind::Video) && !roots.contains(folder) {
            groups.entry((folder.clone(), *kind)).or_default().push(stem);
        }
    }
    let mut datasets: HashMap<String, usize> = HashMap::new();
    for ((folder, _), stems) in groups {
        if stems.len() >= minimum {
            let machine = stems.iter().filter(|stem| looks_machine_named(stem)).count();
            if machine as f64 / stems.len() as f64 >= 0.7 {
                *datasets.entry(folder).or_default() += stems.len();
            }
        }
    }
    datasets
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typescript_is_not_video_but_a_real_transport_stream_is() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("app.ts");
        fs::write(&script, "export const x = 1;\n".repeat(50)).unwrap();
        assert_eq!(classify(&script, &IndexOptions::default()), Classification::Skip("code or data file".into()));
        let video = dir.path().join("clip.ts");
        let mut bytes = vec![0u8; 188 * 4];
        for packet in 0..4 {
            bytes[packet * 188] = 0x47;
        }
        fs::write(&video, bytes).unwrap();
        assert_eq!(classify(&video, &IndexOptions::default()), Classification::Index(FileKind::Video));
        let with_code = IndexOptions { include_code: true, ..Default::default() };
        assert_eq!(classify(&dir.path().join("x.py"), &with_code), Classification::Index(FileKind::Doc));
    }

    #[test]
    fn screenshots_by_name_or_folder() {
        assert!(is_screenshot(Path::new("/h/Pictures/Screenshot from 2026-10-10 10-00-00.png")));
        assert!(is_screenshot(Path::new("/h/Pictures/Screenshots/anything.png")));
        assert!(is_screenshot(Path::new("/h/Images/Capture d'écran du 2026-10-10.png")));
        assert!(!is_screenshot(Path::new("/h/Pictures/beach.jpg")));
    }

    #[test]
    fn machine_names_and_datasets() {
        assert!(looks_machine_named("clip_000123"));
        assert!(looks_machine_named("frame-0042"));
        assert!(looks_machine_named("3f9a1c0e-1b2c-4d5e"));
        assert!(!looks_machine_named("IMG_1234"));
        assert!(!looks_machine_named("beach day"));
        assert!(looks_machine_named("000123"));
        assert!(!looks_machine_named("PXL_20260101_123456789"));
        assert!(!looks_machine_named("Screenshot 2026-10-01 at 9.41.07 PM"));
        let files: Vec<(String, String, FileKind)> =
            (0..400).map(|i| ("/d/clips".to_string(), format!("clip_{i:06}"), FileKind::Video)).collect();
        let found = dataset_folders(&files, &HashSet::new(), 300);
        assert_eq!(found.get("/d/clips"), Some(&400));
        assert!(dataset_folders(&files, &HashSet::from(["/d/clips".to_string()]), 300).is_empty());
    }

    #[test]
    fn code_search_reads_code_and_never_secrets() {
        let options = IndexOptions { code: true, ..Default::default() };
        let kind = |name: &str| classify_code(Path::new(&format!("/r/{name}")), &options);
        assert_eq!(kind("main.rs"), Classification::Index(FileKind::Code));
        assert_eq!(kind("README.md"), Classification::Index(FileKind::Code));
        assert_eq!(kind("Dockerfile.dev"), Classification::Index(FileKind::Code));
        assert_eq!(kind("data.json"), Classification::Skip("data file".into()));
        assert_eq!(kind("app.min.js"), Classification::Skip("minified".into()));
        assert_eq!(kind("LICENSE"), Classification::Skip("license".into()));
        assert_eq!(kind("prod.pem"), Classification::Skip("key or password file".into()));
        assert_eq!(kind("id_ed25519"), Classification::Skip("key or password file".into()));
        assert_eq!(kind("credentials.yaml"), Classification::Skip("key or password file".into()));
        assert_eq!(kind(".env.local"), Classification::Skip("key or password file".into()));
    }
}
