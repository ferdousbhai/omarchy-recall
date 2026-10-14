mod daemon;
mod install;
mod watcher;
use anyhow::{bail, Context, Result};
use recall::{model_files, paths};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;
const SKILL: &str = include_str!("../../omarchy/skills/recall/SKILL.md");
const LIMIT: usize = 1_048_576;

pub(super) fn config_path() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .unwrap_or_else(|| paths::home().join(".config"))
        .join("recall/settings.json")
}
pub(super) fn defaults() -> Value {
    let roots: Vec<_> = ["Documents", "Pictures"]
        .iter()
        .map(|n| paths::home().join(n))
        .filter(|p| p.is_dir())
        .collect();
    json!({"roots": roots, "code_roots": [], "excluded_folders": [], "excluded_types": [],
        "code_excluded": [], "code_editor": "nvim", "index_on_battery": true,
        "encoder_idle_seconds": 120,
        "index_directory": paths::default_index(), "models_directory": model_files::installed()})
}
pub(super) fn load_config() -> Result<Value> {
    let mut value = defaults();
    match fs::read(config_path()) {
        Ok(bytes) => {
            let loaded: Value = serde_json::from_slice(&bytes).context("Invalid settings JSON")?;
            let object = loaded.as_object().context("Settings must be an object")?;
            for (k, v) in object {
                value[k] = v.clone();
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(e) => return Err(e.into()),
    }
    validate_config(&value)?;
    Ok(value)
}
pub(super) fn validate_config(value: &Value) -> Result<()> {
    for key in ["roots", "code_roots", "excluded_folders", "code_excluded"] {
        for path in value[key]
            .as_array()
            .with_context(|| format!("{key} must be an array"))?
        {
            let path = path.as_str().context("Folder paths must be strings")?;
            if !Path::new(path).is_absolute() || path.contains('\0') {
                bail!("Folder paths must be absolute");
            }
        }
    }
    for ext in value["excluded_types"]
        .as_array()
        .context("excluded_types must be an array")?
    {
        if ext.as_str().map_or(true, |s| {
            s.is_empty() || !s.chars().all(|c| c.is_ascii_alphanumeric())
        }) {
            bail!("Excluded extensions must contain letters/numbers without dots");
        }
    }
    for key in ["index_directory", "models_directory"] {
        if !value[key]
            .as_str()
            .is_some_and(|p| Path::new(p).is_absolute() && !p.contains('\0'))
        {
            bail!("{key} must be an absolute path");
        }
    }
    if !value["encoder_idle_seconds"]
        .as_u64()
        .is_some_and(|n| (1..=86400).contains(&n))
    {
        bail!("encoder_idle_seconds must be between 1 and 86400");
    }
    if !value["index_on_battery"].is_boolean() {
        bail!("index_on_battery must be boolean");
    }
    if !EDITORS.contains(&value["code_editor"].as_str().unwrap_or("INVALID")) {
        bail!("Unsupported code editor");
    }
    Ok(())
}
pub(super) fn save_config(value: &Value) -> Result<()> {
    validate_config(value)?;
    atomic_write(&config_path(), &serde_json::to_vec_pretty(value)?)
}
pub(super) fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    fs::create_dir_all(path.parent().context("Missing parent directory")?)?;
    let temp = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(temp);
    }
    result
}
pub(super) fn folders(config: &Value, key: &str) -> Vec<String> {
    config[key]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(String::from)
        .collect()
}
pub(super) fn socket_path() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/run/user/{}", unsafe { libc::geteuid() })))
        .join("recall-for-omarchy.sock")
}
pub(super) fn root() -> Result<PathBuf> {
    let exe = std::env::current_exe()?;
    Ok(exe
        .parent()
        .context("Missing binary directory")?
        .parent()
        .context("Missing target directory")?
        .parent()
        .context("Missing installation directory")?
        .to_path_buf())
}
pub(super) fn engine() -> Result<PathBuf> {
    Ok(std::env::current_exe()?.with_file_name("recall-engine"))
}
pub(super) fn checked(command: &mut Command) -> Result<()> {
    let status = command.status()?;
    if !status.success() {
        bail!("Command failed: {status}");
    }
    Ok(())
}
pub(super) fn request(value: &Value) -> Result<Value> {
    let mut socket = UnixStream::connect(socket_path())
        .context("Search service unavailable; start recall-for-omarchy.service")?;
    socket.set_read_timeout(Some(Duration::from_secs(115)))?;
    socket.set_write_timeout(Some(Duration::from_secs(5)))?;
    writeln!(socket, "{value}")?;
    read_json(&mut BufReader::new(socket), 10 * LIMIT)
}
pub(super) fn read_json(reader: &mut impl BufRead, limit: usize) -> Result<Value> {
    let mut line = Vec::new();
    reader
        .take((limit + 1) as u64)
        .read_until(b'\n', &mut line)?;
    if line.is_empty() || line.len() > limit || line.last() != Some(&b'\n') {
        bail!("Invalid or oversized JSON response");
    }
    Ok(serde_json::from_slice(&line)?)
}
fn output(reply: Value) -> i32 {
    println!("{reply}");
    if reply.get("error").is_some() {
        1
    } else {
        0
    }
}
const HELP: &str = "recall — local semantic search for files and code (JSON output)\n\nCommands:\n  search QUERY [--code] [--kind KIND] [--limit 1..100] [--keyword-only]\n  text SEGMENT_ID [--code]\n  status | doctor | pause | resume\n  models                              pinned model download manifest\n  settings | toggle\n  config [set KEY JSON_VALUE]\n  skill [install]\n  setup [--skip-ocr]\n  ocr\n\nExit codes: 0 success (including no matches), 1 runtime error, 2 invalid arguments.\nSearch requires recall-for-omarchy.service. Settings opens the native Omarchy panel.";

fn payload(args: &[String]) -> Result<Value> {
    let command = args.first().map(String::as_str).unwrap_or("");
    let mut value = json!({"op":command});
    match command {
        "search" => {
            let mut words = Vec::new();
            let mut kinds = Vec::new();
            let mut code = false;
            let mut limit = 10;
            let mut meaning = true;
            let mut at = 1;
            let mut positional = false;
            while at < args.len() {
                let arg = &args[at];
                if positional {
                    words.push(arg.clone());
                } else {
                    match arg.as_str() {
                        "--" => positional = true,
                        "--code" => code = true,
                        "--json" => (),
                        "--keyword-only" => meaning = false,
                        "--limit" | "--kind" => {
                            at += 1;
                            let next = args.get(at).context("Option needs a value")?;
                            if arg == "--limit" {
                                limit = next.parse::<u64>().context("Invalid limit")?;
                            } else {
                                if !["image", "screenshot", "pdf", "doc", "audio", "video"]
                                    .contains(&next.as_str())
                                {
                                    bail!("Invalid search kind");
                                }
                                kinds.push(next.clone());
                            }
                        }
                        s if s.starts_with('-') => bail!("Unknown option: {s}"),
                        _ => words.push(arg.clone()),
                    }
                }
                at += 1;
            }
            let mut query = words.join(" ").trim().to_string();
            if query.is_empty() || query.chars().count() > 4096 {
                bail!("Query must contain 1–4096 characters");
            }
            code |= query.starts_with("code:");
            if !(1..=100).contains(&limit) || (code && !kinds.is_empty()) {
                bail!("Invalid limit or --code/--kind combination");
            }
            if code && !query.starts_with("code:") {
                query = format!("code: {query}");
            }
            value = json!({"op":"search", "query":query, "limit":limit, "meaning":meaning});
            if !kinds.is_empty() {
                value["kinds"] = json!(kinds);
            }
        }
        "text" => {
            if args.len() < 2 || args.len() > 3 || (args.len() == 3 && args[2] != "--code") {
                bail!("Usage: recall text ID [--code]");
            }
            let id: u64 = args[1].parse().context("Invalid segment ID")?;
            if id == 0 {
                bail!("Segment must be positive");
            }
            value = json!({"op":"text", "segment":id, "code":args.len()==3});
        }
        "status" | "pause" | "resume" => {
            if args.len() != 1 {
                bail!("Unexpected arguments");
            }
            if command != "status" {
                value = json!({"op":"pause","paused":command=="pause"});
            }
        }
        _ => bail!("Unknown command: {command}"),
    }
    Ok(value)
}
pub fn run(args: Vec<String>) -> Result<i32> {
    let command = args.first().map(String::as_str).unwrap_or("--help");
    if command == "--version" {
        println!("recall {}", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }
    if command == "--help" || command == "-h" || args.get(1).is_some_and(|s| s == "--help") {
        println!("{HELP}");
        return Ok(0);
    }
    match command {
        "daemon" if args.len() == 1 => daemon::run(),
        "client" if args.len() == 1 => {
            let mut input = std::io::stdin().lock();
            loop {
                // A bounded read prevents a malformed caller from exhausting memory.
                let value = match read_json(&mut input, LIMIT) {
                    Ok(v) => v,
                    Err(e) => {
                        if input.fill_buf()?.is_empty() {
                            break;
                        }
                        println!("{}", json!({"error":e.to_string()}));
                        continue;
                    }
                };
                let id = value.get("id").cloned().unwrap_or(Value::Null);
                let reply =
                    request(&value).unwrap_or_else(|e| json!({"id":id,"error":e.to_string()}));
                println!("{reply}");
                std::io::stdout().flush()?;
            }
            Ok(0)
        }
        "models" if args.len() == 1 => Ok(output(json!(model_files::ALL
            .iter()
            .map(|f| json!({"name":f.name,"bytes":f.bytes,"sha256":f.sha256,"url":f.url()}))
            .collect::<Vec<_>>()))),
        "doctor" if args.len() == 1 => {
            let result = Command::new(engine()?).arg("devices").output()?;
            if !result.status.success() {
                bail!("{}", String::from_utf8_lossy(&result.stderr));
            }
            Ok(output(serde_json::from_slice(&result.stdout)?))
        }
        "skill" => {
            if args.len() == 1 {
                print!("{SKILL}");
                Ok(0)
            } else if args == ["skill", "install"] {
                Ok(output(install::skill()?))
            } else {
                Ok(output(json!({"error":"Usage: recall skill [install]"})).max(2))
            }
        }
        "setup" if args.len() == 1 || args == ["setup", "--skip-ocr"] => {
            if args.len() == 1 {
                checked(Command::new("bash").arg(root()?.join("scripts/setup-ocr.sh")))?;
            }
            install::desktop()?;
            Ok(0)
        }
        "ocr" if args.len() == 1 => {
            checked(Command::new("bash").arg(root()?.join("scripts/setup-ocr.sh")))?;
            Ok(0)
        }
        "settings" | "toggle" if args.len() == 1 => {
            toggle(command == "settings")?;
            Ok(0)
        }
        "config" => {
            if args.len() == 1 {
                return Ok(output(load_config()?));
            }
            if args.len() != 4
                || args[1] != "set"
                || !defaults().as_object().unwrap().contains_key(&args[2])
            {
                return Ok(
                    output(json!({"error":"Usage: recall config set KEY JSON_VALUE"})).max(2),
                );
            }
            let mut config = load_config()?;
            let value = match serde_json::from_str(&args[3]) {
                Ok(value) => value,
                Err(error) => {
                    println!("{}", json!({"error":error.to_string()}));
                    return Ok(2);
                }
            };
            config[&args[2]] = value;
            if let Err(error) = validate_config(&config) {
                println!("{}", json!({"error":error.to_string()}));
                return Ok(2);
            }
            save_config(&config)?;
            Ok(output(config))
        }
        _ => {
            match payload(&args) {
                Ok(payload) => {
                    let mut reply = request(&payload)?;
                    if payload["meaning"] == true
                        && reply.get("encoder_error").is_some_and(|e| !e.is_null())
                    {
                        reply["warning"]=json!(format!("Semantic encoding failed; results may contain only keyword matches: {}",reply["encoder_error"]));
                    }
                    Ok(output(reply))
                }
                Err(e) => {
                    println!("{}", json!({"error":e.to_string()}));
                    Ok(2)
                }
            }
        }
    }
}
pub(super) fn toggle(settings: bool) -> Result<()> {
    checked(
        Command::new(paths::home().join(".local/bin/recall-shell")).args([
            "shell",
            if settings { "summon" } else { "toggle" },
            "ferdous.recall",
            if settings {
                "{\"settings\":true}"
            } else {
                "{}"
            },
        ]),
    )
}
const EDITORS: &[&str] = &[
    "",
    "nvim",
    "code",
    "codium",
    "cursor",
    "windsurf",
    "zed",
    "subl",
    "pycharm",
    "idea",
    "clion",
    "goland",
    "webstorm",
    "rustrover",
    "kate",
    "gnome-text-editor",
    "gedit",
];
pub(super) fn spawn(command: &str, args: &[String]) -> Result<()> {
    // A short-lived reaper avoids zombies without blocking request handling.
    let mut child = Command::new(command)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}
pub(super) fn uri(path: &Path) -> String {
    use std::os::unix::ffi::OsStrExt;
    let mut result = String::from("file://");
    for &b in path.as_os_str().as_bytes() {
        if b.is_ascii_alphanumeric() || b"/-._~".contains(&b) {
            result.push(b as char);
        } else {
            result.push_str(&format!("%{b:02X}"));
        }
    }
    result
}
pub(super) fn open_hit(hit: &Value, config: &Value, reveal: bool) -> Result<()> {
    let path = Path::new(hit["path"].as_str().context("Missing hit path")?).canonicalize()?;
    let code = hit["code"].as_bool().unwrap_or(false);
    if !path.is_file()
        || !folders(config, if code { "code_roots" } else { "roots" })
            .iter()
            .filter_map(|p| fs::canonicalize(p).ok())
            .any(|r| path.starts_with(r))
    {
        bail!("File is outside indexed folders or no longer exists");
    }
    let p = path.to_string_lossy().to_string();
    if reveal {
        let items = json!([uri(&path)]).to_string();
        let status = Command::new("gdbus")
            .args([
                "call",
                "--timeout",
                "3",
                "--session",
                "--dest",
                "org.freedesktop.FileManager1",
                "--object-path",
                "/org/freedesktop/FileManager1",
                "--method",
                "org.freedesktop.FileManager1.ShowItems",
                &items,
                "",
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if status.is_ok_and(|s| s.success()) {
            return Ok(());
        }
        return spawn(
            "xdg-open",
            &[path.parent().unwrap().to_string_lossy().into_owned()],
        );
    }
    let loc = hit["loc"].as_f64().unwrap_or(1.0).max(0.0);
    let line = hit["focus"].as_f64().unwrap_or(loc).max(1.0) as u64;
    if code {
        let editor = config["code_editor"].as_str().unwrap_or("");
        let args = match editor {
            "nvim" => vec!["nvim".into(), format!("+{line}"), p.clone()],
            "code" | "codium" | "cursor" | "windsurf" => vec!["-g".into(), format!("{p}:{line}")],
            "zed" | "subl" => vec![format!("{p}:{line}")],
            "kate" => vec!["-l".into(), line.to_string(), p.clone()],
            "gnome-text-editor" | "gedit" => vec![format!("+{line}"), p.clone()],
            "" => vec![],
            _ => vec!["--line".into(), line.to_string(), p.clone()],
        };
        if !editor.is_empty()
            && spawn(
                if editor == "nvim" {
                    "xdg-terminal-exec"
                } else {
                    editor
                },
                &args,
            )
            .is_ok()
        {
            return Ok(());
        }
    } else if ["audio", "frame"].contains(&hit["segment"].as_str().unwrap_or("")) {
        for (cmd, args) in [
            ("mpv", vec![format!("--start={loc}"), p.clone()]),
            ("vlc", vec![format!("--start-time={loc}"), p.clone()]),
        ] {
            if spawn(cmd, &args).is_ok() {
                return Ok(());
            }
        }
    } else if ["page", "page_image"].contains(&hit["segment"].as_str().unwrap_or("")) {
        let default = Command::new("xdg-mime")
            .args(["query", "default", "application/pdf"])
            .output()
            .ok()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_default();
        let candidates = [
            ("org.pwmt.zathura.desktop", "zathura", "-P"),
            ("org.gnome.Papers.desktop", "papers", "-i"),
            ("org.gnome.Evince.desktop", "evince", "-i"),
            ("org.kde.okular.desktop", "okular", "-p"),
            ("okularApplication_pdf.desktop", "okular", "-p"),
            ("atril.desktop", "atril", "-i"),
            ("xreader.desktop", "xreader", "-i"),
        ];
        for (_, cmd, flag) in candidates.iter().filter(|(id, _, _)| *id == default) {
            if spawn(
                cmd,
                &[
                    flag.to_string(),
                    (loc.max(1.0) as u64).to_string(),
                    p.clone(),
                ],
            )
            .is_ok()
            {
                return Ok(());
            }
        }
    }
    spawn("xdg-open", &[p])
}

#[cfg(test)]
mod tests {
    use super::*;
    fn args(raw: &[&str]) -> Vec<String> {
        raw.iter().map(|s| s.to_string()).collect()
    }
    #[test]
    fn agent_arguments_preserve_search_contract() {
        assert_eq!(
            payload(&args(&["search", "retry", "--code", "--limit", "3"])).unwrap(),
            json!({"op":"search","query":"code: retry","limit":3,"meaning":true})
        );
        assert!(payload(&args(&["search", "code: retry", "--kind", "doc"])).is_err());
        assert!(payload(&args(&["search", "retry", "--limit", "0"])).is_err());
        assert!(payload(&args(&["search", "retry", "--kind", "invalid"])).is_err());
        assert!(payload(&args(&["text", "0"])).is_err());
        assert_eq!(
            payload(&args(&["search", "--", "-literal"])).unwrap()["query"],
            "-literal"
        );
    }
    #[test]
    fn validates_settings_without_discarding_unknown_legacy_fields() {
        let mut config = defaults();
        config["legacy"] = json!(true);
        assert!(validate_config(&config).is_ok());
        config["roots"] = json!(["relative"]);
        assert!(validate_config(&config).is_err());
        config = defaults();
        config["excluded_types"] = json!([".png"]);
        assert!(validate_config(&config).is_err());
        config = defaults();
        config["code_editor"] = json!("sh");
        assert!(validate_config(&config).is_err());
    }
    #[test]
    fn bounded_protocol_rejects_oversized_or_unterminated_messages() {
        assert!(read_json(&mut &b"{}\n"[..], 3).is_ok());
        assert!(read_json(&mut &b"{}\n"[..], 2).is_err());
        assert!(read_json(&mut &b"{}"[..], 20).is_err());
    }
    #[test]
    fn file_uris_escape_special_characters() {
        assert_eq!(
            uri(Path::new("/tmp/a #?'é")),
            "file:///tmp/a%20%23%3F%27%C3%A9"
        );
    }
    #[test]
    fn open_actions_reject_files_outside_scope_before_launching() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("x");
        fs::write(&file, "data").unwrap();
        assert!(open_hit(&json!({"path":file}), &defaults(), false).is_err());
    }
}
