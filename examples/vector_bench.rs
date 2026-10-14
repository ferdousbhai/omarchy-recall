//! Run with: cargo run --release --example vector_bench
use recall::vectors::{VectorStorage, DIMENSION};
use half::f16;
use std::time::Instant;
fn main() {
    for rows in [10_000, 100_000, 1_000_000] {
        let mut storage = VectorStorage::new(rows);
        storage.halves.resize(rows * DIMENSION, f16::from_f32(1.0 / (DIMENSION as f32).sqrt()));
        let query = vec![1.0 / (DIMENSION as f32).sqrt(); DIMENSION];
        storage.score(&query);
        let start = Instant::now();
        for _ in 0..5 { std::hint::black_box(storage.score(&query)); }
        println!("{}", serde_json::json!({"rows": rows, "vector_bytes": rows * DIMENSION * 2,
            "mean_ms": start.elapsed().as_secs_f64() * 200.0}));
    }
}
