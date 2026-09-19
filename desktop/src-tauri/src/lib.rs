use std::io;
use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, RunEvent, State, WindowEvent,
};

/// Per-launch runtime values handed to the React frontend so it can reach the
/// Python sidecar on loopback with a short-lived session token.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeConfig {
    pub api_base_url: String,
    pub session_token: String,
}

impl RuntimeConfig {
    /// Bind a loopback ephemeral port and generate a fresh 256-bit session token.
    pub fn allocate() -> io::Result<Self> {
        let (port, session_token) = allocate_runtime()?;
        Ok(Self {
            api_base_url: format!("http://127.0.0.1:{port}"),
            session_token,
        })
    }
}

/// The running sidecar process, kept so it can be terminated on application exit.
pub struct SidecarProcess(Mutex<Option<Child>>);

impl SidecarProcess {
    #[allow(dead_code)]
    fn ensure_ready(&self, check: impl FnOnce() -> io::Result<()>) -> io::Result<()> {
        let result = check();
        if result.is_err() {
            if let Some(mut child) = self.0.lock().unwrap_or_else(|e| e.into_inner()).take() {
                terminate_sidecar(&mut child);
            }
        }
        result
    }
}

impl Drop for SidecarProcess {
    fn drop(&mut self) {
        if let Some(mut child) = self.0.get_mut().unwrap_or_else(|e| e.into_inner()).take() {
            terminate_sidecar(&mut child);
        }
    }
}

struct ActiveDataRoot(PathBuf);

fn validated_document_path(root: &std::path::Path, path: &std::path::Path) -> Result<PathBuf, String> {
    let managed = root.join("temp/open-documents").canonicalize().map_err(|e| e.to_string())?;
    let target = path.canonicalize().map_err(|e| e.to_string())?;
    let extension = target.extension().and_then(|s| s.to_str()).unwrap_or("").to_ascii_lowercase();
    if !target.starts_with(managed) || !target.is_file() || !matches!(extension.as_str(), "doc" | "docx") {
        return Err("仅允许打开受管理的 Word 简历副本".to_string());
    }
    Ok(target)
}

#[tauri::command]
fn open_document(path: String, root: State<'_, ActiveDataRoot>) -> Result<(), String> {
    let target = validated_document_path(&root.0, std::path::Path::new(&path))?;
    open::that(target).map_err(|e| e.to_string())
}

fn configure_sidecar_command(command: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
}

fn allocate_runtime() -> io::Result<(u16, String)> {
    Ok((allocate_loopback_port()?, generate_session_token()))
}

fn allocate_loopback_port() -> io::Result<u16> {
    // Bind to 127.0.0.1:0 and ask the OS for an ephemeral port. The sidecar
    // re-binds it; the tiny window is inherent to any port reservation scheme.
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    Ok(listener.local_addr()?.port())
}

fn generate_session_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("operating system randomness must be available");
    hex::encode(bytes)
}

/// Forward only the scoped runtime values the sidecar accepts; never leak any
/// other process environment into the child.
pub fn sidecar_arguments(port: u16, token: String, data_root: PathBuf) -> Vec<String> {
    vec![
        "--port".to_string(),
        port.to_string(),
        "--token".to_string(),
        token,
        "--data-root".to_string(),
        data_root.to_string_lossy().into_owned(),
    ]
}

fn default_data_root() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        // 便携式：数据跟随安装目录，放在 <exe 目录>\data 子目录。
        std::env::current_exe()
            .ok()
            .and_then(|exe| exe.parent().map(|p| p.to_path_buf()))
            .map(|dir| dir.join("data"))
            .unwrap_or_else(|| PathBuf::from(".").join("data"))
    }
    #[cfg(target_os = "macos")]
    {
        std::env::var("HOME")
            .map(|home| PathBuf::from(home).join("Library/Application Support/KeRuiRecruit"))
            .unwrap_or_else(|_| PathBuf::from(".").join("KeRuiRecruit"))
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        PathBuf::from(".").join("KeRuiRecruit")
    }
}

fn config_dir() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        std::env::var("APPDATA")
            .map(|dir| PathBuf::from(dir).join("KeRuiRecruit"))
            .unwrap_or_else(|_| PathBuf::from(".").join("KeRuiRecruit"))
    }
    #[cfg(target_os = "macos")]
    {
        std::env::var("HOME")
            .map(|home| PathBuf::from(home).join("Library/Application Support/KeRuiRecruit"))
            .unwrap_or_else(|_| PathBuf::from(".").join("KeRuiRecruit"))
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        PathBuf::from(".").join("KeRuiRecruit")
    }
}

fn data_root_config_path() -> PathBuf {
    config_dir().join("data_root.txt")
}

fn resolve_data_root() -> PathBuf {
    if let Ok(content) = std::fs::read_to_string(data_root_config_path()) {
        let trimmed = content.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    default_data_root()
}

/// 旧版默认数据目录（迁移来源，仅 Windows 需要）。
fn legacy_data_root() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        std::env::var("LOCALAPPDATA")
            .map(|dir| PathBuf::from(dir).join("KeRuiRecruit"))
            .unwrap_or_default()
    }
    #[cfg(not(target_os = "windows"))]
    {
        PathBuf::new()
    }
}

/// 递归复制目录内容（标准库无内置 copy_dir）。
fn copy_dir_recursive(src: &std::path::Path, dst: &std::path::Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let path = entry.path();
        let target = dst.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir_recursive(&path, &target)?;
        } else {
            std::fs::copy(&path, &target)?;
        }
    }
    Ok(())
}

/// 首次启动时把旧 C 盘数据迁移到新默认目录（仅当新目录为空时，避免覆盖）。
fn migrate_legacy_if_needed(data_root: &std::path::Path) -> std::io::Result<()> {
    let legacy = legacy_data_root();
    if legacy.as_os_str().is_empty() || legacy.as_path() == data_root || !legacy.exists() {
        return Ok(());
    }
    if data_root.exists() && std::fs::read_dir(data_root)?.next().is_some() {
        // 新目录已有数据：不覆盖。
        return Ok(());
    }
    copy_dir_recursive(&legacy, data_root)
}

/// 检测数据目录是否可写（创建目录并写入/删除临时探测文件）。
fn ensure_writable(data_root: &std::path::Path) -> std::io::Result<()> {
    std::fs::create_dir_all(data_root)?;
    let probe = data_root.join(".write-test");
    std::fs::write(&probe, b"ok")?;
    let _ = std::fs::remove_file(&probe);
    Ok(())
}

fn validate_data_root(path: &std::path::Path) -> Result<(), String> {
    let text = path.to_string_lossy();
    let lower = text.to_lowercase();

    for marker in ["onedrive", "dropbox", "icloud", "google drive", "box"] {
        if lower.contains(marker) {
            return Err(format!("禁止使用云同步目录（{}）", marker));
        }
    }
    if text.starts_with("\\\\") {
        return Err("禁止使用网络盘".to_string());
    }
    #[cfg(target_os = "windows")]
    {
        if let Ok(windows_dir) = std::env::var("WINDIR") {
            let root = PathBuf::from(windows_dir);
            if path.starts_with(&root) {
                return Err("禁止使用系统目录".to_string());
            }
        }
    }
    Ok(())
}

fn persist_data_root(path: &std::path::Path) -> std::io::Result<()> {
    let dir = config_dir();
    std::fs::create_dir_all(&dir)?;
    std::fs::write(data_root_config_path(), path.to_string_lossy().as_bytes())
}

fn sidecar_candidates(
    executable_directory: &std::path::Path,
    resource_directory: &std::path::Path,
    name: &str,
) -> Vec<PathBuf> {
    vec![
        // 单文件（onefile）与开发模式：直接同名可执行文件
        executable_directory.join(name),
        resource_directory.join(name),
        resource_directory.join("binaries").join(name),
        // 目录模式（onedir）：binaries/<name>/<name> 内部的同名可执行文件
        executable_directory.join(name).join(name),
        resource_directory.join(name).join(name),
        resource_directory.join("binaries").join(name).join(name),
    ]
}

fn resolve_sidecar_binary(app: &tauri::AppHandle) -> PathBuf {
    if let Ok(path) = std::env::var("KERUI_SIDECAR_BIN") {
        return PathBuf::from(path);
    }
    let name = if cfg!(windows) {
        "kerui-recruit-sidecar.exe"
    } else {
        "kerui-recruit-sidecar"
    };
    let executable_directory = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(std::path::Path::to_path_buf));
    let resource_directory = app.path().resource_dir().ok();

    if let (Some(executable_directory), Some(resource_directory)) =
        (&executable_directory, &resource_directory)
    {
        for candidate in sidecar_candidates(executable_directory, resource_directory, name) {
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    executable_directory
        .map(|directory| directory.join(name))
        .unwrap_or_else(|| PathBuf::from(name))
}

#[allow(dead_code)]
fn wait_until_ready(config: &RuntimeConfig, timeout: Duration) -> io::Result<()> {
    let deadline = Instant::now() + timeout;
    let url = format!(
        "{}/health/ready",
        config.api_base_url.trim_end_matches('/')
    );
    while Instant::now() < deadline {
        let ready = tauri::async_runtime::block_on(async {
            reqwest::Client::new()
                .get(&url)
                .timeout(Duration::from_secs(1))
                .send()
                .await
                .map(|response| response.status().is_success())
                .unwrap_or(false)
        });
        if ready {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "sidecar did not become ready in time",
    ))
}

/// Terminate the sidecar and, on Windows, its whole process tree.
///
/// PyInstaller one-file executables fork a child interpreter, so killing only
/// the bootloader leaves the real server process orphaned. Use `taskkill /T`
/// on Windows and a dedicated process group on Unix, then reap the bootloader.
fn terminate_sidecar(child: &mut Child) {
    #[cfg(target_os = "windows")]
    {
        let pid = child.id();
        let mut command = Command::new("taskkill");
        configure_sidecar_command(&mut command);
        let _ = command.args(["/PID", &pid.to_string(), "/T", "/F"]).output();
    }
    #[cfg(unix)]
    {
        let _ = Command::new("/bin/kill").args(["-KILL", "--", &format!("-{}", child.id())]).output();
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[tauri::command]
fn runtime_config(state: State<'_, RuntimeConfig>) -> RuntimeConfig {
    state.inner().clone()
}

#[tauri::command]
fn set_data_root(path: String) -> Result<String, String> {
    let target = PathBuf::from(path.trim());
    validate_data_root(&target)?;
    persist_data_root(&target).map_err(|error| error.to_string())?;
    Ok(target.to_string_lossy().into_owned())
}

/// Open a user-provided URL in the system browser. Only `http`/`https` are
/// allowed; anything else (e.g. `file:`, `javascript:`) is rejected to keep
/// the WebView from navigating to privileged or scriptable schemes.
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    let parsed = url::Url::parse(&url).map_err(|_| "非法链接".to_string())?;
    match parsed.scheme() {
        "http" | "https" => {}
        _ => return Err("仅支持 http/https 链接".to_string()),
    }
    open::that(parsed.as_str()).map_err(|error| error.to_string())
}

/// Save binary content to a user-chosen location via a native file dialog.
/// Returns the chosen path, or `None` if the user cancelled the dialog.
#[tauri::command]
fn save_file(filename: String, content: Vec<u8>) -> Result<Option<String>, String> {
    let path = rfd::FileDialog::new().set_file_name(&filename).save_file();
    match path {
        Some(target) => {
            std::fs::write(&target, &content).map_err(|error| error.to_string())?;
            Ok(Some(target.to_string_lossy().into_owned()))
        }
        None => Ok(None),
    }
}

/// Terminate the sidecar and restart the whole app so saved settings reload.
#[tauri::command]
fn restart_app(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(process) = app.try_state::<SidecarProcess>() {
        if let Some(mut child) = process
            .0
            .lock()
            .map_err(|_| "sidecar 进程状态不可用".to_string())?
            .take()
        {
            terminate_sidecar(&mut child);
        }
    }
    app.restart();
    #[allow(unreachable_code)]
    Ok(())
}

fn create_tray(app: &tauri::App) -> tauri::Result<()> {
    let Some(icon) = app.default_window_icon() else {
        return Ok(());
    };
    let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    TrayIconBuilder::with_id("main-tray")
        .icon(icon.clone())
        .tooltip("工作台")
        .menu(&menu)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            "quit" => {
                app.exit(0);
            }
            _ => {}
        })
        .build(app)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let config = RuntimeConfig::allocate()?;
            let sidecar = resolve_sidecar_binary(app.handle());
            let data_root = resolve_data_root();

            // 仅使用默认路径（未自定义）时，首次启动自动迁移旧 C 盘数据。
            let using_default = std::fs::read_to_string(data_root_config_path()).is_err();
            if using_default {
                if let Err(error) = migrate_legacy_if_needed(&data_root) {
                    eprintln!("migrate legacy data failed: {error}");
                }
            }

            // 可写性检测：不可写则提示用户更换安装位置后退出。
            if let Err(error) = ensure_writable(&data_root) {
                rfd::MessageDialog::new()
                    .set_level(rfd::MessageLevel::Error)
                    .set_title("数据目录不可写")
                    .set_description(&format!(
                        "无法写入数据目录：{}\n\n请将应用安装到可写目录（例如 D 盘，不要安装到 C:\\Program Files 等需要管理员权限的位置），或在「系统设置 → 数据目录」中改为可写路径。\n\n错误：{error}",
                        data_root.display()
                    ))
                    .show();
                std::process::exit(1);
            }

            let arguments = sidecar_arguments(
                parse_port(&config.api_base_url)?,
                config.session_token.clone(),
                data_root.clone(),
            );
            let mut command = Command::new(&sidecar);
            configure_sidecar_command(&mut command);
            let child = command
                .args(&arguments)
                .spawn()
                .map_err(|error| {
                    io::Error::new(
                        error.kind(),
                        format!("failed to start sidecar {}: {error}", sidecar.display()),
                    )
                })?;
            let process = SidecarProcess(Mutex::new(Some(child)));
            // 不阻塞窗口显示：sidecar 在后台就绪，前端轮询 /health/ready 后再加载数据。
            app.manage(process);
            app.manage(ActiveDataRoot(data_root));
            app.manage(config);
            create_tray(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![runtime_config, set_data_root, open_external, open_document, save_file, restart_app])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Closing the window minimizes to the system tray instead of
                // exiting; the tray "退出" menu item performs the real exit.
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| match event {
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => {
                // macOS：点击 Dock 图标重新激活应用时，把被隐藏的主窗口恢复显示。
                if let Some(window) = app_handle.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
            RunEvent::Exit => {
                if let Some(process) = app_handle.try_state::<SidecarProcess>() {
                    if let Some(mut child) = process.0.lock().unwrap().take() {
                        terminate_sidecar(&mut child);
                    }
                }
            }
            _ => {}
        });
}

fn parse_port(api_base_url: &str) -> io::Result<u16> {
    let suffix = api_base_url
        .strip_prefix("http://127.0.0.1:")
        .or_else(|| api_base_url.strip_prefix("http://localhost:"))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "unexpected API base URL"))?;
    suffix
        .trim_end_matches('/')
        .parse::<u16>()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid API port"))
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::{sidecar_arguments, sidecar_candidates, validate_data_root, RuntimeConfig};

    #[test]
    fn document_open_accepts_only_managed_word_files() {
        let root = std::env::temp_dir().join(super::generate_session_token());
        let managed = root.join("temp/open-documents");
        std::fs::create_dir_all(&managed).unwrap();
        let word = managed.join("resume.DOCX");
        std::fs::write(&word, b"fake document").unwrap();
        assert!(super::validated_document_path(&root, &word).is_ok());
        let other = root.join("original.docx");
        std::fs::write(&other, b"canonical").unwrap();
        assert!(super::validated_document_path(&root, &other).is_err());
        let pdf = managed.join("resume.pdf");
        std::fs::write(&pdf, b"pdf").unwrap();
        assert!(super::validated_document_path(&root, &pdf).is_err());
        assert!(super::validated_document_path(&root, &managed).is_err());
        assert!(super::validated_document_path(&root, &managed.join("missing.doc")).is_err());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn failed_readiness_terminates_and_reaps_owned_child() {
        #[cfg(windows)]
        let mut command = { let mut c = std::process::Command::new("powershell.exe"); c.args(["-NoProfile", "-Command", "Start-Sleep -Seconds 60"]); c };
        #[cfg(unix)]
        let mut command = { let mut c = std::process::Command::new("sleep"); c.arg("60"); c };
        super::configure_sidecar_command(&mut command);
        let process = super::SidecarProcess(std::sync::Mutex::new(Some(command.spawn().unwrap())));
        let result = process.ensure_ready(|| Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "fake readiness")));
        assert!(result.is_err());
        assert!(process.0.lock().unwrap().is_none());
    }

    #[test]
    fn runtime_config_uses_loopback_and_a_256_bit_launch_token() {
        let config = RuntimeConfig::allocate().expect("runtime config");

        assert!(config.api_base_url.starts_with("http://127.0.0.1:"));
        assert_eq!(config.session_token.len(), 64);
        assert!(config.session_token.chars().all(|character| character.is_ascii_hexdigit()));
    }

    #[test]
    fn sidecar_arguments_forward_only_scoped_runtime_values() {
        let args = sidecar_arguments(
            43127,
            "a".repeat(64),
            PathBuf::from("C:/Users/example/AppData/Local/KeRuiRecruit"),
        );

        assert_eq!(args, vec![
            "--port", "43127",
            "--token", &"a".repeat(64),
            "--data-root", "C:/Users/example/AppData/Local/KeRuiRecruit",
        ]);
    }

    #[test]
    fn sidecar_candidates_include_tauri_bundled_resource_directory() {
        let candidates = sidecar_candidates(
            std::path::Path::new("C:/Program Files/KeRui"),
            std::path::Path::new("C:/Program Files/KeRui/resources"),
            "kerui-recruit-sidecar.exe",
        );

        assert_eq!(
            candidates,
            vec![
                PathBuf::from("C:/Program Files/KeRui/kerui-recruit-sidecar.exe"),
                PathBuf::from("C:/Program Files/KeRui/resources/kerui-recruit-sidecar.exe"),
                PathBuf::from("C:/Program Files/KeRui/resources/binaries/kerui-recruit-sidecar.exe"),
                PathBuf::from("C:/Program Files/KeRui/kerui-recruit-sidecar.exe/kerui-recruit-sidecar.exe"),
                PathBuf::from("C:/Program Files/KeRui/resources/kerui-recruit-sidecar.exe/kerui-recruit-sidecar.exe"),
                PathBuf::from("C:/Program Files/KeRui/resources/binaries/kerui-recruit-sidecar.exe/kerui-recruit-sidecar.exe"),
            ]
        );
    }

    #[test]
    fn validate_data_root_rejects_cloud_sync_directories() {
        assert!(validate_data_root(std::path::Path::new("C:\\Users\\me\\OneDrive\\data")).is_err());
        assert!(validate_data_root(std::path::Path::new("/Users/me/Dropbox/data")).is_err());
        assert!(validate_data_root(std::path::Path::new("/Users/me/iCloud/data")).is_err());
    }

    #[test]
    fn validate_data_root_rejects_network_paths() {
        assert!(validate_data_root(std::path::Path::new("\\\\server\\share\\data")).is_err());
    }

    #[test]
    fn validate_data_root_accepts_local_directory() {
        assert!(validate_data_root(std::path::Path::new("C:\\Users\\me\\KeRuiRecruit")).is_ok());
    }
}
