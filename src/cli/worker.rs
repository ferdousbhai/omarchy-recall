//! `recall worker [--text-only]`: EmbeddingGemma 2 on llama.cpp as a process, speaking JSON lines (the client is
//! `embed::EmbeddingWorker`), so the model's memory goes back to the system when it exits. llama.cpp's warnings go to
//! stderr.

use std::io::BufRead;

use base64::Engine as _;
use serde_json::{json, Value};

use recall::embed::{Embedder, Embedding};
use recall::llama::{LlamaEmbedder, LlamaModels};

use super::{emit, Arguments};

pub fn serve(args: &Arguments) -> i32 {
    let started = LlamaModels::locate().and_then(|models| LlamaEmbedder::new(&models, args.flag("--text-only"), None));
    let mut embedder = match started {
        Ok(embedder) => embedder,
        Err(error) => {
            emit(json!({"ready": false, "error": error.to_string()}));
            return 1;
        }
    };
    let info = embedder.info().clone();
    let info_fields = json!({
        "model": info.model, "revision": info.revision, "runtime": info.runtime, "dtype": info.dtype, "dim": info.dim,
        "device": recall::llama::device_name(), "text_only": info.text_only, "load_seconds": info.load_seconds.unwrap_or(0.0),
    });
    emit(json!({"ready": true, "info": info_fields}));
    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let Ok(request) = serde_json::from_str::<Value>(&line) else { continue };
        let id = request.get("id").cloned().unwrap_or(Value::Null);
        let inputs: Vec<String> = request["inputs"]
            .as_array()
            .map(|inputs| inputs.iter().filter_map(|input| input.as_str().map(String::from)).collect())
            .unwrap_or_default();
        let embeddings = match request["op"].as_str() {
            Some("quit") => {
                emit(json!({"id": id, "ok": true}));
                embedder.close();
                return 0;
            }
            Some("info") => {
                emit(json!({"id": id, "ok": true, "info": info_fields}));
                continue;
            }
            Some("text") => embedder
                .embed_texts(&inputs)
                .map(|vectors| vectors.into_iter().map(|vector| Embedding { vector: Some(vector), error: None }).collect()),
            Some("image") => embedder.embed_images(&inputs, request["budget"].as_u64().unwrap_or(280) as u32),
            Some("audio") => embedder.embed_audio(&inputs),
            op => {
                emit(json!({"id": id, "ok": false, "error": format!("unknown op {}", op.unwrap_or("(none)"))}));
                continue;
            }
        };
        match embeddings {
            Ok(embeddings) => {
                // Float32 little-endian, one row per input; an input that failed gets zeros and a message.
                let mut bytes = Vec::with_capacity(embeddings.len() * info.dim * 4);
                for embedding in &embeddings {
                    match &embedding.vector {
                        Some(vector) => vector.iter().for_each(|value| bytes.extend_from_slice(&value.to_le_bytes())),
                        None => bytes.extend(std::iter::repeat_n(0u8, info.dim * 4)),
                    }
                }
                let errors: Vec<Value> =
                    embeddings.iter().map(|embedding| embedding.error.clone().map_or(Value::Null, Value::from)).collect();
                emit(json!({
                    "id": id, "ok": true, "dim": info.dim, "count": inputs.len(),
                    "vectors": base64::engine::general_purpose::STANDARD.encode(&bytes), "errors": errors,
                }));
            }
            Err(error) => {
                let message: String = error.to_string().chars().take(500).collect();
                emit(json!({"id": id, "ok": false, "error": message}));
            }
        }
    }
    embedder.close();
    0
}
