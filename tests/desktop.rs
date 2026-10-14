//! Native CLI/service contract, isolated from the user's configuration, models and index.
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
const CLI: &str = env!("CARGO_BIN_EXE_recall");
struct Fixture {
    dir: tempfile::TempDir,
    daemon: Option<Child>,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("config/recall")).unwrap();
        fs::write(dir.path().join("config/recall/settings.json"),json!({"roots":[],"code_roots":[],"index_directory":dir.path().join("index"),"models_directory":dir.path().join("models")}).to_string()).unwrap();
        Self { dir, daemon: None }
    }
    fn command(&self) -> Command {
        let mut c = Command::new(CLI);
        c.env("HOME", self.dir.path())
            .env("XDG_CONFIG_HOME", self.dir.path().join("config"))
            .env("XDG_DATA_HOME", self.dir.path().join("data"))
            .env("XDG_RUNTIME_DIR", self.dir.path())
            .env("CODEX_HOME", self.dir.path().join("codex"))
            .env("RECALL_OFFLINE", "1");
        c
    }
    fn start(&mut self) {
        self.daemon = Some(
            self.command()
                .arg("daemon")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let deadline = Instant::now() + Duration::from_secs(10);
        while !self.dir.path().join("recall-for-omarchy.sock").exists() {
            assert!(Instant::now() < deadline);
            assert!(self.daemon.as_mut().unwrap().try_wait().unwrap().is_none());
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    fn rpc(&self, value: Value) -> Value {
        let mut socket =
            UnixStream::connect(self.dir.path().join("recall-for-omarchy.sock")).unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        writeln!(socket, "{value}").unwrap();
        let mut line = String::new();
        BufReader::new(socket).read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(c) = &mut self.daemon {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}
#[test]
fn skill_is_embedded_and_custom_agent_directories_are_preserved() {
    let f = Fixture::new();
    let output = f.command().arg("skill").output().unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).unwrap(),
        include_str!("../omarchy/skills/recall/SKILL.md")
    );
    let custom = f.dir.path().join(".claude/skills/recall");
    fs::create_dir_all(&custom).unwrap();
    fs::write(custom.join("custom"), "keep").unwrap();
    for _ in 0..2 {
        assert!(f
            .command()
            .args(["skill", "install"])
            .output()
            .unwrap()
            .status
            .success());
    }
    assert_eq!(fs::read_to_string(custom.join("custom")).unwrap(), "keep");
    assert!(f.dir.path().join("codex/skills/recall").is_symlink());
}
#[test]
fn cli_validates_before_contacting_service() {
    let f = Fixture::new();
    for arguments in [
        vec!["search", "x", "--kind", "bad"],
        vec!["search", "code: x", "--kind", "doc"],
        vec!["search", "x", "--limit", "101"],
        vec!["text", "0"],
    ] {
        let out = f.command().args(arguments).output().unwrap();
        assert_eq!(out.status.code(), Some(2));
        assert!(serde_json::from_slice::<Value>(&out.stdout)
            .unwrap()
            .get("error")
            .is_some());
    }
    let output = f.command().arg("status").output().unwrap();
    assert_eq!(output.status.code(), Some(1));
}
#[test]
fn native_socket_pause_config_and_exclusive_daemon() {
    let mut f = Fixture::new();
    f.start();
    let socket = f.dir.path().join("recall-for-omarchy.sock");
    assert_eq!(
        fs::metadata(socket).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(f.rpc(json!({"op":"pause","paused":true,"id":42}))["id"], 42);
    assert_eq!(f.rpc(json!({"op":"status"}))["paused"], true);
    assert_eq!(f.rpc(json!({"op":"pause","paused":false}))["ok"], true);
    assert!(f
        .rpc(json!({"op":"search","query":"x","kinds":["invalid"]}))
        .get("error")
        .is_some());
    assert!(f
        .rpc(json!({"op":"open","hit":{"path":"/etc/passwd"}}))
        .get("error")
        .is_some());
    assert!(f
        .rpc(json!({"op":"configure","config":{"roots":["relative"]}}))
        .get("error")
        .is_some());
    assert_eq!(
        f.rpc(json!({"op":"configure","config":{"encoder_idle_seconds":30}}))["ok"],
        true
    );
    let saved: Value = serde_json::from_slice(
        &fs::read(f.dir.path().join("config/recall/settings.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(saved["encoder_idle_seconds"], 30);
    let output = f.command().arg("daemon").output().unwrap();
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(f.rpc(json!({"op":"status"}))["paused"], false);
    let deadline = Instant::now() + Duration::from_secs(5);
    while f.rpc(json!({"op":"config"}))["config"]["encoder_idle_seconds"] != 30 {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    let out = f
        .command()
        .args(["search", "nothing", "--keyword-only"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stdout)
    );
    assert!(
        serde_json::from_slice::<Value>(&out.stdout).unwrap()["hits"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let pid = f.daemon.as_ref().unwrap().id();
    let workers = || -> Vec<String> {
        let mut pids: Vec<_> = fs::read_dir(format!("/proc/{pid}/task"))
            .unwrap()
            .flatten()
            .flat_map(|task| {
                fs::read_to_string(task.path().join("children"))
                    .unwrap_or_default()
                    .split_whitespace()
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .collect();
        pids.retain(|pid| {
            fs::read(format!("/proc/{pid}/cmdline")).is_ok_and(|bytes| {
                let command = String::from_utf8_lossy(&bytes);
                command.split('\0').nth(1) == Some("serve")
            })
        });
        pids.sort();
        pids
    };
    let before = workers();
    assert!(!before.is_empty());
    std::thread::sleep(Duration::from_millis(100));
    assert!(f
        .command()
        .args(["search", "nothing", "--keyword-only"])
        .output()
        .unwrap()
        .status
        .success());
    assert_eq!(
        before,
        workers(),
        "Search worker must outlive the request thread"
    );
}

#[test]
fn result_opening_preserves_media_offsets_and_code_lines_without_shell_interpolation() {
    let mut f = Fixture::new();
    let files = f.dir.path().join("files");
    fs::create_dir(&files).unwrap();
    let file = files.join("a # $(not-a-command).txt");
    fs::write(&file, "source").unwrap();
    let mut config: Value = serde_json::from_slice(
        &fs::read(f.dir.path().join("config/recall/settings.json")).unwrap(),
    )
    .unwrap();
    config["roots"] = json!([files]);
    config["code_roots"] = json!([files]);
    config["code_editor"] = json!("nvim");
    fs::write(
        f.dir.path().join("config/recall/settings.json"),
        config.to_string(),
    )
    .unwrap();
    let bin = f.dir.path().join("bin");
    fs::create_dir(&bin).unwrap();
    let log = f.dir.path().join("open.log");
    for name in ["mpv", "xdg-terminal-exec"] {
        let command = bin.join(name);
        fs::write(&command, "#!/bin/sh\nlog=\"${RECALL_TEST_OPEN_LOG}.${0##*/}\"\nprintf '%s\\n' \"$@\" > \"$log\"\n").unwrap();
        fs::set_permissions(command, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let mut daemon = f.command();
    daemon
        .env(
            "PATH",
            format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        )
        .env("RECALL_TEST_OPEN_LOG", &log);
    f.daemon = Some(
        daemon
            .arg("daemon")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    while !f.dir.path().join("recall-for-omarchy.sock").exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        f.rpc(json!({"op":"open","hit":{"path":file,"segment":"audio","loc":42.5}}))["ok"],
        true
    );
    assert_eq!(f.rpc(json!({"op":"open","hit":{"path":file,"segment":"lines","loc":1,"focus":14,"code":true}}))["ok"],true);
    let media_log = log.with_extension("log.mpv");
    let editor_log = log.with_extension("log.xdg-terminal-exec");
    loop {
        if fs::read_to_string(&media_log)
            .unwrap_or_default()
            .lines()
            .count()
            == 2
            && fs::read_to_string(&editor_log)
                .unwrap_or_default()
                .lines()
                .count()
                == 3
        {
            break;
        }
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        fs::read_to_string(media_log).unwrap(),
        format!("--start=42.5\n{}\n", file.display())
    );
    assert_eq!(
        fs::read_to_string(editor_log).unwrap(),
        format!("nvim\n+14\n{}\n", file.display())
    );
}

#[test]
fn replacing_the_cli_binary_does_not_break_a_running_coordinator() {
    let mut fixture = Fixture::new();
    let directory = fixture.dir.path().join("bundle/target/release");
    fs::create_dir_all(&directory).unwrap();
    let binary = directory.join("recall");
    fs::copy(CLI, &binary).unwrap();
    fs::copy(
        env!("CARGO_BIN_EXE_recall-engine"),
        directory.join("recall-engine"),
    )
    .unwrap();
    let environment = fixture.command();
    let mut daemon = Command::new(&binary);
    daemon.envs(
        environment
            .get_envs()
            .filter_map(|(k, v)| v.map(|v| (k, v))),
    );
    fixture.daemon = Some(
        daemon
            .arg("daemon")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = Instant::now() + Duration::from_secs(5);
    while !fixture.dir.path().join("recall-for-omarchy.sock").exists() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(20));
    }
    let old = directory.join("recall.before");
    fs::rename(&binary, &old).unwrap();
    fs::copy(CLI, &binary).unwrap();
    fs::remove_file(old).unwrap();
    let response = fixture.rpc(json!({"op":"search","query":"nothing","meaning":false}));
    assert!(response.get("error").is_none(), "{response}");
}
