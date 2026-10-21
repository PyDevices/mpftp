// mpftp desktop: mpftp's web UI in its own window, with no web server.
//
// The page ships inside the app. Beside it the shell runs the bundled
// Python as `mpftp.desktop_host` and passes lines both ways: the page's
// requests go to the host's stdin through the `host_send` command, and
// each line the host prints comes back to the page as a `host-line`
// event. If the host exits, the shell starts it again; when the window
// closes, the host and everything it started go with it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder};

// This many exits inside the window means it's not going to recover.
const CRASH_LIMIT: usize = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(60);

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
    let mut guard = host.stdin.lock().unwrap();
    let stdin = guard.as_mut().ok_or("mpftp isn't running")?;
    stdin
        .write_all(line.as_bytes())
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn host_running(host: State<'_, Arc<Host>>) -> bool {
    host.running.lock().unwrap().is_some()
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

fn supervise(app: AppHandle, host: Arc<Host>, python: PathBuf, log: PathBuf) {
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
                    if !msg.is_empty() {
                        let _ = app.emit("host-line", msg);
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
        .invoke_handler(tauri::generate_handler![host_send, host_running])
        .setup(move |app| {
            let handle = app.handle().clone();
            let opener = handle.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
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

            let log_dir = handle.path().app_log_dir()?;
            fs::create_dir_all(&log_dir)?;
            let log = log_dir.join("host.log");
            // One launch per log; the previous one is kept beside it.
            let _ = fs::rename(&log, log_dir.join("host.previous.log"));
            *host.log.lock().unwrap() = File::create(log_dir.join("shell.log")).ok();
            host.note("shell started");

            let python = python_path(&handle);
            let host = host.clone();
            thread::spawn(move || supervise(handle, host, python, log));
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
