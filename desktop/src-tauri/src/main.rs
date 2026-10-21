// mpftp desktop: a window around mpftp's local web UI.
//
// The shell owns the server. It starts the bundled Python running
// `mpftp.pwa`, waits for the port, and points the window at it. If the
// server exits, the shell starts it again and reloads; when the window
// closes, the server and everything it started go with it.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

// Not the browser PWA's 8317, so both can run at once. A fixed port keeps
// the page's origin, and with it the saved theme and layout, the same
// from one launch to the next.
const PREFERRED_PORT: u16 = 8318;
const START_TIMEOUT: Duration = Duration::from_secs(30);
// This many exits inside the window means it's not going to recover.
const CRASH_LIMIT: usize = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(60);

#[cfg(windows)]
mod job {
    // A Job Object that kills every process in it when its handle closes.
    // The server starts its own sidecar Python, which holds the serial
    // port; without the job, killing the server would orphan it.
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
struct Server {
    running: Mutex<Option<Running>>,
    stopping: Mutex<bool>,
}

impl Server {
    fn stopping(&self) -> bool {
        *self.stopping.lock().unwrap()
    }

    fn stop(&self) {
        *self.stopping.lock().unwrap() = true;
        if let Some(mut r) = self.running.lock().unwrap().take() {
            let _ = r.child.kill();
            let _ = r.child.wait();
            // Dropping the job takes the sidecar with it.
        }
    }
}

fn pick_port() -> u16 {
    if TcpListener::bind(("127.0.0.1", PREFERRED_PORT)).is_ok() {
        return PREFERRED_PORT;
    }
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(PREFERRED_PORT)
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

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn spawn_server(python: &Path, port: u16, log: &Path) -> std::io::Result<Child> {
    let out = File::options().create(true).append(true).open(log)?;
    let mut cmd = Command::new(python);
    cmd.args(["-m", "mpftp.pwa", "--no-open", "--port", &port.to_string()])
        .current_dir(home_dir())
        // The server starts its sidecar with this same Python.
        .env("MPFTP_PYTHON", python)
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONUTF8", "1")
        .stdin(Stdio::null())
        .stdout(out.try_clone()?)
        .stderr(out);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn()
}

fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok()
}

fn splash_url(base: &Url, state: &str, detail: &str) -> Url {
    let mut u = base.clone();
    let frag = format!("{state}|{detail}");
    u.set_fragment(Some(&frag));
    u
}

fn supervise(window: WebviewWindow, server: Arc<Server>, python: PathBuf, log: PathBuf) {
    let splash = window.url().expect("splash url");
    let log_text = log.display().to_string();
    let mut exits: Vec<Instant> = Vec::new();

    loop {
        if server.stopping() {
            return;
        }
        let port = pick_port();
        let child = match spawn_server(&python, port, &log) {
            Ok(c) => c,
            Err(e) => {
                let _ = fs::write(&log, format!("could not start {}: {e}\n", python.display()));
                let _ = window.navigate(splash_url(&splash, "failed", &log_text));
                return;
            }
        };
        let job = job::Job::new();
        if let Some(j) = &job {
            j.assign(&child);
        }
        *server.running.lock().unwrap() = Some(Running { child, _job: job });

        // Wait for the port, or for the server to give up first.
        let started = Instant::now();
        let mut up = false;
        while started.elapsed() < START_TIMEOUT && !server.stopping() {
            if port_open(port) {
                up = true;
                break;
            }
            if exited(&server) {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        if up {
            let url: Url = format!("http://127.0.0.1:{port}/").parse().unwrap();
            let _ = window.navigate(url);
        }

        while !server.stopping() && !exited(&server) {
            thread::sleep(Duration::from_millis(300));
        }
        if server.stopping() {
            return;
        }
        // Gone on its own: drop the job so nothing it started is left
        // holding the serial port, then try again unless it keeps dying.
        server.running.lock().unwrap().take();
        let now = Instant::now();
        exits.retain(|t| now.duration_since(*t) < CRASH_WINDOW);
        exits.push(now);
        if exits.len() >= CRASH_LIMIT {
            let _ = window.navigate(splash_url(&splash, "failed", &log_text));
            return;
        }
        let _ = window.navigate(splash_url(&splash, "restarting", ""));
        thread::sleep(Duration::from_millis(500));
    }
}

fn exited(server: &Server) -> bool {
    match server.running.lock().unwrap().as_mut() {
        Some(r) => !matches!(r.child.try_wait(), Ok(None)),
        None => true,
    }
}

fn main() {
    let server = Arc::new(Server::default());
    let server_for_exit = server.clone();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .setup(move |app| {
            let handle = app.handle().clone();
            let opener = handle.clone();
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
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
            let log = log_dir.join("server.log");
            // One launch per log; the previous one is kept beside it.
            let _ = fs::rename(&log, log_dir.join("server.previous.log"));

            let python = python_path(&handle);
            let server = server.clone();
            thread::spawn(move || supervise(window, server, python, log));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building mpftp");

    app.run(move |_app, event| {
        if let RunEvent::Exit = event {
            server_for_exit.stop();
        }
    });
}
