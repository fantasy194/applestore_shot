use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::Command,
    sync::{mpsc, watch, Semaphore},
};
use uuid::Uuid;

#[path = "purchase_queue.rs"]
mod purchase_queue;
#[path = "workspace_feed.rs"]
mod workspace_feed;
#[path = "purchase_progress.rs"]
mod purchase_progress;
pub use purchase_progress::PurchaseProgress;
#[path = "monitor_restart.rs"]
mod monitor_restart;

const ACTIONS: &[&str] = &[
    "enroll",
    "login",
    "me",
    "orders",
    "cancel_order",
    "bag",
    "clear_bag",
    "add",
    "purchase",
    "checkout",
    "catalog",
    "address_read",
    "address_save",
    "region",
    "import_auth",
    "forget_password",
    "qr",
    "session_maintain",
    "inventory_settings_read",
    "inventory_settings_save",
    "store_search",
    "inventory_check",
    "inventory_monitor",
];

fn inventory_action(action: &str) -> bool {
    matches!(action, "inventory_settings_read" | "inventory_settings_save" | "store_search" | "inventory_check" | "inventory_monitor")
}

fn pickup_concurrency(params: &Value) -> Result<u32, String> {
    params.get("concurrency").map_or(Ok(1), |value| {
        value.as_u64().filter(|value| (1..=10).contains(value)).map(|value| value as u32)
            .ok_or_else(|| "自提并发数必须为 1–10 的整数".into())
    })
}

fn purchase_retry_count(params: &Value) -> Result<u32, String> {
    params.get("retry_count").map_or(Ok(0), |value| {
        value.as_u64().filter(|value| *value <= 10).map(|value| value as u32)
            .ok_or_else(|| "下单失败重试次数必须为 0–10 的整数".into())
    })
}

fn normalize_purchase_params(action: &str, params: &mut Value) -> Result<(), String> {
    if action != "purchase" { return Ok(()) }
    let retry_count = purchase_retry_count(params)?;
    if params["prepare_only"] == true && retry_count > 0 {
        return Err("提前准备仅执行一次，请将下单失败重试次数设为 0".into());
    }
    params["retry_count"] = json!(retry_count);
    if params["fulfillment"] == "RETAIL" {
        params["concurrency"] = json!(pickup_concurrency(params)?);
    }
    Ok(())
}
// A local workspace scope, never registered as an Apple account.
const LOCAL_ID: &str = "00000000-0000-0000-0000-000000000000";
fn scope_dir(root: &Path, id: &str) -> PathBuf {
    if id == LOCAL_ID {
        root.join("settings")
    } else {
        root.join("accounts").join(id)
    }
}
fn local_defaults() -> Account {
    Account {
        id: LOCAL_ID.into(),
        name: "通用预设".into(),
        email: String::new(),
        pending: false,
        preset_sku: default_sku(),
        preset_quantity: default_quantity(),
    }
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct PurchaseSchedule {
    pub at_ms: u64,
    pub sku: String,
    pub quantity: u32,
    pub fulfillment: String,
    #[serde(default = "default_concurrency")]
    pub concurrency: u32,
    #[serde(default)]
    pub retry_count: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub triggered_at_ms: Option<u64>,
}
fn default_concurrency() -> u32 { 1 }
fn purchase_schedule(action: &str, params: &Value, current_ms: u64) -> Result<Option<PurchaseSchedule>, String> {
    let Some(target) = params.get("scheduled_at") else { return Ok(None) };
    if action != "purchase" || params["prepare_only"] == true {
        return Err("定时仅支持正式购买商品".into());
    }
    let at_ms = target.as_u64().filter(|at| *at > current_ms.saturating_add(1000) && *at <= 253_402_300_799_999)
        .ok_or("请设置至少一秒后的有效定时时间")?;
    let sku = params["sku"].as_str().unwrap_or("").trim().to_ascii_uppercase();
    let sku = sku.strip_suffix("/A").unwrap_or(&sku).to_owned();
    if sku.is_empty() || sku.len() > 30 || !sku.chars().all(|c| c.is_ascii_alphanumeric()) {
        return Err("定时商品 SKU 无效".into());
    }
    let quantity = params["quantity"].as_u64().filter(|q| *q > 0 && *q <= u32::MAX as u64)
        .ok_or("定时购买数量必须为正整数")? as u32;
    let fulfillment = match params.get("fulfillment") {
        None => "HOME", Some(value) => value.as_str().ok_or("定时收货方式无效")?
    };
    if !matches!(fulfillment, "HOME" | "RETAIL") { return Err("定时收货方式无效".into()) }
    let concurrency = pickup_concurrency(params)?;
    let retry_count = purchase_retry_count(params)?;
    Ok(Some(PurchaseSchedule { at_ms, sku, quantity, fulfillment: fulfillment.into(), concurrency, retry_count, triggered_at_ms: None }))
}
fn check_schedule_lateness(target: u64, current: u64) -> Result<(), String> {
    if current.saturating_sub(target) > 3000 {
        return Err("已错过定时时间超过 3 秒（可能因休眠或系统繁忙），未补发下单；请重新设置".into());
    }
    Ok(())
}
async fn wait_for_schedule(target: u64, cancel: &mut watch::Receiver<bool>) -> Result<(), String> {
    loop {
        if *cancel.borrow() { return Err("cancelled".into()) }
        let current = now_ms();
        if current >= target { return check_schedule_lateness(target, current) }
        let duration = std::time::Duration::from_millis((target - current).min(250));
        tokio::select! { biased;
            _ = cancel.changed() => return Err("cancelled".into()),
            _ = tokio::time::sleep(duration) => {}
        }
    }
}

pub fn terminal(status: &str) -> bool {
    matches!(status, "succeeded" | "failed" | "cancelled" | "interrupted")
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Account {
    pub id: String,
    pub name: String,
    pub email: String,
    #[serde(default)]
    pub pending: bool,
    #[serde(default = "default_sku")]
    pub preset_sku: String,
    #[serde(default = "default_quantity")]
    pub preset_quantity: u32,
}
fn default_sku() -> String {
    "MK2U4CH".into()
}
fn default_quantity() -> u32 {
    1
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: String,
    pub account_id: String,
    pub action: String,
    pub status: String,
    pub stage: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub purchase_progress: Option<PurchaseProgress>,
    pub created_at: u64,
    pub updated_at: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub execution_started_at_ms: Option<u64>,
    pub prompt: Option<Value>,
    pub result: Option<Value>,
    pub order_submitted: bool,
    pub logs: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schedule: Option<PurchaseSchedule>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub queue_job_ids: Vec<String>,
}
#[derive(Serialize)]
pub struct Snapshot {
    pub accounts: Vec<Account>,
    pub tasks: Vec<Task>,
    pub data_dir: String,
    pub defaults: Account,
}
/// Desktop IPC and local HTTP share the same task engine and isolated worker protocol.
#[derive(Clone)]
pub struct TaskHost {
    pub worker: PathBuf,
    pub script: Option<PathBuf>,
    pub on_update: Arc<dyn Fn(&Task) + Send + Sync>,
}
impl TaskHost {
    fn command(&self) -> Result<Command, String> {
        if !self.worker.is_file() {
            return Err("缺少业务进程，请重新构建完整版本".into());
        }
        let mut command = Command::new(&self.worker);
        if let Some(script) = &self.script {
            command.arg(script);
        }
        Ok(command)
    }
}

struct Control {
    cancel: watch::Sender<bool>,
    input: mpsc::Sender<Value>,
    pid: Option<u32>,
    browser_pid: Option<u32>,
}
#[derive(Clone, Default)]
struct SessionCredential {
    password: Option<String>,
    generation: u64,
    rejected: bool,
}

pub struct Engine {
    pub root: PathBuf,
    accounts: Mutex<Vec<Account>>,
    // Process-lifetime only; never part of snapshots, task files, or activity logs.
    session_passwords: Mutex<HashMap<String, SessionCredential>>,
    tasks: Mutex<HashMap<String, Task>>,
    workspace_feed: Mutex<workspace_feed::WorkspaceFeed>,
    controls: Mutex<HashMap<String, Control>>,
    locks: Mutex<HashMap<String, Arc<Semaphore>>>,
    defaults: Mutex<Account>,
    removing: Mutex<HashSet<String>>,
    monitor_dispatches: Mutex<HashMap<String, MonitorEpisode>>,
    monitor_restart: Mutex<monitor_restart::MonitorRestart>,
    purchase_queue: Mutex<purchase_queue::QueueState>,
    queue_runtime: Mutex<purchase_queue::QueueRuntime>,
    queue_stopping: std::sync::atomic::AtomicBool,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct MonitorEpisode {
    attempted: bool,
    #[serde(default)]
    task_id: Option<String>,
}

fn remove_private_payment_links(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            for key in ["qr_data", "payment_url", "qr_url", "qr_image"] { fields.remove(key); }
            for child in fields.values_mut() { remove_private_payment_links(child); }
        }
        Value::Array(rows) => { for child in rows { remove_private_payment_links(child); } }
        _ => {}
    }
}

fn resolved_official_status(status: &str) -> bool {
    matches!(status, "PAYMENT_EXPIRED_STORED_ORDER" | "CANCELED" | "CANCELLED" | "PAID" | "PROCESSING"
        | "PREPARING_TO_SHIP" | "SHIPPED" | "DELIVERED" | "READY_FOR_PICKUP" | "PICKED_UP" | "PICKUP_COMPLETE")
        || status.strip_prefix("CANCELED_").or_else(|| status.strip_prefix("CANCELLED_")).is_some_and(|tail|
            !tail.is_empty() && tail.chars().all(|c| c.is_ascii_uppercase() || c == '_'))
}

fn official_order_resolved(tasks: &HashMap<String, Task>, account_id: &str, number: &str) -> bool {
    let mut latest = None;
    for task in tasks.values().filter(|task| task.account_id == account_id && task.status == "succeeded") {
        let Some(result) = &task.result else { continue };
        if result["success"] != true { continue }
        let evidence = if task.action == "cancel_order" && result["order_number"] == number {
            matches!(result["status"].as_str(), Some("order_cancelled" | "order_expired")).then_some(true)
        } else if task.action == "orders" {
            result["orders"].as_array().and_then(|orders| orders.iter().find(|order| order["order_number"] == number))
                .filter(|order| order["verified_by"] == "official_detail")
                .and_then(|order| order["items"].as_array())
                .filter(|items| !items.is_empty())
                .map(|items| items.iter().all(|item| item["status"].as_str().is_some_and(resolved_official_status)))
        } else { None };
        if let Some(resolved) = evidence {
            let key = (task.updated_at, task.created_at);
            if latest.as_ref().is_none_or(|(previous, _)| key >= *previous) { latest = Some((key, resolved)); }
        }
    }
    latest.is_some_and(|(_, resolved)| resolved)
}

pub(crate) fn task_has_unknown_submission(task: &Task) -> bool {
    task.order_submitted && task.result.is_none()
        || task.result.as_ref().is_some_and(purchase_queue::contains_unknown)
}

pub(crate) fn unresolved_submission(task: &Task, tasks: &HashMap<String, Task>) -> bool {
    if !task.order_submitted { return false }
    let Some(result) = &task.result else { return true };
    let mut numbers = HashSet::new();
    if let Some(number) = result["order_number"].as_str().filter(|number| !number.is_empty()) { numbers.insert(number); }
    if let Some(orders) = result["pending_orders"].as_array() {
        for order in orders {
            if let Some(number) = order["order_number"].as_str().filter(|number| !number.is_empty()) { numbers.insert(number); }
        }
    }
    if result["attempts"].as_array().is_some_and(|attempts| attempts.iter().any(|attempt|
        attempt["status"] == "submission_unknown" || attempt["order_submitted"] == true
        && attempt.get("result").is_none_or(|value| value["order_number"].as_str().is_none_or(str::is_empty)))) { return true }
    if numbers.is_empty() {
        return !matches!(result["status"].as_str(),Some("order_paid" | "order_cancelled" | "order_expired"));
    }
    numbers.into_iter().any(|number| !official_order_resolved(tasks, &task.account_id, number))
}

// Keep task submission and removal exclusive, including while the vault worker runs.
struct AccountRemoval<'a> {
    engine: &'a Engine,
    id: String,
}
impl Drop for AccountRemoval<'_> {
    fn drop(&mut self) {
        self.engine.removing.lock().unwrap().remove(&self.id);
    }
}

pub fn private_json(path: &Path, data: &impl Serialize) -> Result<(), String> {
    let parent = path.parent().ok_or("无效路径")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let mut tmp = tempfile::NamedTempFile::new_in(parent).map_err(|e| e.to_string())?;
    serde_json::to_writer_pretty(&mut tmp, data).map_err(|e| e.to_string())?;
    tmp.flush().map_err(|e| e.to_string())?;
    tmp.persist(path).map_err(|e| e.to_string())?;
    Ok(())
}

impl Engine {
    fn append_event(&self, event: &str, context: &Value) {
        crate::activity::append(&self.root, event, context);
    }
    pub fn new(root: PathBuf) -> Result<Arc<Self>, String> {
        fs::create_dir_all(&root).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .map_err(|e| e.to_string())?;
        }
        let path = root.join("accounts.json");
        let accounts: Vec<Account> = if path.exists() {
            serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
                .map_err(|e| format!("账号目录格式损坏：{e}"))?
        } else {
            vec![]
        };
        let defaults_path = root.join("settings/purchase.json");
        let mut defaults: Account = if defaults_path.exists() {
            serde_json::from_slice(&fs::read(defaults_path).map_err(|e| e.to_string())?)
                .map_err(|e| format!("通用购买预设格式损坏：{e}"))?
        } else {
            local_defaults()
        };
        defaults.id = LOCAL_ID.into();
        let mut tasks = HashMap::new();
        let mut latest_legacy_monitor = None;
        for account in accounts.iter().chain(std::iter::once(&local_defaults())) {
            Uuid::parse_str(&account.id).map_err(|_| "账号标识无效")?;
            if let Ok(entries) = fs::read_dir(scope_dir(&root, &account.id).join("tasks"))
            {
                for entry in entries.flatten() {
                    if let Ok(raw) = fs::read(entry.path().join("task.json")) {
                        if let Ok(mut task) = serde_json::from_slice::<Task>(&raw) {
                            if task.account_id != account.id || Uuid::parse_str(&task.id).is_err() {
                                continue;
                            }
                            if task.account_id == LOCAL_ID && task.action == "inventory_monitor" {
                                if latest_legacy_monitor.as_ref().is_none_or(|(previous, _)| task.created_at > *previous) {
                                    latest_legacy_monitor = Some((task.created_at, !terminal(&task.status)));
                                } else if let Some((created_at, enabled)) = latest_legacy_monitor.as_mut() {
                                    // Historical timestamps have second precision. UUIDs do
                                    // not establish order; a same-second stop must win.
                                    if task.created_at == *created_at { *enabled &= !terminal(&task.status); }
                                }
                            }
                            if !terminal(&task.status) {
                                task.status = "interrupted".into();
                                task.prompt = None;
                                task.stage = if task.schedule.is_some() && !task.order_submitted {
                                    "本地服务已重启，定时任务已停止；请重新设定，未补发下单"
                                } else if task.order_submitted {
                                    "应用上次退出时正在提交订单，请先查询订单"
                                } else {
                                    "应用上次退出时任务中断，可重新发起操作"
                                }
                                .into();
                            }
                            tasks.insert(task.id.clone(), task);
                        }
                    }
                }
            }
        }
        let dispatch_path = root.join("settings/monitor_dispatches.json");
        let monitor_dispatches = if dispatch_path.exists() {
            serde_json::from_slice(&fs::read(dispatch_path).map_err(|error| error.to_string())?)
                .map_err(|_| "库存自动下单记录损坏，请先核实已有订单，未恢复自动下单")?
        } else { HashMap::new() };
        let purchase_queue = purchase_queue::QueueState::load(&root, &monitor_dispatches)?;
        let monitor_restart = monitor_restart::MonitorRestart::load(&root,
            latest_legacy_monitor.is_some_and(|(_, enabled)| enabled))?;
        Ok(Arc::new(Self {
            root,
            accounts: Mutex::new(accounts),
            session_passwords: Mutex::new(HashMap::new()),
            tasks: Mutex::new(tasks),
            workspace_feed: Mutex::new(workspace_feed::WorkspaceFeed::default()),
            controls: Mutex::new(HashMap::new()),
            locks: Mutex::new(HashMap::new()),
            defaults: Mutex::new(defaults),
            removing: Mutex::new(HashSet::new()),
            monitor_dispatches: Mutex::new(monitor_dispatches),
            monitor_restart: Mutex::new(monitor_restart),
            purchase_queue: Mutex::new(purchase_queue),
            queue_runtime: Mutex::new(purchase_queue::QueueRuntime::default()),
            queue_stopping: std::sync::atomic::AtomicBool::new(false),
        }))
    }
    pub fn snapshot(&self) -> Snapshot {
        let mut tasks: Vec<_> = self.tasks.lock().unwrap().values().cloned().collect();
        tasks.sort_by_key(|t| std::cmp::Reverse(t.created_at));
        Snapshot {
            accounts: self.accounts.lock().unwrap().clone(),
            tasks,
            data_dir: self.root.to_string_lossy().into(),
            defaults: self.defaults.lock().unwrap().clone(),
        }
    }
    pub fn add_account(&self, email: String) -> Result<Account, String> {
        let email = email.trim().to_lowercase();
        if email.is_empty() || email.len() > 254 || email.chars().any(char::is_whitespace) {
            return Err("请输入有效的 Apple 账号".into());
        }
        let mut accounts = self.accounts.lock().unwrap();
        if accounts.iter().any(|a| a.email == email) {
            return Err("这个账号已添加，请从左侧选择".into());
        }
        let defaults = self.defaults.lock().unwrap().clone();
        let account = Account {
            id: Uuid::new_v4().to_string(),
            name: email.clone(),
            email,
            pending: true,
            preset_sku: defaults.preset_sku,
            preset_quantity: defaults.preset_quantity,
        };
        let address = self.root.join("settings/checkout.json");
        if address.exists() {
            let config: Value =
                serde_json::from_slice(&fs::read(address).map_err(|e| e.to_string())?)
                    .map_err(|e| format!("通用地址格式损坏：{e}"))?;
            private_json(
                &scope_dir(&self.root, &account.id).join("checkout.json"),
                &config,
            )?;
        }
        let mut next = accounts.clone();
        next.push(account.clone());
        private_json(&self.root.join("accounts.json"), &next)?;
        *accounts = next;
        Ok(account)
    }
    fn reserve_removal(&self, id: &str) -> Result<AccountRemoval<'_>, String> {
        Uuid::parse_str(id).map_err(|_| "账号标识无效")?;
        let mut removing = self.removing.lock().unwrap();
        if !self.accounts.lock().unwrap().iter().any(|a| a.id == id) {
            return Err("账号不存在".into());
        }
        if removing.contains(id) {
            return Err("这个账号正在移除，请稍候".into());
        }
        let tasks = self.tasks.lock().unwrap();
        let controls = self.controls.lock().unwrap();
        if tasks.values().any(|t| {
            t.account_id == id && (!terminal(&t.status) || controls.contains_key(&t.id))
        }) {
            return Err("该账号仍有任务执行或收尾中，请完成或取消后再移除".into());
        }
        removing.insert(id.into());
        Ok(AccountRemoval { engine: self, id: id.into() })
    }
    pub async fn remove_account(&self, host: &TaskHost, id: &str) -> Result<(), String> {
        let _reservation = self.reserve_removal(id)?;
        // Use the same OS vault as login; no Apple requests or credentials on stdout.
        let mut command = host.command()?;
        command.arg("--forget-account").arg(id)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(windows)] command.creation_flags(0x08000000);
        let status = tokio::time::timeout(std::time::Duration::from_secs(30), command.status())
            .await.map_err(|_| "清理保存密码超时，账号尚未移除，请重试")?
            .map_err(|_| "无法启动凭据清理，账号尚未移除，请重试")?;
        if !status.success() {
            return Err("无法清除系统保存的密码，账号尚未移除，请检查凭据保险库后重试".into());
        }
        self.finish_removal(id)
    }
    fn finish_removal(&self, id: &str) -> Result<(), String> {
        let mut accounts = self.accounts.lock().unwrap();
        let next: Vec<_> = accounts.iter().filter(|a| a.id != id).cloned().collect();
        let directory = scope_dir(&self.root, id);
        if let Err(error) = fs::remove_dir_all(&directory) {
            if error.kind() != std::io::ErrorKind::NotFound {
                return Err("账号的本地文件未完全清理，请关闭该账号的浏览器后重试".into());
            }
        }
        private_json(&self.root.join("accounts.json"), &next)?;
        *accounts = next;
        self.tasks.lock().unwrap().retain(|_, t| t.account_id != id);
        self.locks.lock().unwrap().remove(id);
        self.session_passwords.lock().unwrap().remove(id);
        Ok(())
    }
    fn sync_account_identity(
        &self,
        id: &str,
        result: &Value,
        enrolling: bool,
    ) -> Result<(), String> {
        if result["success"] != true
            || result["logged_in"] != true
            || result["scope"] != "account"
            || result["apple_id"]
                .as_str()
                .is_none_or(|v| v.trim().is_empty())
        {
            return Err("未确认商城登录与账号信息".into());
        }
        let mut accounts = self.accounts.lock().unwrap();
        let mut next = accounts.clone();
        let account = next.iter_mut().find(|a| a.id == id).ok_or("账号不存在")?;
        if enrolling {
            account.pending = false;
        }
        account.name = result["name"]
            .as_str()
            .map(str::trim)
            .filter(|name| !name.is_empty())
            .unwrap_or(&account.email)
            .to_owned();
        private_json(&self.root.join("accounts.json"), &next)?;
        *accounts = next;
        Ok(())
    }
    pub fn save_preset(
        &self,
        account_id: &str,
        sku: String,
        quantity: u32,
    ) -> Result<Account, String> {
        let sku = sku.trim().to_ascii_uppercase();
        if sku.is_empty()
            || sku.len() > 30
            || !sku.chars().all(|c| c.is_ascii_alphanumeric() || c == '/')
            || !(1..=999).contains(&quantity)
        {
            return Err("请填写有效的 SKU 和 1–999 的数量".into());
        }
        if account_id == LOCAL_ID {
            let mut defaults = self.defaults.lock().unwrap();
            let mut next = defaults.clone();
            next.preset_sku = sku;
            next.preset_quantity = quantity;
            private_json(&self.root.join("settings/purchase.json"), &next)?;
            *defaults = next.clone();
            return Ok(next);
        }
        let mut accounts = self.accounts.lock().unwrap();
        let mut next = accounts.clone();
        let account = next
            .iter_mut()
            .find(|a| a.id == account_id)
            .ok_or("账号不存在")?;
        account.preset_sku = sku;
        account.preset_quantity = quantity;
        let result = account.clone();
        private_json(&self.root.join("accounts.json"), &next)?;
        *accounts = next;
        Ok(result)
    }
    fn remove_invalid_account(&self, id: &str, current_task: &str) -> Result<bool, String> {
        let mut accounts = self.accounts.lock().unwrap();
        // Incomplete enrollment is still a draft, not an expired saved account.
        if !accounts.iter().any(|a| a.id == id && !a.pending) {
            return Ok(false);
        }
        let next: Vec<_> = accounts.iter().filter(|a| a.id != id).cloned().collect();
        private_json(&self.root.join("accounts.json"), &next)?;
        *accounts = next;
        drop(accounts);
        let tasks = self.tasks.lock().unwrap();
        let controls = self.controls.lock().unwrap();
        for task in tasks
            .values()
            .filter(|t| t.account_id == id && t.id != current_task && !terminal(&t.status))
        {
            if let Some(control) = controls.get(&task.id) {
                let _ = control.cancel.send(true);
            }
        }
        Ok(true)
    }
    // Share the queue acknowledgement gate with aggregate presentation, so a
    // rejected old target event cannot change the banner after queue validation.
    fn apply_worker_event(&self, app: &TaskHost, account: &str, id: &str, event: &Value, queue_stream: bool) {
        if event.get("account_id").is_some_and(|owner|owner!=account) || event.get("task_id").is_some_and(|owner|owner!=id) {return}
        let kind=event["type"].as_str().unwrap_or("");
        if queue_stream && matches!(kind,"queue_ready"|"queue_started"|"queue_progress"|"queue_finished"|"queue_idle"|"stage")
            && !self.queue_worker_event(account,id,event) {return}
        self.update(app,id,|task|apply_event(task,event));
    }
    fn update(&self, app: &TaskHost, id: &str, change: impl FnOnce(&mut Task)) {
        let task = {
            let mut tasks = self.tasks.lock().unwrap();
            let Some(task) = tasks.get_mut(id) else {
                return;
            };
            change(task);
            task.updated_at = now();
            task.clone()
        };
        let mut disk = task.clone();
        disk.prompt = None; // Password/code replies are never retained. Prompts become stale on restart.
        if let Some(result) = disk.result.as_mut() { remove_private_payment_links(result); }
        let path = scope_dir(&self.root, &task.account_id)
            .join("tasks")
            .join(id)
            .join("task.json");
        if let Err(error) = private_json(&path, &disk) {
            eprintln!("无法保存任务状态：{error}");
        }
        (app.on_update)(&task);
    }

    fn inventory_dispatch(self: &Arc<Self>, app: &TaskHost, result: &Value) -> Vec<Value> {
        self.queue_inventory(app, result)
    }
    pub fn start(
        self: &Arc<Self>,
        app: TaskHost,
        account_id: String,
        action: String,
        params: Value,
    ) -> Result<String, String> {
        if !ACTIONS.contains(&action.as_str()) || !params.is_object() {
            return Err("不支持的操作或参数".into());
        }
        if params.to_string().len() > 900_000 {
            return Err("任务参数过大".into());
        }
        let mut params = params;
        // Accept prefilled credentials through the same account cache, but remove
        // secrets before any business parameters can reach diagnostics/persistence.
        let supplied_password = params.as_object_mut().unwrap().remove("password");
        if supplied_password.as_ref().is_some_and(|value| value.as_str().is_none_or(|s| s.is_empty() || s.len()>4096)) {
            return Err("密码必须为非空字符串且不超过 4096 字节".into());
        }
        let remember_password = params.as_object_mut().unwrap().remove("remember").is_some_and(|value| value==true);
        let schedule = purchase_schedule(&action, &params, now_ms())?;
        normalize_purchase_params(&action, &mut params)?;
        if let Some(s) = &schedule {
            // Freeze purchase fields at scheduling time; the worker reads only saved contact details later.
            params["sku"] = json!(s.sku);
            params["quantity"] = json!(s.quantity);
            params["fulfillment"] = json!(s.fulfillment);
            params["retry_count"] = json!(s.retry_count);
            params.as_object_mut().unwrap().remove("scheduled_at");
        }
        let removing = self.removing.lock().unwrap();
        if removing.contains(&account_id) {
            return Err("这个账号正在移除，无法开始新任务".into());
        }
        let account = if account_id == LOCAL_ID {
            if !matches!(action.as_str(), "catalog" | "address_read" | "address_save") && !inventory_action(&action) {
                return Err("此操作需要选择并登录 Apple 账号".into());
            }
            local_defaults()
        } else {
            self.accounts
                .lock()
                .unwrap()
                .iter()
                .find(|a| a.id == account_id)
                .cloned()
                .ok_or("账号不存在")?
        };
        if account.pending && !matches!(action.as_str(), "enroll" | "address_read" | "address_save")
        {
            return Err("请先完成账号添加与登录验证".into());
        }
        let mut tasks = self.tasks.lock().unwrap();
        if params["queue_stream"]==true && (
            tasks.values().any(|task|task.account_id==account_id && (!terminal(&task.status)
                || task.order_submitted && task.result.as_ref().is_none_or(purchase_queue::contains_unknown)
                || task.queue_job_ids.is_empty() && unresolved_submission(task,&tasks)))
            || self.locks.lock().unwrap().get(&account_id).is_some_and(|lock|lock.available_permits()==0)) {
            return Err("账号有进行中的任务或待核实订单，目标保留在下单队列中".into());
        }
        if action == "session_maintain" && (
            tasks.values().any(|task|task.account_id==account_id && (matches!(task.status.as_str(), "running" | "waiting")
                || task.action=="session_maintain" && !terminal(&task.status)
                || task_has_unknown_submission(task)))
            || self.locks.lock().unwrap().get(&account_id).is_some_and(|lock|lock.available_permits()==0)) {
            return Err("账号有进行中的任务，本轮登录维护已跳过，不排队占用账号".into());
        }
        let purchase_priority=matches!(action.as_str(), "purchase" | "checkout") && params["prepare_only"]!=true && schedule.is_none();
        if purchase_priority {
            let controls=self.controls.lock().unwrap();
            for maintenance in tasks.values().filter(|task|task.account_id==account_id && task.action=="session_maintain" && !terminal(&task.status)) {
                if let Some(control)=controls.get(&maintenance.id) {let _=control.cancel.send(true);}
            }
        }
        if params["inventory_trigger"] == true && (
            tasks.values().any(|task| task.account_id == account_id && (!terminal(&task.status) || unresolved_submission(task, &tasks)))
            || self.locks.lock().unwrap().get(&account_id).is_some_and(|lock| lock.available_permits() == 0)) {
            return Err("库存已变化但账号正忙或已有待核实订单，未排队自动下单".into());
        }
        if action == "inventory_monitor" {
            if let Some(existing) = tasks.values().find(|task| task.action == action && !terminal(&task.status)) {
                self.monitor_restart.lock().unwrap().set_enabled(&self.root, true)?;
                return Ok(existing.id.clone());
            }
        }
        if (action == "checkout" || action == "purchase" && params["prepare_only"] != true) && tasks.values().any(|t|
            t.account_id == account_id && t.schedule.is_some() && !terminal(&t.status)) {
            return Err("该账号已有定时抢购，请先取消原定时任务，避免重复下单".into());
        }
        // Read/check requests can be triggered again by page navigation.
        // Reuse the outstanding task instead of building a duplicate queue.
        if matches!(action.as_str(), "me" | "address_read") {
            if let Some(existing) = tasks.values().find(|t| {
                t.account_id == account_id && t.action == action && !terminal(&t.status)
            }) {
                return Ok(existing.id.clone());
            }
        }
        if tasks.values().any(|t| t.account_id == account_id && t.status == "waiting" && !(purchase_priority && t.action=="session_maintain")) {
            return Err("该账号正在等待输入或订单确认，请从顶部进行中的任务继续操作，或先取消该任务".into());
        }
        if tasks
            .values()
            .filter(|t| t.account_id == account_id && !terminal(&t.status))
            .count()
            >= 10
        {
            return Err("该账号等待中的任务过多，请先完成或取消".into());
        }
        if action == "inventory_monitor" {
            self.monitor_restart.lock().unwrap().set_enabled(&self.root, true)?;
        }
        let id = Uuid::new_v4().to_string();
        tasks.insert(
            id.clone(),
            Task {
                id: id.clone(),
                account_id: account_id.clone(),
                action: action.clone(),
                status: if schedule.is_some() { "scheduled" } else { "queued" }.into(),
                stage: if schedule.is_some() { "等待定时触发；电脑须保持唤醒，本地服务须保持运行" } else { "等待执行" }.into(),
                created_at: now(),
                updated_at: now(),
                execution_started_at_ms: None,
                purchase_progress: None,
                prompt: None,
                result: None,
                order_submitted: false,
                logs: vec![],
                schedule,
                queue_job_ids: params["queue_targets"].as_array().map(|targets| targets.iter()
                    .filter_map(|target| target["queue_job_id"].as_str().map(str::to_owned)).collect()).unwrap_or_default(),
            },
        );
        drop(tasks);
        let (cancel, receiver) = watch::channel(false);
        let (input, inputs) = mpsc::channel(16);
        self.controls.lock().unwrap().insert(
            id.clone(),
            Control {
                cancel,
                input,
                pid: None,
                browser_pid: None,
            },
        );
        let lock = self
            .locks
            .lock()
            .unwrap()
            .entry(account_id.clone())
            .or_insert_with(|| Arc::new(Semaphore::new(1)))
            .clone();
        if action == "forget_password" || supplied_password.is_some() {
            let mut credentials = self.session_passwords.lock().unwrap();
            let state = credentials.entry(account_id.clone()).or_default();
            state.generation += 1;
            state.password = if action == "forget_password" { None } else {
                supplied_password.and_then(|value| value.as_str().map(str::to_owned))
            };
            state.rejected = false;
        }
        if remember_password { params["_remember_password"] = json!(true); }
        self.update(&app, &id, |_| {});
        let engine = self.clone();
        let task_id = id.clone();
        drop(removing);
        tokio::spawn(async move {
            engine
                .execute(
                    app, account, task_id, action, params, lock, receiver, inputs,
                )
                .await;
        });
        Ok(id)
    }
    pub fn reply(
        &self,
        app: &TaskHost,
        account_id: &str,
        task_id: &str,
        request_id: &str,
        value: Value,
    ) -> Result<(), String> {
        let mut tasks = self.tasks.lock().unwrap();
        let task = tasks.get_mut(task_id).ok_or("任务不存在")?;
        validate_reply(task, account_id, request_id, &value)?;
        let password = (task.prompt.as_ref().is_some_and(|prompt| prompt["kind"]=="password"))
            .then(|| value["password"].as_str().map(str::to_owned)).flatten();
        let mut credentials = self.session_passwords.lock().unwrap();
        let generation = credentials.get(account_id).map_or(1, |state| state.generation + 1);
        let mut message = json!({"type":"reply", "account_id": account_id, "task_id":task_id,"request_id":request_id,"value":value});
        if password.is_some() { message["_credential_generation"] = json!(generation); }
        self.controls
            .lock()
            .unwrap()
            .get(task_id)
            .ok_or("任务已结束")?
            .input
            .try_send(message)
            .map_err(|_| "无法发送输入，任务可能已退出")?;
        if let Some(password) = password {
            credentials.insert(account_id.into(), SessionCredential {password:Some(password),generation,rejected:false});
        }
        drop(credentials);
        task.prompt = None;
        task.status = "running".into();
        task.stage = "继续执行".into();
        drop(tasks);
        // The worker can publish its next prompt immediately. Never clear it after releasing the lock.
        self.update(app, task_id, |_| {});
        Ok(())
    }
    pub fn cancel(&self, account_id: &str, task_id: &str) -> Result<(), String> {
        let tasks = self.tasks.lock().unwrap();
        if tasks
            .get(task_id)
            .is_none_or(|t| t.account_id != account_id)
        {
            return Err("账号与任务不匹配".into());
        }
        if tasks.get(task_id).is_some_and(|task| task.action == "inventory_monitor")
            && !tasks.values().any(|task| task.id != task_id && task.action == "inventory_monitor" && !terminal(&task.status)) {
            self.monitor_restart.lock().unwrap().set_enabled(&self.root, false)?;
        }
        if let Some(control) = self.controls.lock().unwrap().get(task_id) {
            let _ = control.cancel.send(true);
        }
        Ok(())
    }
    async fn execute(
        self: Arc<Self>,
        app: TaskHost,
        account: Account,
        id: String,
        action: String,
        params: Value,
        lock: Arc<Semaphore>,
        mut cancel: watch::Receiver<bool>,
        mut inputs: mpsc::Receiver<Value>,
    ) {
        // Only serialize work for this account; other accounts run independently.
        let result: Result<(), String> = async {
            let schedule = self.tasks.lock().unwrap().get(&id).and_then(|t| t.schedule.clone());
            if let Some(s) = &schedule { wait_for_schedule(s.at_ms, &mut cancel).await?; }
            let _account = if inventory_action(&action) {
                // Anonymous monitoring has its own transport and must not block local settings/discovery.
                if *cancel.borrow() { return Err("cancelled".into()) }
                None
            } else if action == "session_maintain" {
                // A snapshot may become stale before this future is polled.
                // Background preheat must skip instead of waiting behind an order.
                if *cancel.borrow() { return Err("cancelled".into()) }
                Some(lock.try_acquire_owned().map_err(|_| "账号正在执行任务，登录预热已跳过；未排队占用账号")?)
            } else if let Some(s) = &schedule {
                {
                    let tasks = self.tasks.lock().unwrap();
                    let controls = self.controls.lock().unwrap();
                    for maintenance in tasks.values().filter(|t| t.account_id == account.id
                        && t.id != id && t.action == "session_maintain" && !terminal(&t.status)) {
                        if let Some(control) = controls.get(&maintenance.id) { let _ = control.cancel.send(true); }
                    }
                }
                if self.tasks.lock().unwrap().values().any(|t| t.account_id == account.id && t.id != id
                    && t.action != "session_maintain" && !terminal(&t.status)) {
                    return Err("定时时间已到，但该账号仍有未完成任务；未延迟排队下单，请处理后重新设定".into());
                }
                // Only the preempted background preheat may need a brief shutdown.
                // Preserve the existing three-second scheduling lateness budget.
                let remaining = s.at_ms.saturating_add(3000).saturating_sub(now_ms());
                let permit = tokio::time::timeout(std::time::Duration::from_millis(remaining),
                    acquire_account(lock, &mut cancel)).await
                    .map_err(|_| "定时时间已到，但预热窗口未及时释放；未延迟下单")??;
                if *cancel.borrow() { return Err("cancelled".into()) }
                check_schedule_lateness(s.at_ms, now_ms())?;
                self.update(&app, &id, |t| {
                    if let Some(s) = t.schedule.as_mut() { s.triggered_at_ms = Some(now_ms()); }
                    t.status = "queued".into();
                    t.stage = "定时时间已到，准备启动下单".into();
                });
                Some(permit)
            } else { Some(acquire_account(lock, &mut cancel).await?) };
            if account.id != LOCAL_ID && !self.accounts.lock().unwrap().iter().any(|a| a.id == account.id) {
                return Err("账号已移除，任务已停止".into());
            }
            let mut command = app.command()?;
            command.arg("--data-dir").arg(&self.root).arg("--account-id").arg(&account.id).arg("--task-id").arg(&id)
                .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true).env("PYTHONUNBUFFERED", "1").env("PYTHONIOENCODING", "utf-8");
            #[cfg(windows)] command.creation_flags(0x08000000);
            let mut child = command.spawn().map_err(|e| format!("无法启动业务进程：{e}"))?;
            if let Some(control) = self.controls.lock().unwrap().get_mut(&id) { control.pid = child.id(); }
            let mut stdin = child.stdin.take().ok_or("没有任务输入通道")?;
            let stdout = child.stdout.take().ok_or("没有任务输出通道")?;
            if let Some(s) = &schedule {
                if *cancel.borrow() { return Err("cancelled".into()) }
                check_schedule_lateness(s.at_ms, now_ms())?;
            }
            let state = self.session_passwords.lock().unwrap().get(&account.id).cloned().unwrap_or_default();
            let mut credential_generation = state.generation;
            let credential = state.password;
            let credential_rejected = state.rejected;
            stdin.write_all(format!("{}\n", json!({"account_id":account.id,"task_id":id,"account":account.email,"action":action,"params":params,"credential":credential,"credential_rejected":credential_rejected})).as_bytes()).await.map_err(|e| e.to_string())?;
            let mut lines = BufReader::new(stdout).lines();
            self.update(&app, &id, |t| { t.status = "running".into(); t.stage = "开始执行".into();
                t.execution_started_at_ms = Some(now_ms()); });
            let mut completed = false;
            loop {
                tokio::select! {
                    _ = cancel.changed() => {
                        let _ = stdin.write_all(format!("{}\n", json!({"type":"cancel","account_id":account.id,"task_id":id})).as_bytes()).await;
                        if tokio::time::timeout(std::time::Duration::from_secs(2), child.wait()).await.is_err() { let _ = child.kill().await; }
                        return Err("cancelled".into());
                    },
                    Some(mut message) = inputs.recv() => {
                        if let Some(generation) = message.as_object_mut().and_then(|value| value.remove("_credential_generation")).and_then(|value| value.as_u64()) {
                            credential_generation = generation;
                        }
                        stdin.write_all(format!("{message}\n").as_bytes()).await.map_err(|e| e.to_string())?;
                    },
                    line = lines.next_line() => {
                        let Some(line) = line.map_err(|e| e.to_string())? else { break; };
                        if line.len() > 4_000_000 { return Err("业务进程输出超出限制".into()); }
                        let mut event: Value = serde_json::from_str(&line).map_err(|_| "业务进程返回了无效的数据")?;
                        if event["account_id"] != account.id || event["task_id"] != id || event["v"] != 1 { return Err("业务进程的账号或任务标识不匹配".into()); }
                        let kind = event["type"].as_str().unwrap_or("").to_owned();
                        if kind == "credential_rejected" {
                            let mut credentials = self.session_passwords.lock().unwrap();
                            let state = credentials.entry(account.id.clone()).or_default();
                            if state.generation == credential_generation {
                                state.password = None;
                                state.rejected = true;
                            }
                            continue;
                        }
                        if kind == "inventory" && action == "inventory_monitor" && !*cancel.borrow() {
                            let outcomes = self.inventory_dispatch(&app, &event["result"]);
                            event["result"]["dispatches"] = json!(outcomes);
                        }
                        if kind == "result" && matches!(action.as_str(), "enroll" | "me") {
                            self.sync_account_identity(&account.id, &event["result"], action == "enroll")?;
                        }
                        if kind=="result" && action=="inventory_settings_save" && event["result"]["success"]==true {
                            self.queue_inventory_settings_changed();
                        }
                        if kind == "error" && event["account_invalid"] == true && event["order_submitted"] != true
                            && self.remove_invalid_account(&account.id, &id)? {
                            let reason = event["message"].as_str().unwrap_or("重新认证失败").to_owned();
                            event["account_removed"] = json!(true);
                            event["message"] = json!(format!("{}：当前用户已失效，请重新登录。账号已移除，可自行重新添加。", account.email));
                            self.update(&app, &id, |t| t.logs.push(reason));
                        }
                        if event.get("browser_pid").is_some() {
                            if let Some(control) = self.controls.lock().unwrap().get_mut(&id) { control.browser_pid = event["browser_pid"].as_u64().map(|n| n as u32); }
                        }
                        self.apply_worker_event(&app, &account.id, &id, &event, params["queue_stream"] == true);
                        if matches!(kind.as_str(), "result" | "error" | "cancelled") { completed = true; }
                    }
                }
            }
            let exit = child.wait().await.map_err(|e| e.to_string())?;
            if !completed || !exit.success() { return Err("业务进程意外退出，请查询当前状态后再试".into()); }
            Ok(())
        }.await;
        let browser_pid = self.controls.lock().unwrap().get(&id).and_then(|c| c.browser_pid);
        if let Some(pid) = browser_pid {
            stop_process(pid).await;
        }
        if let Err(error) = result {
            self.update(&app, &id, |t| {
                t.prompt = None;
                t.status = if error == "cancelled" {
                    "cancelled"
                } else {
                    "failed"
                }
                .into();
                t.stage = if error == "cancelled" {
                    "任务已取消".into()
                } else {
                    error
                };
                if t.order_submitted {
                    t.stage.push_str("；订单可能已提交，请先查询订单确认结果");
                }
            });
        }
        self.controls.lock().unwrap().remove(&id);
        if params["queue_stream"] == true { self.queue_worker_ended(&account.id, &id); }
    }
    pub async fn shutdown(&self) {
        self.queue_stopping.store(true, std::sync::atomic::Ordering::SeqCst);
        self.queue_pause_for_shutdown();
        {
            let controls = self.controls.lock().unwrap();
            for control in controls.values() {
                let _ = control.cancel.send(true);
            }
        }
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;
        let pids: Vec<_> = self
            .controls
            .lock()
            .unwrap()
            .values()
            .flat_map(|c| [c.browser_pid, c.pid])
            .flatten()
            .collect();
        for pid in pids {
            stop_process(pid).await;
        }
    }
}

async fn acquire_account(
    account: Arc<Semaphore>,
    cancel: &mut watch::Receiver<bool>,
) -> Result<tokio::sync::OwnedSemaphorePermit, String> {
    if *cancel.borrow() {
        return Err("cancelled".into());
    }
    let account = tokio::select! { p=account.acquire_owned()=>p.map_err(|e|e.to_string())?, _=cancel.changed()=>return Err("cancelled".into()) };
    if *cancel.borrow() {
        return Err("cancelled".into());
    }
    Ok(account)
}

pub fn validate_reply(
    task: &Task,
    account: &str,
    request: &str,
    value: &Value,
) -> Result<(), String> {
    if task.account_id != account || task.status != "waiting" {
        return Err("账号与等待输入的任务不匹配".into());
    }
    let prompt = task.prompt.as_ref().ok_or("任务不在等待输入")?;
    if prompt["id"] != request {
        return Err("此输入已过期，请使用当前提示".into());
    }
    let valid = match prompt["kind"].as_str() {
        Some("confirm") => value.is_boolean(),
        Some("password") => value
            .get("password")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty() && s.len() <= 4096),
        Some("select") => prompt["options"]
            .as_array()
            .is_some_and(|rows| rows.iter().any(|r| &r["value"] == value)),
        Some("text" | "code") => value.as_str().is_some_and(|s| s.len() <= 4096),
        _ => false,
    };
    if valid {
        Ok(())
    } else {
        Err("输入格式或选项无效".into())
    }
}

pub fn apply_event(task: &mut Task, event: &Value) {
    purchase_progress::update(task, event);
    if event["order_submitted"] == true {
        task.order_submitted = true;
    }
    match event["type"].as_str().unwrap_or("") {
        "inventory" => {
            task.status = "running".into();
            task.stage = "正在监控门店实时库存".into();
            task.result = Some(event["result"].clone());
        }
        "prompt" => {
            task.status = "waiting".into();
            task.prompt = Some(event["prompt"].clone());
            task.stage = event["prompt"]["label"]
                .as_str()
                .unwrap_or("等待输入")
                .into();
        }
        "stage" => {
            task.stage = event["message"].as_str().unwrap_or("执行中").into();
        }
        "log" => {
            task.logs.push(
                event["message"]
                    .as_str()
                    .unwrap_or("")
                    .chars()
                    .take(2000)
                    .collect(),
            );
            if task.logs.len() > 100 {
                task.logs.remove(0);
            }
        }
        "progress_result" => {
            if task.action == "purchase" && !terminal(&task.status) && !task.order_submitted
                && event["result"]["status"] == "purchase_retry"
                && task.result.as_ref().is_none_or(|result| result["status"] != "pending_payment"
                    && result["pending_orders"].as_array().is_none_or(|orders| orders.is_empty())) {
                task.result = Some(event["result"].clone());
                task.stage = event["result"]["message"].as_str().unwrap_or("正在处理下单失败重试")
                    .chars().take(2000).collect();
            } else if !terminal(&task.status) && matches!(event["result"]["status"].as_str(),Some("parallel_running" | "parallel_complete" | "queue_running" | "queue_complete")) {
                task.result = Some(event["result"].clone());
                task.stage = if event["result"]["status"].as_str().is_some_and(|status|status.starts_with("queue_")) { "正在执行门店下单队列" } else { "并发自提下单执行中" }.into();
            } else if task.order_submitted && !terminal(&task.status)
                && event["result"]["status"] == "pending_payment"
                && event["result"]["order_number"].as_str().is_some_and(|n| !n.is_empty()) {
                task.result = Some(event["result"].clone());
                task.prompt = None;
                task.stage = "订单已创建，正在加载付款二维码".into();
            }
        }
        "queue_finished" => {
            let matches_prompt=task.prompt.as_ref().is_some_and(|prompt| prompt["queue_job_id"]==event["queue_job_id"]
                && prompt["queue_job_id"].as_str().is_some()
                && prompt["queue_assignment"]==event["queue_assignment"]);
            if matches_prompt && !terminal(&task.status) {
                task.prompt=None;task.status="running".into();task.stage="正在执行门店下单队列".into();
            }
        }
        "result" => {
            let operation_failed = matches!(task.action.as_str(),"purchase"|"session_maintain") && event["result"]["success"] == false;
            task.status = if operation_failed { "failed" } else { "succeeded" }.into();
            task.result = Some(event["result"].clone());
            task.prompt = None;
            task.stage = if operation_failed {
                event["result"]["message"].as_str().filter(|message| !message.trim().is_empty())
                    .unwrap_or(if task.action=="session_maintain" {"本次登录维护未完成"}else{"本次下单未成功"}).chars().take(2000).collect()
            } else { "已完成".into() };
        }
        "error" => {
            task.status = "failed".into();
            task.stage = event["message"].as_str().unwrap_or("操作失败").into();
            task.prompt = None;
            if event["account_removed"] == true {
                task.result = Some(json!({"success": false, "account_removed": true}));
            }
        }
        "cancelled" => {
            task.status = "cancelled".into();
            task.stage = "任务已取消".into();
            task.prompt = None;
        }
        _ => {}
    }
    if !task.queue_job_ids.is_empty() {
        if let Some(result)=task.result.as_mut().and_then(Value::as_object_mut) {
            result.insert("queue_job_ids".into(),json!(task.queue_job_ids));
        }
    }
}

async fn stop_process(pid: u32) {
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(0x08000000)
            .output()
            .await;
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("/bin/kill")
            .args(["-TERM", &pid.to_string()])
            .output()
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn task() -> Task {
        Task {
            id: "t".into(),
            account_id: "a".into(),
            action: "login".into(),
            status: "waiting".into(),
            stage: "".into(),
            created_at: 0,
            updated_at: 0,
            execution_started_at_ms: None,
            purchase_progress: None,
            prompt: Some(json!({"id":"p","kind":"code"})),
            result: None,
            order_submitted: false,
            logs: vec![],
            schedule: None,
            queue_job_ids: vec![],
        }
    }
    #[test]
    fn order_progress_is_visible_without_finishing_task() {
        let mut t = task();
        t.status = "running".into();
        t.order_submitted = true;
        apply_event(&mut t, &json!({"type":"progress_result","result":{
            "status":"pending_payment","order_number":"TEST-001","amount_due":"RMB 69","qr_loading":true}}));
        assert_eq!(t.status, "running");
        assert_eq!(t.result.as_ref().unwrap()["order_number"], "TEST-001");
        apply_event(&mut t, &json!({"type":"error","message":"QR interrupted"}));
        assert_eq!(t.status, "failed");
        assert_eq!(t.result.as_ref().unwrap()["status"], "pending_payment");
    }

    #[test]
    fn retry_progress_is_visible_before_submission_and_preserved_on_failure() {
        let mut purchase=task(); purchase.action="purchase".into(); purchase.status="running".into();
        let result=json!({"status":"purchase_retry","message":"第 1 次下单失败，准备第 2 次执行。",
            "retry_summary":{"retry_count":2,"attempts_made":1,"retries_used":0,"stop_reason":"retrying"}});
        apply_event(&mut purchase,&json!({"type":"progress_result","result":result}));
        assert_eq!(purchase.status,"running");
        assert!(!purchase.order_submitted);
        assert_eq!(purchase.result.as_ref().unwrap(),&result);
        assert_eq!(purchase.stage,result["message"].as_str().unwrap());
        apply_event(&mut purchase,&json!({"type":"error","message":"下单失败，已用完配置的重试次数。"}));
        assert_eq!(purchase.status,"failed");
        assert_eq!(purchase.result.as_ref().unwrap()["retry_summary"]["attempts_made"],1);
    }

    #[test]
    fn retry_progress_cannot_rewind_submission_payment_terminal_or_other_actions() {
        let progress=json!({"type":"progress_result","result":{"status":"purchase_retry","message":"retry",
            "retry_summary":{"retry_count":2,"attempts_made":1,"retries_used":0,"stop_reason":"retrying"}}});
        let pending=json!({"status":"pending_payment","order_number":"W111111"});
        for submitted in [false,true] {
            let mut purchase=task(); purchase.action="purchase".into(); purchase.status="running".into();
            purchase.result=Some(pending.clone()); purchase.order_submitted=submitted; purchase.stage="付款二维码已就绪".into();
            apply_event(&mut purchase,&progress);
            assert_eq!(purchase.result.as_ref().unwrap(),&pending);
            assert_eq!(purchase.stage,"付款二维码已就绪");
            assert_eq!(purchase.order_submitted,submitted);
        }
        let mut submitted=task(); submitted.action="purchase".into(); submitted.status="running".into();
        submitted.order_submitted=true; apply_event(&mut submitted,&progress);
        assert!(submitted.result.is_none());
        for status in ["succeeded","failed","cancelled","interrupted"] {
            let mut terminal_task=task(); terminal_task.action="purchase".into(); terminal_task.status=status.into();
            terminal_task.result=Some(json!({"success":false}));
            apply_event(&mut terminal_task,&progress);
            assert_eq!(terminal_task.status,status);
            assert_eq!(terminal_task.result.unwrap(),json!({"success":false}));
        }
        let mut other=task(); other.action="orders".into(); other.status="running".into();
        apply_event(&mut other,&progress); assert!(other.result.is_none());
        let mut parallel=task(); parallel.action="purchase".into(); parallel.status="running".into();
        parallel.result=Some(json!({"status":"parallel_running","pending_orders":[pending]}));
        apply_event(&mut parallel,&progress);
        assert_eq!(parallel.result.unwrap()["status"],"parallel_running");
    }

    #[test]
    fn failed_purchase_result_preserves_parallel_attempts_and_retry_summary() {
        let mut purchase=task(); purchase.action="purchase".into(); purchase.status="running".into();
        purchase.order_submitted=true;
        let result=json!({"success":false,"status":"parallel_complete","message":"所有下单尝试均未成功。",
            "attempts":[{"status":"failed","error":"门店库存不足"},{"status":"submission_unknown","order_submitted":true}],
            "retry_summary":{"retry_count":2,"attempts_made":3,"retries_used":2,"stop_reason":"exhausted"}});
        apply_event(&mut purchase,&json!({"type":"result","result":result}));
        assert_eq!(purchase.status,"failed");
        assert_eq!(purchase.stage,"所有下单尝试均未成功。");
        assert!(purchase.order_submitted);
        assert!(purchase.prompt.is_none());
        assert_eq!(purchase.result.unwrap(),result);
        for message in [Value::Null,json!(""),json!("   ")] {
            let mut failed=task(); failed.action="purchase".into(); failed.status="running".into();
            apply_event(&mut failed,&json!({"type":"result","result":{"success":false,"message":message}}));
            assert_eq!(failed.status,"failed"); assert_eq!(failed.stage,"本次下单未成功");
        }
    }

    #[test]
    fn partial_parallel_success_and_nonpurchase_results_keep_existing_completion_behavior() {
        let mut purchase=task(); purchase.action="purchase".into(); purchase.status="running".into();
        let partial=json!({"success":true,"status":"parallel_complete","pending_orders":[{"order_number":"W111111","status":"pending_payment"}],
            "attempts":[{"status":"pending_payment"},{"status":"failed"}],
            "retry_summary":{"retry_count":2,"attempts_made":1,"retries_used":0,"stop_reason":"success"}});
        apply_event(&mut purchase,&json!({"type":"result","order_submitted":true,"result":partial}));
        assert_eq!(purchase.status,"succeeded"); assert_eq!(purchase.stage,"已完成");
        assert!(purchase.order_submitted); assert_eq!(purchase.result.unwrap(),partial);
        let mut other=task(); other.action="cancel_order".into(); other.status="running".into();
        let declined=json!({"success":false,"status":"cancellation_unavailable","message":"官网当前不允许取消。"});
        apply_event(&mut other,&json!({"type":"result","result":declined}));
        assert_eq!(other.status,"succeeded"); assert_eq!(other.stage,"已完成");
        assert_eq!(other.result.unwrap(),declined);
    }

    #[tokio::test]
    async fn session_maintenance_skips_busy_accounts_and_manual_purchase_cancels_only_maintenance() {
        let root=tempfile::tempdir().unwrap();let engine=Engine::new(root.path().into()).unwrap();
        let account=engine.add_account("maintenance@example.test".into()).unwrap();engine.accounts.lock().unwrap()[0].pending=false;
        let host=TaskHost {worker:root.path().join("missing"),script:None,on_update:Arc::new(|_|{})};
        let maintenance=engine.start(host.clone(),account.id.clone(),"session_maintain".into(),json!({})).unwrap();
        assert!(engine.start(host.clone(),account.id.clone(),"session_maintain".into(),json!({})).unwrap_err().contains("已跳过"));
        engine.start(host.clone(),account.id.clone(),"purchase".into(),json!({"sku":"MLH74FE","quantity":1,"fulfillment":"RETAIL"})).unwrap();
        assert!(*engine.controls.lock().unwrap()[&maintenance].cancel.borrow());
        let another=engine.add_account("maintenance-sem@example.test".into()).unwrap();engine.accounts.lock().unwrap()[1].pending=false;
        let sem=Arc::new(Semaphore::new(1));let _held=sem.acquire().await.unwrap();engine.locks.lock().unwrap().insert(another.id.clone(),sem.clone());
        assert!(engine.start(host,another.id,"session_maintain".into(),json!({})).is_err());
    }

    #[tokio::test]
    async fn session_maintenance_never_waits_when_account_becomes_busy_after_admission() {
        let root=tempfile::tempdir().unwrap();let engine=Engine::new(root.path().into()).unwrap();
        let account=engine.add_account("maintenance-race@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending=false;
        let host=TaskHost {worker:root.path().join("must-not-spawn"),script:None,on_update:Arc::new(|_|{})};
        let id=engine.start(host,account.id.clone(),"session_maintain".into(),json!({})).unwrap();
        let lock=engine.locks.lock().unwrap()[&account.id].clone();
        let _held=lock.acquire().await.unwrap();
        for _ in 0..100 {
            if terminal(&engine.tasks.lock().unwrap()[&id].status) { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let task=engine.tasks.lock().unwrap()[&id].clone();
        assert_eq!(task.status,"failed");
        assert!(task.stage.contains("已跳过"),"{}",task.stage);
        assert!(task.execution_started_at_ms.is_none());
    }

    #[test]
    fn completed_queue_assignment_clears_only_its_own_prompt_and_keeps_job_associations() {
        let mut queued=task();queued.action="purchase".into();queued.queue_job_ids=vec!["job-a".into(),"job-b".into()];
        queued.prompt=Some(json!({"id":"request","kind":"code","queue_job_id":"job-a","queue_assignment":2}));
        apply_event(&mut queued,&json!({"type":"queue_finished","queue_job_id":"job-b","queue_assignment":2}));assert!(queued.prompt.is_some());
        apply_event(&mut queued,&json!({"type":"queue_finished","queue_job_id":"job-a","queue_assignment":1}));assert!(queued.prompt.is_some());
        apply_event(&mut queued,&json!({"type":"queue_finished","queue_job_id":"job-a","queue_assignment":2}));assert!(queued.prompt.is_none());assert_eq!(queued.status,"running");
        apply_event(&mut queued,&json!({"type":"result","result":{"success":true,"status":"queue_complete"}}));
        assert_eq!(queued.result.unwrap()["queue_job_ids"],json!(["job-a","job-b"]));
    }

    #[test]
    fn blocked_session_maintenance_result_is_failed_without_submission_or_account_removal() {
        let root=tempfile::tempdir().unwrap();let engine=Engine::new(root.path().into()).unwrap();
        let account=engine.add_account("blocked-maintenance@example.test".into()).unwrap();
        let mut maintenance=task();maintenance.id=Uuid::new_v4().to_string();maintenance.account_id=account.id.clone();
        maintenance.action="session_maintain".into();maintenance.status="running".into();maintenance.order_submitted=false;
        let id=maintenance.id.clone();engine.tasks.lock().unwrap().insert(id.clone(),maintenance);
        let host=TaskHost {worker:PathBuf::from("unused"),script:None,on_update:Arc::new(|_|{})};
        let result=json!({"success":false,"status":"blocked","requires_login":true,"message":"官网拒绝本次会话交换，请在原账号工作台重新验证。","http_status":541});
        engine.update(&host,&id,|task|apply_event(task,&json!({"type":"result","result":result})));
        let snapshot=engine.snapshot();let task=snapshot.tasks.iter().find(|task|task.id==id).unwrap();
        assert_eq!(task.status,"failed");assert_eq!(task.stage,result["message"].as_str().unwrap());assert_eq!(task.result.as_ref().unwrap(),&result);
        assert!(!task.order_submitted);assert_eq!(snapshot.accounts.len(),1);assert_eq!(snapshot.accounts[0].id,account.id);
        assert!(Engine::new(root.path().into()).unwrap().snapshot().accounts.iter().any(|saved|saved.id==account.id));
    }
    #[test]
    fn replies_are_bound_to_account_and_request() {
        let t = task();
        assert!(validate_reply(&t, "a", "p", &json!("012345")).is_ok());
        assert!(validate_reply(&t, "b", "p", &json!("012345")).is_err());
        assert!(validate_reply(&t, "a", "old", &json!("012345")).is_err());
    }
    #[tokio::test]
    async fn repeated_read_tasks_reuse_the_outstanding_task() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending = false;
        let host = TaskHost { worker: PathBuf::from("unused"), script: None, on_update: Arc::new(|_| {}) };
        for action in ["me", "address_read"] {
            for status in ["queued", "running", "waiting"] {
                let mut existing = task();
                existing.account_id = a.id.clone();
                existing.action = action.into();
                existing.status = status.into();
                engine.tasks.lock().unwrap().insert(existing.id.clone(), existing);
                assert_eq!(engine.start(host.clone(), a.id.clone(), action.into(), json!({})).unwrap(), "t");
                assert_eq!(engine.tasks.lock().unwrap().len(), 1);
            }
        }
    }
    #[tokio::test]
    async fn waiting_for_input_blocks_new_tasks_only_for_that_account() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        let b = engine.add_account("b@example.test".into()).unwrap();
        for account in engine.accounts.lock().unwrap().iter_mut() { account.pending = false; }
        let mut waiting = task();
        waiting.account_id = a.id.clone();
        waiting.action = "purchase".into();
        engine.tasks.lock().unwrap().insert(waiting.id.clone(), waiting);
        let host = TaskHost { worker: PathBuf::from("unused"), script: None, on_update: Arc::new(|_| {}) };
        assert!(engine.start(host.clone(), a.id.clone(), "address_read".into(), json!({})).unwrap_err().contains("正在等待输入"));
        assert!(engine.start(host.clone(), b.id, "address_read".into(), json!({})).is_ok());
        engine.tasks.lock().unwrap().get_mut("t").unwrap().status = "cancelled".into();
        assert!(engine.start(host, a.id, "address_read".into(), json!({})).is_ok());
    }
    #[test]
    fn confirmation_is_explicit_and_selection_is_constrained() {
        let mut t = task();
        t.prompt = Some(json!({"id":"p","kind":"confirm"}));
        assert!(validate_reply(&t, "a", "p", &json!("")).is_err());
        assert!(validate_reply(&t, "a", "p", &json!(true)).is_ok());
        t.prompt = Some(json!({"id":"p","kind":"select","options":[{"value":"delivery"}]}));
        assert!(validate_reply(&t, "a", "p", &json!("pickup")).is_err());
    }
    #[test]
    fn accounts_persist_and_duplicates_are_rejected() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("USER@example.test".into()).unwrap();
        assert!(engine.add_account("user@example.test".into()).is_err());
        assert_eq!(
            Engine::new(temp.path().into()).unwrap().snapshot().accounts[0].id,
            a.id
        );
    }
    #[test]
    fn manual_removal_clears_only_target_scope_and_allows_readding() {
        for pending in [true, false] {
            let temp = tempfile::tempdir().unwrap();
            let engine = Engine::new(temp.path().into()).unwrap();
            let a = engine.add_account("a@example.test".into()).unwrap();
            let b = engine.add_account("b@example.test".into()).unwrap();
            engine.accounts.lock().unwrap()[0].pending = pending;
            for id in [&a.id, &b.id, LOCAL_ID] {
                private_json(&scope_dir(temp.path(), id).join("auth.json"), &json!({"fixture":true})).unwrap();
            }
            let mut t = task();
            t.account_id = a.id.clone();
            t.status = "succeeded".into();
            engine.tasks.lock().unwrap().insert(t.id.clone(), t);
            let reservation = engine.reserve_removal(&a.id).unwrap();
            engine.finish_removal(&a.id).unwrap();
            drop(reservation);
            assert!(!scope_dir(temp.path(), &a.id).exists());
            assert!(scope_dir(temp.path(), &b.id).join("auth.json").exists());
            assert!(scope_dir(temp.path(), LOCAL_ID).join("auth.json").exists());
            assert!(engine.snapshot().tasks.is_empty());
            let restored = Engine::new(temp.path().into()).unwrap();
            assert_eq!(restored.snapshot().accounts.len(), 1);
            assert_eq!(restored.snapshot().accounts[0].id, b.id);
            assert!(restored.add_account(a.email).is_ok());
        }
    }
    #[test]
    fn manual_removal_rejects_busy_invalid_and_local_scopes() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        for id in ["../", LOCAL_ID, &Uuid::new_v4().to_string()] {
            assert!(engine.reserve_removal(id).is_err());
        }
        for status in ["running", "queued", "waiting"] {
            let mut t = task();
            t.account_id = a.id.clone();
            t.status = status.into();
            engine.tasks.lock().unwrap().insert(t.id.clone(), t);
            assert!(engine.reserve_removal(&a.id).is_err());
        }
        engine.tasks.lock().unwrap().get_mut("t").unwrap().status = "succeeded".into();
        let (cancel, _) = watch::channel(false);
        let (input, _) = mpsc::channel(1);
        engine.controls.lock().unwrap().insert("t".into(), Control { cancel, input, pid: None, browser_pid: None });
        assert!(engine.reserve_removal(&a.id).is_err());
        engine.controls.lock().unwrap().clear();
        assert!(engine.reserve_removal(&a.id).is_ok());
    }
    #[tokio::test]
    async fn removal_reservation_blocks_new_tasks_and_releases_on_failure() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        let host = TaskHost { worker: temp.path().join("missing-worker"), script: None, on_update: Arc::new(|_| {}) };
        let reservation = engine.reserve_removal(&a.id).unwrap();
        assert!(engine.reserve_removal(&a.id).is_err());
        assert!(engine.start(host.clone(), a.id.clone(), "enroll".into(), json!({})).unwrap_err().contains("正在移除"));
        drop(reservation);
        assert!(engine.remove_account(&host, &a.id).await.is_err());
        assert_eq!(engine.snapshot().accounts.len(), 1);
        assert!(engine.reserve_removal(&a.id).is_ok());
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn manual_removal_waits_for_successful_vault_cleanup() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        let auth = scope_dir(temp.path(), &a.id).join("auth.json");
        private_json(&auth, &json!({"fixture":true})).unwrap();
        let script = temp.path().join("vault.sh");
        let host = TaskHost { worker: "/bin/sh".into(), script: Some(script.clone()), on_update: Arc::new(|_| {}) };
        fs::write(&script, "exit 1\n").unwrap();
        assert!(engine.remove_account(&host, &a.id).await.is_err());
        assert!(auth.exists());
        assert_eq!(engine.snapshot().accounts.len(), 1);
        fs::write(&script, format!("test \"$1\" = --forget-account && test \"$2\" = {}\n", a.id)).unwrap();
        engine.remove_account(&host, &a.id).await.unwrap();
        assert!(!auth.exists());
        assert!(engine.snapshot().accounts.is_empty());
    }
    #[test]
    fn local_defaults_are_copied_only_for_new_accounts() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let old = engine.add_account("old@example.test".into()).unwrap();
        engine.save_preset(LOCAL_ID, "MK2W4CH".into(), 3).unwrap();
        private_json(
            &temp.path().join("settings/checkout.json"),
            &json!({"address":{"street":"测试路"}}),
        )
        .unwrap();
        let new = engine.add_account("new@example.test".into()).unwrap();
        assert_eq!(new.preset_sku, "MK2W4CH");
        assert_eq!(new.preset_quantity, 3);
        assert!(scope_dir(temp.path(), &new.id)
            .join("checkout.json")
            .exists());
        assert!(!scope_dir(temp.path(), &old.id)
            .join("checkout.json")
            .exists());
        assert!(new.pending);
        let restored = Engine::new(temp.path().into()).unwrap().snapshot();
        assert_eq!(restored.defaults.preset_sku, "MK2W4CH");
        assert_eq!(restored.accounts.len(), 2);
        assert_eq!(restored.accounts[0].preset_sku, "MK2U4CH");
    }
    #[tokio::test]
    async fn local_scope_allows_only_anonymous_queries_and_local_addresses() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let host = TaskHost {
            worker: PathBuf::from("unused"),
            script: None,
            on_update: Arc::new(|_| {}),
        };
        for action in ACTIONS {
            let result = engine.start(host.clone(), LOCAL_ID.into(), (*action).into(), json!({}));
            assert_eq!(
                result.is_ok(),
                matches!(*action, "catalog" | "address_read" | "address_save") || inventory_action(action),
                "{action}"
            );
        }
        assert!(engine.snapshot().accounts.is_empty());
        assert!(temp.path().join("settings/tasks").exists());
        assert!(!temp.path().join("accounts").exists());
    }
    #[test]
    fn interrupted_submission_is_not_replayed() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("user@example.test".into()).unwrap();
        let mut t = task();
        t.id = Uuid::new_v4().to_string();
        t.account_id = a.id.clone();
        t.status = "running".into();
        t.order_submitted = true;
        private_json(
            &temp
                .path()
                .join("accounts")
                .join(&a.id)
                .join("tasks")
                .join(&t.id)
                .join("task.json"),
            &t,
        )
        .unwrap();
        let restored = Engine::new(temp.path().into()).unwrap().snapshot();
        assert_eq!(restored.tasks[0].status, "interrupted");
        assert!(restored.tasks[0].prompt.is_none());
    }
    #[tokio::test]
    async fn pending_account_requires_verified_enrollment_and_survives_restart() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        assert!(a.pending);
        let host = TaskHost {
            worker: PathBuf::from("unused"),
            script: None,
            on_update: Arc::new(|_| {}),
        };
        assert!(engine
            .start(host, a.id.clone(), "purchase".into(), json!({}))
            .is_err());
        assert!(engine
            .sync_account_identity(
                &a.id,
                &json!({"success":true,"logged_in":true,"scope":"store_menu"}),
                true
            )
            .is_err());
        assert!(Engine::new(temp.path().into()).unwrap().snapshot().accounts[0].pending);
        engine.sync_account_identity(&a.id, &json!({"success":true,"logged_in":true,"scope":"account","apple_id":"a@example.test"}), true).unwrap();
        assert!(!Engine::new(temp.path().into()).unwrap().snapshot().accounts[0].pending);
    }
    #[test]
    fn verified_nicknames_persist_per_account_and_empty_names_fall_back() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        let b = engine.add_account("b@example.test".into()).unwrap();
        assert_eq!(a.name, a.email);
        let result = |name: Value| json!({"success": true, "logged_in": true, "scope": "account", "apple_id": a.email, "name": name});
        engine
            .sync_account_identity(&a.id, &result(json!("  官方昵称  ")), true)
            .unwrap();
        let restored = Engine::new(temp.path().into()).unwrap().snapshot();
        assert_eq!(restored.accounts[0].name, "官方昵称");
        assert!(!restored.accounts[0].pending);
        assert_eq!(restored.accounts[1].name, b.email);
        engine
            .sync_account_identity(&a.id, &result(json!("更新昵称")), false)
            .unwrap();
        assert_eq!(engine.snapshot().accounts[0].name, "更新昵称");
        assert!(engine
            .sync_account_identity(&a.id, &json!({"success": false, "name": "不能覆盖"}), false)
            .is_err());
        assert_eq!(engine.snapshot().accounts[0].name, "更新昵称");
        engine
            .sync_account_identity(&a.id, &result(Value::Null), false)
            .unwrap();
        assert_eq!(
            Engine::new(temp.path().into()).unwrap().snapshot().accounts[0].name,
            a.email
        );
    }
    #[test]
    fn existing_accounts_without_pending_flag_are_preserved() {
        let account: Account = serde_json::from_value(
            json!({"id":Uuid::new_v4().to_string(),"name":"旧账号","email":"a@example.test"}),
        )
        .unwrap();
        assert!(!account.pending);
    }
    #[tokio::test]
    async fn invalid_account_is_removed_without_affecting_other_accounts() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("a@example.test".into()).unwrap();
        let b = engine.add_account("b@example.test".into()).unwrap();
        assert!(!engine.remove_invalid_account(&a.id, "current").unwrap());
        engine
            .sync_account_identity(
                &a.id,
                &json!({"success":true,"logged_in":true,"scope":"account","apple_id":a.email}),
                true,
            )
            .unwrap();
        let mut queued = task();
        queued.account_id = a.id.clone();
        engine
            .tasks
            .lock()
            .unwrap()
            .insert(queued.id.clone(), queued.clone());
        let (cancel, rx) = watch::channel(false);
        let (input, _) = mpsc::channel(1);
        engine.controls.lock().unwrap().insert(
            queued.id.clone(),
            Control {
                cancel,
                input,
                pid: None,
                browser_pid: None,
            },
        );
        assert!(engine.remove_invalid_account(&a.id, "current").unwrap());
        assert!(*rx.borrow());
        let restored = Engine::new(temp.path().into()).unwrap().snapshot();
        assert_eq!(restored.accounts.len(), 1);
        assert_eq!(restored.accounts[0].id, b.id);
        let host = TaskHost {
            worker: PathBuf::from("unused"),
            script: None,
            on_update: Arc::new(|_| {}),
        };
        assert!(engine
            .start(host, a.id.clone(), "me".into(), json!({}))
            .is_err());
        let fresh = engine.add_account(a.email.clone()).unwrap();
        assert_ne!(fresh.id, a.id);
        assert!(fresh.pending);
        apply_event(
            &mut queued,
            &json!({"type":"error","account_removed":true,"message":"当前用户已失效，请重新登录"}),
        );
        assert_eq!(queued.result.unwrap()["account_removed"], true);
    }
    #[tokio::test]
    async fn account_locks_serialize_only_the_same_account() {
        let a = Arc::new(Semaphore::new(1));
        let b = Arc::new(Semaphore::new(1));
        let _first = a.acquire().await.unwrap();
        assert!(a.try_acquire().is_err());
        assert!(b.try_acquire().is_ok());
    }
    #[tokio::test]
    async fn a_queued_account_does_not_block_other_accounts_and_cancels_cleanly() {
        let a = Arc::new(Semaphore::new(1));
        let (sender, mut rx) = watch::channel(false);
        let first = acquire_account(a.clone(), &mut rx).await.unwrap();
        let queued_account = a.clone();
        let mut queued_rx = rx.clone();
        let queued =
            tokio::spawn(async move { acquire_account(queued_account, &mut queued_rx).await });
        tokio::task::yield_now().await;
        assert!(!queued.is_finished());
        // Holding more than three accounts must not queue unrelated accounts.
        let mut others = Vec::new();
        for _ in 0..7 {
            others.push(
                tokio::time::timeout(
                    std::time::Duration::from_secs(1),
                    acquire_account(Arc::new(Semaphore::new(1)), &mut rx),
                )
                .await
                .unwrap()
                .unwrap(),
            );
        }
        sender.send(true).unwrap();
        assert!(
            tokio::time::timeout(std::time::Duration::from_secs(1), queued)
                .await
                .unwrap()
                .unwrap()
                .is_err()
        );
        drop(first);
        assert_eq!(a.available_permits(), 1);
    }
    #[test]
    fn sku_presets_are_separate_and_survive_restart() {
        let temp = tempfile::tempdir().unwrap();
        let e = Engine::new(temp.path().into()).unwrap();
        let a = e.add_account("a@example.test".into()).unwrap();
        let b = e.add_account("b@example.test".into()).unwrap();
        e.save_preset(&a.id, "mlh74fe".into(), 3).unwrap();
        let state = Engine::new(temp.path().into()).unwrap().snapshot();
        assert_eq!(
            state
                .accounts
                .iter()
                .find(|p| p.id == a.id)
                .unwrap()
                .preset_quantity,
            3
        );
        assert_eq!(
            state
                .accounts
                .iter()
                .find(|p| p.id == b.id)
                .unwrap()
                .preset_sku,
            "MK2U4CH"
        );
    }
    fn scheduled_params(at: u64) -> Value {
        json!({"sku":"MJT84CH","quantity":1,"fulfillment":"RETAIL","scheduled_at":at})
    }
    #[test]
    fn schedule_validates_time_and_freezes_purchase_selection() {
        let params = scheduled_params(10_000);
        let s = purchase_schedule("purchase", &params, 1_000).unwrap().unwrap();
        assert_eq!((s.at_ms, s.sku.as_str(), s.quantity, s.fulfillment.as_str()), (10_000,"MJT84CH",1,"RETAIL"));
        assert!(purchase_schedule("login", &params, 1_000).is_err());
        for patch in [json!({"scheduled_at":999}), json!({"scheduled_at":"10000"}),
                      json!({"quantity":0}), json!({"quantity":1.5}), json!({"sku":"../bad"}),
                      json!({"fulfillment":"OTHER"}), json!({"prepare_only":true}),
                      json!({"retry_count":-1}), json!({"retry_count":11}), json!({"retry_count":1.5}),
                      json!({"retry_count":"2"}), json!({"retry_count":true}), json!({"retry_count":null})] {
            let mut bad = params.clone();
            bad.as_object_mut().unwrap().extend(patch.as_object().unwrap().clone());
            assert!(purchase_schedule("purchase", &bad, 1_000).is_err());
        }
        assert!(purchase_schedule("purchase", &json!({}), 1_000).unwrap().is_none());
        assert!(check_schedule_lateness(1000, 4000).is_ok());
        assert!(check_schedule_lateness(1000, 4001).is_err());
    }
    #[tokio::test]
    async fn schedule_wait_observes_cancellation_and_never_waits_on_a_missed_time() {
        let (tx, mut rx) = watch::channel(false);
        tx.send(true).unwrap();
        assert_eq!(wait_for_schedule(now_ms()+100_000, &mut rx).await.unwrap_err(), "cancelled");
        let (_tx, mut rx) = watch::channel(false);
        assert!(wait_for_schedule(now_ms()-10_000, &mut rx).await.unwrap_err().contains("错过"));
        assert!(wait_for_schedule(now_ms(), &mut rx).await.is_ok());
    }
    #[tokio::test]
    async fn scheduled_task_does_not_hold_account_lock_and_cancels_before_worker_spawn() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("schedule@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending = false;
        let host = TaskHost { worker: temp.path().join("missing-worker"), script:None, on_update:Arc::new(|_|{}) };
        let id = engine.start(host.clone(), a.id.clone(), "purchase".into(), scheduled_params(now_ms()+60_000)).unwrap();
        tokio::task::yield_now().await;
        assert_eq!(engine.tasks.lock().unwrap()[&id].status,"scheduled");
        assert!(engine.controls.lock().unwrap()[&id].pid.is_none());
        let lock = engine.locks.lock().unwrap()[&a.id].clone();
        let permit = lock.try_acquire_owned().unwrap();
        drop(permit);
        assert!(engine.start(host.clone(),a.id.clone(),"purchase".into(),scheduled_params(now_ms()+120_000)).unwrap_err().contains("已有定时"));
        assert!(engine.start(host.clone(),a.id.clone(),"purchase".into(),json!({"sku":"MJT84CH","quantity":1})).is_err());
        assert!(engine.start(host.clone(),a.id.clone(),"checkout".into(),json!({})).is_err());
        engine.cancel(&a.id,&id).unwrap();
        for _ in 0..100 {
            if terminal(&engine.tasks.lock().unwrap()[&id].status) { break }
            tokio::time::sleep(std::time::Duration::from_millis(2)).await;
        }
        assert_eq!(engine.tasks.lock().unwrap()[&id].status,"cancelled");
        assert!(engine.tasks.lock().unwrap()[&id].schedule.as_ref().unwrap().triggered_at_ms.is_none());
    }
    #[test]
    fn scheduled_tasks_are_interrupted_on_restart_and_not_automatically_replayed() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("restart@example.test".into()).unwrap();
        let mut scheduled = task();
        scheduled.id = Uuid::new_v4().to_string(); scheduled.account_id=a.id.clone();
        scheduled.action="purchase".into(); scheduled.status="scheduled".into(); scheduled.prompt=None;
        scheduled.schedule=purchase_schedule("purchase",&scheduled_params(now_ms()+60_000),now_ms()).unwrap();
        private_json(&scope_dir(temp.path(),&a.id).join("tasks").join(&scheduled.id).join("task.json"),&scheduled).unwrap();
        let restored=Engine::new(temp.path().into()).unwrap();
        let task=restored.tasks.lock().unwrap()[&scheduled.id].clone();
        assert_eq!(task.status,"interrupted"); assert!(task.stage.contains("定时任务已停止"));
        assert!(restored.controls.lock().unwrap().is_empty());
    }
    #[tokio::test]
    async fn passwords_reuse_only_private_ipc_across_tasks_and_rejections_stop_reuse() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let account = engine.add_account("cache@example.test".into()).unwrap();
        let another = engine.add_account("other@example.test".into()).unwrap();
        for account in engine.accounts.lock().unwrap().iter_mut() { account.pending = false; }
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python = project.join(if cfg!(windows) {".venv/Scripts/python.exe"} else {".venv/bin/python"});
        let script = temp.path().join("credential-fixture.py");
        fs::write(&script, r#"import json,sys
r=json.loads(sys.stdin.readline())
def emit(kind, **data):
 print(json.dumps(dict(v=1,account_id=r['account_id'],task_id=r['task_id'],type=kind,**data)),flush=True)
assert 'password' not in r['params']
if r['params'].get('ask'):
 emit('prompt',prompt={'id':'fixture-password','kind':'password','label':'fixture'})
 reply=json.loads(sys.stdin.readline())
 assert reply['value']['password']=='fixture-secret-never-on-disk'
if r['params'].get('reject_delay'):
 import time
 emit('stage',message='fixture-before-reject')
 time.sleep(.5)
 emit('credential_rejected')
if r['params'].get('reject'):
 emit('credential_rejected')
emit('result',result={'success':True,'has_credential':r.get('credential')=='fixture-secret-never-on-disk','rejected':r.get('credential_rejected',False)})
"#).unwrap();
        let host = TaskHost { worker: python, script: Some(script), on_update: Arc::new(|_| {}) };
        let id = engine.start(host.clone(), account.id.clone(), "bag".into(), json!({"ask":true})).unwrap();
        for _ in 0..500 {
            if engine.tasks.lock().unwrap()[&id].status=="waiting" { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert!(engine.reply(&host,&another.id,&id,"fixture-password",json!({"password":"fixture-secret-never-on-disk"})).is_err());
        assert!(engine.session_passwords.lock().unwrap().is_empty());
        engine.reply(&host,&account.id,&id,"fixture-password",json!({"password":"fixture-secret-never-on-disk","remember":false})).unwrap();
        for _ in 0..500 {
            if terminal(&engine.tasks.lock().unwrap()[&id].status) && !engine.controls.lock().unwrap().contains_key(&id) { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        for (owner, params, has_credential, rejected) in [
            (account.id.clone(),json!({}),true,false),
            (another.id.clone(),json!({}),false,false),
            (account.id.clone(),json!({"reject":true}),true,false),
            (account.id.clone(),json!({}),false,true),
            (account.id.clone(),json!({"password":"fixture-secret-never-on-disk"}),true,false),
        ] {
            let id=engine.start(host.clone(),owner,"bag".into(),params).unwrap();
            for _ in 0..500 {
                if terminal(&engine.tasks.lock().unwrap()[&id].status) && !engine.controls.lock().unwrap().contains_key(&id) { break; }
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
            let task=engine.tasks.lock().unwrap()[&id].clone();
            assert_eq!(task.status,"succeeded","{}",task.stage);
            assert_eq!(task.result.as_ref().unwrap()["has_credential"],has_credential);
            assert_eq!(task.result.as_ref().unwrap()["rejected"],rejected);
            let disk=fs::read_to_string(scope_dir(temp.path(),&task.account_id).join("tasks").join(&id).join("task.json")).unwrap();
            assert!(!disk.contains("fixture-secret-never-on-disk"));
            assert!(!serde_json::to_string(&engine.snapshot()).unwrap().contains("fixture-secret-never-on-disk"));
        }
        let old=engine.start(host.clone(),account.id.clone(),"bag".into(),json!({"reject_delay":true})).unwrap();
        for _ in 0..500 {
            if engine.tasks.lock().unwrap()[&old].stage=="fixture-before-reject" { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let newer=engine.start(host.clone(),account.id.clone(),"bag".into(),json!({"password":"fixture-secret-never-on-disk"})).unwrap();
        for _ in 0..500 {
            if terminal(&engine.tasks.lock().unwrap()[&newer].status) && !engine.controls.lock().unwrap().contains_key(&newer) { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert_eq!(engine.tasks.lock().unwrap()[&newer].result.as_ref().unwrap()["has_credential"],true);
        assert!(!engine.session_passwords.lock().unwrap().get(&account.id).unwrap().rejected);
        let id=engine.start(host.clone(),account.id.clone(),"forget_password".into(),json!({})).unwrap();
        assert!(engine.session_passwords.lock().unwrap().get(&account.id).unwrap().password.is_none());
        for _ in 0..500 {
            if terminal(&engine.tasks.lock().unwrap()[&id].status) { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert!(Engine::new(temp.path().into()).unwrap().session_passwords.lock().unwrap().is_empty());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn scheduled_dispatch_runs_once_at_deadline_with_frozen_params() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        let a = engine.add_account("dispatch@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending=false;
        let script=temp.path().join("fake.sh");
        fs::write(&script, r#"IFS= read -r payload
printf '%s\n' "$payload" >> "$2/dispatch.jsonl"
printf '{"v":1,"type":"result","account_id":"%s","task_id":"%s","result":{"success":true,"status":"mock_completed"}}\n' "$4" "$6"
"#).unwrap();
        let host=TaskHost { worker:"/bin/sh".into(),script:Some(script),on_update:Arc::new(|_|{}) };
        let at=now_ms()+1200;
        let id=engine.start(host,a.id.clone(),"purchase".into(),scheduled_params(at)).unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        assert!(!temp.path().join("dispatch.jsonl").exists());
        engine.save_preset(&a.id,"MK2M4CH".into(),2).unwrap();
        for _ in 0..300 {
            if terminal(&engine.tasks.lock().unwrap()[&id].status) { break }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let task=engine.tasks.lock().unwrap()[&id].clone();
        assert_eq!(task.status,"succeeded", "{}",task.stage);
        assert!(task.schedule.unwrap().triggered_at_ms.unwrap() >= at);
        let raw=fs::read_to_string(temp.path().join("dispatch.jsonl")).unwrap();
        assert_eq!(raw.lines().count(),1);
        let input:Value=serde_json::from_str(raw.trim()).unwrap();
        assert_eq!(input["params"],json!({"sku":"MJT84CH","quantity":1,"fulfillment":"RETAIL","concurrency":1,"retry_count":0}));
        tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        assert_eq!(fs::read_to_string(temp.path().join("dispatch.jsonl")).unwrap().lines().count(),1);
    }
    #[tokio::test]
    async fn scheduled_task_fails_if_another_account_task_is_unfinished_at_deadline() {
        let temp = tempfile::tempdir().unwrap();
        let engine=Engine::new(temp.path().into()).unwrap();
        let a=engine.add_account("busy@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending=false;
        let host=TaskHost { worker:temp.path().join("must-not-spawn"),script:None,on_update:Arc::new(|_|{}) };
        let id=engine.start(host,a.id.clone(),"purchase".into(),scheduled_params(now_ms()+1200)).unwrap();
        let mut busy=task(); busy.account_id=a.id.clone(); busy.status="running".into();
        engine.tasks.lock().unwrap().insert("t".into(),busy);
        for _ in 0..300 {
            if terminal(&engine.tasks.lock().unwrap()[&id].status) { break }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let task=engine.tasks.lock().unwrap()[&id].clone();
        assert_eq!(task.status,"failed"); assert!(task.stage.contains("未完成任务"));
        assert!(task.schedule.unwrap().triggered_at_ms.is_none());
    }

    #[tokio::test]
    async fn scheduled_deadline_preempts_background_preheat_without_extending_lateness() {
        let temp = tempfile::tempdir().unwrap();
        let engine=Engine::new(temp.path().into()).unwrap();
        let account=engine.add_account("scheduled-warm@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending=false;
        let project=Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python=project.join(if cfg!(windows) {".venv/Scripts/python.exe"} else {".venv/bin/python"});
        let script=temp.path().join("scheduled-warm-fixture.py");
        fs::write(&script, r#"import json,sys
r=json.loads(sys.stdin.readline())
def emit(kind, **data):
 print(json.dumps(dict(v=1,account_id=r['account_id'],task_id=r['task_id'],type=kind,**data)),flush=True)
if r['action']=='session_maintain':
 emit('stage',message='Offline fixture preheat waiting')
 for line in sys.stdin:
  if json.loads(line).get('type')=='cancel':
   emit('cancelled'); break
else:
 assert r['action']=='purchase'
 emit('result',result={'success':True,'status':'fixture_completed','order_submitted':False})
"#).unwrap();
        let host=TaskHost {worker:python,script:Some(script),on_update:Arc::new(|_|{})};
        let at=now_ms()+1500;
        let scheduled=engine.start(host.clone(),account.id.clone(),"purchase".into(),scheduled_params(at)).unwrap();
        let warm=engine.start(host,account.id.clone(),"session_maintain".into(),json!({"warm_only":true})).unwrap();
        for _ in 0..600 {
            if terminal(&engine.tasks.lock().unwrap()[&scheduled].status) { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        let tasks=engine.tasks.lock().unwrap();
        assert_eq!(tasks[&warm].status,"cancelled");
        assert_eq!(tasks[&scheduled].status,"succeeded","{}",tasks[&scheduled].stage);
        let triggered=tasks[&scheduled].schedule.as_ref().unwrap().triggered_at_ms.unwrap();
        assert!(triggered>=at && triggered-at<=3000);
        assert!(tasks[&scheduled].execution_started_at_ms.is_some());
        assert!(!tasks[&scheduled].order_submitted);
    }

    #[test]
    fn pickup_parallelism_is_bounded_and_schedule_retains_selection() {
        assert_eq!(pickup_concurrency(&json!({})).unwrap(), 1);
        for value in [json!(0), json!(11), json!(1.5), json!("3"), Value::Null, json!(true)] {
            assert!(pickup_concurrency(&json!({"concurrency":value})).is_err());
        }
        assert_eq!(pickup_concurrency(&json!({"concurrency":1})).unwrap(), 1);
        assert_eq!(pickup_concurrency(&json!({"concurrency":3})).unwrap(), 3);
        let defaults = purchase_schedule("purchase", &scheduled_params(10_000), 1000).unwrap().unwrap();
        assert_eq!(defaults.concurrency, 1);
        assert_eq!(defaults.retry_count, 0);
        let legacy: PurchaseSchedule = serde_json::from_value(json!({"at_ms":10_000,"sku":"MJT84CH","quantity":1,"fulfillment":"RETAIL"})).unwrap();
        assert_eq!(legacy.concurrency, 1);
        assert_eq!(legacy.retry_count, 0);
        let explicit: PurchaseSchedule = serde_json::from_value(json!({"at_ms":10_000,"sku":"MJT84CH","quantity":1,"fulfillment":"RETAIL","concurrency":3,"retry_count":2})).unwrap();
        assert_eq!(explicit.concurrency, 3);
        assert_eq!(explicit.retry_count, 2);
        let mut params = scheduled_params(10_000);
        params["concurrency"] = json!(7);
        params["retry_count"] = json!(2);
        let frozen = purchase_schedule("purchase", &params, 1000).unwrap().unwrap();
        params["concurrency"] = json!(1);
        params["retry_count"] = json!(0);
        assert_eq!(frozen.concurrency, 7);
        assert_eq!(frozen.retry_count, 2);
    }

    #[test]
    fn purchase_retry_payload_defaults_to_zero_and_explicit_retries_stay_serial_worker_input() {
        assert_eq!(purchase_retry_count(&json!({})).unwrap(), 0);
        for value in [json!(-1),json!(11),json!(1.5),json!("2"),json!(true),Value::Null] {
            assert!(purchase_retry_count(&json!({"retry_count":value})).is_err());
        }
        for fulfillment in ["HOME", "RETAIL"] {
            let mut defaults=json!({"sku":"MLH74FE","quantity":1,"fulfillment":fulfillment});
            normalize_purchase_params("purchase", &mut defaults).unwrap();
            assert_eq!(defaults["retry_count"],0);
            if fulfillment == "RETAIL" { assert_eq!(defaults["concurrency"],1); }
            let mut explicit=json!({"sku":"MLH74FE","quantity":1,"fulfillment":fulfillment,"retry_count":2,"concurrency":3});
            normalize_purchase_params("purchase", &mut explicit).unwrap();
            assert_eq!(explicit["retry_count"],2);
            assert_eq!(explicit["concurrency"],3);
        }
        let mut preparation=json!({"sku":"MLH74FE","quantity":1,"fulfillment":"RETAIL","prepare_only":true,"retry_count":2});
        assert!(normalize_purchase_params("purchase", &mut preparation).unwrap_err().contains("仅执行一次"));
        preparation["retry_count"]=json!(0);
        normalize_purchase_params("purchase", &mut preparation).unwrap();
        assert_eq!(preparation["retry_count"],0);
        let mut read_only=json!({"retry_count":"irrelevant"});
        normalize_purchase_params("orders", &mut read_only).unwrap();
        assert_eq!(read_only["retry_count"],"irrelevant");
    }

    #[tokio::test]
    async fn invalid_inventory_retry_count_cannot_reserve_or_start_purchase() {
        let temp=tempfile::tempdir().unwrap();
        let engine=Engine::new(temp.path().into()).unwrap();
        let account=engine.add_account("retry-stock@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending=false;
        let host=TaskHost {worker:temp.path().join("must-not-run"),script:None,on_update:Arc::new(|_|{})};
        let mut target=stock_target(&account.id);
        target["retry_count"]=json!(11);
        let outcome=engine.inventory_dispatch(&host,&json!({"targets":[target]}));
        assert_eq!(outcome[0]["status"],"blocked");
        assert!(outcome[0]["message"].as_str().unwrap().contains("重试次数"));
        assert!(engine.snapshot().tasks.is_empty());
        assert!(engine.monitor_dispatches.lock().unwrap().is_empty());
    }

    fn stock_target(account_id: &str) -> Value {
        json!({"id":"cloth-r390","sku":"MLH74FE/A","quantity":1,"store_id":"R390", "store_name":"测试门店",
            "location":"200000","account_id":account_id,"auto_purchase":true,"status":"available","checked_at_ms":now_ms(),"concurrency":2})
    }

    #[tokio::test]
    async fn monitor_restock_queue_is_fresh_durable_and_never_rearmed_by_unknown() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        engine.queue_stopping.store(true,std::sync::atomic::Ordering::SeqCst);
        let account = engine.add_account("stock@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending = false;
        let host = TaskHost { worker:temp.path().join("absent"),script:None,on_update:Arc::new(|_|{}) };
        let mut target = stock_target(&account.id);
        target["checked_at_ms"] = json!(now_ms()-20_000);
        let result = engine.inventory_dispatch(&host, &json!({"targets":[target.clone()]}));
        assert_eq!(result[0]["status"], "waiting_stock");
        assert!(engine.snapshot().tasks.is_empty());
        target["checked_at_ms"] = json!(now_ms());
        let first = engine.inventory_dispatch(&host, &json!({"targets":[target.clone(),target.clone()]}));
        assert_eq!(first[0]["status"], "queued");
        assert_eq!(first[1]["status"], "already_queued");
        assert!(engine.snapshot().tasks.is_empty());
        target["status"] = json!("unknown");
        engine.inventory_dispatch(&host, &json!({"targets":[target.clone()]}));
        let restored = Engine::new(temp.path().into()).unwrap();
        restored.queue_stopping.store(true,std::sync::atomic::Ordering::SeqCst);
        restored.accounts.lock().unwrap()[0].pending = false;
        target["status"] = json!("available");
        let after_restart = restored.inventory_dispatch(&host, &json!({"targets":[target.clone()]}));
        assert_eq!(after_restart[0]["status"], "already_queued");
        target["status"] = json!("unavailable");
        restored.inventory_dispatch(&host, &json!({"targets":[target.clone()]}));
        target["status"] = json!("available");
        let restock = restored.inventory_dispatch(&host, &json!({"targets":[target]}));
        assert_eq!(restock[0]["status"], "already_queued");
        let view=restored.queue_command(host,"status",&json!({})).unwrap();
        assert_eq!(view["jobs"].as_array().unwrap().len(),1);
        assert_eq!(view["queued_count"],1);
        assert_eq!(view["jobs"][0]["id"],first[0]["queue_job_id"]);
        assert_eq!(view["jobs"][0]["sequence"],1);
        assert_eq!(view["jobs"][0]["status"],"queued");
    }

    #[tokio::test]
    async fn stock_trigger_preserves_fifo_job_behind_login_schedule_or_uncertain_submission() {
        let temp = tempfile::tempdir().unwrap();
        let engine = Engine::new(temp.path().into()).unwrap();
        engine.queue_stopping.store(true,std::sync::atomic::Ordering::SeqCst);
        let account = engine.add_account("busy-stock@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending = false;
        let host = TaskHost { worker:temp.path().join("must-not-run"),script:None,on_update:Arc::new(|_|{}) };
        let target = stock_target(&account.id);
        for state in ["waiting", "scheduled", "running", "failed", "interrupted", "succeeded"] {
            let mut existing = task();
            existing.account_id = account.id.clone();
            existing.status = state.into();
            if terminal(state) { existing.order_submitted=true; }
            if state == "succeeded" { existing.result=Some(json!({"status":"pending_payment","order_number":"W123456"})); }
            engine.tasks.lock().unwrap().insert("existing".into(),existing);
            let outcome = engine.inventory_dispatch(&host, &json!({"targets":[target.clone()]}));
            assert!(matches!(outcome[0]["status"].as_str(),Some("queued"|"already_queued")),"{state}");
            let view=engine.queue_command(host.clone(),"status",&json!({})).unwrap();
            assert_eq!(view["queued_count"],1);
        }
        assert_eq!(engine.snapshot().tasks.len(),1);
    }

    #[test]
    fn monitor_events_remain_running_and_restart_interrupts_local_monitor() {
        let temp = tempfile::tempdir().unwrap();
        let mut monitor = task();
        monitor.id=Uuid::new_v4().to_string();
        monitor.account_id=LOCAL_ID.into(); monitor.action="inventory_monitor".into();
        apply_event(&mut monitor, &json!({"type":"inventory","result":{"monitoring":true,"targets":[]}}));
        assert_eq!(monitor.status,"running");
        assert_eq!(monitor.result.as_ref().unwrap()["monitoring"],true);
        private_json(&scope_dir(temp.path(),LOCAL_ID).join("tasks").join(&monitor.id).join("task.json"), &monitor).unwrap();
        let restored=Engine::new(temp.path().into()).unwrap();
        let monitor=&restored.snapshot().tasks[0];
        assert_eq!(monitor.action,"inventory_monitor");
        assert_eq!(monitor.status,"interrupted");
        assert!(restored.controls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn anonymous_monitor_is_singleton_and_does_not_hold_settings_lock() {
        let temp=tempfile::tempdir().unwrap();
        let engine=Engine::new(temp.path().into()).unwrap();
        let lock=Arc::new(Semaphore::new(1));
        engine.locks.lock().unwrap().insert(LOCAL_ID.into(),lock.clone());
        let _held=lock.acquire().await.unwrap();
        let host=TaskHost {worker:temp.path().join("absent"),script:None,on_update:Arc::new(|_|{})};
        let first=engine.start(host.clone(),LOCAL_ID.into(),"inventory_monitor".into(),json!({})).unwrap();
        let duplicate=engine.start(host,LOCAL_ID.into(),"inventory_monitor".into(),json!({})).unwrap();
        assert_eq!(first,duplicate);
        for _ in 0..100 {
            if terminal(&engine.tasks.lock().unwrap()[&first].status) { break }
            tokio::task::yield_now().await;
        }
        // It reaches the missing-worker error even though another local task holds the lock.
        assert_eq!(engine.tasks.lock().unwrap()[&first].status,"failed");
    }

    #[test]
    fn nested_parallel_payment_links_are_removed_from_persisted_task_results() {
        let mut value=json!({"status":"pending_payment","payment_url":"secret-one","attempts":[
            {"result":{"order_number":"W123456","qr_data":"secret-two","qr_url":"secret-three"}}]});
        remove_private_payment_links(&mut value);
        assert_eq!(value,json!({"status":"pending_payment","attempts":[{"result":{"order_number":"W123456"}}]}));
    }

    #[test]
    fn authoritative_later_order_status_resolves_only_matching_parallel_orders() {
        let mut purchase=task(); purchase.account_id="account".into(); purchase.status="succeeded".into();
        purchase.order_submitted=true;
        purchase.result=Some(json!({"status":"parallel_complete","pending_orders":[
            {"order_number":"W111111","status":"pending_payment"},{"order_number":"W222222","status":"pending_payment"}]}));
        let mut tasks=HashMap::new(); tasks.insert("purchase".into(),purchase.clone());
        assert!(unresolved_submission(&purchase,&tasks));
        let mut orders=task(); orders.account_id="account".into(); orders.action="orders".into(); orders.status="succeeded".into();
        orders.result=Some(json!({"success":true,"orders":[
            {"order_number":"W111111","verified_by":"official_detail","items":[{"status":"PROCESSING"}]},
            {"order_number":"W222222","verified_by":"official_detail","items":[{"status":"PAYMENT_DUE_STORED_ORDER"}]}]}));
        tasks.insert("orders".into(),orders.clone());
        assert!(unresolved_submission(&purchase,&tasks));
        orders.result.as_mut().unwrap()["orders"][1]["items"][0]["status"]=json!("PAYMENT_EXPIRED_STORED_ORDER");
        tasks.insert("orders".into(),orders.clone());
        assert!(!unresolved_submission(&purchase,&tasks));
        orders.result.as_mut().unwrap()["orders"][0]["verified_by"]=Value::Null;
        tasks.insert("orders".into(),orders.clone());
        assert!(unresolved_submission(&purchase,&tasks));
        // A different account cannot clear this account's pending purchase.
        orders.account_id="other-account".into(); tasks.insert("orders".into(),orders);
        assert!(unresolved_submission(&purchase,&tasks));
        // An attempt without a known order number stays blocked even if other orders are resolved.
        purchase.result.as_mut().unwrap()["attempts"]=json!([{"status":"submission_unknown","order_submitted":true}]);
        assert!(unresolved_submission(&purchase,&tasks));
    }

    #[test]
    fn confirmed_cancellation_can_resolve_a_pending_purchase() {
        let mut purchase=task(); purchase.account_id="account".into(); purchase.status="succeeded".into(); purchase.order_submitted=true;
        purchase.result=Some(json!({"status":"pending_payment","order_number":"W111111"}));
        let mut cancellation=task(); cancellation.account_id="account".into(); cancellation.action="cancel_order".into(); cancellation.status="succeeded".into();
        cancellation.result=Some(json!({"success":true,"status":"order_cancelled","order_number":"W111111"}));
        let mut tasks=HashMap::new(); tasks.insert("purchase".into(),purchase.clone()); tasks.insert("cancel".into(),cancellation);
        assert!(!unresolved_submission(&purchase,&tasks));
    }

}
