//! The EmbeddingGemma 2 files the engine runs: ggml-org's Q8_0 GGUF conversion at one pinned revision of the repo
//! (made from google/embeddinggemma-2 at 914f7f8). Never the F16 files: the model's activations overflow float16.

use std::env;
use std::fs::File as FsFile;
use std::io::Read;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::paths;

pub const REPOSITORY: &str = "ggml-org/embeddinggemma-2-GGUF";
pub const REVISION: &str = "bfcd298762cc34d0357ece5ebdd31791a3a374d8";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ModelFile {
    pub name: &'static str,
    pub bytes: u64,
    pub sha256: &'static str,
}

impl ModelFile {
    /// Hugging Face's address for this file at the pinned revision.
    pub fn url(&self) -> String {
        format!("https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{}", self.name)
    }
}

/// The text model (310 MB): all that queries need.
pub const TEXT: ModelFile = ModelFile {
    name: "embeddinggemma-2-Q8_0.gguf",
    bytes: 309_855_456,
    sha256: "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135",
};
/// The image and audio encoders (555 MB).
pub const MEDIA: ModelFile = ModelFile {
    name: "mmproj-embeddinggemma-2-Q8_0.gguf",
    bytes: 554_821_024,
    sha256: "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f",
};
/// In download order.
pub const ALL: [ModelFile; 2] = [TEXT, MEDIA];

/// Where the app keeps them.
pub fn installed() -> PathBuf {
    paths::support().join("Models")
}

/// The pinned revision's folder in the Hugging Face cache (`hf download` puts the files there): `$HF_HUB_CACHE`,
/// `$HF_HOME/hub`, or `~/.cache/huggingface/hub`.
pub fn hugging_face_snapshot() -> PathBuf {
    let hub = env::var_os("HF_HUB_CACHE")
        .map(PathBuf::from)
        .or_else(|| env::var_os("HF_HOME").map(|home| PathBuf::from(home).join("hub")))
        .unwrap_or_else(|| paths::home().join(".cache/huggingface/hub"));
    hub.join(format!("models--{}", REPOSITORY.replace('/', "--"))).join("snapshots").join(REVISION)
}

/// `file` in the Hugging Face cache, when it's there at the right size (its checksum is for the caller to check).
pub fn cached_copy(file: &ModelFile) -> Option<PathBuf> {
    let path = hugging_face_snapshot().join(file.name).canonicalize().ok()?;
    (path.metadata().ok()?.len() == file.bytes).then_some(path)
}

/// The files that aren't in `folder` yet. Only verified files get their final name, so being there is enough.
pub fn missing(folder: &Path) -> Vec<ModelFile> {
    ALL.into_iter().filter(|file| !folder.join(file.name).exists()).collect()
}

/// The SHA-256 of a file as lowercase hex, read in 8 MB pieces.
pub fn sha256(path: &Path) -> std::io::Result<String> {
    let mut file = FsFile::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 8 << 20];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher.finalize().iter().map(|byte| format!("{byte:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hashes_files_in_pieces() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("f");
        let bytes: Vec<u8> = (0..(9u32 << 20)).map(|i| (i % 251) as u8).collect();
        std::fs::write(&path, &bytes).unwrap();
        let expected: String = Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(sha256(&path).unwrap(), expected);
    }

    #[test]
    fn missing_files_in_download_order() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(missing(dir.path()), vec![TEXT, MEDIA]);
        std::fs::write(dir.path().join(TEXT.name), b"x").unwrap();
        assert_eq!(missing(dir.path()), vec![MEDIA]);
    }
}
