//! Per-account idle preheat. Login or pickup preparation; orders are never submitted.
use crate::{activity, engine::{Engine, Snapshot, TaskHost, task_has_unknown_submission}};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::HashMap, fs, path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock}, time::{Duration, SystemTime, UNIX_EPOCH}};
use tokio::sync::watch;

static SERVICES: OnceLock<Mutex<HashMap<PathBuf, Arc<Maintenance>>>> = OnceLock::new();
fn services() -> &'static Mutex<HashMap<PathBuf, Arc<Maintenance>>> {
    SERVICES.get_or_init(|| Mutex::new(HashMap::new()))
}
fn now_ms() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64 }
fn default_enabled() -> bool { true }
fn default_interval() -> u32 { 10 }
fn warm_mode() -> String { "idle_warm_login".into() }
fn default_flow() -> String { "login".into() }
fn valid_flow(flow: &str) -> bool { matches!(flow, "login" | "pickup_prepare") }
fn flow_label(flow: &str) -> &'static str {
    if flow == "pickup_prepare" { "提前准备自提（不下单）" } else { "预热登录（不加购）" }
}
fn configure_flow(settings: &mut Settings, value: &Value, current: u64) -> Result<(), String> {
    let flow = value.as_str().filter(|flow| valid_flow(flow)).ok_or("预热流程无效")?;
    if settings.flow != flow { settings.enabled_at_ms = current; settings.flow = flow.into(); }
    Ok(())
}

#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    #[serde(default = "default_flow")]
    flow: String,
    #[serde(default)]
    mode: String,
    #[serde(default = "default_enabled")]
    enabled: bool,
    #[serde(default = "default_interval")]
    interval_minutes: u32,
    #[serde(default)]
    enabled_at_ms: u64,
    #[serde(default)]
    last_runs: Vec<Run>,
}
impl Default for Settings {
    fn default() -> Self { Self { flow: default_flow(), mode: warm_mode(), enabled: true, interval_minutes: 10,
        enabled_at_ms: 0, last_runs: Vec::new() } }
}

#[derive(Clone, Serialize, Deserialize)]
struct Run {
    #[serde(default = "default_flow")]
    flow: String,
    account_id: String,
    #[serde(default)]
    preparation_stage: Option<String>,
    #[serde(default)]
    order_ready: bool,
    status: String,
    last_checked_at_ms: u64,
    #[serde(default)]
    last_warm_at_ms: u64,
    next_check_at_ms: u64,
    message: String,
    requires_login: bool,
    cooldown_until_ms: u64,
    #[serde(default)]
    task_id: Option<String>,
}

pub struct Maintenance {
    root: PathBuf,
    started_at_ms: u64,
    settings: Mutex<Settings>,
    settings_error: Mutex<Option<String>>,
    stopping: watch::Sender<bool>,
}

fn save(root: &Path, settings: &Settings) -> Result<(), String> {
    let directory = root.join("settings");
    fs::create_dir_all(&directory).map_err(|_| "无法创建会话维护设置目录")?;
    let path = directory.join("session-maintenance.json");
    let temporary = directory.join(format!(".session-maintenance-{}.tmp", uuid::Uuid::new_v4()));
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
    use std::io::Write;
    let mut file = options.open(&temporary).map_err(|_| "无法保存会话维护设置")?;
    let result = (|| {
        file.write_all(&serde_json::to_vec(settings).map_err(|_| "会话维护设置无效")?)
            .map_err(|_| "无法写入会话维护设置")?;
        file.sync_all().map_err(|_| "无法同步会话维护设置")?;
        drop(file);
        fs::rename(&temporary, &path).map_err(|_| "无法替换会话维护设置")
    })();
    if result.is_err() { let _ = fs::remove_file(&temporary); }
    result.map_err(str::to_owned)
}

fn load(root: &Path) -> (Settings, Option<String>) {
    let path = root.join("settings/session-maintenance.json");
    if !path.exists() { return (Settings::default(), None); }
    match fs::read(path).ok().and_then(|raw| serde_json::from_slice::<Settings>(&raw).ok()) {
        Some(mut settings) if (5..=240).contains(&settings.interval_minutes)
            && settings.last_runs.len() <= 1000 && valid_flow(&settings.flow)
            && settings.last_runs.iter().all(|run| valid_flow(&run.flow)) => {
                if settings.mode != warm_mode() {
                    // Legacy read-only sign-in redirects are not a failed login attempt.
                    // Preserve their cooldown, and preserve manual recovery for 541 blocks.
                    for run in &mut settings.last_runs {
                        if run.status == "requires_login" { run.requires_login = false; }
                    }
                }
                settings.mode = warm_mode();
                settings.interval_minutes = 10;
                (settings, None)
            },
        _ => (Settings { enabled: false, ..Settings::default() },
            Some("会话维护配置无法读取，已暂停自动维护；请重新保存设置。".into())),
    }
}

fn active(status: &str) -> bool { matches!(status, "queued" | "running" | "waiting" | "scheduled") }

fn last_order_activity(snapshot: &Snapshot, account: &str) -> u64 {
    snapshot.tasks.iter().filter(|task| task.account_id == account
        && matches!(task.action.as_str(), "purchase" | "checkout")
        && (task.execution_started_at_ms.is_some()
            || matches!(task.status.as_str(), "running" | "waiting")
            || task.schedule.as_ref().is_some_and(|schedule| schedule.triggered_at_ms.is_some())))
        .map(|task| task.updated_at.max(task.created_at).saturating_mul(1000)).max().unwrap_or(0)
}

fn blocked_reason(snapshot: &Snapshot, account: &str) -> Option<&'static str> {
    if snapshot.tasks.iter().any(|task| task.account_id == account
        && (matches!(task.status.as_str(), "running" | "waiting")
            || task.action == "session_maintain" && active(&task.status))) {
        return Some("account_busy");
    }
    if snapshot.tasks.iter().any(|task| task.account_id == account
        && task_has_unknown_submission(task)) {
        return Some("order_unresolved");
    }
    None
}

fn next_check(snapshot: &Snapshot, run: &Run, settings: &Settings, started_at: u64) -> u64 {
    started_at.max(settings.enabled_at_ms).max(last_order_activity(snapshot, &run.account_id))
        .max(run.last_checked_at_ms).max(run.last_warm_at_ms)
        .saturating_add(u64::from(settings.interval_minutes) * 60_000)
        .max(run.cooldown_until_ms)
}

impl Maintenance {
    fn refresh(&self, snapshot: &Snapshot) {
        let mut settings = self.settings.lock().unwrap();
        let before = serde_json::to_value(&*settings).ok();
        let interval = settings.interval_minutes;
        let flow = settings.flow.clone();
        for account in snapshot.accounts.iter().filter(|account| !account.pending) {
            if !settings.last_runs.iter().any(|run| run.account_id == account.id) {
                settings.last_runs.push(Run { flow: flow.clone(), account_id: account.id.clone(), preparation_stage: None, order_ready: false, status: "waiting".into(),
                    last_checked_at_ms: 0, last_warm_at_ms: 0,
                    next_check_at_ms: self.started_at_ms + u64::from(interval) * 60_000,
                    message: format!("下单空闲10分钟后执行{}。", flow_label(&flow)), requires_login: false,
                    cooldown_until_ms: 0, task_id: None });
            }
        }
        settings.last_runs.retain(|run| snapshot.accounts.iter().any(|account| account.id == run.account_id));
        for run in &mut settings.last_runs {
            if let Some(task) = run.task_id.as_ref().and_then(|id| snapshot.tasks.iter().find(|task| &task.id == id)) {
                if !active(&task.status) && run.status == "running" {
                    run.last_warm_at_ms = task.updated_at.saturating_mul(1000);
                    let result = task.result.as_ref().unwrap_or(&Value::Null);
                    run.last_checked_at_ms = result["checked_at_ms"].as_u64()
                        .filter(|stamp| *stamp >= task.created_at.saturating_mul(1000)
                            && *stamp < task.updated_at.saturating_add(1).saturating_mul(1000))
                        .unwrap_or_else(|| task.updated_at.saturating_mul(1000));
                    run.status = result["status"].as_str().filter(|status|
                        matches!(*status, "healthy" | "prepared" | "waiting_stock" | "requires_login" | "blocked" | "failed")
                            && !(run.flow == "pickup_prepare" && *status == "healthy"))
                        .unwrap_or(if task.status == "succeeded" && run.flow == "login" { "healthy" } else { "failed" }).into();
                    run.preparation_stage = result["preparation_stage"].as_str().map(str::to_owned);
                    run.order_ready = result["order_ready"] == true && result["review_verified"] == true && run.status == "prepared";
                    run.message = result["message"].as_str().unwrap_or(&task.stage).chars().take(400).collect();
                    run.requires_login = result["requires_login"] == true;
                    run.cooldown_until_ms = result["cooldown_until_ms"].as_u64().unwrap_or(0);
                    if task.status == "interrupted" || task.status == "cancelled" {
                        run.message = "上次空闲预热已中断，下单优先；未重放购买。".into();
                    }
                }
            }
            if run.requires_login && snapshot.tasks.iter().any(|task| task.account_id == run.account_id
                && matches!(task.action.as_str(), "login" | "enroll" | "me")
                && task.status == "succeeded" && task.updated_at.saturating_mul(1000) >= run.last_checked_at_ms
                && task.result.as_ref().is_some_and(|result| result["logged_in"] == true)) {
                run.requires_login = false;
                run.cooldown_until_ms = 0;
                run.status = "waiting".into();
                run.message = "账号已重新验证，等待下次空闲维护。".into();
            }
        }
        let config = settings.clone();
        for run in &mut settings.last_runs { run.next_check_at_ms = next_check(snapshot, run, &config, self.started_at_ms); }
        if before != serde_json::to_value(&*settings).ok() && self.settings_error.lock().unwrap().is_none() {
            let _ = save(&self.root, &settings);
        }
    }

    fn status(&self, engine: &Engine) -> Value {
        let snapshot = engine.snapshot();
        self.refresh(&snapshot);
        let mut result = serde_json::to_value(self.settings.lock().unwrap().clone()).unwrap_or(json!({}));
        result["settings_error"] = json!(self.settings_error.lock().unwrap().clone());
        let current = now_ms();
        if let Some(runs) = result["last_runs"].as_array_mut() {
            for run in runs {
                let account = run["account_id"].as_str().unwrap_or("").to_owned();
                let reason = blocked_reason(&snapshot, &account).or_else(||
                    (run["requires_login"] == true).then_some("requires_login"));
                run["last_order_activity_at_ms"] = json!(last_order_activity(&snapshot, &account));
                run["remaining_seconds"] = json!(run["next_check_at_ms"].as_u64().unwrap_or(0)
                    .saturating_sub(current).saturating_add(999) / 1000);
                run["busy"] = json!(reason == Some("account_busy"));
                run["blocked_reason"] = json!(reason);
            }
        }
        result
    }

    fn run_account(&self, engine: &Arc<Engine>, host: &TaskHost, account_id: &str) -> Result<(), String> {
        if *self.stopping.borrow() { return Err("会话维护正在退出".into()); }
        let snapshot = engine.snapshot();
        let account = snapshot.accounts.iter().find(|account| account.id == account_id && !account.pending)
            .ok_or("请先选择已完成登录的账号")?;
        if let Some(reason) = blocked_reason(&snapshot, account_id) {
            return Err(if reason == "order_unresolved" { "账号有待核实下单结果，暂停登录预热" }
                else { "账号正在执行任务，稍后再预热" }.into());
        }
        // Freeze this dispatch. Changing the configured flow never relabels an active task.
        let flow = self.settings.lock().unwrap().flow.clone();
        let mut params = json!({"maintenance_trigger":true,"warm_only":true,"preheat_flow":flow});
        if flow == "pickup_prepare" {
            params["sku"] = json!(account.preset_sku);
            params["quantity"] = json!(account.preset_quantity);
        }
        let task_id = engine.start(host.clone(), account_id.into(), "session_maintain".into(), params)?;
        let mut settings = self.settings.lock().unwrap();
        if let Some(run) = settings.last_runs.iter_mut().find(|run| run.account_id == account_id) {
            run.task_id = Some(task_id.clone());
            run.flow = flow.clone();
            run.preparation_stage = None;
            run.order_ready = false;
            run.status = "running".into();
            run.message = format!("正在{}；下单任务优先。", flow_label(&flow));
        }
        if self.settings_error.lock().unwrap().is_none() { let _ = save(&self.root, &settings); }
        activity::append(&self.root, "session_maintenance_started", &json!({"account_id":account_id,
            "task_id":task_id,"category":"authentication","action":"session_maintain",
            "message":format!("开始空闲预热：{}。", flow_label(&flow)),"flow":flow,"status":"running"}));
        Ok(())
    }

    fn tick(&self, engine: &Arc<Engine>, host: &TaskHost) {
        self.tick_at(engine, host, now_ms());
    }

    fn tick_at(&self, engine: &Arc<Engine>, host: &TaskHost, current: u64) {
        if *self.stopping.borrow() { return; }
        let snapshot = engine.snapshot();
        self.refresh(&snapshot);
        let settings = self.settings.lock().unwrap().clone();
        if !settings.enabled { return; }
        // One browser at a time for idle maintenance; purchase dispatch takes priority.
        if snapshot.tasks.iter().any(|task| task.action == "session_maintain" && active(&task.status)) { return; }
        for run in settings.last_runs {
            if run.requires_login || run.status == "running" || run.next_check_at_ms > current { continue; }
            if blocked_reason(&snapshot, &run.account_id).is_some() { continue; }
            if self.run_account(engine, host, &run.account_id).is_ok() { break; }
        }
    }
}

pub fn start(engine: Arc<Engine>, host: TaskHost) {
    let root = engine.root.clone();
    let (stopping, mut stop) = watch::channel(false);
    let (settings, error) = load(&root);
    let service = Arc::new(Maintenance { root: root.clone(), started_at_ms: now_ms(),
        settings: Mutex::new(settings), settings_error: Mutex::new(error), stopping });
    {
        let mut registry = services().lock().unwrap();
        if registry.contains_key(&root) { return; }
        registry.insert(root.clone(), service.clone());
    }
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = stop.changed() => break,
                _ = tokio::time::sleep(Duration::from_secs(15)) => service.tick(&engine, &host),
            }
        }
        services().lock().unwrap().remove(&root);
    });
}

pub async fn command(engine: Arc<Engine>, host: TaskHost, operation: &str, params: &Value) -> Result<Value, String> {
    if !params.is_object() { return Err("会话维护参数无效".into()); }
    start(engine.clone(), host.clone());
    let service = services().lock().unwrap().get(&engine.root).cloned().ok_or("会话维护正在退出")?;
    match operation {
        "status" => {},
        "configure" => {
            let mut next = service.settings.lock().unwrap().clone();
            if let Some(enabled) = params.get("enabled") {
                let enabled = enabled.as_bool().ok_or("会话维护开关无效")?;
                if enabled && !next.enabled { next.enabled_at_ms = now_ms(); }
                next.enabled = enabled;
            }
            if let Some(flow) = params.get("flow") { configure_flow(&mut next, flow, now_ms())?; }
            if let Some(interval) = params.get("interval_minutes") {
                if interval.as_u64() != Some(10) { return Err("登录预热在下单空闲10分钟后执行".into()); }
            }
            save(&service.root, &next)?;
            *service.settings.lock().unwrap() = next;
            *service.settings_error.lock().unwrap() = None;
            activity::append(&service.root, "session_maintenance_configured", &json!({
                "category":"authentication","action":"session_maintain",
                "message":"空闲会话维护设置已保存。","status":"configured"}));
        },
        "run" => {
            service.refresh(&engine.snapshot());
            service.run_account(&engine, &host, params["account_id"].as_str().ok_or("请选择账号")?)?;
        },
        _ => return Err("不支持的会话维护操作".into()),
    }
    Ok(service.status(&engine))
}

pub fn stop(root: &Path) {
    if let Some(service) = services().lock().unwrap().get(root) { let _ = service.stopping.send(true); }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_engine(root: &Path) -> (Arc<Engine>, String) {
        let account = uuid::Uuid::new_v4().to_string();
        fs::write(root.join("accounts.json"), serde_json::to_vec(&json!([{
            "id":account,"name":"Fixture","email":"fixture@example.test","pending":false
        }])).unwrap()).unwrap();
        (Engine::new(root.into()).unwrap(), account)
    }

    fn fixture_service(root: &Path, started_at_ms: u64) -> Maintenance {
        let (settings, error) = load(root);
        Maintenance { root: root.into(), started_at_ms, settings: Mutex::new(settings),
            settings_error: Mutex::new(error), stopping: watch::channel(false).0 }
    }

    fn fixture_task(account: &str, action: &str, status: &str, updated_at: u64) -> crate::engine::Task {
        serde_json::from_value(json!({"id":uuid::Uuid::new_v4(),"account_id":account,
            "action":action,"status":status,"stage":"fixture","created_at":updated_at,
            "updated_at":updated_at,"execution_started_at_ms":updated_at*1000,
            "prompt":null,"result":null,"order_submitted":false,"logs":[]})).unwrap()
    }

    #[test]
    fn flow_changes_delay_next_preheat_but_freeze_the_active_run_and_preserve_blocks() {
        let mut settings = Settings::default();
        settings.last_runs.push(Run { flow: "login".into(), account_id: "fixture".into(), preparation_stage: None, order_ready: false,
            status: "running".into(), last_checked_at_ms: 10, last_warm_at_ms: 20,
            next_check_at_ms: 30, message: "fixture".into(), requires_login: true,
            cooldown_until_ms: 3_600_000, task_id: Some("active-fixture".into()) });
        configure_flow(&mut settings, &json!("pickup_prepare"), 1000).unwrap();
        assert_eq!(settings.flow, "pickup_prepare");
        assert_eq!(settings.enabled_at_ms, 1000);
        let run = &settings.last_runs[0];
        assert_eq!(run.flow, "login");
        assert_eq!(run.task_id.as_deref(), Some("active-fixture"));
        assert_eq!(run.status, "running");
        assert!(run.requires_login);
        assert_eq!(run.cooldown_until_ms, 3_600_000);
        configure_flow(&mut settings, &json!("pickup_prepare"), 2000).unwrap();
        assert_eq!(settings.enabled_at_ms, 1000);
        assert!(configure_flow(&mut settings, &json!("purchase"), 3000).is_err());
        assert_eq!(settings.flow, "pickup_prepare");
        let temporary = tempfile::tempdir().unwrap();
        save(temporary.path(), &settings).unwrap();
        let (loaded, error) = load(temporary.path());
        assert!(error.is_none());
        assert_eq!(loaded.flow, "pickup_prepare");
        assert_eq!(loaded.last_runs[0].flow, "login");
    }

    #[test]
    fn only_order_activity_and_preheat_completion_reset_the_idle_timer() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account) = fixture_engine(temporary.path());
        let service = fixture_service(temporary.path(), 1000);
        let mut snapshot = engine.snapshot();
        service.refresh(&snapshot);
        let mut run = service.settings.lock().unwrap().last_runs[0].clone();
        snapshot.tasks.push(fixture_task(&account, "bag", "succeeded", 1000));
        assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 601_000);
        for action in ["purchase", "checkout"] {
            snapshot.tasks = vec![fixture_task(&account, action, "failed", 200)];
            assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 800_000);
        }
        run.last_warm_at_ms = 400_000;
        assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 1_000_000);
        let settings = Settings { enabled_at_ms:500_000, ..Settings::default() };
        assert_eq!(next_check(&snapshot, &run, &settings, 1000), 1_100_000);
    }

    #[test]
    fn any_busy_task_or_unknown_order_prevents_idle_preheat() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account) = fixture_engine(temporary.path());
        let mut snapshot = engine.snapshot();
        for action in ["purchase", "checkout", "login", "me"] {
            for status in ["running", "waiting"] {
                snapshot.tasks = vec![fixture_task(&account, action, status, 200)];
                assert_eq!(blocked_reason(&snapshot, &account), Some("account_busy"));
            }
        }
        let mut task = fixture_task(&account, "purchase", "failed", 200);
        task.order_submitted = true;
        task.result = Some(json!({"status":"submission_unknown","order_submitted":true}));
        snapshot.tasks = vec![task];
        assert_eq!(blocked_reason(&snapshot, &account), Some("order_unresolved"));
        assert_eq!(blocked_reason(&snapshot, "another-account"), None);
        snapshot.tasks[0].result = Some(json!({"status":"pending_payment","order_submitted":true,
                                              "order_number":"FIXTURE-001"}));
        assert_eq!(blocked_reason(&snapshot, &account), None);
    }

    #[test]
    fn unstarted_schedule_and_queue_do_not_reset_or_block_preheat() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account) = fixture_engine(temporary.path());
        let service = fixture_service(temporary.path(), 1000);
        let mut snapshot = engine.snapshot();
        service.refresh(&snapshot);
        let run = service.settings.lock().unwrap().last_runs[0].clone();
        for status in ["scheduled", "queued", "cancelled"] {
            let mut task = fixture_task(&account, "purchase", status, 1000);
            task.execution_started_at_ms = None;
            snapshot.tasks = vec![task];
            assert_eq!(blocked_reason(&snapshot, &account), None);
            assert_eq!(last_order_activity(&snapshot, &account), 0);
            assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 601_000);
        }
    }

    #[test]
    fn legacy_read_only_settings_migrate_to_ten_minute_warm_login_without_erasing_blocks() {
        let temporary = tempfile::tempdir().unwrap();
        fs::create_dir_all(temporary.path().join("settings")).unwrap();
        fs::write(temporary.path().join("settings/session-maintenance.json"), serde_json::to_vec(&json!({
            "enabled":true,"interval_minutes":30,"last_runs":[
                {"account_id":"signin","status":"requires_login","last_checked_at_ms":100,
                 "next_check_at_ms":200,"message":"fixture","requires_login":true,"cooldown_until_ms":900},
                {"account_id":"blocked","status":"blocked","last_checked_at_ms":100,
                 "next_check_at_ms":200,"message":"fixture","requires_login":true,"cooldown_until_ms":900}]
        })).unwrap()).unwrap();
        let (settings, error) = load(temporary.path());
        assert!(error.is_none());
        assert_eq!(settings.mode, "idle_warm_login");
        assert_eq!(settings.interval_minutes, 10);
        assert!(!settings.last_runs[0].requires_login);
        assert_eq!(settings.last_runs[0].cooldown_until_ms, 900);
        assert!(settings.last_runs[1].requires_login);
    }

    #[test]
    fn idle_due_time_never_runs_on_startup_or_before_recent_account_activity() {
        let engine = Engine::new(tempfile::tempdir().unwrap().path().into()).unwrap();
        let mut snapshot = engine.snapshot();
        let run = Run { flow: default_flow(), account_id: "fixture-account".into(), preparation_stage: None, order_ready: false, status:"waiting".into(), last_checked_at_ms:0, last_warm_at_ms:0,
            next_check_at_ms:0,message:String::new(),requires_login:false,cooldown_until_ms:0,task_id:None };
        assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 601_000);
        let mut task: crate::engine::Task = serde_json::from_value(json!({"id":"fixture-task",
            "account_id":"fixture-account","action":"purchase","status":"succeeded","stage":"",
            "created_at":200,"updated_at":200,"execution_started_at_ms":200000,
            "prompt":null,"result":null,"order_submitted":false,"logs":[]})).unwrap();
        snapshot.tasks.push(task.clone());
        assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 800_000);
        task.account_id = "another-account".into();
        snapshot.tasks = vec![task];
        assert_eq!(next_check(&snapshot, &run, &Settings::default(), 1000), 601_000);
    }

    #[test]
    fn cooldown_and_configuration_survive_restart_without_resetting_bad_settings() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let settings = Settings { interval_minutes:10, enabled:false, ..Settings::default() };
        save(root, &settings).unwrap();
        save(root, &settings).unwrap(); // Replacing an existing file also works on Windows.
        assert_eq!(load(root).0.interval_minutes, 10);
        assert!(!load(root).0.enabled);
        fs::write(root.join("settings/session-maintenance.json"), b"not json").unwrap();
        assert!(!load(root).0.enabled);
        assert!(load(root).1.is_some());
        let (engine, _) = fixture_engine(root);
        let service = fixture_service(root, now_ms());
        service.refresh(&engine.snapshot());
        assert!(!service.settings.lock().unwrap().enabled);
        assert_eq!(fs::read(root.join("settings/session-maintenance.json")).unwrap(), b"not json");
    }

    #[test]
    fn failed_maintenance_keeps_cooldown_until_a_later_verified_login() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account) = fixture_engine(temporary.path());
        let service = fixture_service(temporary.path(), 1000);
        let mut snapshot = engine.snapshot();
        service.refresh(&snapshot);
        let maintenance_id = uuid::Uuid::new_v4().to_string();
        {
            let mut settings = service.settings.lock().unwrap();
            settings.last_runs[0].task_id = Some(maintenance_id.clone());
            settings.last_runs[0].status = "running".into();
        }
        let maintenance: crate::engine::Task = serde_json::from_value(json!({
            "id":maintenance_id,"account_id":account,"action":"session_maintain","status":"failed",
            "stage":"Fixture storefront blocked","created_at":199,"updated_at":201,"prompt":null,
            "order_submitted":false,"logs":[],"result":{"success":false,"status":"blocked",
                "checked_at_ms":200700,"requires_login":true,"cooldown_until_ms":3800700,
                "message":"Fixture storefront blocked"}})).unwrap();
        let mut login: crate::engine::Task = serde_json::from_value(json!({
            "id":uuid::Uuid::new_v4(),"account_id":account,"action":"login","status":"succeeded",
            "stage":"","created_at":190,"updated_at":200,"prompt":null,
            "order_submitted":false,"logs":[],"result":{"success":true,"logged_in":true}})).unwrap();
        snapshot.tasks = vec![maintenance.clone(), login.clone()];
        service.refresh(&snapshot);
        let blocked = service.settings.lock().unwrap().last_runs[0].clone();
        assert!(blocked.requires_login);
        assert_eq!(blocked.status, "blocked");
        assert_eq!(blocked.last_checked_at_ms, 200700);
        assert_eq!(blocked.next_check_at_ms, 3800700);
        login.created_at = 202;
        login.updated_at = 203;
        snapshot.tasks = vec![maintenance, login];
        service.refresh(&snapshot);
        let recovered = service.settings.lock().unwrap().last_runs[0].clone();
        assert!(!recovered.requires_login);
        assert_eq!(recovered.cooldown_until_ms, 0);
        assert_eq!(recovered.status, "waiting");
    }

    #[tokio::test]
    async fn pickup_preheat_freezes_saved_account_selection_and_keeps_partial_result_after_flow_change() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let (engine, account) = fixture_engine(root);
        engine.save_preset(&account, "MK2P4CH".into(), 2).unwrap();
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python = project.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" });
        let script = root.join("pickup-maintenance-fixture.py");
        fs::write(&script, "import json,sys,time\nr=json.loads(sys.stdin.readline())\nassert r['action']=='session_maintain'\nassert r['params']=={'maintenance_trigger':True,'warm_only':True,'preheat_flow':'pickup_prepare','sku':'MK2P4CH','quantity':2}\nresult={'success':True,'status':'waiting_stock','preparation_stage':'fulfillment','review_verified':False,'order_ready':False,'requires_login':False,'cooldown_until_ms':0,'checked_at_ms':int(time.time()*1000),'message':'Offline partial preparation; no order'}\nprint(json.dumps({'v':1,'account_id':r['account_id'],'task_id':r['task_id'],'type':'result','result':result}),flush=True)\n").unwrap();
        let host = TaskHost { worker: python, script: Some(script), on_update: Arc::new(|_|{}) };
        let service = fixture_service(root, now_ms());
        service.refresh(&engine.snapshot());
        service.settings.lock().unwrap().flow = "pickup_prepare".into();
        service.run_account(&engine, &host, &account).unwrap();
        configure_flow(&mut service.settings.lock().unwrap(), &json!("login"), now_ms()).unwrap();
        for _ in 0..400 {
            if engine.snapshot().tasks.iter().all(|task| !active(&task.status)) { break; }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let snapshot = engine.snapshot();
        assert_eq!(snapshot.tasks.len(), 1);
        assert_eq!(snapshot.tasks[0].status, "succeeded");
        assert!(!snapshot.tasks[0].order_submitted);
        service.refresh(&snapshot);
        let settings = service.settings.lock().unwrap();
        assert_eq!(settings.flow, "login");
        assert_eq!(settings.last_runs[0].flow, "pickup_prepare");
        assert_eq!(settings.last_runs[0].status, "waiting_stock");
        assert_eq!(settings.last_runs[0].preparation_stage.as_deref(), Some("fulfillment"));
        assert!(!settings.last_runs[0].order_ready);
        drop(settings);
        engine.shutdown().await;
    }

    #[tokio::test]
    async fn due_idle_account_launches_one_real_worker_and_does_not_repeat_until_the_interval() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let (engine, account) = fixture_engine(root);
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python = project.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" });
        assert!(python.is_file(), "prepare the worker environment before testing");
        let script = root.join("read-only-maintenance-fixture.py");
        fs::write(&script, "import json,sys,time\nr=json.loads(sys.stdin.readline())\nassert r['action']=='session_maintain'\nassert r['params']=={'maintenance_trigger':True,'warm_only':True,'preheat_flow':'login'}\nresult={'success':True,'status':'healthy','requires_login':False,'cooldown_until_ms':0,'checked_at_ms':int(time.time()*1000),'message':'Read-only offline fixture'}\nprint(json.dumps({'v':1,'account_id':r['account_id'],'task_id':r['task_id'],'type':'result','result':result}),flush=True)\n").unwrap();
        let host = TaskHost { worker: python, script: Some(script), on_update: Arc::new(|_|{}) };
        let current = now_ms();
        engine.start(host.clone(), account.clone(), "purchase".into(), json!({
            "sku":"MLH74FE","quantity":1,"fulfillment":"RETAIL","scheduled_at":current+60_000})).unwrap();
        let service = fixture_service(root, current - 11 * 60_000);
        service.tick_at(&engine, &host, current);
        service.tick_at(&engine, &host, current);
        assert_eq!(engine.snapshot().tasks.len(), 2);
        for _ in 0..400 {
            if engine.snapshot().tasks.iter().filter(|task| task.action=="session_maintain")
                .all(|task| !active(&task.status)) { break; }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        let snapshot = engine.snapshot();
        let warm = snapshot.tasks.iter().find(|task| task.action=="session_maintain").unwrap();
        assert_eq!(warm.status, "succeeded");
        assert_eq!(warm.account_id, account);
        assert!(!warm.order_submitted);
        service.refresh(&snapshot);
        let run = service.settings.lock().unwrap().last_runs[0].clone();
        assert_eq!(run.status, "healthy");
        assert_eq!(run.task_id.as_deref(), Some(warm.id.as_str()));
        assert!(run.next_check_at_ms >= current + 9 * 60_000);
        service.tick_at(&engine, &host, run.next_check_at_ms - 1);
        assert_eq!(engine.snapshot().tasks.len(), 2);
        engine.shutdown().await;
    }
}
