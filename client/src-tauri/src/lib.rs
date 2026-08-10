use serde::Serialize;
use std::fs;
use std::path::Path;
use std::process::Command;
use tauri::{AppHandle, Manager};

const SEED_COMPONENTS: [&str; 5] = ["scripts", "rules", "strategies", "config", "database"];

fn runtime_root() -> Result<std::path::PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "无法定位用户目录".to_string())?;
    Ok(std::path::PathBuf::from(home).join("Library/Application Support/HighDividend"))
}

fn copy_seed_path(source: &Path, target: &Path) -> Result<(), String> {
    if source.is_dir() {
        fs::create_dir_all(target).map_err(|error| format!("无法创建运行时目录：{error}"))?;
        for entry in fs::read_dir(source).map_err(|error| format!("无法读取运行时资源：{error}"))? {
            let entry = entry.map_err(|error| format!("无法读取运行时资源项：{error}"))?;
            copy_seed_path(&entry.path(), &target.join(entry.file_name()))?;
        }
    } else if source.is_file() {
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).map_err(|error| format!("无法创建运行时目录：{error}"))?;
        }
        fs::copy(source, target).map_err(|error| format!("无法部署运行时资源：{error}"))?;
    }
    Ok(())
}

fn copy_seed_tree(seed_root: &Path, runtime_root: &Path) -> Result<(), String> {
    fs::create_dir_all(runtime_root).map_err(|error| format!("无法创建运行时根目录：{error}"))?;
    for component in SEED_COMPONENTS {
        let source = seed_root.join(component);
        if source.exists() {
            copy_seed_path(&source, &runtime_root.join(component))?;
        }
    }
    Ok(())
}

#[derive(Debug, Serialize)]
struct RuntimeStatus {
    runtime_root: String,
    code_ready: bool,
    data_ready: bool,
    python_available: bool,
    bundled_seed_available: bool,
    missing: Vec<String>,
}

fn inspect_runtime(seed_available: bool) -> Result<RuntimeStatus, String> {
    let runtime = runtime_root()?;
    let required = [
        "scripts/desktop_core.py",
        "scripts/release_protocol.py",
        "rules/stable_dividend_stock_v1.json",
        "strategies/stable_dividend_stock_v1.json",
        "config/candidate_universe_core.json",
        "database/workbench_schema.sql",
    ];
    let missing = required
        .iter()
        .filter(|relative| !runtime.join(relative).is_file())
        .map(|relative| (*relative).to_string())
        .collect::<Vec<_>>();
    let data_ready = runtime.join("current-release.json").is_file();
    Ok(RuntimeStatus {
        runtime_root: runtime.display().to_string(),
        code_ready: missing.is_empty(),
        data_ready,
        python_available: Path::new("/usr/bin/python3").is_file(),
        bundled_seed_available: seed_available,
        missing,
    })
}

#[tauri::command]
fn runtime_status(app: AppHandle) -> Result<RuntimeStatus, String> {
    let seed_available = app
        .path()
        .resource_dir()
        .map(|path| SEED_COMPONENTS.iter().any(|component| path.join(component).exists()))
        .unwrap_or(false);
    inspect_runtime(seed_available)
}

#[tauri::command]
fn bootstrap_runtime(app: AppHandle) -> Result<RuntimeStatus, String> {
    let seed_root = app.path().resource_dir().map_err(|error| format!("无法定位内置运行时资源：{error}"))?;
    if !SEED_COMPONENTS.iter().any(|component| seed_root.join(component).exists()) {
        return Err("安装包未包含内置研究运行时资源".to_string());
    }
    let runtime = runtime_root()?;
    copy_seed_tree(&seed_root, &runtime)?;
    inspect_runtime(true)
}

#[cfg(test)]
mod tests {
    use super::copy_seed_tree;
    use std::fs;

    #[test]
    fn bootstrap_copies_seed_components_without_touching_data() {
        let root = std::env::temp_dir().join(format!("high-dividend-bootstrap-{}", std::process::id()));
        let seed = root.join("seed");
        let runtime = root.join("runtime");
        fs::create_dir_all(seed.join("scripts")).unwrap();
        fs::create_dir_all(seed.join("rules")).unwrap();
        fs::create_dir_all(&runtime).unwrap();
        fs::write(seed.join("scripts/desktop_core.py"), "seed-core").unwrap();
        fs::write(seed.join("rules/stable.json"), "seed-rule").unwrap();
        fs::create_dir_all(runtime.join("data")).unwrap();
        fs::write(runtime.join("data/keep.db"), "user-data").unwrap();

        copy_seed_tree(&seed, &runtime).unwrap();

        assert_eq!(fs::read_to_string(runtime.join("scripts/desktop_core.py")).unwrap(), "seed-core");
        assert_eq!(fs::read_to_string(runtime.join("rules/stable.json")).unwrap(), "seed-rule");
        assert_eq!(fs::read_to_string(runtime.join("data/keep.db")).unwrap(), "user-data");
        let _ = fs::remove_dir_all(root);
    }
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
    } else {
        Err(if stdout.is_empty() {
            String::from_utf8_lossy(&output.stderr).to_string()
        } else {
            stdout
        })
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![invoke_core, runtime_status, bootstrap_runtime])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
