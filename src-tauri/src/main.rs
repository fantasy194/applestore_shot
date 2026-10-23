#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod engine;
mod browser_remote;
mod activity;
mod session_maintenance;
use engine::{Account, Engine, Snapshot, TaskHost};
use serde_json::Value;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tauri::{Emitter, Manager, State};

#[tauri::command]
fn snapshot(state: State<'_, Arc<Engine>>) -> Snapshot {
    state.snapshot()
}
#[tauri::command]
fn task_detail(state: State<'_, Arc<Engine>>, account_id: String, task_id: String, include_logs: Option<bool>) -> Result<Value, String> {
    state.task_read(&account_id, &task_id, include_logs.unwrap_or(true))
}
#[tauri::command]
fn add_account(state: State<'_, Arc<Engine>>, email: String) -> Result<Account, String> {
    state.add_account(email)
}
#[tauri::command]
async fn remove_account(
    host: State<'_, TaskHost>,
    state: State<'_, Arc<Engine>>,
    account_id: String,
) -> Result<(), String> {
    state.remove_account(host.inner(), &account_id).await
}
#[tauri::command]
fn save_preset(
    state: State<'_, Arc<Engine>>,
    account_id: String,
    sku: String,
    quantity: u32,
) -> Result<Account, String> {
    state.save_preset(&account_id, sku, quantity)
}
#[tauri::command]
async fn start_task(
    host: State<'_, TaskHost>,
    state: State<'_, Arc<Engine>>,
    account_id: String,
    action: String,
    params: Value,
) -> Result<String, String> {
    state
        .inner()
        .start(host.inner().clone(), account_id, action, params)
}
#[tauri::command]
fn reply_task(
    host: State<'_, TaskHost>,
    state: State<'_, Arc<Engine>>,
    account_id: String,
    task_id: String,
    request_id: String,
    value: Value,
) -> Result<(), String> {
    state.reply(host.inner(), &account_id, &task_id, &request_id, value)
}
#[tauri::command]
fn cancel_task(
    state: State<'_, Arc<Engine>>,
    account_id: String,
    task_id: String,
) -> Result<(), String> {
    state.cancel(&account_id, &task_id)
}

#[tauri::command]
async fn browser_command(
    state: State<'_, Arc<Engine>>,
    account_id: String,
    task_id: String,
    operation: String,
    params: Value,
) -> Result<Value, String> {
    browser_remote::command(state.inner(), &account_id, &task_id, &operation, &params).await
}

#[tauri::command]
async fn queue_command(
    host: State<'_, TaskHost>,
    state: State<'_, Arc<Engine>>,
    operation: String,
    params: Value,
) -> Result<Value, String> {
    state.inner().queue_command(host.inner().clone(), &operation, &params)
}

#[tauri::command]
async fn activity_logs(
    host: State<'_, TaskHost>,
    state: State<'_, Arc<Engine>>,
    params: Value,
) -> Result<Value, String> {
    activity::query(host.inner(), &state.root, params).await
}

#[tauri::command]
async fn maintenance_command(
    host: State<'_, TaskHost>,
    state: State<'_, Arc<Engine>>,
    operation: String,
    params: Value,
) -> Result<Value, String> {
    session_maintenance::command(state.inner().clone(), host.inner().clone(), &operation, &params).await
}

fn main() {
    let closing = Arc::new(AtomicBool::new(false));
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .setup(|app| {
            let engine = Engine::new(app.path().app_data_dir()?).map_err(std::io::Error::other)?;
            let handle = app.handle().clone();
            #[cfg(debug_assertions)]
            let (worker, script) = {
                let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .unwrap();
                (
                    root.join(if cfg!(windows) {
                        ".venv/Scripts/python.exe"
                    } else {
                        ".venv/bin/python"
                    }),
                    Some(root.join("worker/main.py")),
                )
            };
            #[cfg(not(debug_assertions))]
            let (worker, script) = (
                app.path()
                    .resource_dir()?
                    .join("worker")
                    .join(if cfg!(windows) {
                        "duo-killer-worker.exe"
                    } else {
                        "duo-killer-worker"
                    }),
                None,
            );
            let host = TaskHost {
                worker,
                script,
                on_update: Arc::new(move |task| {
                    let _ = handle.emit("task-updated", task);
                }),
            };
            tauri::async_runtime::block_on(async {
                activity::start(host.clone(), engine.root.clone());
                if let Err(error) = engine.restore_monitor(host.clone()) { eprintln!("{error}"); }
                session_maintenance::start(engine.clone(), host.clone());
            });
            app.manage(host);
            app.manage(engine);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            snapshot,
            task_detail,
            add_account,
            remove_account,
            save_preset,
            start_task,
            reply_task,
            cancel_task,
            browser_command,
            queue_command,
            activity_logs,
            maintenance_command
        ])
        .build(tauri::generate_context!())
        .expect("无法启动 DUO Killer");
    app.run(move |app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !closing.swap(true, Ordering::SeqCst) {
                api.prevent_exit();
                let app = app.clone();
                let engine = app.state::<Arc<Engine>>().inner().clone();
                tauri::async_runtime::spawn(async move {
                    session_maintenance::stop(&engine.root);
                    engine.shutdown().await;
                    activity::stop(&engine.root).await;
                    app.exit(0);
                });
            }
        }
    });
}
