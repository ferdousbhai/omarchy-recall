//! Native user CLI and desktop coordinator. The inference workers remain isolated processes.
mod desktop;
fn main() {
    let code = match desktop::run(std::env::args().skip(1).collect()) {
        Ok(code) => code,
        Err(error) => {
            println!("{}", serde_json::json!({"error": error.to_string()}));
            1
        }
    };
    std::process::exit(code);
}
