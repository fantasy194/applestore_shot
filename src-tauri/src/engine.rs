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
];
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub triggered_at_ms: Option<u64>,
}
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
    Ok(Some(PurchaseSchedule { at_ms, sku, quantity, fulfillment: fulfillment.into(), triggered_at_ms: None }))
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
    pub created_at: u64,
    pub updated_at: u64,
    pub prompt: Option<Value>,
    pub result: Option<Value>,
    pub order_submitted: bool,
    pub logs: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schedule: Option<PurchaseSchedule>,
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
pub struct Engine {
    pub root: PathBuf,
    accounts: Mutex<Vec<Account>>,
    tasks: Mutex<HashMap<String, Task>>,
    controls: Mutex<HashMap<String, Control>>,
    locks: Mutex<HashMap<String, Arc<Semaphore>>>,
    defaults: Mutex<Account>,
    removing: Mutex<HashSet<String>>,
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
        for account in &accounts {
            Uuid::parse_str(&account.id).map_err(|_| "账号标识无效")?;
            if let Ok(entries) = fs::read_dir(root.join("accounts").join(&account.id).join("tasks"))
            {
                for entry in entries.flatten() {
                    if let Ok(raw) = fs::read(entry.path().join("task.json")) {
                        if let Ok(mut task) = serde_json::from_slice::<Task>(&raw) {
                            if task.account_id != account.id || Uuid::parse_str(&task.id).is_err() {
                                continue;
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
        Ok(Arc::new(Self {
            root,
            accounts: Mutex::new(accounts),
            tasks: Mutex::new(tasks),
            controls: Mutex::new(HashMap::new()),
            locks: Mutex::new(HashMap::new()),
            defaults: Mutex::new(defaults),
            removing: Mutex::new(HashSet::new()),
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
        if let Some(result) = disk.result.as_mut().and_then(Value::as_object_mut) {
            for key in ["qr_data", "payment_url", "qr_url"] {
                result.remove(key);
            }
        }
        let path = scope_dir(&self.root, &task.account_id)
            .join("tasks")
            .join(id)
            .join("task.json");
        if let Err(error) = private_json(&path, &disk) {
            eprintln!("无法保存任务状态：{error}");
        }
        (app.on_update)(&task);
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
        let schedule = purchase_schedule(&action, &params, now_ms())?;
        let params = if let Some(s) = &schedule {
            json!({"sku":s.sku,"quantity":s.quantity,"fulfillment":s.fulfillment})
        } else { params };
        let removing = self.removing.lock().unwrap();
        if removing.contains(&account_id) {
            return Err("这个账号正在移除，无法开始新任务".into());
        }
        let account = if account_id == LOCAL_ID {
            if !matches!(action.as_str(), "catalog" | "address_read" | "address_save") {
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
        if tasks.values().any(|t| t.account_id == account_id && t.status == "waiting") {
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
                prompt: None,
                result: None,
                order_submitted: false,
                logs: vec![],
                schedule,
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
        let message = json!({"type":"reply", "account_id": account_id, "task_id":task_id,"request_id":request_id,"value":value});
        self.controls
            .lock()
            .unwrap()
            .get(task_id)
            .ok_or("任务已结束")?
            .input
            .try_send(message)
            .map_err(|_| "无法发送输入，任务可能已退出")?;
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
            let _account = if let Some(s) = &schedule {
                if self.tasks.lock().unwrap().values().any(|t| t.account_id == account.id && t.id != id && !terminal(&t.status)) {
                    return Err("定时时间已到，但该账号仍有未完成任务；未延迟排队下单，请处理后重新设定".into());
                }
                let permit = lock.try_acquire_owned().map_err(|_| "定时时间已到，但账号正忙；未延迟下单")?;
                if *cancel.borrow() { return Err("cancelled".into()) }
                check_schedule_lateness(s.at_ms, now_ms())?;
                self.update(&app, &id, |t| {
                    if let Some(s) = t.schedule.as_mut() { s.triggered_at_ms = Some(now_ms()); }
                    t.status = "queued".into();
                    t.stage = "定时时间已到，准备启动下单".into();
                });
                permit
            } else { acquire_account(lock, &mut cancel).await? };
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
            stdin.write_all(format!("{}\n", json!({"account_id":account.id,"task_id":id,"account":account.email,"action":action,"params":params})).as_bytes()).await.map_err(|e| e.to_string())?;
            let mut lines = BufReader::new(stdout).lines();
            self.update(&app, &id, |t| { t.status = "running".into(); t.stage = "开始执行".into(); });
            let mut completed = false;
            loop {
                tokio::select! {
                    _ = cancel.changed() => {
                        let _ = stdin.write_all(format!("{}\n", json!({"type":"cancel","account_id":account.id,"task_id":id})).as_bytes()).await;
                        if tokio::time::timeout(std::time::Duration::from_secs(2), child.wait()).await.is_err() { let _ = child.kill().await; }
                        return Err("cancelled".into());
                    },
                    Some(message) = inputs.recv() => { stdin.write_all(format!("{message}\n").as_bytes()).await.map_err(|e| e.to_string())?; },
                    line = lines.next_line() => {
                        let Some(line) = line.map_err(|e| e.to_string())? else { break; };
                        if line.len() > 4_000_000 { return Err("业务进程输出超出限制".into()); }
                        let mut event: Value = serde_json::from_str(&line).map_err(|_| "业务进程返回了无效的数据")?;
                        if event["account_id"] != account.id || event["task_id"] != id || event["v"] != 1 { return Err("业务进程的账号或任务标识不匹配".into()); }
                        let kind = event["type"].as_str().unwrap_or("").to_owned();
                        if kind == "result" && matches!(action.as_str(), "enroll" | "me") {
                            self.sync_account_identity(&account.id, &event["result"], action == "enroll")?;
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
                        self.update(&app, &id, |t| apply_event(t, &event));
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
    }
    pub async fn shutdown(&self) {
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
    if event["order_submitted"] == true {
        task.order_submitted = true;
    }
    match event["type"].as_str().unwrap_or("") {
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
            if task.order_submitted && !terminal(&task.status)
                && event["result"]["status"] == "pending_payment"
                && event["result"]["order_number"].as_str().is_some_and(|n| !n.is_empty()) {
                task.result = Some(event["result"].clone());
                task.prompt = None;
                task.stage = "订单已创建，正在加载付款二维码".into();
            }
        }
        "result" => {
            task.status = "succeeded".into();
            task.result = Some(event["result"].clone());
            task.prompt = None;
            task.stage = "已完成".into();
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
            prompt: Some(json!({"id":"p","kind":"code"})),
            result: None,
            order_submitted: false,
            logs: vec![],
            schedule: None,
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
                matches!(*action, "catalog" | "address_read" | "address_save"),
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
                      json!({"fulfillment":"OTHER"}), json!({"prepare_only":true})] {
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
        assert_eq!(input["params"],json!({"sku":"MJT84CH","quantity":1,"fulfillment":"RETAIL"}));
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

}
