use std::process::Command;

fn runtime_root() -> Result<std::path::PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "无法定位用户目录".to_string())?;
    Ok(std::path::PathBuf::from(home).join("Library/Application Support/HighDividend"))
}

#[tauri::command]
fn invoke_core(command: String, payload_json: String) -> Result<String, String> {
    let runtime = runtime_root()?;
    let script = runtime.join("scripts/desktop_core.py");
    let workbench = runtime.join("workbench.db");
    if !script.is_file() {
        return Err(format!("本机研究内核不存在：{}。请先部署运行时。", script.display()));
    }
    let output = Command::new("/usr/bin/python3")
        .arg(&script)
        .arg("--command")
        .arg(command)
        .arg("--payload")
        .arg(payload_json)
        .arg("--runtime-root")
        .arg(&runtime)
        .arg("--workbench")
        .arg(&workbench)
        .output()
        .map_err(|error| format!("无法启动本地研究内核：{error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    if output.status.success() {
        Ok(stdout)
    } else if !stdout.is_empty() {
        Ok(stdout)
    } else {
        Err(String::from_utf8_lossy(&output.stderr).to_string())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![invoke_core])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
