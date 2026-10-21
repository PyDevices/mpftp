// mpftp desktop: mpftp's web UI in its own window, with no web server.
//
// The page ships inside the app. Beside it the shell runs the bundled
// Python as `mpftp.desktop_host` and passes lines both ways: the page's
// requests go to the host's stdin through the `host_send` command, and
// each line the host prints comes back to the page as a `host-line`
// event. If the host exits, the shell starts it again; when the window
// closes, the host and everything it started go with it.
//
// The emulator is a second window: MicroPython compiled to WebAssembly,
// which the sidecar drives as a board on a serial line (emu://). Its lines
// ride the same channel; the shell sends them to that window instead of
// the page, and opens the window when the sidecar asks for the board.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use base64::Engine;
use serde_json::{json, Value};
use tauri::webview::NewWindowResponse;
use tauri::{
    AppHandle, Emitter, LogicalSize, Manager, RunEvent, State, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder, WindowEvent,
};

// This many exits inside the window means it's not going to recover.
const CRASH_LIMIT: usize = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(60);

// Sidecar notifications for the emulator window start like this.
const EMU_PREFIX: &str = "{\"type\": \"notify\", \"method\": \"emu_";
const EMU_WINDOW: &str = "emulator";
// A Ctrl-C the emulator hasn't answered this long after it was sent means a
// program that never hands the page back: reload the window.
const EMU_FROZEN: Duration = Duration::from_millis(1500);
// How long a closing emulator window gets to save its files.
const EMU_SAVE_GRACE: Duration = Duration::from_millis(1500);

#[cfg(windows)]
mod job {
    // A Job Object that kills every process in it when its handle closes.
    // The host starts its own sidecar Python, which holds the serial port;
    // without the job, killing the host would orphan it.
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::*;

    pub struct Job(HANDLE);
    unsafe impl Send for Job {}

    impl Job {
        pub fn new() -> Option<Job> {
            unsafe {
                let h = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if h.is_null() {
                    return None;
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    h,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    CloseHandle(h);
                    return None;
                }
                Some(Job(h))
            }
        }

        pub fn assign(&self, child: &std::process::Child) {
            unsafe {
                AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

#[cfg(not(windows))]
mod job {
    pub struct Job;
    impl Job {
        pub fn new() -> Option<Job> {
            Some(Job)
        }
        pub fn assign(&self, _child: &std::process::Child) {}
    }
}

struct Running {
    child: Child,
    _job: Option<job::Job>,
}

#[derive(Default)]
struct Host {
    running: Mutex<Option<Running>>,
    // Separate from `running`, so a write that stalls can't hold up shutdown.
    stdin: Mutex<Option<ChildStdin>>,
    stopping: Mutex<bool>,
    log: Mutex<Option<File>>,
}

impl Host {
    fn stopping(&self) -> bool {
        *self.stopping.lock().unwrap()
    }

    fn note(&self, what: &str) {
        if let Some(f) = self.log.lock().unwrap().as_mut() {
            let t = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            let _ = writeln!(f, "{t:.3} {what}");
        }
    }

    fn send_line(&self, line: &str) -> Result<(), String> {
        let mut guard = self.stdin.lock().unwrap();
        let stdin = guard.as_mut().ok_or("mpftp isn't running")?;
        stdin
            .write_all(line.as_bytes())
            .and_then(|_| stdin.write_all(b"\n"))
            .and_then(|_| stdin.flush())
            .map_err(|e| e.to_string())
    }

    fn stop(&self) {
        self.note("stop: begin");
        *self.stopping.lock().unwrap() = true;
        // Closing stdin is the host's cue to shut its sidecar down.
        if let Ok(mut stdin) = self.stdin.try_lock() {
            stdin.take();
            self.note("stop: stdin closed");
        } else {
            self.note("stop: stdin busy, going straight to kill");
        }
        let taken = self.running.lock().unwrap().take();
        self.note("stop: have the child");
        if let Some(mut r) = taken {
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                if let Ok(Some(_)) = r.child.try_wait() {
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
            let _ = r.child.kill();
            let _ = r.child.wait();
            // Dropping the job takes anything left with it.
        }
        self.note("stop: done");
    }
}

#[tauri::command]
fn host_send(line: String, host: State<'_, Arc<Host>>) -> Result<(), String> {
    host.send_line(&line)
}

#[tauri::command]
fn host_running(host: State<'_, Arc<Host>>) -> bool {
    host.running.lock().unwrap().is_some()
}

// The emulator window.

#[derive(Default)]
struct Emu {
    // The last heartbeat from a window whose board is up; None while it
    // boots, so a window still loading is never taken for a frozen one.
    alive: Mutex<Option<Instant>>,
    opening: Mutex<bool>,
    // One freeze check at a time: a Stop sends a storm of Ctrl-Cs.
    watching: Mutex<bool>,
    // The window is being replaced, not closed: the board stays plugged in.
    replacing: Mutex<bool>,
}

fn flash_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("emulator").join("flash"))
}

// "/lib/x.py" -> lib/x.py under the flash folder, or None for anything that
// would land outside it.
fn flash_path(root: &Path, board_path: &str) -> Option<PathBuf> {
    let mut out = root.to_path_buf();
    let mut any = false;
    for part in board_path.split('/') {
        match part {
            "" | "." => continue,
            ".." => return None,
            p if p.contains(['\\', ':']) => return None,
            p => {
                out.push(p);
                any = true;
            }
        }
    }
    any.then_some(out)
}

fn walk_flash(root: &Path, dir: &Path, dirs: &mut Vec<Value>, files: &mut Vec<Value>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(rel) = path.strip_prefix(root) else { continue };
        let board = format!("/{}", rel.to_string_lossy().replace('\\', "/"));
        if path.is_dir() {
            dirs.push(json!(board));
            walk_flash(root, &path, dirs, files);
        } else if let Ok(data) = fs::read(&path) {
            let b64 = base64::engine::general_purpose::STANDARD.encode(data);
            files.push(json!({ "path": board, "b64": b64 }));
        }
    }
}

#[tauri::command]
fn emu_fs_load(app: AppHandle) -> Result<Option<Value>, String> {
    let root = flash_dir(&app)?;
    if !root.is_dir() {
        return Ok(None);
    }
    let (mut dirs, mut files) = (Vec::new(), Vec::new());
    walk_flash(&root, &root, &mut dirs, &mut files);
    Ok(Some(json!({ "dirs": dirs, "files": files })))
}

// The whole flash at once: written beside the old one, then swapped in, so
// a crash mid-save leaves one or the other.
#[tauri::command]
fn emu_fs_save(app: AppHandle, snapshot: Value) -> Result<(), String> {
    let root = flash_dir(&app)?;
    let parent = root.parent().ok_or("no parent")?.to_path_buf();
    let fresh = parent.join("flash.new");
    let old = parent.join("flash.old");
    let _ = fs::remove_dir_all(&fresh);
    fs::create_dir_all(&fresh).map_err(|e| e.to_string())?;
    for d in snapshot["dirs"].as_array().into_iter().flatten() {
        if let Some(p) = d.as_str().and_then(|d| flash_path(&fresh, d)) {
            let _ = fs::create_dir_all(p);
        }
    }
    for f in snapshot["files"].as_array().into_iter().flatten() {
        let (Some(path), Some(b64)) = (f["path"].as_str(), f["b64"].as_str()) else { continue };
        let Some(p) = flash_path(&fresh, path) else { continue };
        let Ok(data) = base64::engine::general_purpose::STANDARD.decode(b64) else { continue };
        if let Some(dir) = p.parent() {
            let _ = fs::create_dir_all(dir);
        }
        fs::write(&p, data).map_err(|e| format!("{path}: {e}"))?;
    }
    let _ = fs::remove_dir_all(&old);
    if root.exists() {
        fs::rename(&root, &old).map_err(|e| e.to_string())?;
    }
    fs::rename(&fresh, &root).map_err(|e| e.to_string())?;
    let _ = fs::remove_dir_all(&old);
    Ok(())
}

#[tauri::command]
fn emu_fit(window: WebviewWindow, width: f64, height: f64) -> Result<(), String> {
    window
        .set_size(LogicalSize::new(width, height))
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn emu_pin(window: WebviewWindow, on: bool) -> Result<(), String> {
    window.set_always_on_top(on).map_err(|e| e.to_string())
}

#[tauri::command]
fn emu_alive(ready: bool, emu: State<'_, Arc<Emu>>) {
    if ready {
        *emu.alive.lock().unwrap() = Some(Instant::now());
    }
}

fn open_emulator(app: &AppHandle, host: &Arc<Host>, emu: &Arc<Emu>) {
    if let Some(w) = app.get_webview_window(EMU_WINDOW) {
        let _ = w.show();
        return;
    }
    {
        let mut opening = emu.opening.lock().unwrap();
        if *opening {
            return;
        }
        *opening = true;
    }
    let (app, host, emu) = (app.clone(), host.clone(), emu.clone());
    // Off the reader thread: it mustn't stall while a window is built.
    thread::spawn(move || {
        let built = WebviewWindowBuilder::new(
            &app,
            EMU_WINDOW,
            WebviewUrl::App("emulator/emulator.html".into()),
        )
        .title("mpftp Emulator")
        .inner_size(480.0, 120.0)
        .min_inner_size(360.0, 100.0)
        .background_color(tauri::window::Color(30, 30, 30, 255))
        .disable_drag_drop_handler()
        .build();
        *emu.opening.lock().unwrap() = false;
        match built {
            Ok(w) => {
                host.note("emulator window opened");
                let host = host.clone();
                let emu = emu.clone();
                let closing = app.clone();
                w.on_window_event(move |event| match event {
                    // The page saves its files and closes itself; one that
                    // is stuck in a program is closed anyway.
                    WindowEvent::CloseRequested { api, .. } => {
                        api.prevent_close();
                        close_emulator(&closing, None);
                    }
                    WindowEvent::Destroyed => {
                        if *emu.replacing.lock().unwrap() {
                            return;
                        }
                        host.note("emulator window closed");
                        let _ = host.send_line("{\"emu_gone\": true}");
                    }
                    _ => {}
                });
            }
            Err(e) => {
                host.note(&format!("emulator window failed: {e}"));
                let _ = host.send_line("{\"emu_gone\": true}");
            }
        }
    });
}

// Ask the emulator window to save and close, and close it after the grace
// period whatever it says; then run `after`, if given.
fn close_emulator(app: &AppHandle, after: Option<Box<dyn FnOnce() + Send>>) {
    let Some(w) = app.get_webview_window(EMU_WINDOW) else {
        if let Some(f) = after {
            f();
        }
        return;
    };
    let _ = app.emit_to(EMU_WINDOW, "emu-close", ());
    let app = app.clone();
    thread::spawn(move || {
        let deadline = Instant::now() + EMU_SAVE_GRACE;
        while app.get_webview_window(EMU_WINDOW).is_some() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(50));
        }
        if app.get_webview_window(EMU_WINDOW).is_some() {
            let _ = w.destroy();
        }
        if let Some(f) = after {
            f();
        }
    });
}

// One sidecar line for the emulator.
fn route_emu(app: &AppHandle, host: &Arc<Host>, emu: &Arc<Emu>, line: &str) {
    let Ok(msg) = serde_json::from_str::<Value>(line) else { return };
    match msg["method"].as_str() {
        Some("emu_open") => open_emulator(app, host, emu),
        Some("emu_out") => {
            let ctrl_c = msg["params"]["b64"]
                .as_str()
                .and_then(|b| base64::engine::general_purpose::STANDARD.decode(b).ok())
                .is_some_and(|bytes| bytes.contains(&0x03));
            if ctrl_c {
                watch_for_freeze(app.clone(), host.clone(), emu.clone());
            }
        }
        _ => {}
    }
    let _ = app.emit_to(EMU_WINDOW, "emu-line", line);
}

// A program that never hands the page back can't see Ctrl-C, and a page
// stuck in its script can't be reloaded either: the reload waits on the
// same stuck renderer. So the window is replaced, which takes the renderer
// down with it; the new one boots from the flash folder, and the sidecar's
// link carries on as if the board had been reset.
fn watch_for_freeze(app: AppHandle, host: Arc<Host>, emu: Arc<Emu>) {
    {
        let mut watching = emu.watching.lock().unwrap();
        if *watching || emu.alive.lock().unwrap().is_none() {
            return;
        }
        *watching = true;
    }
    let sent = Instant::now();
    thread::spawn(move || {
        thread::sleep(EMU_FROZEN);
        let frozen = emu.alive.lock().unwrap().is_some_and(|t| t < sent);
        if frozen {
            if let Some(w) = app.get_webview_window(EMU_WINDOW) {
                host.note("emulator frozen past a Ctrl-C: replacing its window");
                *emu.alive.lock().unwrap() = None;
                *emu.replacing.lock().unwrap() = true;
                let _ = w.destroy();
                let deadline = Instant::now() + Duration::from_secs(5);
                while app.get_webview_window(EMU_WINDOW).is_some() && Instant::now() < deadline {
                    thread::sleep(Duration::from_millis(50));
                }
                *emu.replacing.lock().unwrap() = false;
                open_emulator(&app, &host, &emu);
            }
        }
        *emu.watching.lock().unwrap() = false;
    });
}

fn python_path(app: &AppHandle) -> PathBuf {
    if let Ok(p) = std::env::var("MPFTP_DESKTOP_PYTHON") {
        return PathBuf::from(p);
    }
    let dir = app.path().resource_dir().unwrap_or_default().join("python");
    if cfg!(windows) {
        dir.join("python.exe")
    } else {
        dir.join("bin").join("python3")
    }
}

fn spawn_host(python: &Path, log: &Path) -> std::io::Result<(Child, ChildStdin, ChildStdout)> {
    let err = File::options().create(true).append(true).open(log)?;
    let mut cmd = Command::new(python);
    cmd.args(["-m", "mpftp.desktop_host"])
        // The host starts its sidecar with this same Python.
        .env("MPFTP_PYTHON", python)
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONUTF8", "1")
        // List the emulator among the ports.
        .env("MPFTP_EMULATOR", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(err);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn()?;
    let stdin = child.stdin.take().expect("piped stdin");
    let stdout = child.stdout.take().expect("piped stdout");
    Ok((child, stdin, stdout))
}

fn show_failure(app: &AppHandle, log: &Path) {
    let msg = format!(
        "mpftp stopped several times in a row, so it won't be restarted. Its log is at {}",
        log.display()
    );
    if let Some(w) = app.get_webview_window("main") {
        let js = format!(
            "document.body.innerHTML = '';\
             const p = document.createElement('p');\
             p.style.cssText = 'margin:3rem;font:15px system-ui;color:#ccc';\
             p.textContent = {};\
             document.body.appendChild(p);",
            serde_json_string(&msg)
        );
        let _ = w.eval(&js);
    }
}

fn serde_json_string(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn supervise(app: AppHandle, host: Arc<Host>, emu: Arc<Emu>, python: PathBuf, log: PathBuf) {
    let mut exits: Vec<Instant> = Vec::new();

    loop {
        if host.stopping() {
            return;
        }
        let (child, stdin, stdout) = match spawn_host(&python, &log) {
            Ok(parts) => parts,
            Err(e) => {
                let _ = fs::write(&log, format!("could not start {}: {e}\n", python.display()));
                show_failure(&app, &log);
                return;
            }
        };
        let job = job::Job::new();
        if let Some(j) = &job {
            j.assign(&child);
        }
        *host.running.lock().unwrap() = Some(Running { child, _job: job });
        *host.stdin.lock().unwrap() = Some(stdin);
        host.note("host started");
        let _ = app.emit("host-status", true);

        // Every line the host prints is one message for the page.
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    let msg = line.trim_end_matches(['\r', '\n']);
                    if msg.starts_with(EMU_PREFIX) {
                        route_emu(&app, &host, &emu, msg);
                    } else if !msg.is_empty() {
                        let _ = app.emit_to("main", "host-line", msg);
                    }
                }
            }
        }

        // stdout closed: the host is gone, or going.
        host.note("host stdout closed");
        if host.stopping() {
            return;
        }
        host.stdin.lock().unwrap().take();
        if let Some(mut r) = host.running.lock().unwrap().take() {
            let _ = r.child.kill();
            let _ = r.child.wait();
            // The job drops here, so nothing it started keeps the serial port.
        }
        let _ = app.emit("host-status", false);
        let now = Instant::now();
        exits.retain(|t| now.duration_since(*t) < CRASH_WINDOW);
        exits.push(now);
        if exits.len() >= CRASH_LIMIT {
            show_failure(&app, &log);
            return;
        }
        thread::sleep(Duration::from_millis(500));
    }
}

fn main() {
    let host = Arc::new(Host::default());
    let host_for_exit = host.clone();
    let emu = Arc::new(Emu::default());

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .manage(host.clone())
        .manage(emu.clone())
        .invoke_handler(tauri::generate_handler![
            host_send,
            host_running,
            emu_fs_load,
            emu_fs_save,
            emu_fit,
            emu_pin,
            emu_alive
        ])
        .setup(move |app| {
            let handle = app.handle().clone();
            let opener = handle.clone();
            let main = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("mpftp")
                .inner_size(1280.0, 820.0)
                .min_inner_size(640.0, 400.0)
                .background_color(tauri::window::Color(30, 30, 30, 255))
                // The page does its own drag and drop (files onto the board
                // panel); the shell's handler would swallow those events.
                .disable_drag_drop_handler()
                .zoom_hotkeys_enabled(true)
                // Links that ask for a new window go to the default browser.
                .on_new_window(move |url, _features| {
                    use tauri_plugin_opener::OpenerExt;
                    let _ = opener.opener().open_url(url.as_str(), None::<&str>);
                    NewWindowResponse::Deny
                })
                .build()?;
            // The emulator window doesn't outlive the app's own, but it gets
            // to save its files first.
            let quitter = handle.clone();
            main.on_window_event(move |event| match event {
                WindowEvent::CloseRequested { api, .. } => {
                    if quitter.get_webview_window(EMU_WINDOW).is_some() {
                        api.prevent_close();
                        let app = quitter.clone();
                        close_emulator(&quitter, Some(Box::new(move || app.exit(0))));
                    }
                }
                WindowEvent::Destroyed => quitter.exit(0),
                _ => {}
            });

            let log_dir = handle.path().app_log_dir()?;
            fs::create_dir_all(&log_dir)?;
            let log = log_dir.join("host.log");
            // One launch per log; the previous one is kept beside it.
            let _ = fs::rename(&log, log_dir.join("host.previous.log"));
            *host.log.lock().unwrap() = File::create(log_dir.join("shell.log")).ok();
            host.note("shell started");

            let python = python_path(&handle);
            let host = host.clone();
            let emu = emu.clone();
            thread::spawn(move || supervise(handle, host, emu, python, log));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building mpftp");

    app.run(move |_app, event| match event {
        RunEvent::ExitRequested { .. } => host_for_exit.note("exit requested"),
        RunEvent::Exit => {
            host_for_exit.note("exit");
            host_for_exit.stop();
        }
        _ => {}
    });
}
