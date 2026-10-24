//! Persistent FIFO acknowledgements around a bounded Tokio dispatcher.
//! Workers retain the account lock and stream isolated per-store jobs; recovery
//! never automatically replays a job that may already have reached Apple.
use super::*;
use std::sync::atomic::Ordering;
use std::io::Read;

const CAPACITY: usize = 500;
const HISTORY: usize = 200;
const FRESH_MS: u64 = 10_000;
const CHILD_SLOTS: u32 = 10;
const RESTORE_PRIORITY_MODE: &str = "下单方式已变更，任务暂停。恢复本账号的「预选多店」和自动抢购授权后，有货时继续；改用固定单店请取消此任务。";
const RESTORE_FIXED_MODE: &str = "下单方式已变更，任务暂停。恢复本账号的「固定单店」和自动抢购授权后，有货时继续；改用预选多店请取消此任务。";

fn default_mode() -> String { "sequential".into() }
fn default_limit() -> u32 { 1 }

#[derive(Clone, Serialize, Deserialize)]
pub(super) struct QueueJob {
    pub id: String,
    pub sequence: u64,
    pub enqueued_at_ms: u64,
    #[serde(default)]
    pub stock_detected_at_ms: Option<u64>,
    #[serde(default)]
    pub queued_at_ms: Option<u64>,
    #[serde(default)]
    pub started_at_ms: Option<u64>,
    #[serde(default)]
    pub dispatched_at_ms: Option<u64>,
    #[serde(default)]
    pub submit_started_at_ms: Option<u64>,
    #[serde(default)]
    pub purchase_group_id: Option<String>,
    #[serde(default)]
    pub order_confirmed_at_ms: Option<u64>,
    pub account_id: String,
    pub target_id: String,
    pub sku: String,
    pub quantity: u32,
    pub store_id: String,
    pub store_name: String,
    pub location: String,
    pub concurrency: u32,
    #[serde(default, skip_serializing_if="Option::is_none")]
    pub requested_concurrency: Option<u32>,
    #[serde(default)]
    pub retry_count: u32,
    pub status: String,
    pub stock_status: String,
    pub checked_at_ms: u64,
    #[serde(default)]
    pub signal_confirmed: bool,
    pub task_id: Option<String>,
    pub message: String,
    pub result: Option<Value>,
    #[serde(default)]
    pub order_submitted: bool,
    #[serde(default)]
    pub assignment: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pickup_policy: Option<Value>,
    #[serde(default)]
    pub authorization_confirmed: bool,
    #[serde(default)]
    pub authorized_stores: Vec<String>,
    episode_key: String,
}

impl QueueJob {
    fn pending(&self) -> bool { matches!(self.status.as_str(), "queued" | "waiting_stock") }
    fn inflight(&self) -> bool { matches!(self.status.as_str(), "dispatched" | "running" | "cancelling") }
    fn terminal(&self) -> bool { !self.pending() && !self.inflight() }
    fn wait_for_confirmation(&mut self,message:&str) {
        self.status="waiting_stock".into();self.stock_status="unknown".into();self.checked_at_ms=0;
        self.authorization_confirmed=false;self.signal_confirmed=false;self.authorized_stores.clear();self.message=message.into();
    }
    fn eligible(&self) -> bool {
        self.pending() && self.authorization_confirmed && self.signal_confirmed
    }
    fn payload(&self) -> Value {
        let mut payload=json!({"queue_job_id":self.id,"sku":self.sku,"quantity":self.quantity,"fulfillment":"RETAIL",
            "pickup_store_id":self.store_id,"pickup_store_name":self.store_name,"pickup_location":self.location,
            "concurrency":self.concurrency,"requested_concurrency":self.requested_concurrency.unwrap_or(self.concurrency),"retry_count":self.retry_count,"checked_at_ms":self.checked_at_ms,
            "enqueued_at_ms":self.enqueued_at_ms,"stock_detected_at_ms":self.stock_detected_at_ms,"queued_at_ms":self.queued_at_ms,"dispatched_at_ms":self.dispatched_at_ms,
            "sequence":self.sequence,"queue_assignment":self.assignment,"purchase_group_id":self.purchase_group_id,"inventory_trigger":true});
        if let Some(policy)=&self.pickup_policy {
            let mut active=policy.clone();
            active["stores"]=json!(policy["stores"].as_array().unwrap().iter().filter(|store|
                self.authorized_stores.iter().any(|id|store["store_id"]==*id)).cloned().collect::<Vec<_>>());
            payload["pickup_policy"]=active;
        }
        payload
    }
    fn context(&self) -> Value {
        json!({"account_id":self.account_id,"task_id":self.task_id,"queue_job_id":self.id,"target_id":self.target_id,
            "sku":self.sku,"quantity":self.quantity,"store_id":self.store_id,"store_name":self.store_name,
            "status":self.status,"message":self.message,"sequence":self.sequence,"enqueued_at_ms":self.enqueued_at_ms,
            "queue_assignment":self.assignment,"purchase_group_id":self.purchase_group_id,
            "stock_detected_at_ms":self.stock_detected_at_ms,"queued_at_ms":self.queued_at_ms,"dispatched_at_ms":self.dispatched_at_ms,
            "started_at_ms":self.started_at_ms,"submit_started_at_ms":self.submit_started_at_ms,"order_confirmed_at_ms":self.order_confirmed_at_ms})
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub(super) struct QueueState {
    #[serde(default = "default_mode")]
    mode: String,
    #[serde(default)]
    purchase_groups: HashMap<String, PurchaseGroup>,
    #[serde(default = "default_limit")]
    max_concurrency: u32,
    #[serde(default)]
    paused: bool,
    #[serde(default)]
    resume_after_restart: Option<bool>,
    #[serde(default)]
    next_sequence: u64,
    #[serde(default)]
    jobs: Vec<QueueJob>,
    #[serde(default)]
    episodes: HashMap<String, String>,
    #[serde(default)]
    blocked_accounts: HashMap<String, String>,
    #[serde(default)]
    priority_migrated: HashSet<String>,
    #[serde(default)]
    policy_rejections: HashMap<String, PolicyRejection>,
}

#[derive(Clone,Serialize,Deserialize)]
struct PurchaseGroup {id:String, policy:Value, status:String, authorization_active:bool}

#[derive(Clone,Serialize,Deserialize)]
struct PolicyRejection {account_id:String,sku:String,quantity:u32,message:String,checked_at_ms:u64}

impl Default for QueueState {
    fn default() -> Self {
        Self {mode:default_mode(),purchase_groups:HashMap::new(),max_concurrency:1,paused:false,resume_after_restart:Some(true),next_sequence:0,jobs:vec![],episodes:HashMap::new(),blocked_accounts:HashMap::new(),priority_migrated:HashSet::new(),policy_rejections:HashMap::new()}
    }
}

impl QueueState {
    pub(super) fn load(root: &Path, legacy: &HashMap<String, MonitorEpisode>) -> Result<Self, String> {
        let path=root.join("settings/purchase_queue.json");
        let exists=path.exists();
        let mut state:Self=if exists {
            serde_json::from_slice(&fs::read(&path).map_err(|_| "无法读取下单队列")?)
                .map_err(|_| "下单队列记录损坏，未恢复下单，请先核实已有订单")?
        } else { Self::default() };
        if !matches!(state.mode.as_str(),"sequential"|"parallel") || !(1..=10).contains(&state.max_concurrency)
            || state.jobs.len()>CAPACITY+HISTORY+10 {
            return Err("下单队列配置或容量无效，未恢复下单".into());
        }
        // Legacy paused state is the only available user intent. A previously
        // persisted pause remains paused; never infer resume from old inventory.
        if state.resume_after_restart.is_none() {state.resume_after_restart=Some(!state.paused);}
        if state.purchase_groups.len()>CAPACITY || state.purchase_groups.values().any(|group|Uuid::parse_str(&group.id).is_err()
            || !matches!(group.status.as_str(),"active"|"succeeded"|"submission_unknown"|"cancelled") || clean_priority_policy(&group.policy).as_ref()!=Ok(&group.policy)) {
            return Err("购买意图记录无效，未恢复下单".into());
        }
        let mut identities=HashSet::new();
        for job in &state.jobs {
            if Uuid::parse_str(&job.id).is_err() || Uuid::parse_str(&job.account_id).is_err() || job.account_id==LOCAL_ID
                || job.task_id.as_deref().is_some_and(|id|Uuid::parse_str(id).is_err()) || !identities.insert(job.id.clone())
                || job.sequence==0 || job.sequence>state.next_sequence || !(1..=10).contains(&job.concurrency) || job.requested_concurrency.is_some_and(|value|!(1..=10).contains(&value)) || job.retry_count>10
                || !(1..=999).contains(&job.quantity) || job.sku.is_empty() || job.sku.len()>30
                || !job.sku.chars().all(|ch|ch.is_ascii_alphanumeric()||ch=='/') || !job.store_id.starts_with('R')
                || job.store_id.len()<2 || job.store_id.len()>10 || !job.store_id[1..].chars().all(|ch|ch.is_ascii_digit())
                || !matches!(job.status.as_str(),"queued"|"waiting_stock"|"dispatched"|"running"|"cancelling"|"succeeded"|"failed"|"cancelled"|"interrupted"|"submission_unknown") {
                return Err("下单队列任务记录无效，未恢复下单，请先核实已有订单".into());
            }
            if job.purchase_group_id.as_ref().is_some_and(|id|Uuid::parse_str(id).is_err()) {return Err("购买意图标识无效".into())}
            if job.pickup_policy.as_ref().is_some_and(|policy|clean_priority_policy(policy).as_ref()!=Ok(policy)) {
                return Err("下单队列候选门店规则无效，未恢复下单".into());
            }
        }
        let mut migrated_groups=false;
        let mut legacy_primary:HashMap<String,(bool,u64,String)>=HashMap::new();
        for job in state.jobs.iter().filter(|job|job.pickup_policy.is_some()&&job.purchase_group_id.is_none()) {
            let key=priority_key(&job.account_id,&job.sku,job.quantity);let candidate=(job.pending()||job.inflight(),job.sequence,job.id.clone());
            if legacy_primary.get(&key).is_none_or(|old|(candidate.0,candidate.1)>(old.0,old.1)){legacy_primary.insert(key,candidate);}
        }
        for job in &mut state.jobs {
            if job.pickup_policy.is_some() && job.purchase_group_id.is_none() {
                let key=priority_key(&job.account_id,&job.sku,job.quantity);
                // Old FIFO episodes reused a key across replenish cycles. Only the
                // current pending/latest intent belongs to the current group;
                // historical attempts keep separate terminal identities.
                let mut historical=PurchaseGroup{id:Uuid::new_v4().to_string(),policy:job.pickup_policy.clone().unwrap(),status:"cancelled".into(),authorization_active:false};
                let group=if legacy_primary.get(&key).is_some_and(|(_,_,id)|id==&job.id) {
                    state.purchase_groups.entry(key).or_insert_with(||PurchaseGroup {
                        id:Uuid::new_v4().to_string(),policy:job.pickup_policy.clone().unwrap(),status:"active".into(),authorization_active:true})
                } else {&mut historical};
                job.purchase_group_id=Some(group.id.clone());
                if job.inflight() || contains_unknown(job.result.as_ref().unwrap_or(&Value::Null)) {
                    group.status="submission_unknown".into();
                } else if job.status=="succeeded" && job.result.as_ref().is_some_and(contains_known_order) && group.status!="submission_unknown" {
                    group.status="succeeded".into();
                } else if matches!(job.status.as_str(),"cancelled"|"failed") && group.status=="active" {
                    group.status="cancelled".into();
                }
                // A legacy pool becomes one attempt for its recorded anchor only.
                // Other stores may enter as new jobs after a new observation.
                let mut policy=job.pickup_policy.clone().unwrap();
                policy["stores"]=json!(policy["stores"].as_array().unwrap().iter().filter(|store|store["store_id"]==job.store_id).cloned().collect::<Vec<_>>());
                if policy["stores"].as_array().unwrap().is_empty() {job.wait_for_confirmation("旧候选池缺少对应门店，等待重新授权");}
                else {job.pickup_policy=Some(policy);}
                job.episode_key=format!("group|{}|{}",group.id,job.store_id);
                state.episodes.insert(job.episode_key.clone(),job.id.clone());
                migrated_groups=true;
            }
        }
        for job in &mut state.jobs {
            if job.pending() && job.purchase_group_id.as_ref().is_some_and(|id|state.purchase_groups.values().any(|group|group.id==*id&&group.status=="succeeded")) {
                job.status="cancelled".into();job.message="同一购买意图已有确认订单，已取消未执行门店".into();migrated_groups=true;
            }
        }
        state.jobs.sort_by_key(|job|(job.enqueued_at_ms,job.sequence));
        if !exists {
            for (key,episode) in legacy {
                if episode.attempted { state.episodes.entry(key.clone()).or_insert_with(|| "legacy-attempt".into()); }
            }
        }
        // Authentication is recovered within the owning worker. Older versions
        // persisted this as a manual-resume gate; retain only unrelated blocks.
        let previous_blocks=state.blocked_accounts.len();
        state.blocked_accounts.retain(|_,reason|reason!="authentication");
        let migrated_authentication=previous_blocks!=state.blocked_accounts.len();
        let mut recovered=false;
        let mut migrated_stock_time=false;
        let current=now_ms();
        for job in &mut state.jobs {
            // Before dedicated timeline fields existed, enqueue stored the
            // original available sample's checked_at_ms (not insertion time).
            // Recover that durable evidence, never the latest sample or now.
            if job.stock_detected_at_ms.is_none() && job.enqueued_at_ms>0
                && job.enqueued_at_ms<=current.saturating_add(1000) {
                job.stock_detected_at_ms=Some(job.enqueued_at_ms);
                migrated_stock_time=true;
            }
            if job.inflight() {
                job.status="interrupted".into();
                job.message="本地服务重启，已派发任务已中断，未自动重放；请先查询官网订单".into();
                state.blocked_accounts.insert(job.account_id.clone(),"submission_unknown".into());
                recovered=true;
            } else if job.pending() && (job.order_submitted || job.result.as_ref().is_some_and(|result|
                result["order_submitted"]==true || result["submitted"]==true || contains_unknown(result))) {
                // Submission evidence is durable even when a legacy result belongs
                // to another allocation. It must never return to dispatchable work.
                let known=job.result.as_ref().is_some_and(|result|
                    result["queue_assignment"].as_u64().is_none_or(|assignment|assignment==job.assignment)
                        && !contains_unknown(result) && contains_known_order(result));
                job.status=if known {"succeeded"} else {"submission_unknown"}.into();
                job.message=if known {"订单已有确认编号，未自动重放"} else {"订单提交结果未知，未重放并已阻止该账号后续分配"}.into();
                if !known {state.blocked_accounts.insert(job.account_id.clone(),"submission_unknown".into());}
                job.authorization_confirmed=false;job.signal_confirmed=false;job.authorized_stores.clear();
                recovered=true;
            } else if job.pending() && job.assignment>0 && job.result.as_ref().is_some_and(|result|
                result["status"]=="paused" && result["started"]==false && result["pause_reason"]=="authentication"
                    && result["queue_assignment"].as_u64().is_none_or(|assignment|assignment==job.assignment)) {
                // Legacy workers acknowledged an allocated authentication failure,
                // then put it back into queued. Removing the account gate must not
                // replay that failed allocation as though it had never run.
                let result=job.result.as_ref().unwrap();
                job.status="failed".into();
                job.message=result["message"].as_str().filter(|message|!message.trim().is_empty())
                    .unwrap_or("此前自动认证未完成，本次下单已失败").chars().take(2000).collect();
                job.authorization_confirmed=false;job.signal_confirmed=false;job.authorized_stores.clear();
                recovered=true;
            } else if job.pending() {
                if job.concurrency>1 {job.requested_concurrency=Some(job.concurrency);job.concurrency=1;}
                job.authorization_confirmed=false;job.signal_confirmed=false;job.authorized_stores.clear();
                job.stock_status="unknown".into(); job.checked_at_ms=0; job.status="waiting_stock".into();
                job.message="服务已重启，等待新的监控库存确认；暂停的队列需手动继续".into(); recovered=true;
            }
        }
        if recovered { state.paused=true; }
        if exists && (recovered || migrated_stock_time || migrated_authentication || migrated_groups) { state.save(root)?; }
        Ok(state)
    }
    pub(super) fn owns_active_prompt_assignment(&self,account:&str,task:&str,event:&Value)->bool {
        self.jobs.iter().any(|job|job.account_id==account && job.task_id.as_deref()==Some(task)
            && event["queue_job_id"]==job.id && event["queue_assignment"].as_u64()==Some(job.assignment)
            && job.inflight())
    }
    fn limit(&self) -> usize { if self.mode=="parallel" {self.max_concurrency as usize} else {1} }
    fn active(&self) -> usize { self.jobs.iter().filter(|job| job.inflight()).count() }
    fn active_child_slots(&self) -> u32 {self.jobs.iter().filter(|job|job.inflight()).map(|job|job.concurrency).sum()}
    fn prune(&mut self) {
        let mut remaining=self.jobs.iter().filter(|job| job.terminal()).count();
        self.jobs.retain(|job| {
            if job.terminal() && remaining>HISTORY { remaining-=1; false } else { true }
        });
    }
    fn save(&self, root: &Path) -> Result<(), String> {
        let mut disk=self.clone();
        for job in &mut disk.jobs { if let Some(result)=job.result.as_mut() { remove_private_payment_links(result); } }
        private_json(&root.join("settings/purchase_queue.json"),&disk)
    }
    fn view(&self) -> Value {
        json!({"mode":self.mode,"max_concurrency":self.max_concurrency,"paused":self.paused,"capacity":CAPACITY,
            "running_count":self.active(),"queued_count":self.jobs.iter().filter(|job| job.pending()).count(),
            "child_capacity":CHILD_SLOTS,"reserved_child_slots":self.active_child_slots(),
            "blocked_accounts":self.blocked_accounts.iter().map(|(account,reason)|json!({"account_id":account,"reason":reason})).collect::<Vec<_>>(),
            "policy_errors":self.policy_rejections.values().collect::<Vec<_>>(),
            "jobs":self.jobs})
    }
}

#[derive(Default)]
pub(super) struct QueueRuntime {
    host: Option<TaskHost>,
    pumping: bool,
    coordinators: HashMap<String, Coordinator>,
}

struct Coordinator { task_id:String, ready:bool, idle:bool, closing:bool }

fn target_key(target: &Value) -> Result<(String,String,String,u32), String> {
    let account=target["account_id"].as_str().unwrap_or("");
    let sku=target["sku"].as_str().unwrap_or("").trim().to_ascii_uppercase();
    let store=target["store_id"].as_str().unwrap_or("").trim().to_ascii_uppercase();
    let quantity=target["quantity"].as_u64().unwrap_or(0);
    if Uuid::parse_str(account).is_err() || account==LOCAL_ID || sku.is_empty() || sku.len()>30
        || !sku.chars().all(|c|c.is_ascii_alphanumeric()||c=='/') || !store.starts_with('R') || store.len()<2 || store.len()>10
        || !store[1..].chars().all(|c|c.is_ascii_digit()) || !(1..=999).contains(&quantity) {
        return Err("自动下单目标缺少有效账号、SKU、数量或门店编号".into());
    }
    Ok((account.into(),sku,store,quantity as u32))
}

fn sample_eligible(target: &Value, current:u64, check_age:bool) -> bool {
    target["checked_at_ms"].as_u64().is_some_and(|at|at>0 && at<=current.saturating_add(1000) && (!check_age || current.saturating_sub(at)<=FRESH_MS))
}

fn canonical_sku(sku:&str)->String {
    let value=sku.trim().to_ascii_uppercase();value.strip_suffix("/A").unwrap_or(&value).to_owned()
}

fn policy_store_ids(policy:&Value)->Vec<String> {
    let mut stores:Vec<_>=policy["stores"].as_array().into_iter().flatten().filter_map(|store|store["store_id"].as_str().map(str::to_owned)).collect();
    stores.sort();stores
}

fn priority_key(account:&str,sku:&str,quantity:u32)->String {format!("priority|{account}|{}|{quantity}",canonical_sku(sku))}

fn clean_store(value:&Value)->Result<Value,String> {
    let id=value["store_id"].as_str().unwrap_or("").trim();
    let name=value["store_name"].as_str().unwrap_or("").trim();let search=value["search"].as_str().unwrap_or("").trim();
    if !id.starts_with('R') || id.len()<2 || id.len()>9 || !id[1..].chars().all(|c|c.is_ascii_digit())
        || name.is_empty() || name.chars().count()>200 || search.is_empty() || search.chars().count()>200 {
        return Err("预选门店必须包含有效编号、名称和城市或邮编".into());
    }
    Ok(json!({"store_id":id,"store_name":name,"search":search}))
}

fn clean_priority_policy(pickup:&Value)->Result<Value,String> {
    if pickup["selection_mode"]!="priority" {return Err("候选门店规则必须使用 priority 模式".into())}
    let stores=pickup["stores"].as_array().filter(|stores|!stores.is_empty()&&stores.len()<=30).ok_or("优先门店列表必须包含 1–30 家门店")?;
    let mut ids=HashSet::new();let mut selected=vec![];
    for store in stores {
        let safe=clean_store(store)?;
        if !ids.insert(safe["store_id"].as_str().unwrap().to_owned()) {return Err("预选门店不能重复".into())}
        selected.push(safe);
    }
    let contact=match pickup.get("contact_mode") {None=>"local",Some(value)=>value.as_str().ok_or("自提联系人模式无效")?};
    if !matches!(contact,"local"|"official") {return Err("自提联系人模式无效".into())}
    let rule=pickup.get("time_slot_rule");
    if rule.is_some_and(|rule|!rule.is_object()) {return Err("自提时间段规则无效".into())}
    let mode=match rule.and_then(|rule|rule.get("mode")) {None=>"earliest",Some(value)=>value.as_str().ok_or("自提时间段规则无效")?};
    let fallback=match rule.and_then(|rule|rule.get("fallback")) {None=>"earliest",Some(value)=>value.as_str().ok_or("自提时间段后备方式无效")?};
    if !matches!(mode,"earliest"|"latest"|"window") || !matches!(fallback,"earliest"|"stop") {return Err("自提时间段规则或后备方式无效".into())}
    let mut safe_rule=json!({"mode":mode,"fallback":fallback});
    if mode=="window" {
        let start=rule.and_then(|rule|rule["start"].as_str()).unwrap_or("");let end=rule.and_then(|rule|rule["end"].as_str()).unwrap_or("");
        let clock=|value:&str| {
            let bytes=value.as_bytes();bytes.len()==5&&bytes[2]==b':'&&bytes[0..2].iter().all(u8::is_ascii_digit)&&bytes[3..5].iter().all(u8::is_ascii_digit)
                &&value[0..2].parse::<u8>().is_ok_and(|hour|hour<24)&&value[3..5].parse::<u8>().is_ok_and(|minute|minute<60)
        };
        if !clock(start)||!clock(end)||start>=end {return Err("自提时间窗口必须为同日 HH:MM，开始早于结束".into())}
        safe_rule["start"]=json!(start);safe_rule["end"]=json!(end);
    }
    Ok(json!({"selection_mode":"priority","stores":selected,"time_slot_rule":safe_rule,"contact_mode":contact}))
}

fn read_account_priority(root:&Path,account:&str)->Result<Option<Value>,String> {
    let path=scope_dir(root,account).join("checkout.json");
    let metadata=match fs::symlink_metadata(&path) {Ok(value)=>value,Err(error) if error.kind()==std::io::ErrorKind::NotFound=>return Ok(None),Err(_)=>return Err("无法读取账号自提规则，未自动下单".into())};
    const MAX:u64=262_144;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len()>MAX {return Err("账号自提配置不是有效的有界本地文件".into())}
    let mut raw=vec![];fs::File::open(&path).map_err(|_|"无法打开账号自提规则")?.take(MAX+1).read_to_end(&mut raw).map_err(|_|"无法读取账号自提规则")?;
    if raw.len()>MAX as usize {return Err("账号自提配置超出大小限制".into())}
    let config:Value=serde_json::from_slice(&raw).map_err(|_|"账号自提规则格式错误，未自动下单")?;
    if !config.is_object() {return Err("账号自提规则格式错误，未自动下单".into())}
    let Some(pickup)=config.get("pickup") else{return Ok(None)};
    if !pickup.is_object() {return Err("账号自提规则格式错误，未自动下单".into())}
    match match pickup.get("selection_mode") {None=>"fixed",Some(value)=>value.as_str().ok_or("自提门店选择模式无效，未自动下单")?} {
        "fixed"=>Ok(None),"priority"=>clean_priority_policy(pickup).map(Some),_=>Err("自提门店选择模式无效，未自动下单".into()),
    }
}

// This is receipt evidence, not the Apple order creation time. Only a
// current matching worker ACK with a pending order number can supply it.
fn queue_order_confirmation_at(result:&Value,enqueued:u64,received:u64)->Option<u64> {
    fn valid_time(value:&Value,earliest:u64,received:u64)->Option<u64> {
        value.as_u64().filter(|at|*at>0&&*at>=earliest&&*at<=received)
    }
    fn visit(node:&Value,depth:usize,pending_entry:bool,remaining:&mut usize,earliest:u64,received:u64)->Option<u64> {
        if depth>3 || *remaining==0 {return None}
        *remaining-=1;
        let number=node["order_number"].as_str().or_else(||node["result"]["order_number"].as_str());
        let has_number=number.is_some_and(|number|!number.trim().is_empty()&&number.len()<=128
            &&number.trim().bytes().all(|byte|byte.is_ascii_alphanumeric()||byte==b'-'));
        let pending=(pending_entry&&!matches!(node["status"].as_str(),Some("submission_unknown"|"payment_unknown")))||node["status"]=="pending_payment";
        if pending&&has_number {
            return Some(valid_time(&node["payment_confirmed_at_ms"],earliest,received)
                .or_else(||valid_time(&node["result"]["payment_confirmed_at_ms"],earliest,received)).unwrap_or(received));
        }
        if depth==3 {return None}
        for key in ["pending_orders","queue_jobs","attempts","jobs"] {
            if let Some(rows)=node[key].as_array() {
                for row in rows {
                    if *remaining==0 {return None}
                    if let Some(at)=visit(row,depth+1,key=="pending_orders",remaining,earliest,received) {return Some(at)}
                }
            }
        }
        node.get("result").filter(|value|value.is_object())
            .and_then(|nested|visit(nested,depth+1,false,remaining,earliest,received))
    }
    let mut remaining=100;
    let confirmed=visit(result,0,false,&mut remaining,enqueued,received)?;
    Some(valid_time(&result["payment_confirmed_at_ms"],enqueued,received).unwrap_or(confirmed))
}

fn contains_known_order(node:&Value)->bool {
    node["order_number"].as_str().is_some_and(|number|!number.trim().is_empty())
        || ["result","attempts","queue_jobs","jobs","pending_orders"].iter().any(|key|match node.get(*key) {
            Some(Value::Object(_))=>contains_known_order(&node[*key]),
            Some(Value::Array(rows))=>rows.iter().filter(|child|child.is_object()).any(contains_known_order),
            _=>false,
        })
}

pub(super) fn contains_unknown(result: &Value) -> bool {
    const CHILDREN: [&str;5] = ["result","attempts","queue_jobs","jobs","pending_orders"];
    fn children_any(node:&Value,predicate:fn(&Value)->bool)->bool {
        CHILDREN.iter().any(|key|match node.get(*key) {
            Some(Value::Object(_))=>predicate(&node[*key]),
            Some(Value::Array(rows))=>rows.iter().filter(|child|child.is_object()).any(predicate),
            _=>false,
        })
    }
    if !result.is_object() {return false}
    if matches!(result["status"].as_str(),Some("submission_unknown"|"unknown_submission"|"payment_unknown"|"verification_required")) {return true}
    // Inspect each outcome before considering aggregate evidence. One known
    // order cannot resolve another submitted child whose order is unknown.
    if children_any(result,contains_unknown) {return true}
    (result["order_submitted"]==true || result["submitted"]==true) && !contains_known_order(result)
}

impl Engine {
    fn policy_inventory_targets(&self,queue:&mut QueueState,targets:&[Value],_current:u64)->Vec<Value> {
        let mut groups:Vec<(String,String,u32,Vec<Value>)>=vec![];
        let mut output=vec![];
        for source in targets.iter().take(100) {
            let mut target=source.clone();
            if let Some(fields)=target.as_object_mut() {for key in ["_queue_episode_key","_queue_reject","_queue_authorized_stores","_queue_purchase_group_id","pickup_policy"] {fields.remove(key);}}
            match target_key(&target) {
                Ok((account,sku,_,quantity))=>{
                    let sku=canonical_sku(&sku);
                    if let Some((_,_,_,rows))=groups.iter_mut().find(|(owner,part,count,_)|*owner==account&&*part==sku&&*count==quantity) {rows.push(target);}
                    else {groups.push((account,sku,quantity,vec![target]));}
                },
                Err(_)=>output.push(target),
            }
        }
        let mut configs:HashMap<String,Result<Option<Value>,String>>=HashMap::new();
        let observed_groups:HashSet<_>=groups.iter().map(|(account,sku,quantity,_)|priority_key(account,sku,*quantity)).collect();
        for (key,group) in &mut queue.purchase_groups {if !observed_groups.contains(key){group.authorization_active=false;}}
        queue.policy_rejections.retain(|key,_|observed_groups.contains(key));
        for (account,sku,quantity,rows) in groups {
            let key=priority_key(&account,&sku,quantity);
            let configured=configs.entry(account.clone()).or_insert_with(||read_account_priority(&self.root,&account)).clone();
            let same_group=|job:&QueueJob|job.account_id==account&&canonical_sku(&job.sku)==sku&&job.quantity==quantity;
            let base=match configured {
                Ok(Some(policy))=>policy,
                Ok(None)=>{
                    if let Some(group)=queue.purchase_groups.get_mut(&key){group.authorization_active=false;}
                    for job in queue.jobs.iter_mut().filter(|job|same_group(job)&&job.pending()&&job.pickup_policy.is_some()) {
                        job.status="waiting_stock".into();job.authorization_confirmed=false;job.signal_confirmed=false;job.checked_at_ms=0;job.message=RESTORE_PRIORITY_MODE.into();
                    }
                    queue.priority_migrated.remove(&key);
                    if queue.jobs.iter().any(|job|same_group(job)&&job.pending()&&job.pickup_policy.is_some()) {
                        continue; // Preserve the frozen pool intent without creating competing fixed jobs.
                    }
                    if queue.episodes.contains_key(&key)&&queue.jobs.iter().any(|job|same_group(job)&&job.pickup_policy.is_some()&&(job.inflight()||job.order_submitted)) {
                        for row in &rows {
                            if let Ok((owner,part,store,_))=target_key(row) {queue.episodes.entry(format!("{owner}|{part}|{store}")).or_insert_with(||"mode-transition-existing-attempt".into());}
                        }
                    }
                    output.extend(rows);continue;
                },
                Err(error)=>{
                    for job in queue.jobs.iter_mut().filter(|job|same_group(job)&&job.pending()) {job.status="waiting_stock".into();job.stock_status="unknown".into();job.checked_at_ms=0;job.message=error.clone();}
                    if let Some(mut rejected)=rows.iter().find(|row|row["auto_purchase"]==true).cloned() {
                        rejected["_queue_reject"]=json!(error);output.push(rejected);
                    }
                    continue;
                },
            };
            let configured_stores=base["stores"].as_array().unwrap();
            let authorized:Vec<Value>=configured_stores.iter().filter(|store|rows.iter().any(|row|row["auto_purchase"]==true&&row["store_id"]==store["store_id"])).cloned().collect();
            let mut execution=None;let mut conflict=None;
            for store in &authorized {
                let matches:Vec<_>=rows.iter().filter(|row|row["auto_purchase"]==true&&row["store_id"]==store["store_id"]).collect();
                if matches.len()!=1 {conflict=Some("同一候选门店存在重复监控目标，请先合并，未自动扩大购买".to_owned());break}
                match (pickup_concurrency(matches[0]),purchase_retry_count(matches[0])) {
                    (Ok(_concurrency),Ok(retry))=>{
                        let value=retry;
                        if execution.is_some_and(|previous|previous!=value) {conflict=Some("同一购买意图的失败重试次数不一致，请统一配置后再执行".into());break}
                        execution=Some(value);
                    },
                    (Err(error),_)|(_,Err(error))=>{conflict=Some(error);break},
                }
            }
            if queue.priority_migrated.insert(key.clone()) {
                let fixed_attempt=queue.jobs.iter().any(|job|same_group(job)&&job.pickup_policy.is_none()&&(job.inflight()||job.order_submitted))
                    || authorized.iter().any(|store|queue.episodes.get(&format!("{account}|{}|{}",rows[0]["sku"].as_str().unwrap_or(&sku),store["store_id"].as_str().unwrap())).is_some_and(|value|value=="legacy-attempt"));
                if fixed_attempt {queue.episodes.entry(key.clone()).or_insert_with(||"mode-transition-existing-attempt".into());}
                for job in queue.jobs.iter_mut().filter(|job|same_group(job)&&job.pickup_policy.is_none()&&job.pending()) {
                    job.status="waiting_stock".into();job.authorization_confirmed=false;job.signal_confirmed=false;job.checked_at_ms=0;job.message=RESTORE_FIXED_MODE.into();
                }
            }
            if queue.jobs.iter().any(|job|same_group(job)&&job.pending()&&job.pickup_policy.is_none()) {
                // An existing fixed intent owns this group until explicitly cancelled
                // or its original mode is restored. Do not create a second pool intent.
                if let Some(row)=rows.first() {output.push(json!({
                    "id":row["id"],"account_id":account,"sku":row["sku"],"quantity":quantity,
                    "store_id":row["store_id"],"auto_purchase":false,"status":"unknown","checked_at_ms":0,
                    "_queue_episode_key":key,"_queue_reject":RESTORE_FIXED_MODE}));}
                continue;
            }
            let active=!authorized.is_empty();
            if !active {
                if let Some(group)=queue.purchase_groups.get_mut(&key) {group.authorization_active=false;}
                continue;
            }
            let mut scope=base.clone();scope["stores"]=json!(authorized);
            let replace=queue.purchase_groups.get(&key).is_none_or(|group|
                policy_store_ids(&group.policy)!=policy_store_ids(&scope) || !group.authorization_active
                    && !queue.jobs.iter().any(|job|job.purchase_group_id.as_deref()==Some(&group.id)&&(job.pending()||job.inflight())));
            if replace {
                if queue.purchase_groups.len()>=CAPACITY && !queue.purchase_groups.contains_key(&key) {continue}
                if let Some(previous)=queue.purchase_groups.get(&key) {
                    let old_prefix=format!("group|{}|",previous.id);queue.episodes.retain(|episode,_|!episode.starts_with(&old_prefix));
                    for job in queue.jobs.iter_mut().filter(|job|job.pending()&&job.purchase_group_id.as_deref()==Some(&previous.id)) {
                        job.status="cancelled".into();job.message="购买目标授权已变更，已取消原意图未执行门店".into();
                    }
                }
                let status=if queue.episodes.contains_key(&key) && !queue.purchase_groups.contains_key(&key) {"succeeded"} else {"active"};
                queue.purchase_groups.insert(key.clone(),PurchaseGroup {id:Uuid::new_v4().to_string(),policy:scope.clone(),status:status.into(),authorization_active:true});
            }
            let group=queue.purchase_groups.get_mut(&key).unwrap();group.authorization_active=true;
            let prefix=format!("group|{}|",group.id);
            if !queue.episodes.keys().any(|episode|episode.starts_with(&prefix)){group.policy=scope;}
            let group=group.clone();
            // Each store keeps its own discovery time. Within one observation,
            // configured store order supplies the stable FIFO tiebreaker.
            for store in group.policy["stores"].as_array().unwrap() {
                let Some(mut row)=rows.iter().find(|row|row["store_id"]==store["store_id"]).cloned() else {continue};
                row["_queue_episode_key"]=json!(format!("group|{}|{}",group.id,store["store_id"].as_str().unwrap()));
                row["_queue_purchase_group_id"]=json!(group.id);
                row["_queue_authorized_stores"]=json!([store["store_id"].clone()]);
                let mut policy=group.policy.clone();policy["stores"]=json!([store.clone()]);row["pickup_policy"]=policy;
                if group.status!="active" {row["auto_purchase"]=json!(false);}
                if let Some(message)=&conflict {row["_queue_reject"]=json!(message);}
                output.push(row);
            }
        }
        for job in queue.jobs.iter_mut().filter(|job|job.pending()&&job.pickup_policy.is_some()&&!observed_groups.contains(&priority_key(&job.account_id,&job.sku,job.quantity))) {
            job.wait_for_confirmation("该购买目标已移出监控，保留原排队位置，等待授权与库存恢复");
        }
        output
    }

    fn revalidate_pending_pickup_policies(&self,queue:&mut QueueState)->bool {
        let mut changed=false;let mut configs:HashMap<String,Result<Option<Value>,String>>=HashMap::new();
        for job in queue.jobs.iter_mut().filter(|job|job.pending()) {
            let config=configs.entry(job.account_id.clone()).or_insert_with(||read_account_priority(&self.root,&job.account_id));
            let reason=match (job.pickup_policy.as_ref(),config) {
                (Some(_),Ok(Some(current)))=>{
                    let before=job.authorized_stores.len();
                    job.authorized_stores.retain(|id|current["stores"].as_array().unwrap().iter().any(|store|store["store_id"]==*id));
                    changed|=before!=job.authorized_stores.len();
                    if job.authorization_confirmed&&!job.authorized_stores.iter().any(|id|*id==job.store_id) {
                        Some("原候选门店授权已变更，保留排队位置，等待授权候选的新库存确认".to_owned())
                    }else {None}
                },
                (Some(_),Ok(None))=>Some(RESTORE_PRIORITY_MODE.into()),
                (None,Ok(Some(_)))=>Some(RESTORE_FIXED_MODE.into()),
                (_,Err(message))=>Some(message.clone()),
                _=>None,
            };
            if let Some(message)=reason {if job.authorization_confirmed||job.checked_at_ms!=0||job.message!=message {
                job.wait_for_confirmation(&message);changed=true;
            }}
        }
        changed
    }

    pub(super) fn queue_inventory_settings_changed(&self,config:Option<&Value>,targets_changed:bool) {
        if !targets_changed {return;}
        let mut queue=self.purchase_queue.lock().unwrap();
        if let Some(targets)=config.and_then(|config|config["targets"].as_array()) {
            for (key,group) in &mut queue.purchase_groups {
                let authorized=targets.iter().any(|target|target["auto_purchase"]==true&&target_key(target).is_ok_and(|(account,sku,store,quantity)|
                    priority_key(&account,&sku,quantity)==*key&&group.policy["stores"].as_array().unwrap().iter().any(|allowed|allowed["store_id"]==store)));
                // Preserve the explicit disabled edge even if re-enabled before
                // monitoring resumes. A normal save must never rearm a goal.
                if !authorized {group.authorization_active=false;}
            }
        }
        for job in queue.jobs.iter_mut().filter(|job|job.pending()) {job.wait_for_confirmation("监控配置已变更，保留排队位置，等待当前授权目标的新库存确认");}
        if queue.save(&self.root).is_err(){queue.paused=true;}
    }

    pub fn queue_command(self: &Arc<Self>, host: TaskHost, operation: &str, params: &Value) -> Result<Value,String> {
        if !params.is_object() {return Err("队列参数必须是对象".into())}
        let mut queue=self.purchase_queue.lock().unwrap();
        let previous=queue.clone();
        let mut cancel_jobs=vec![];
        match operation {
            "status"=>{},
            "configure"=>{
                let mode=params["mode"].as_str().ok_or("请选择顺序或固定并发模式")?;
                let limit=params["max_concurrency"].as_u64().filter(|value|(1..=10).contains(value)).ok_or("队列并发数必须为 1–10 的整数")?;
                if !matches!(mode,"sequential"|"parallel") {return Err("队列模式无效".into())}
                if (queue.mode!=mode || queue.max_concurrency!=limit as u32)
                    && (queue.active()>0 || !self.queue_runtime.lock().unwrap().coordinators.is_empty()) {
                    return Err("请先暂停队列并等待正在执行的任务结束，再修改执行策略；排队任务仍保留".into());
                }
                queue.mode=mode.into();queue.max_concurrency=limit as u32;
            },
            "pause"=>{queue.paused=true;queue.resume_after_restart=Some(false);},
            "resume"=>{
                if let Some(account)=params.get("account_id").and_then(Value::as_str) {
                    if queue.blocked_accounts.get(account).is_some_and(|reason|reason=="submission_unknown") {
                        return Err("该账号有未知提交，不能直接继续；请先核对官网订单".into());
                    }
                    queue.blocked_accounts.remove(account);
                }
                queue.paused=false;
                queue.resume_after_restart=Some(true);
            },
            "cancel"=>{
                let all=params["all"]==true;
                let id=params["job_id"].as_str().unwrap_or("");
                if !all && !queue.jobs.iter().any(|job|job.id==id) {return Err("队列任务不存在".into())}
                if all {queue.paused=true;queue.resume_after_restart=Some(false);
                    for group in queue.purchase_groups.values_mut().filter(|group|group.status=="active"){group.status="cancelled".into();}
                }
                for job in queue.jobs.iter_mut().filter(|job|all||job.id==id) {
                    if job.pending() {job.status="cancelled".into();job.message="已取消排队，未发起下单".into();}
                    else if job.inflight() {job.status="cancelling".into();job.message="正在停止此任务；已提交结果仍需官网确认".into();cancel_jobs.push((job.account_id.clone(),job.task_id.clone(),job.id.clone(),job.assignment));}
                }
            },
            _=>return Err("不支持的队列操作".into()),
        }
        if operation!="status" {
            queue.prune();
            if let Err(error)=queue.save(&self.root) {*queue=previous;return Err(error)}
        }
        if operation!="status" {self.append_event(&format!("queue_{operation}"),&json!({"mode":queue.mode,"max_concurrency":queue.max_concurrency,
            "paused":queue.paused,"account_id":params["account_id"],"queue_job_id":params["job_id"],"all":params["all"]==true}));}
        let view=queue.view();drop(queue);
        for (account,task,id,assignment) in cancel_jobs {
            if let Some(task)=task {
                let claimed=self.tasks.lock().unwrap().get(&task).is_some_and(|current|current.prepared_handoff.as_ref().is_some_and(|claim|
                    claim["source"]=="queue"&&claim["queue_job_id"]==id&&claim["queue_assignment"].as_u64()==Some(assignment)));
                if claimed {let _=self.cancel(&account,&task);}
                else {let _=self.queue_send(&account,&task,json!({"type":"queue_cancel","queue_job_id":id,"queue_assignment":assignment}));}
            }
        }
        if operation!="status" {self.ensure_queue_pump(host);}
        Ok(view)
    }

    pub(super) fn queue_inventory(self: &Arc<Self>, host:&TaskHost, result:&Value) -> Vec<Value> {
        // A scheduled pause is not a new stock/authorization observation.
        // Existing accepted jobs keep their independent dispatch lifecycle.
        if result["schedule_state"]["paused"] == true { return vec![]; }
        let Some(targets)=result["targets"].as_array() else {return vec![]};
        let current=now_ms();
        let mut queue=self.purchase_queue.lock().unwrap();
        let previous=queue.clone();
        let mut outcomes=vec![];
        // A monitor result refreshes authorization, not an already accepted signal.
        // Missing targets invalidate authorization after the complete snapshot is read.
        let targets=self.policy_inventory_targets(&mut queue,targets,current);
        let mut observed=HashSet::new();
        for target in &targets {
            let (account,sku,store,quantity)=match target_key(target) {Ok(value)=>value,Err(message)=>{
                if target["auto_purchase"]==true {outcomes.push(json!({"target_id":target["id"],"status":"blocked","message":message}));} continue;
            }};
            let key=target["_queue_episode_key"].as_str().map(str::to_owned).unwrap_or_else(||format!("{account}|{sku}|{store}"));
            let fresh=sample_eligible(target,current,true);
            observed.insert((key.clone(),quantity));
            let stock=target["status"].as_str().unwrap_or("unknown");
            for job in queue.jobs.iter_mut().filter(|job|job.episode_key==key && job.quantity==quantity && job.pending()) {
                job.stock_status=stock.into();
                job.authorization_confirmed=target["auto_purchase"]==true;
                job.authorized_stores=target["_queue_authorized_stores"].as_array().map(|rows|rows.iter().filter_map(Value::as_str).map(str::to_owned).collect()).unwrap_or_default();
                if let Some(policy)=target.get("pickup_policy") {
                    job.pickup_policy=Some(policy.clone());
                    job.store_id=store.clone();job.store_name=target["store_name"].as_str().unwrap_or("").into();job.location=target["location"].as_str().unwrap_or("").into();
                }
                if !job.authorization_confirmed {
                    job.wait_for_confirmation("监控授权已关闭，保留排队位置，等待授权与库存恢复");
                } else {
                    if fresh && stock=="available" {job.signal_confirmed=true;job.checked_at_ms=target["checked_at_ms"].as_u64().unwrap_or(0);}
                    if job.signal_confirmed {job.status="queued".into();job.message="已收到有货信号，等待按发现顺序执行".into();}
                    else {job.status="waiting_stock".into();job.message="等待当前授权目标的新有货信号".into();}
                }
            }
            if let Some(message)=target["_queue_reject"].as_str() {
                for job in queue.jobs.iter_mut().filter(|job|job.episode_key==key&&job.pending()) {
                    job.wait_for_confirmation(message);
                }
                let error_key=priority_key(&account,&sku,quantity);
                if queue.policy_rejections.get(&error_key).is_none_or(|previous|previous.message!=message) {
                    self.append_event("queue_policy_rejected",&json!({"account_id":account,"sku":sku,"quantity":quantity,"message":message,"status":"blocked"}));
                }
                queue.policy_rejections.insert(error_key,PolicyRejection {account_id:account.clone(),sku:canonical_sku(&sku),quantity,message:message.into(),checked_at_ms:current});
                outcomes.push(json!({"target_id":target["id"],"status":"blocked","message":message}));continue;
            }
            queue.policy_rejections.remove(&priority_key(&account,&sku,quantity));
            if fresh && stock=="unavailable" {
                // A waiting intent already owns this episode; rearm only after
                // terminal work, never create a replacement for a retained job.
                if target["_queue_purchase_group_id"].is_null() && !queue.jobs.iter().any(|job|job.episode_key==key&&job.pending()) {queue.episodes.remove(&key);}
                continue
            }
            if target["auto_purchase"]!=true || stock!="available" {continue}
            if !fresh {outcomes.push(json!({"target_id":target["id"],"status":"waiting_stock","message":"库存检查时间无效或已超过 10 秒"}));continue}
            if let Some(id)=queue.episodes.get(&key) {outcomes.push(json!({"target_id":target["id"],"status":"already_queued","queue_job_id":id}));continue}
            let concurrency=match pickup_concurrency(target) {Ok(value)=>value,Err(message)=>{outcomes.push(json!({"target_id":target["id"],"status":"blocked","message":message}));continue}};
            let retries=match purchase_retry_count(target) {Ok(value)=>value,Err(message)=>{outcomes.push(json!({"target_id":target["id"],"status":"blocked","message":message}));continue}};
            if queue.jobs.iter().filter(|job|!job.terminal()).count()>=CAPACITY {
                outcomes.push(json!({"target_id":target["id"],"status":"capacity_full","message":"下单队列已满，本轮未入队，下一轮有货时再尝试"}));continue;
            }
            let id=Uuid::new_v4().to_string();queue.next_sequence=queue.next_sequence.saturating_add(1);
            let sequence=queue.next_sequence;
            queue.jobs.push(QueueJob {id:id.clone(),sequence,enqueued_at_ms:current,
                stock_detected_at_ms:target["checked_at_ms"].as_u64().filter(|at|*at>0),queued_at_ms:Some(current),
                started_at_ms:None,dispatched_at_ms:None,submit_started_at_ms:None,purchase_group_id:target["_queue_purchase_group_id"].as_str().map(str::to_owned),order_confirmed_at_ms:None,account_id:account,target_id:target["id"].as_str().unwrap_or("").chars().take(100).collect(),sku,quantity,
                store_id:store,store_name:target["store_name"].as_str().unwrap_or("").chars().take(160).collect(),
                location:target["location"].as_str().unwrap_or("").chars().take(160).collect(),concurrency:1,requested_concurrency:(concurrency>1).then_some(concurrency),retry_count:retries,
                status:"queued".into(),stock_status:"available".into(),checked_at_ms:target["checked_at_ms"].as_u64().unwrap_or(0),signal_confirmed:true,
                task_id:None,message:"库存已确认，等待按发现时间执行；账号忙时保留排队".into(),result:None,order_submitted:false,assignment:0,pickup_policy:target.get("pickup_policy").cloned(),
                authorization_confirmed:true,authorized_stores:target["_queue_authorized_stores"].as_array().map(|rows|rows.iter().filter_map(Value::as_str).map(str::to_owned).collect()).unwrap_or_default(),episode_key:key.clone()});
            queue.episodes.insert(key,id.clone());
            outcomes.push(json!({"target_id":target["id"],"status":"queued","queue_job_id":id,"sequence":sequence}));
        }
        for job in queue.jobs.iter_mut().filter(|job|job.pending()&&!observed.contains(&(job.episode_key.clone(),job.quantity))) {
            job.wait_for_confirmation("目标已移出监控，保留排队位置，等待授权与库存恢复");
        }
        queue.prune();queue.jobs.sort_by_key(|job|(job.enqueued_at_ms,job.sequence));
        if let Err(error)=queue.save(&self.root) {*queue=previous;return vec![json!({"status":"blocked","message":format!("无法保存下单队列，未分配购买：{error}")})]}
        for job in &queue.jobs {
            if previous.jobs.iter().find(|before|before.id==job.id).is_some_and(|before|
                before.pickup_policy!=job.pickup_policy || before.status!=job.status&&job.status=="cancelled") {
                self.append_event("queue_policy_changed",&job.context());
            }
            if previous.jobs.iter().find(|before|before.id==job.id).is_some_and(|before|
                before.authorization_confirmed!=job.authorization_confirmed||before.authorized_stores!=job.authorized_stores
                || before.status!=job.status&&job.status=="waiting_stock") {
                self.append_event("queue_authorization_changed",&job.context());
            }
        }
        for outcome in &outcomes {
            if outcome["status"]=="queued" {
                if let Some(job)=queue.jobs.iter().find(|job|Some(job.id.as_str())==outcome["queue_job_id"].as_str()) {self.append_event("queue_enqueued",&job.context());}
            }
        }
        let priority_accounts:HashSet<_>=if !queue.paused {queue.jobs.iter().filter(|job|job.pending()&&job.eligible()&&!queue.blocked_accounts.contains_key(&job.account_id))
            .map(|job|job.account_id.clone()).collect()}else{HashSet::new()};
        drop(queue);
        if !priority_accounts.is_empty() {
            let tasks=self.tasks.lock().unwrap();let controls=self.controls.lock().unwrap();
            for maintenance in tasks.values().filter(|task|priority_accounts.contains(&task.account_id)&&is_preemptible_preheat(task)&&!terminal(&task.status)
                &&task.prompt.as_ref().is_none_or(|prompt|prompt["kind"]!="prepared_purchase")) {
                if let Some(control)=controls.get(&maintenance.id) {let _=control.cancel.send(true);}
            }
        }
        self.ensure_queue_pump(host.clone());outcomes
    }

    fn queue_send(&self, account:&str, task:&str, mut message:Value) -> Result<(),String> {
        message["account_id"]=json!(account);message["task_id"]=json!(task);
        self.controls.lock().unwrap().get(task).ok_or("队列协调器已退出")?.input.try_send(message)
            .map_err(|_|"队列协调器输入繁忙或已关闭".into())
    }

    fn ensure_queue_pump(self:&Arc<Self>,host:TaskHost) {
        let mut runtime=self.queue_runtime.lock().unwrap();runtime.host=Some(host);
        if runtime.pumping || self.queue_stopping.load(Ordering::SeqCst) {return}
        runtime.pumping=true;drop(runtime);
        let engine=self.clone();
        tokio::spawn(async move {
            let mut clock=tokio::time::interval(std::time::Duration::from_millis(200));
            clock.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                clock.tick().await;
                if engine.queue_stopping.load(Ordering::SeqCst) {break}
                engine.queue_pump_once();
            }
        });
    }

    fn queue_account_message(&self,account:&str,coordinator:Option<&Coordinator>) -> Option<String> {
        if !self.accounts.lock().unwrap().iter().any(|item|item.id==account&&!item.pending) {return Some("账号未完成登录验证，任务仍保留排队".into())}
        let tasks=self.tasks.lock().unwrap();
        for task in tasks.values().filter(|task|task.account_id==account) {
            if coordinator.is_some_and(|current|current.task_id==task.id) {continue}
            if !terminal(&task.status) {
                if is_preemptible_preheat(task) {
                    if task.prompt.as_ref().is_some_and(|prompt|prompt["kind"]=="prepared_purchase") {continue}
                    if let Some(control)=self.controls.lock().unwrap().get(&task.id) {let _=control.cancel.send(true);}
                    return Some("已请求停止空闲会话预热，保留队列位置等待账号释放".into());
                }
                return Some("账号正在登录、执行手动或定时任务，队列等待其结束".into())
            }
            if task.order_submitted && (task.result.as_ref().is_none_or(contains_unknown)) {return Some("该账号存在未知订单提交，队列停止自动分配".into())}
            if task.queue_job_ids.is_empty() && unresolved_submission(task,&tasks) {return Some("该账号有待核实订单，请查询官网确认后继续队列".into())}
        }
        if coordinator.is_none() && !tasks.values().any(|task|task.account_id==account&&!terminal(&task.status)
            &&is_preemptible_preheat(task)&&task.prompt.as_ref().is_some_and(|prompt|prompt["kind"]=="prepared_purchase")) && self.locks.lock().unwrap().get(account).is_some_and(|lock|lock.available_permits()==0) {return Some("账号正在收尾，队列等待释放".into())}
        None
    }

    fn queue_pump_once(self:&Arc<Self>) {
        let mut queue=self.purchase_queue.lock().unwrap();
        let mut runtime=self.queue_runtime.lock().unwrap();
        let Some(host)=runtime.host.clone() else{return};
        let mut changed=self.revalidate_pending_pickup_policies(&mut queue);
        if !queue.paused {
            let mut remaining=queue.limit().saturating_sub(queue.active());
            let mut child_remaining=CHILD_SLOTS.saturating_sub(queue.active_child_slots());
            let mut groups:Vec<(String,Vec<usize>)>=vec![];
            for index in 0..queue.jobs.len() {
                if remaining==0 {break}
                let job=&queue.jobs[index];
                if !job.pending() || !job.eligible() {continue}
                let account=job.account_id.clone();
                if queue.jobs.iter().any(|other|other.account_id==account&&other.inflight()) || groups.iter().any(|(owner,_)|owner==&account) {continue}
                let waiting=if let Some(reason)=queue.blocked_accounts.get(&account) {Some(match reason.as_str() {
                    "submission_unknown"=>"该账号存在未知提交，未重放任务",
                    _=>"账号准备未完成，任务保留排队；请核实连接和登录状态后继续",
                }.into())}
                    else if let Some(coordinator)=runtime.coordinators.get(&account) {
                        if coordinator.closing || !coordinator.ready {Some("账号队列协调器正在准备或退出，任务保留排队".into())} else {self.queue_account_message(&account,Some(coordinator))}
                    } else {self.queue_account_message(&account,None)};
                if let Some(message)=waiting {
                    if queue.jobs[index].message!=message {queue.jobs[index].message=message;changed=true;}
                    continue;
                }
                if queue.jobs[index].concurrency>child_remaining {
                    if queue.jobs[index].message!="全局 10 个下单子路数已预留，保留队列位置等待释放" {queue.jobs[index].message="全局 10 个下单子路数已预留，保留队列位置等待释放".into();changed=true;}
                    break;
                }
                if let Some((_,indices))=groups.iter_mut().find(|(key,_)|key==&account) {indices.push(index)} else {groups.push((account,vec![index]));}
                remaining-=1;
                child_remaining=child_remaining.saturating_sub(queue.jobs[index].concurrency);
            }
            for (account,indices) in groups {
                let previous=queue.clone();
                for &index in &indices {queue.jobs[index].status="dispatched".into();queue.jobs[index].dispatched_at_ms=Some(now_ms());queue.jobs[index].assignment=queue.jobs[index].assignment.saturating_add(1);queue.jobs[index].message="已持久化分配，等待执行".into();}
                let targets:Vec<_>=indices.iter().map(|&index|queue.jobs[index].payload()).collect();
                if queue.save(&self.root).is_err() {*queue=previous;queue.paused=true;break}
                if let Some(coordinator)=runtime.coordinators.get_mut(&account) {
                    for &index in &indices {queue.jobs[index].task_id=Some(coordinator.task_id.clone());}
                    if queue.save(&self.root).is_err() {*queue=previous;queue.paused=true;break}
                    if self.queue_send(&account,&coordinator.task_id,json!({"type":"queue_add","queue_targets":targets,
                        "queue_max_concurrency":1,"queue_mode":"sequential"})).is_err() {
                        *queue=previous;for &index in &indices {queue.jobs[index].message="协调器输入繁忙，任务仍保留排队".into();}
                    } else {
                        coordinator.idle=false;
                        let ids:Vec<_>=indices.iter().map(|&index|queue.jobs[index].id.clone()).collect();
                        self.update(&host,&coordinator.task_id,|task|for id in &ids {if !task.queue_job_ids.contains(id) {task.queue_job_ids.push(id.clone());}});
                    }
                } else {
                    let params=json!({"queue_stream":true,"queue_targets":targets,"queue_mode":"sequential",
                        "queue_max_concurrency":1,"concurrency":1,"retry_count":0,"fulfillment":"RETAIL"});
                    match self.start(host.clone(),account.clone(),"purchase".into(),params) {
                        Ok(task)=>{
                            for &index in &indices {queue.jobs[index].task_id=Some(task.clone());}
                            let claimed=self.tasks.lock().unwrap().get(&task).is_some_and(|current|current.prepared_handoff.as_ref().is_some_and(|claim|claim["source"]=="queue"));
                            runtime.coordinators.insert(account,Coordinator {task_id:task,ready:claimed,idle:false,closing:claimed});
                        }
                        Err(message)=>{*queue=previous;for &index in &indices {queue.jobs[index].message=message.clone();}}
                    }
                }
                for &index in &indices {if queue.jobs[index].inflight(){self.append_event("queue_dispatched",&queue.jobs[index].context());}}
                changed=true;
            }
        }
        for (account,coordinator) in &mut runtime.coordinators {
            if coordinator.idle && !coordinator.closing && !queue.jobs.iter().any(|job|(job.inflight()||job.pending()&&job.eligible())&&job.account_id==*account) {
                if self.queue_send(account,&coordinator.task_id,json!({"type":"queue_close"})).is_ok() {coordinator.closing=true;}
            }
        }
        if changed && queue.save(&self.root).is_err() {queue.paused=true;}
    }

    pub(super) fn queue_worker_event(&self,account:&str,task:&str,event:&Value)->bool {
        let mut queue=self.purchase_queue.lock().unwrap();
        let mut runtime=self.queue_runtime.lock().unwrap();
        if runtime.coordinators.get(account).is_none_or(|coordinator|coordinator.task_id!=task) {return false}
        let kind=event["type"].as_str().unwrap_or("");
        // Stage text belongs to one assigned target, never the whole coordinator.
        // Keep this presentation update separate from result/dispatch/stop handling.
        if kind=="stage" {
            let Some(message)=event["message"].as_str().map(str::trim).filter(|message|!message.is_empty()) else {return false};
            // Coordinator preflight has no target identity; target stages need the exact current assignment.
            if event.get("queue_job_id").is_none() {return event.get("queue_assignment").is_none()}
            let Some(id)=event["queue_job_id"].as_str() else {return false};
            let Some(assignment)=event["queue_assignment"].as_u64() else {return false};
            let Some(job)=queue.jobs.iter_mut().find(|job|job.id==id&&job.account_id==account
                &&job.task_id.as_deref()==Some(task)&&job.assignment==assignment
                &&matches!(job.status.as_str(),"dispatched"|"running")) else {return false};
            if event.get("purchase_group_id").is_some_and(|value|!value.is_null()&&value.as_str()!=job.purchase_group_id.as_deref()) {return false}
            if event["order_submitted"]==true {job.order_submitted=true;if job.submit_started_at_ms.is_none(){job.submit_started_at_ms=Some(now_ms());}}
            let message:String=message.chars().take(2000).collect();
            if job.message!=message || event["order_submitted"]==true {
                job.message=message;
                if queue.save(&self.root).is_err() {queue.paused=true;}
            }
            return true;
        }
        let mut accepted=matches!(kind,"queue_ready"|"queue_idle");
        if kind=="queue_ready" {if let Some(coordinator)=runtime.coordinators.get_mut(account){coordinator.ready=true;}}
        if kind=="queue_idle" {if let Some(coordinator)=runtime.coordinators.get_mut(account){coordinator.idle=true;}}
        if let Some(id)=event["queue_job_id"].as_str() {
            if let Some(index)=queue.jobs.iter().position(|job|job.id==id&&job.account_id==account&&job.task_id.as_deref()==Some(task)) {
                let assignment=event["queue_assignment"].as_u64().or_else(||event["result"]["queue_assignment"].as_u64());
                if assignment!=Some(queue.jobs[index].assignment) {return false}
                if event.get("purchase_group_id").is_some_and(|value|!value.is_null()&&value.as_str()!=queue.jobs[index].purchase_group_id.as_deref()) {return false}
                if queue.jobs[index].inflight() {
                    accepted=matches!(kind,"queue_started"|"queue_progress"|"queue_finished");
                    if kind=="queue_started" && matches!(queue.jobs[index].status.as_str(),"dispatched"|"running") && queue.jobs[index].started_at_ms.is_none() {
                        queue.jobs[index].started_at_ms=Some(now_ms());
                    }
                    if kind=="queue_started" {queue.jobs[index].status="running".into();queue.jobs[index].message="正在执行此门店下单".into();}
                    if matches!(kind,"queue_progress"|"queue_finished") {
                        let mut result=event["result"].clone();remove_private_payment_links(&mut result);
                        if queue.jobs[index].order_confirmed_at_ms.is_none() {
                            queue.jobs[index].order_confirmed_at_ms=queue_order_confirmation_at(&result,queue.jobs[index].enqueued_at_ms,now_ms());
                        }
                        let unknown=contains_unknown(&result) || ((queue.jobs[index].order_submitted || event["order_submitted"]==true) && !contains_known_order(&result));
                        if result["order_submitted"]==true || event["order_submitted"]==true {queue.jobs[index].order_submitted=true;if queue.jobs[index].submit_started_at_ms.is_none(){queue.jobs[index].submit_started_at_ms=Some(now_ms());}}
                        if kind=="queue_finished" {
                            let status=result["status"].as_str().unwrap_or("");
                            if unknown {
                                queue.jobs[index].status="submission_unknown".into();queue.jobs[index].message="订单提交结果未知，未重放并已阻止该账号后续分配".into();
                                queue.blocked_accounts.insert(account.into(),"submission_unknown".into());
                            } else if status=="paused" && result["started"]==false {
                                queue.jobs[index].task_id=None;
                                if result["pause_reason"]=="inventory_stale" {
                                    queue.jobs[index].status="queued".into();
                                    queue.jobs[index].message="已收到有货信号，继续按原排队位置执行".into();
                                } else {
                                    // The worker already owns automatic authentication and its
                                    // bounded retry budget. Finish this allocation on failure;
                                    // returning it to queued would create an unbounded retry loop.
                                    queue.jobs[index].status="failed".into();
                                    queue.jobs[index].message=result["message"].as_str().unwrap_or("本次账号准备未完成，下单失败").chars().take(2000).collect();
                                }
                            } else {
                                queue.jobs[index].status=if status=="cancelled" {"cancelled"} else if result["success"]==true || status=="pending_payment" || status=="succeeded" {"succeeded"} else {"failed"}.into();
                                queue.jobs[index].message=result["message"].as_str().unwrap_or(if queue.jobs[index].status=="succeeded" {"本次下单完成，请核对付款状态"} else {"本次下单未成功"}).chars().take(2000).collect();
                            }
                        }
                        if let Some(group_id)=queue.jobs[index].purchase_group_id.clone() {
                            if unknown || contains_known_order(&result) {
                                let group_status=if unknown {"submission_unknown"} else {"succeeded"};
                                for group in queue.purchase_groups.values_mut().filter(|group|group.id==group_id) {group.status=group_status.into();}
                                if unknown {queue.blocked_accounts.insert(account.into(),"submission_unknown".into());}
                                else {
                                    for sibling in queue.jobs.iter_mut().filter(|job|job.pending()&&job.purchase_group_id.as_deref()==Some(&group_id)) {
                                        sibling.status="cancelled".into();sibling.authorization_confirmed=false;sibling.signal_confirmed=false;
                                        sibling.message="同一购买意图已有确认订单，已取消未执行门店".into();
                                    }
                                }
                            }
                        }
                        queue.jobs[index].result=Some(result);
                    }
                    if matches!(kind,"queue_started"|"queue_finished") {self.append_event(kind,&queue.jobs[index].context());}
                }
            }
        }
        if queue.blocked_accounts.contains_key(account) {
            if let Some(coordinator)=runtime.coordinators.get_mut(account) {
                if !coordinator.closing && self.queue_send(account,task,json!({"type":"queue_close"})).is_ok() {coordinator.closing=true;}
            }
        }
        queue.prune();if queue.save(&self.root).is_err(){queue.paused=true;}
        accepted
    }

    pub(super) fn queue_worker_ended(&self,account:&str,task:&str) {
        let mut queue=self.purchase_queue.lock().unwrap();
        let mut runtime=self.queue_runtime.lock().unwrap();
        if runtime.coordinators.get(account).is_none_or(|coordinator|coordinator.task_id!=task) {return}
        let parent=self.tasks.lock().unwrap().get(task).cloned();
        for job in queue.jobs.iter_mut().filter(|job|job.account_id==account&&job.task_id.as_deref()==Some(task)&&job.inflight()) {
            job.status="interrupted".into();job.message="协调器已退出但未收到此任务的完成确认；未自动重放，请先核实官网订单".into();
            if parent.as_ref().is_some_and(|task|task.order_submitted) || job.order_submitted {job.order_submitted=true;}
        }
        if queue.jobs.iter().any(|job|job.account_id==account&&job.task_id.as_deref()==Some(task)&&job.status=="interrupted") {
            queue.blocked_accounts.insert(account.into(),"submission_unknown".into());
        }
        runtime.coordinators.remove(account);
        queue.prune();if queue.save(&self.root).is_err(){queue.paused=true;}
    }

    pub(super) fn queue_pause_for_shutdown(&self) {
        let mut queue=self.purchase_queue.lock().unwrap();
        if queue.resume_after_restart.is_none() {queue.resume_after_restart=Some(!queue.paused);}
        queue.paused=true;let _=queue.save(&self.root);
    }

    pub(super) fn queue_restore_running_intent(self:&Arc<Self>, host:TaskHost)->Result<(),String> {
        let mut queue=self.purchase_queue.lock().unwrap();
        if queue.resume_after_restart!=Some(true) {return Ok(())}
        // Loading already invalidated every pending sample and blocked interrupted
        // assignments. Restoring dispatch intent cannot replay those purchases.
        queue.paused=false;
        if let Err(error)=queue.save(&self.root) {queue.paused=true;return Err(error)}
        drop(queue);
        self.ensure_queue_pump(host);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (tempfile::TempDir,Arc<Engine>,TaskHost,String) {
        let root=tempfile::tempdir().unwrap();let engine=Engine::new(root.path().into()).unwrap();
        engine.queue_stopping.store(true,Ordering::SeqCst); // Drive the dispatcher manually, never launch a worker.
        let account=engine.add_account("queue@example.test".into()).unwrap();engine.accounts.lock().unwrap()[0].pending=false;
        let host=TaskHost {worker:root.path().join("not-a-worker"),script:None,on_update:Arc::new(|_|{})};
        (root,engine,host,account.id)
    }
    fn target(account:&str,store:&str,at:u64)->Value {
        json!({"id":format!("cloth-{store}"),"account_id":account,"sku":"MLH74FE/A","quantity":1,
            "store_id":store,"store_name":format!("门店 {store}"),"location":"200000","auto_purchase":true,
            "status":"available","checked_at_ms":at})
    }
    fn observe(engine:&Arc<Engine>,host:&TaskHost,targets:Vec<Value>)->Vec<Value> {engine.queue_inventory(host,&json!({"targets":targets}))}
    fn jobs(engine:&Engine)->Vec<QueueJob> {engine.purchase_queue.lock().unwrap().jobs.clone()}
    fn ack(engine:&Engine,job:&QueueJob,kind:&str,result:Value) {
        engine.queue_worker_event(&job.account_id,job.task_id.as_deref().unwrap(),&json!({"type":kind,"queue_job_id":job.id,"queue_assignment":job.assignment,"result":result}));
    }
    fn ready(engine:&Engine,job:&QueueJob) {
        engine.queue_worker_event(&job.account_id,job.task_id.as_deref().unwrap(),&json!({"type":"queue_ready"}));
    }

    fn priority(stores:&[&str])->Value {
        json!({"selection_mode":"priority","stores":stores.iter().map(|id|json!({"store_id":id,"store_name":format!("门店 {id}"),"search":"200000"})).collect::<Vec<_>>(),
            "time_slot_rule":{"mode":"window","start":"09:00","end":"12:00","fallback":"stop"},"contact_mode":"official"})
    }
    fn save_policy(root:&Path,account:&str,policy:Value) {
        private_json(&scope_dir(root,account).join("checkout.json"),&json!({"pickup":policy,"contact":{"firstName":"private-fixture-only"},"signKey":"private-sign-fixture"})).unwrap();
    }
    fn negative(mut row:Value)->Value {row["status"]=json!("unavailable");row}

    #[tokio::test]
    async fn schedule_only_settings_save_preserves_pending_purchase_signals() {
        let (_root,engine,host,account)=fixture();
        let signal=target(&account,"R001",now_ms());
        observe(&engine,&host,vec![signal.clone()]);
        let before=serde_json::to_value(&*engine.purchase_queue.lock().unwrap()).unwrap();
        engine.queue_inventory_settings_changed(Some(&json!({"targets":[signal],
            "schedule":{"enabled":true,"mode":"exclude","start":"01:00","end":"05:00"}})),false);
        assert_eq!(serde_json::to_value(&*engine.purchase_queue.lock().unwrap()).unwrap(),before);
        engine.queue_inventory_settings_changed(None,true);
        assert!(!jobs(&engine)[0].signal_confirmed);
        assert_eq!(jobs(&engine)[0].status,"waiting_stock");
    }

    #[tokio::test]
    async fn scheduled_monitor_pause_keeps_accepted_jobs_authorized_and_executable() {
        for priority_pool in [false,true] {
            let (root,engine,host,account)=fixture();
            if priority_pool {save_policy(root.path(),&account,priority(&["R001","R002"]));}
            observe(&engine,&host,vec![target(&account,"R001",now_ms())]);
            let previous=serde_json::to_value(&*engine.purchase_queue.lock().unwrap()).unwrap();
            let disk=fs::read(root.path().join("settings/purchase_queue.json")).unwrap();
            assert!(engine.inventory_dispatch(&host,&json!({"monitoring":true,
                "schedule_state":{"paused":true,"reason":"excluded_window"},"targets":[]})).is_empty());
            assert_eq!(serde_json::to_value(&*engine.purchase_queue.lock().unwrap()).unwrap(),previous);
            assert_eq!(fs::read(root.path().join("settings/purchase_queue.json")).unwrap(),disk);
            engine.queue_pump_once();
            let current=jobs(&engine);
            assert_eq!(current.len(),1);
            assert_eq!(current[0].status,"dispatched");
            assert!(current[0].authorization_confirmed);
        }
    }

    #[tokio::test]
    async fn scheduled_monitor_pause_cannot_enqueue_targets_even_with_available_payload() {
        let (_root,engine,host,account)=fixture();
        assert!(engine.inventory_dispatch(&host,&json!({"monitoring":true,
            "schedule_state":{"paused":true,"reason":"outside_window"},
            "targets":[target(&account,"R001",now_ms())]})).is_empty());
        assert!(jobs(&engine).is_empty());
        observe(&engine,&host,vec![target(&account,"R001",now_ms())]);
        assert_eq!(jobs(&engine).len(),1);
    }

    #[tokio::test]
    async fn priority_stores_enqueue_separately_with_shared_group_and_frozen_single_store() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R002","R001","R003"]));
        let at=now_ms();let mut waiting=target(&account,"R003",at);waiting["status"]=json!("unknown");waiting["checked_at_ms"]=Value::Null;
        observe(&engine,&host,vec![target(&account,"R001",at),target(&account,"R002",at),waiting]);
        let first=jobs(&engine);assert_eq!(first.len(),2);assert_eq!(first[0].store_id,"R002");assert_eq!(first[1].store_id,"R001");
        assert_eq!(first[0].purchase_group_id,first[1].purchase_group_id);assert!(first[0].purchase_group_id.is_some());
        for job in &first {let payload=job.payload();assert_eq!(payload["pickup_policy"]["stores"].as_array().unwrap().len(),1);assert_eq!(payload["pickup_policy"]["stores"][0]["store_id"],job.store_id);}
        observe(&engine,&host,vec![target(&account,"R001",at),target(&account,"R002",at),target(&account,"R003",at-1)]);
        let after=jobs(&engine);assert_eq!(after.len(),3);assert_eq!(after[0].store_id,"R002");assert_eq!(after[2].store_id,"R003");
        assert!(!serde_json::to_string(&after).unwrap().contains("private-fixture"));
    }

    #[tokio::test]
    async fn priority_confirmed_order_ends_group_and_requires_explicit_reauthorization() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        let at=now_ms();observe(&engine,&host,vec![target(&account,"R001",at),target(&account,"R002",at)]);engine.queue_pump_once();
        let first=jobs(&engine);ready(&engine,&first[0]);
        ack(&engine,&first[0],"queue_finished",json!({"status":"pending_payment","success":true,"order_submitted":true,"order_number":"W111111"}));
        let done=jobs(&engine);assert_eq!(done[0].status,"succeeded");assert_eq!(done[1].status,"cancelled");
        observe(&engine,&host,vec![negative(target(&account,"R001",now_ms())),negative(target(&account,"R002",now_ms()))]);
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);assert_eq!(jobs(&engine).len(),2);
        let mut a=target(&account,"R001",now_ms());let mut b=target(&account,"R002",now_ms());a["auto_purchase"]=json!(false);b["auto_purchase"]=json!(false);
        observe(&engine,&host,vec![a,b]);observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        let reauthorized=jobs(&engine);assert_eq!(reauthorized.len(),4);assert_ne!(reauthorized[2].purchase_group_id,first[0].purchase_group_id);
    }

    #[tokio::test]
    async fn stopped_monitor_saves_capture_explicit_reauthorization_without_polling() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001"]));
        let active=target(&account,"R001",now_ms());observe(&engine,&host,vec![active.clone()]);engine.queue_pump_once();let first=jobs(&engine)[0].clone();ready(&engine,&first);
        ack(&engine,&first,"queue_finished",json!({"status":"pending_payment","success":true,"order_submitted":true,"order_number":"W111111"}));
        let same=json!({"targets":[active.clone()]});engine.queue_inventory_settings_changed(Some(&same),true);
        observe(&engine,&host,vec![active.clone()]);assert_eq!(jobs(&engine).len(),1);
        let mut disabled=active.clone();disabled["auto_purchase"]=json!(false);
        engine.queue_inventory_settings_changed(Some(&json!({"targets":[disabled]})),true);
        engine.queue_inventory_settings_changed(Some(&same),true);
        let loaded=QueueState::load(root.path(),&HashMap::new()).unwrap();assert!(!loaded.purchase_groups[&priority_key(&account,&first.sku,1)].authorization_active);
        observe(&engine,&host,vec![active]);let after=jobs(&engine);assert_eq!(after.len(),2);assert_ne!(after[1].purchase_group_id,first.purchase_group_id);
        assert_eq!(after[0].status,"succeeded");
    }

    #[tokio::test]
    async fn priority_conflicts_are_visible_and_never_reserve_or_expand_execution() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        for field in ["retry_count"] {
            let a=target(&account,"R001",now_ms());let mut b=target(&account,"R002",now_ms());b[field]=json!(2);
            let outcomes=observe(&engine,&host,vec![a,b]);assert_eq!(outcomes[0]["status"],"blocked");
            assert!(jobs(&engine).is_empty());let state=engine.queue_command(host.clone(),"status",&json!({})).unwrap();
            assert_eq!(state["policy_errors"].as_array().unwrap().len(),1);assert!(state["policy_errors"][0]["message"].as_str().unwrap().contains("不一致"));
        }
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        assert_eq!(jobs(&engine)[0].concurrency,1);assert_eq!(jobs(&engine)[0].retry_count,0);
        assert!(engine.queue_command(host.clone(),"status",&json!({})).unwrap()["policy_errors"].as_array().unwrap().is_empty());
        let (root,other,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        let mut a=target(&account,"R001",now_ms());let mut b=target(&account,"R002",now_ms());a["concurrency"]=json!(3);b["concurrency"]=json!(5);
        observe(&other,&host,vec![a,b]);let accepted=jobs(&other);assert_eq!(accepted.len(),2);
        assert!(accepted.iter().all(|job|job.concurrency==1));assert_eq!(accepted[0].requested_concurrency,Some(3));assert_eq!(accepted[1].requested_concurrency,Some(5));
    }

    #[tokio::test]
    async fn priority_execution_conflict_still_blocks_an_accepted_signal_during_negative_stock() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        let a=negative(target(&account,"R001",now_ms()));let mut b=negative(target(&account,"R002",now_ms()));b["retry_count"]=json!(2);
        let outcomes=observe(&engine,&host,vec![a,b]);assert_eq!(outcomes[0]["status"],"blocked");
        engine.queue_pump_once();assert!(engine.snapshot().tasks.is_empty());assert!(!jobs(&engine)[0].eligible());
        assert!(jobs(&engine)[0].message.contains("不一致"));
    }

    #[tokio::test]
    async fn priority_groups_distinct_quantities_and_preserves_explicit_execution_values() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        let mut targets=vec![];
        for quantity in [1,2] {for store in ["R001","R002"] {
            let mut row=target(&account,store,now_ms());row["quantity"]=json!(quantity);row["concurrency"]=json!(3);row["retry_count"]=json!(2);targets.push(row);
        }}
        let outcomes=observe(&engine,&host,targets);assert_eq!(outcomes.len(),4);assert_eq!(jobs(&engine).len(),4);
        assert!(jobs(&engine).iter().all(|job|job.concurrency==1&&job.requested_concurrency==Some(3)&&job.retry_count==2));
        assert_ne!(jobs(&engine)[0].episode_key,jobs(&engine)[1].episode_key);
        assert_eq!(jobs(&engine)[0].payload()["retry_count"],2);
        engine.queue_inventory_settings_changed(None,true);assert!(jobs(&engine).iter().all(|job|job.status=="waiting_stock"&&job.checked_at_ms==0));
    }

    #[tokio::test]
    async fn priority_authorization_scope_change_creates_explicit_new_intent_without_reviving_old_jobs() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);let initial=jobs(&engine);
        save_policy(root.path(),&account,priority(&["R003","R002"]));
        observe(&engine,&host,vec![target(&account,"R003",now_ms()),target(&account,"R002",now_ms())]);let after=jobs(&engine);
        assert_eq!(after.len(),4);assert!(after[..2].iter().all(|job|job.status=="cancelled"));
        assert_ne!(after[2].purchase_group_id,initial[0].purchase_group_id);
        for job in after.iter().skip(2) {assert_eq!(job.payload()["pickup_policy"]["stores"].as_array().unwrap().len(),1);}
        observe(&engine,&host,vec![]);assert!(jobs(&engine).iter().skip(2).all(|job|!job.eligible()));
    }

    #[tokio::test]
    async fn priority_pump_rechecks_each_store_and_bad_configuration_blocks_pending() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        let mut bad=priority(&["R002"]);bad["contact_mode"]=json!(true);save_policy(root.path(),&account,bad);
        engine.queue_pump_once();assert!(jobs(&engine).iter().all(|job|job.status=="waiting_stock"));assert!(engine.snapshot().tasks.is_empty());
        save_policy(root.path(),&account,priority(&["R002"]));engine.queue_pump_once();assert!(jobs(&engine).iter().all(|job|!job.eligible()));
    }

    #[tokio::test]
    async fn fixed_to_priority_migration_retains_pending_without_competing_pool_intent() {
        let (root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        assert_eq!(jobs(&engine).len(),2);engine.queue_pump_once();assert!(jobs(&engine)[0].inflight());
        save_policy(root.path(),&account,priority(&["R001","R002"]));let outcomes=observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        assert_eq!(outcomes[0]["status"],"blocked");assert!(jobs(&engine)[0].inflight());assert_eq!(jobs(&engine)[1].status,"waiting_stock");assert!(!jobs(&engine)[1].authorization_confirmed);assert_eq!(jobs(&engine).len(),2);
    }

    #[test]
    fn priority_policy_read_is_bounded_strict_and_does_not_copy_contact_fields() {
        let (root,_,_,account)=fixture();let mut policy=priority(&["R001"]);policy["id_last4"]=json!("AB12");save_policy(root.path(),&account,policy);
        assert!(read_account_priority(root.path(),&account).unwrap().unwrap().get("id_last4").is_none());
        for field in ["selection_mode","contact_mode"] {let mut bad=priority(&["R001"]);bad[field]=json!(1);save_policy(root.path(),&account,bad);assert!(read_account_priority(root.path(),&account).is_err());}
        let mut bad=priority(&["R001"]);bad["time_slot_rule"]["mode"]=json!(1);save_policy(root.path(),&account,bad);assert!(read_account_priority(root.path(),&account).is_err());
        fs::write(scope_dir(root.path(),&account).join("checkout.json"),vec![b' ';262_145]).unwrap();assert!(read_account_priority(root.path(),&account).is_err());
    }

    #[tokio::test]
    async fn fifo_uses_actual_enqueue_time_and_stable_batch_sequence() {
        let (_root,engine,host,account)=fixture();let at=now_ms();
        observe(&engine,&host,vec![target(&account,"R392",at),target(&account,"R390",at-1),target(&account,"R391",at)]);
        let queue=jobs(&engine);
        assert_eq!(queue.iter().map(|job|job.store_id.as_str()).collect::<Vec<_>>(),vec!["R392","R390","R391"]);
        assert_eq!(queue.iter().map(|job|job.sequence).collect::<Vec<_>>(),vec![1,2,3]);
        engine.queue_pump_once();
        let queue=jobs(&engine);assert_eq!(queue.iter().filter(|job|job.inflight()).count(),1);
        assert_eq!(queue[0].status,"dispatched");assert_eq!(queue[1].status,"queued");
        assert_eq!(queue[0].payload()["queue_assignment"],1);
        assert_eq!(queue[0].payload()["concurrency"],1);assert_eq!(queue[0].payload()["retry_count"],0);
    }

    #[tokio::test]
    async fn same_account_jobs_serialize_bag_mutation_while_other_accounts_run_in_parallel() {
        let (_root,engine,host,account)=fixture();let other=engine.add_account("parallel@example.test".into()).unwrap();engine.accounts.lock().unwrap()[1].pending=false;
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":3})).unwrap();
        let at=now_ms();observe(&engine,&host,vec![target(&account,"R001",at),target(&account,"R002",at),target(&other.id,"R003",at)]);engine.queue_pump_once();
        let first=jobs(&engine);assert_eq!(first.iter().filter(|job|job.inflight()).count(),2);assert!(first[0].inflight());assert!(first[1].pending());assert!(first[2].inflight());
        ready(&engine,&first[0]);ack(&engine,&first[0],"queue_finished",json!({"success":false,"status":"failed"}));engine.queue_pump_once();
        let next=jobs(&engine);assert!(next[1].inflight());assert_eq!(next[1].task_id,first[0].task_id);
    }

    #[tokio::test]
    async fn sequential_known_pending_order_does_not_drop_authorized_next_store() {
        let (_root,engine,host,account)=fixture();let at=now_ms();
        observe(&engine,&host,vec![target(&account,"R390",at),target(&account,"R391",at)]);engine.queue_pump_once();
        let first=jobs(&engine);ready(&engine,&first[0]);
        ack(&engine,&first[0],"queue_finished",json!({"success":true,"status":"pending_payment","order_submitted":true,"order_number":"W111111"}));
        let task_id=first[0].task_id.clone().unwrap();
        {let mut tasks=engine.tasks.lock().unwrap();let task=tasks.get_mut(&task_id).unwrap();task.order_submitted=true;task.status="running".into();}
        engine.queue_pump_once();let after=jobs(&engine);
        assert_eq!(after[0].status,"succeeded");assert_eq!(after[1].status,"dispatched");assert_eq!(after[1].task_id,Some(task_id));
        assert_eq!(after.iter().filter(|job|job.inflight()).count(),1);
    }

    #[tokio::test]
    async fn busy_account_keeps_fifo_position_while_other_account_uses_free_capacity() {
        let (_root,engine,host,account)=fixture();
        let second=engine.add_account("second@example.test".into()).unwrap();engine.accounts.lock().unwrap()[1].pending=false;
        engine.start(host.clone(),account.clone(),"me".into(),json!({})).unwrap();
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        let at=now_ms();observe(&engine,&host,vec![target(&account,"R390",at),target(&second.id,"R391",at+1)]);
        engine.queue_pump_once();let after=jobs(&engine);
        assert_eq!(after[0].status,"queued");assert!(after[0].message.contains("队列等待"));
        assert_eq!(after[1].status,"dispatched");assert_eq!(after[0].sequence,1);
        for task in engine.tasks.lock().unwrap().values_mut().filter(|task|task.account_id==account) {task.status="succeeded".into();}
        engine.queue_pump_once();let after=jobs(&engine);
        assert_eq!(after[0].status,"dispatched");assert_eq!(after.iter().filter(|job|job.inflight()).count(),2);
    }

    #[tokio::test]
    async fn legacy_priority_pool_migrates_to_one_store_without_replaying_inflight_or_failed_work() {
        for previous in ["queued","failed","running"] {
            let (root,engine,host,account)=fixture();let policy=priority(&["R001","R002"]);save_policy(root.path(),&account,policy.clone());
            observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
            let mut disk=serde_json::to_value(&*engine.purchase_queue.lock().unwrap()).unwrap();
            disk.as_object_mut().unwrap().remove("purchase_groups");disk["jobs"].as_array_mut().unwrap().truncate(1);
            disk["jobs"][0].as_object_mut().unwrap().remove("purchase_group_id");disk["jobs"][0]["pickup_policy"]=policy;
            disk["jobs"][0]["status"]=json!(previous);disk["jobs"][0]["episode_key"]=json!(priority_key(&account,"MLH74FE",1));
            private_json(&root.path().join("settings/purchase_queue.json"),&disk).unwrap();
            let loaded=QueueState::load(root.path(),&HashMap::new()).unwrap();let job=&loaded.jobs[0];
            assert!(job.purchase_group_id.is_some());assert_eq!(job.pickup_policy.as_ref().unwrap()["stores"].as_array().unwrap().len(),1);
            assert!(!job.eligible());assert_eq!(job.status,match previous {"running"=>"interrupted","queued"=>"waiting_stock",_=>"failed"});
            if previous=="running" {assert_eq!(loaded.blocked_accounts[&account],"submission_unknown");}
        }
    }

    #[tokio::test]
    async fn legacy_failed_history_does_not_merge_into_a_later_pending_purchase_intent() {
        let (root,engine,host,account)=fixture();let policy=priority(&["R001","R002"]);save_policy(root.path(),&account,policy.clone());
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        let mut disk=serde_json::to_value(&*engine.purchase_queue.lock().unwrap()).unwrap();disk.as_object_mut().unwrap().remove("purchase_groups");
        for (index,job) in disk["jobs"].as_array_mut().unwrap().iter_mut().enumerate() {
            job.as_object_mut().unwrap().remove("purchase_group_id");job["pickup_policy"]=policy.clone();job["episode_key"]=json!(priority_key(&account,"MLH74FE",1));
            job["status"]=json!(if index==0 {"failed"}else {"queued"});
        }
        private_json(&root.path().join("settings/purchase_queue.json"),&disk).unwrap();let loaded=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert_eq!(loaded.jobs[0].status,"failed");assert_eq!(loaded.jobs[1].status,"waiting_stock");assert_ne!(loaded.jobs[0].purchase_group_id,loaded.jobs[1].purchase_group_id);
        assert_eq!(loaded.purchase_groups[&priority_key(&account,"MLH74FE",1)].status,"active");
    }

    #[tokio::test]
    async fn failed_store_finishes_once_then_next_store_executes_and_cancel_all_ends_unseen_alternatives() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002","R003"]));
        let mut unseen=target(&account,"R003",now_ms());unseen["status"]=json!("unknown");unseen["checked_at_ms"]=Value::Null;
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms()),unseen.clone()]);engine.queue_pump_once();
        let first=jobs(&engine)[0].clone();ready(&engine,&first);ack(&engine,&first,"queue_finished",json!({"status":"failed","success":false,"message":"官网确认无货"}));engine.queue_pump_once();
        let after=jobs(&engine);assert_eq!(after[0].status,"failed");assert!(after[1].inflight());assert_eq!(after[0].purchase_group_id,after[1].purchase_group_id);
        ack(&engine,&after[1],"queue_finished",json!({"status":"failed","success":false}));
        engine.queue_command(host.clone(),"cancel",&json!({"all":true})).unwrap();
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms()),target(&account,"R003",now_ms())]);
        assert_eq!(jobs(&engine).len(),2);assert!(jobs(&engine).iter().all(|job|job.status=="failed"));
    }

    #[tokio::test]
    async fn group_unknown_submission_blocks_account_and_rejects_mismatched_group_ack() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);engine.queue_pump_once();let first=jobs(&engine)[0].clone();
        assert!(!engine.queue_worker_event(&account,first.task_id.as_deref().unwrap(),&json!({"type":"queue_started","queue_job_id":first.id,"queue_assignment":first.assignment,"purchase_group_id":Uuid::new_v4().to_string()})));
        ready(&engine,&first);ack(&engine,&first,"queue_finished",json!({"status":"submission_unknown","order_submitted":true}));
        engine.queue_pump_once();let after=jobs(&engine);assert_eq!(after[0].status,"submission_unknown");assert!(after[1].pending());assert!(!after[1].inflight());
        assert_eq!(engine.purchase_queue.lock().unwrap().blocked_accounts[&account],"submission_unknown");
    }

    #[tokio::test]
    async fn priority_each_store_needs_one_fresh_signal_and_failed_attempts_never_rearm() {
        let (root,engine,host,account)=fixture();let at=now_ms();save_policy(root.path(),&account,priority(&["R001","R002"]));
        observe(&engine,&host,vec![target(&account,"R001",at-20_000),target(&account,"R002",at)]);
        assert_eq!(jobs(&engine).len(),1);assert_eq!(jobs(&engine)[0].store_id,"R002");
        engine.purchase_queue.lock().unwrap().jobs[0].status="failed".into();
        observe(&engine,&host,vec![negative(target(&account,"R001",at)),negative(target(&account,"R002",at))]);
        observe(&engine,&host,vec![target(&account,"R001",at),target(&account,"R002",at)]);
        let after=jobs(&engine);assert_eq!(after.len(),2);assert_eq!(after[0].status,"failed");assert_eq!(after[1].store_id,"R001");
        assert_eq!(after[0].purchase_group_id,after[1].purchase_group_id);
    }

    #[tokio::test]
    async fn old_stock_does_not_rearm_or_enqueue_new_intents() {
        let (_root,engine,host,account)=fixture();let at=now_ms();
        engine.queue_command(host.clone(),"configure",&json!({"mode":"sequential","max_concurrency":1,"check_inventory_freshness":false})).unwrap();
        observe(&engine,&host,vec![target(&account,"R001",at)]);
        {let mut queue=engine.purchase_queue.lock().unwrap();queue.jobs[0].status="succeeded".into();}
        observe(&engine,&host,vec![negative(target(&account,"R001",at-20_000))]);
        observe(&engine,&host,vec![target(&account,"R001",at)]);
        assert_eq!(jobs(&engine).len(),1);
        observe(&engine,&host,vec![target(&account,"R002",at-20_000)]);
        assert_eq!(jobs(&engine).len(),1);
        observe(&engine,&host,vec![negative(target(&account,"R001",now_ms()))]);
        observe(&engine,&host,vec![target(&account,"R001",now_ms())]);
        assert_eq!(jobs(&engine).len(),2);
    }

    #[tokio::test]
    async fn accepted_signal_keeps_fifo_when_older_than_ten_seconds() {
        let (_root,engine,host,account)=fixture();let at=now_ms();
        observe(&engine,&host,vec![target(&account,"R001",at),target(&account,"R002",at+1)]);
        let before=jobs(&engine);
        engine.purchase_queue.lock().unwrap().jobs[0].checked_at_ms=at-20_000;
        engine.queue_pump_once();let after=jobs(&engine);
        assert_eq!(after[0].status,"dispatched");assert_eq!(after[0].id,before[0].id);
        assert_eq!(after[0].sequence,before[0].sequence);assert_eq!(after[1].status,"queued");
    }

    #[tokio::test]
    async fn legacy_freshness_configuration_is_ignored_and_removed_from_persistence() {
        let (root,engine,host,_account)=fixture();
        for legacy in [json!(true),json!(false),json!("obsolete")] {
            let view=engine.queue_command(host.clone(),"configure",&json!({"mode":"sequential","max_concurrency":1,"check_inventory_freshness":legacy})).unwrap();
            assert!(view.get("check_inventory_freshness").is_none());
        }
        let mut persisted:Value=serde_json::from_slice(&fs::read(root.path().join("settings/purchase_queue.json")).unwrap()).unwrap();
        assert!(persisted.get("check_inventory_freshness").is_none());
        persisted["check_inventory_freshness"]=json!(true);
        let legacy:QueueState=serde_json::from_value(persisted).unwrap();
        assert!(serde_json::to_value(legacy).unwrap().get("check_inventory_freshness").is_none());
    }

    #[tokio::test]
    async fn subsequent_negative_unknown_and_stale_samples_do_not_gate_fixed_or_priority_intents() {
        for pool in [false,true] {for status in ["unavailable","unknown","available"] {
            let (root,engine,host,account)=fixture();
            if pool {save_policy(root.path(),&account,priority(&["R390","R391"]));}
            let mut signal=target(&account,"R390",now_ms());signal["retry_count"]=json!(2);
            observe(&engine,&host,vec![signal]);let initial=jobs(&engine)[0].clone();
            let mut next=target(&account,"R390",if status=="available" {now_ms()-20_000}else {now_ms()});next["status"]=json!(status);
            if status=="unknown" {next["checked_at_ms"]=Value::Null;}
            observe(&engine,&host,vec![next]);
            let pending=jobs(&engine)[0].clone();assert_eq!(pending.status,"queued");assert!(pending.eligible());
            assert_eq!(pending.id,initial.id);assert_eq!(pending.sequence,initial.sequence);
            assert_eq!(pending.checked_at_ms,initial.checked_at_ms);assert_eq!(pending.store_id,initial.store_id);
            if status=="unavailable" {
                let mut unknown=target(&account,"R390",now_ms());unknown["status"]=json!("unknown");unknown["checked_at_ms"]=Value::Null;
                observe(&engine,&host,vec![unknown]);assert!(jobs(&engine)[0].eligible());assert_eq!(jobs(&engine)[0].id,initial.id);
            }
            engine.queue_pump_once();let dispatched=jobs(&engine)[0].clone();assert_eq!(dispatched.status,"dispatched");
            assert_eq!(dispatched.payload()["retry_count"],2);assert!(dispatched.payload().get("check_inventory_freshness").is_none());
            ready(&engine,&dispatched);
            ack(&engine,&dispatched,"queue_finished",json!({"status":"failed","success":false,"order_submitted":false,"message":"官网结账：所选门店已无货","retry_summary":{"attempts":3,"retry_count":2}}));
            let failed=jobs(&engine)[0].clone();assert_eq!(failed.status,"failed");assert_eq!(failed.message,"官网结账：所选门店已无货");
            assert_eq!(failed.result.as_ref().unwrap()["retry_summary"]["attempts"],3);
            let mut renewed=target(&account,"R390",now_ms());renewed["retry_count"]=json!(2);
            observe(&engine,&host,vec![renewed.clone()]);assert_eq!(jobs(&engine).len(),1);
            observe(&engine,&host,vec![negative(target(&account,"R390",now_ms()))]);
            observe(&engine,&host,vec![renewed]);assert_eq!(jobs(&engine).len(),if pool {1}else {2});
            assert_eq!(jobs(&engine)[0].message,"官网结账：所选门店已无货");
        }}
    }

    #[tokio::test]
    async fn fixed_authorization_removal_and_missing_targets_wait_then_resume_original_fifo() {
        let (_root,engine,host,account)=fixture();
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);let initial=jobs(&engine);
        let mut unauthorized=target(&account,"R001",now_ms());unauthorized["auto_purchase"]=json!(false);
        observe(&engine,&host,vec![unauthorized]);engine.queue_pump_once();
        assert!(engine.snapshot().tasks.is_empty());
        let waiting=jobs(&engine);assert!(waiting.iter().all(|job|job.status=="waiting_stock"&&!job.authorization_confirmed));
        assert_eq!(waiting.iter().map(|job|(&job.id,job.sequence)).collect::<Vec<_>>(),initial.iter().map(|job|(&job.id,job.sequence)).collect::<Vec<_>>());
        // Restore in the reverse observation order; original FIFO wins.
        let outcomes=observe(&engine,&host,vec![target(&account,"R002",now_ms()),target(&account,"R001",now_ms())]);
        assert!(outcomes.iter().all(|outcome|outcome["status"]=="already_queued"));engine.queue_pump_once();
        let after=jobs(&engine);assert_eq!(after.len(),2);assert_eq!(after[0].id,initial[0].id);assert!(after[0].inflight());assert!(after[1].pending());
    }

    #[tokio::test]
    async fn priority_removed_or_disabled_authorization_keeps_each_unexecuted_store_waiting() {
        let (root,engine,host,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);let initial=jobs(&engine);
        observe(&engine,&host,vec![]);engine.queue_pump_once();assert!(engine.snapshot().tasks.is_empty());
        assert!(jobs(&engine).iter().all(|job|job.status=="waiting_stock"));
        let mut a=target(&account,"R001",now_ms());let mut b=target(&account,"R002",now_ms());a["auto_purchase"]=json!(false);b["auto_purchase"]=json!(false);
        observe(&engine,&host,vec![a,b]);assert!(jobs(&engine).iter().all(|job|!job.eligible()));
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        assert_eq!(jobs(&engine).len(),2);assert_eq!(jobs(&engine)[0].id,initial[0].id);assert_eq!(jobs(&engine)[1].id,initial[1].id);
        engine.queue_pump_once();let after=jobs(&engine);assert!(after[0].inflight());assert!(after[1].pending());
    }

    #[tokio::test]
    async fn fixed_intents_recover_original_mode_or_require_explicit_cancel_before_pool_mode() {
        let (root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);let original=jobs(&engine);
        save_policy(root.path(),&account,priority(&["R001","R002"]));observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);engine.queue_pump_once();
        assert!(engine.snapshot().tasks.is_empty());assert_eq!(jobs(&engine).len(),2);assert!(jobs(&engine).iter().all(|job|job.status=="waiting_stock"));
        save_policy(root.path(),&account,json!({}));observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        assert!(jobs(&engine).iter().all(|job|job.status=="queued"&&job.authorization_confirmed));assert_eq!(jobs(&engine)[0].id,original[0].id);
        engine.queue_command(host.clone(),"cancel",&json!({"all":true})).unwrap();
        save_policy(root.path(),&account,priority(&["R001","R002"]));let outcomes=observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);
        assert_eq!(outcomes[0]["status"],"queued");let after=jobs(&engine);assert_eq!(after.len(),4);assert_eq!(after[0].status,"cancelled");assert_eq!(after[1].status,"cancelled");assert!(after[2].purchase_group_id.is_some());assert_eq!(after[2].purchase_group_id,after[3].purchase_group_id);
    }

    #[tokio::test]
    async fn manual_cancel_is_terminal_and_negative_stock_never_revives_its_history() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R001",now_ms())]);let original=jobs(&engine)[0].clone();
        engine.queue_command(host.clone(),"cancel",&json!({"job_id":original.id})).unwrap();
        observe(&engine,&host,vec![target(&account,"R001",now_ms())]);assert_eq!(jobs(&engine).len(),1);assert_eq!(jobs(&engine)[0].status,"cancelled");
        observe(&engine,&host,vec![negative(target(&account,"R001",now_ms()))]);observe(&engine,&host,vec![target(&account,"R001",now_ms())]);
        let after=jobs(&engine);assert_eq!(after.len(),2);assert_eq!(after[0].id,original.id);assert_eq!(after[0].status,"cancelled");assert_ne!(after[1].id,original.id);assert!(after[1].sequence>original.sequence);
    }

    #[tokio::test]
    async fn changed_target_quantity_does_not_authorize_a_different_frozen_purchase() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R001",now_ms())]);let original=jobs(&engine)[0].clone();
        let mut changed=target(&account,"R001",now_ms());changed["quantity"]=json!(2);observe(&engine,&host,vec![changed]);engine.queue_pump_once();
        assert!(engine.snapshot().tasks.is_empty());assert_eq!(jobs(&engine)[0].status,"waiting_stock");assert!(!jobs(&engine)[0].authorization_confirmed);
        observe(&engine,&host,vec![target(&account,"R001",now_ms())]);assert_eq!(jobs(&engine)[0].id,original.id);assert!(jobs(&engine)[0].authorization_confirmed);
    }

    #[tokio::test]
    async fn only_dispatchable_queue_work_preempts_background_session_warmup() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R001",now_ms())]);
        observe(&engine,&host,vec![]);
        let maintenance=engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        engine.queue_pump_once();assert!(!*engine.controls.lock().unwrap()[&maintenance].cancel.borrow());
        // Restore via the queue state to exercise pump preemption independently
        // of the inventory-triggered preemption pathway.
        {let mut queue=engine.purchase_queue.lock().unwrap();let job=&mut queue.jobs[0];job.authorization_confirmed=true;job.signal_confirmed=true;job.stock_status="available".into();job.checked_at_ms=now_ms();job.status="queued".into();}
        engine.queue_pump_once();assert!(*engine.controls.lock().unwrap()[&maintenance].cancel.borrow());
        assert_eq!(jobs(&engine)[0].status,"queued");assert!(jobs(&engine)[0].message.contains("空闲会话预热"));
    }

    #[tokio::test]
    async fn pause_resume_cancel_and_capacity_have_bounded_explicit_state() {
        let (_root,engine,host,account)=fixture();
        engine.queue_command(host.clone(),"pause",&json!({})).unwrap();
        let at=now_ms();observe(&engine,&host,vec![target(&account,"R390",at)]);engine.queue_pump_once();
        assert_eq!(jobs(&engine)[0].status,"queued");assert!(engine.snapshot().tasks.is_empty());
        let id=jobs(&engine)[0].id.clone();engine.queue_command(host.clone(),"cancel",&json!({"job_id":id})).unwrap();
        assert_eq!(jobs(&engine)[0].status,"cancelled");
        engine.queue_command(host.clone(),"resume",&json!({})).unwrap();
        for offset in 0..5 {observe(&engine,&host,(0..100).map(|i|target(&account,&format!("R{}",1000+offset*100+i),now_ms())).collect());}
        assert_eq!(engine.queue_command(host.clone(),"status",&json!({})).unwrap()["queued_count"],500);
        let overflow=observe(&engine,&host,vec![target(&account,"R9999",now_ms())]);assert_eq!(overflow[0]["status"],"capacity_full");
        engine.queue_command(host.clone(),"cancel",&json!({"all":true})).unwrap();
        let state=engine.queue_command(host,"status",&json!({})).unwrap();assert_eq!(state["queued_count"],0);assert_eq!(state["paused"],true);
        assert!(state["jobs"].as_array().unwrap().len()<=HISTORY);
    }

    #[tokio::test]
    async fn restart_interrupts_inflight_without_replay_and_preserves_waiting_fifo() {
        let (root,engine,host,account)=fixture();let at=now_ms();
        observe(&engine,&host,vec![target(&account,"R390",at),target(&account,"R391",at)]);engine.queue_pump_once();
        let old=jobs(&engine);assert!(old[0].inflight());
        let restored=Engine::new(root.path().into()).unwrap();restored.queue_stopping.store(true,Ordering::SeqCst);
        let state=restored.queue_command(host.clone(),"status",&json!({})).unwrap();
        assert_eq!(state["paused"],true);assert_eq!(state["running_count"],0);assert_eq!(state["queued_count"],1);
        let recovered=jobs(&restored);assert_eq!(recovered[0].status,"interrupted");assert_eq!(recovered[1].status,"waiting_stock");
        assert_eq!(old[1].sequence,recovered[1].sequence);assert_eq!(recovered[1].checked_at_ms,0);
        assert!(restored.queue_command(host,"resume",&json!({"account_id":account})).is_err());
        assert_eq!(restored.queue_runtime.lock().unwrap().coordinators.len(),0);
    }

    #[tokio::test]
    async fn restart_pending_intent_requires_new_positive_signal_before_dispatch() {
        for pool in [false,true] {
            let (root,engine,host,account)=fixture();
            if pool {save_policy(root.path(),&account,priority(&["R390"]));}
            observe(&engine,&host,vec![target(&account,"R390",now_ms())]);let original=jobs(&engine)[0].clone();
            let restored=Engine::new(root.path().into()).unwrap();restored.queue_stopping.store(true,Ordering::SeqCst);
            restored.accounts.lock().unwrap()[0].pending=false;
            restored.queue_command(host.clone(),"resume",&json!({})).unwrap();
            observe(&restored,&host,vec![negative(target(&account,"R390",now_ms()))]);restored.queue_pump_once();
            assert!(!jobs(&restored)[0].signal_confirmed);assert_eq!(jobs(&restored)[0].status,"waiting_stock");assert!(restored.snapshot().tasks.is_empty());
            let mut unknown=target(&account,"R390",now_ms());unknown["status"]=json!("unknown");
            observe(&restored,&host,vec![unknown]);restored.queue_pump_once();assert!(restored.snapshot().tasks.is_empty());
            observe(&restored,&host,vec![target(&account,"R390",now_ms()-20_000)]);restored.queue_pump_once();assert!(restored.snapshot().tasks.is_empty());
            observe(&restored,&host,vec![target(&account,"R390",now_ms())]);restored.queue_pump_once();
            let dispatched=jobs(&restored)[0].clone();assert_eq!(dispatched.id,original.id);assert_eq!(dispatched.sequence,original.sequence);assert_eq!(dispatched.status,"dispatched");
        }
    }

    #[tokio::test]
    async fn unstarted_legacy_age_ack_cannot_requeue_a_previous_unknown_submission() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();
        let job=jobs(&engine)[0].clone();ready(&engine,&job);
        ack(&engine,&job,"queue_progress",json!({"status":"submission_unknown","order_submitted":true}));
        ack(&engine,&job,"queue_finished",json!({"status":"paused","started":false,"order_submitted":false,"pause_reason":"inventory_stale"}));
        assert_eq!(jobs(&engine)[0].status,"submission_unknown");
        assert_eq!(engine.purchase_queue.lock().unwrap().blocked_accounts.get(&account).map(String::as_str),Some("submission_unknown"));
        observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();
        assert_eq!(jobs(&engine).len(),1);assert_eq!(jobs(&engine)[0].assignment,1);
    }

    #[tokio::test]
    async fn legacy_age_ack_with_current_event_submission_evidence_cannot_requeue() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();
        let job=jobs(&engine)[0].clone();ready(&engine,&job);
        engine.queue_worker_event(&account,job.task_id.as_deref().unwrap(),&json!({"type":"queue_finished","queue_job_id":job.id,
            "queue_assignment":job.assignment,"order_submitted":true,"result":{"status":"paused","started":false,"order_submitted":false,"pause_reason":"inventory_stale"}}));
        assert_eq!(jobs(&engine)[0].status,"submission_unknown");assert!(jobs(&engine)[0].order_submitted);
        assert_eq!(engine.purchase_queue.lock().unwrap().blocked_accounts.get(&account).map(String::as_str),Some("submission_unknown"));
    }

    #[tokio::test]
    async fn assignment_generation_rejects_late_ack_after_stale_unstarted_requeue() {
        let (_root,engine,host,account)=fixture();let at=now_ms();observe(&engine,&host,vec![target(&account,"R390",at)]);engine.queue_pump_once();
        let old=jobs(&engine)[0].clone();ready(&engine,&old);
        ack(&engine,&old,"queue_finished",json!({"status":"paused","started":false,"order_submitted":false,"pause_reason":"inventory_stale"}));
        assert_eq!(jobs(&engine)[0].status,"queued");assert!(engine.purchase_queue.lock().unwrap().blocked_accounts.is_empty());
        observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();
        let assigned=jobs(&engine)[0].clone();assert_eq!(assigned.assignment,2);assert_eq!(assigned.task_id,old.task_id);
        ack(&engine,&old,"queue_finished",json!({"success":true,"status":"succeeded"}));assert_eq!(jobs(&engine)[0].status,"dispatched");
        ack(&engine,&assigned,"queue_started",Value::Null);assert_eq!(jobs(&engine)[0].status,"running");
        ack(&engine,&assigned,"queue_finished",json!({"success":false,"status":"failed"}));assert_eq!(jobs(&engine)[0].status,"failed");
    }

    #[tokio::test]
    async fn queue_stock_and_creation_times_are_immutable_compatible_and_do_not_change_fifo() {
        let (root,engine,host,account)=fixture();
        engine.queue_command(host.clone(),"pause",&json!({})).unwrap();
        let detected=now_ms().saturating_sub(500);
        observe(&engine,&host,vec![target(&account,"R390",detected),target(&account,"R391",detected)]);
        let before=jobs(&engine);assert_eq!(before[0].stock_detected_at_ms,Some(detected));
        assert!(before[0].enqueued_at_ms>detected);assert_eq!(before[0].queued_at_ms,Some(before[0].enqueued_at_ms));
        let view=engine.queue_command(host.clone(),"status",&json!({})).unwrap();
        assert_eq!(view["jobs"][0]["stock_detected_at_ms"],detected);
        assert_eq!(view["jobs"][0]["queued_at_ms"],before[0].queued_at_ms.unwrap());
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),negative(target(&account,"R391",now_ms()))]);
        let after=jobs(&engine);
        for (old,new) in before.iter().zip(&after) {
            assert_eq!(new.id,old.id);assert_eq!(new.sequence,old.sequence);assert_eq!(new.enqueued_at_ms,old.enqueued_at_ms);
            assert_eq!(new.stock_detected_at_ms,old.stock_detected_at_ms);assert_eq!(new.queued_at_ms,old.queued_at_ms);
        }
        assert!(after[0].sequence<after[1].sequence);assert_eq!(after[1].status,"queued");
        let restored=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert_eq!(restored.jobs[0].stock_detected_at_ms,Some(detected));assert_eq!(restored.jobs[0].queued_at_ms,before[0].queued_at_ms);
        assert_eq!(restored.jobs[0].checked_at_ms,0);
        let mut legacy=serde_json::to_value(&before[0]).unwrap();
        legacy.as_object_mut().unwrap().remove("stock_detected_at_ms");legacy.as_object_mut().unwrap().remove("queued_at_ms");
        let old:QueueJob=serde_json::from_value(legacy).unwrap();assert!(old.stock_detected_at_ms.is_none()&&old.queued_at_ms.is_none());
    }

    #[tokio::test]
    async fn legacy_stock_times_recover_original_sample_without_inventing_other_events() {
        let (root,engine,host,account)=fixture();
        let original=now_ms().saturating_sub(500);
        observe(&engine,&host,(390..395).map(|store|target(&account,&format!("R{store}"),original)).collect());
        let path=root.path().join("settings/purchase_queue.json");
        let mut disk:Value=serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        let rows=disk["jobs"].as_array_mut().unwrap();
        for row in rows.iter_mut() {
            row["status"]=json!("succeeded");
            row["enqueued_at_ms"]=json!(original);
            row["checked_at_ms"]=json!(original+300);
            for field in ["stock_detected_at_ms","queued_at_ms","started_at_ms","order_confirmed_at_ms"] {
                row.as_object_mut().unwrap().remove(field);
            }
        }
        rows[1]["status"]=json!("waiting_stock");
        rows[2]["enqueued_at_ms"]=json!(0);
        rows[3]["enqueued_at_ms"]=json!(now_ms()+60_000);
        rows[4]["stock_detected_at_ms"]=json!(original-100);
        private_json(&path,&disk).unwrap();

        let loaded=QueueState::load(root.path(),&HashMap::new()).unwrap();
        // Load sorts FIFO, so inspect identity rather than the pre-load position.
        for row in disk["jobs"].as_array().unwrap() {
            let job=loaded.jobs.iter().find(|job|job.id==row["id"]).unwrap();
            let expected=if row["enqueued_at_ms"]==0 || row["enqueued_at_ms"].as_u64().unwrap()>now_ms()+1000 {None}
                else if row["stock_detected_at_ms"].is_number() {row["stock_detected_at_ms"].as_u64()}
                else {Some(original)};
            assert_eq!(job.stock_detected_at_ms,expected);
            assert_eq!(job.enqueued_at_ms,row["enqueued_at_ms"].as_u64().unwrap());
            assert!(job.queued_at_ms.is_none()&&job.started_at_ms.is_none()&&job.order_confirmed_at_ms.is_none());
            if row["status"]=="waiting_stock" {assert_eq!(job.checked_at_ms,0);assert!(!job.authorization_confirmed);}
        }
        let persisted:QueueState=serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(persisted.jobs.iter().map(|job|(&job.id,job.stock_detected_at_ms)).collect::<Vec<_>>(),
            loaded.jobs.iter().map(|job|(&job.id,job.stock_detected_at_ms)).collect::<Vec<_>>());
        let reloaded=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert_eq!(serde_json::to_value(&reloaded.jobs).unwrap(),serde_json::to_value(&loaded.jobs).unwrap());

        // Terminal-only history must persist the migration too: it does not
        // otherwise enter restart recovery or trigger a queue write.
        let mut terminal=serde_json::to_value(&loaded).unwrap();
        for row in terminal["jobs"].as_array_mut().unwrap() {
            row["status"]=json!("succeeded");
            row.as_object_mut().unwrap().remove("stock_detected_at_ms");
        }
        private_json(&path,&terminal).unwrap();
        let terminal_loaded=QueueState::load(root.path(),&HashMap::new()).unwrap();
        let terminal_persisted:QueueState=serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert!(terminal_loaded.jobs.iter().any(|job|job.stock_detected_at_ms==Some(original)));
        assert_eq!(serde_json::to_value(&terminal_persisted.jobs).unwrap(),serde_json::to_value(&terminal_loaded.jobs).unwrap());
    }

    #[tokio::test]
    async fn queue_timestamps_record_first_matching_ack_and_survive_persistence() {
        let (root,engine,host,account)=fixture();
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);engine.queue_pump_once();
        let initial=jobs(&engine);assert!(initial.iter().all(|job|job.started_at_ms.is_none()&&job.order_confirmed_at_ms.is_none()));
        let job=&initial[0];let task=job.task_id.as_deref().unwrap();
        let started=json!({"type":"queue_started","queue_job_id":job.id,"queue_assignment":job.assignment});
        engine.queue_worker_event("another-account",task,&started);
        engine.queue_worker_event(&account,"another-task",&started);
        for (key,value) in [("queue_job_id",json!("another-job")),("queue_assignment",json!(job.assignment+1))] {
            let mut invalid=started.clone();invalid[key]=value;engine.queue_worker_event(&account,task,&invalid);
        }
        assert!(jobs(&engine).iter().all(|job|job.started_at_ms.is_none()));
        ack(&engine,job,"queue_started",Value::Null);let first_start=jobs(&engine)[0].started_at_ms.unwrap();
        std::thread::sleep(std::time::Duration::from_millis(2));
        ack(&engine,job,"queue_started",Value::Null);assert_eq!(jobs(&engine)[0].started_at_ms,Some(first_start));
        let confirmed=job.enqueued_at_ms;
        ack(&engine,job,"queue_progress",json!({"status":"parallel_complete","order_submitted":true,
            "pending_orders":[{"order_number":"W123"}],"payment_confirmed_at_ms":confirmed}));
        assert_eq!(jobs(&engine)[0].order_confirmed_at_ms,Some(confirmed));
        ack(&engine,job,"queue_finished",json!({"status":"pending_payment","order_number":"W123","payment_confirmed_at_ms":now_ms()}));
        let completed=jobs(&engine);assert_eq!(completed[0].started_at_ms,Some(first_start));assert_eq!(completed[0].order_confirmed_at_ms,Some(confirmed));
        assert!(completed[1].started_at_ms.is_none()&&completed[1].order_confirmed_at_ms.is_none());
        ack(&engine,job,"queue_started",Value::Null);assert_eq!(jobs(&engine)[0].started_at_ms,Some(first_start));
        let restored=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert_eq!(restored.jobs[0].started_at_ms,Some(first_start));assert_eq!(restored.jobs[0].order_confirmed_at_ms,Some(confirmed));
        let mut legacy=serde_json::to_value(&completed[0]).unwrap();
        legacy.as_object_mut().unwrap().remove("started_at_ms");legacy.as_object_mut().unwrap().remove("order_confirmed_at_ms");
        let old:QueueJob=serde_json::from_value(legacy).unwrap();assert!(old.started_at_ms.is_none()&&old.order_confirmed_at_ms.is_none());
    }

    #[tokio::test]
    async fn queue_timestamps_do_not_invent_start_or_confirmation_for_unexecuted_or_unknown_jobs() {
        let (_root,engine,host,account)=fixture();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);
        let queued=jobs(&engine)[1].clone();engine.queue_command(host.clone(),"cancel",&json!({"job_id":queued.id})).unwrap();
        engine.queue_pump_once();let job=jobs(&engine)[0].clone();ready(&engine,&job);
        ack(&engine,&job,"queue_progress",json!({"status":"running","order_submitted":false}));
        assert!(jobs(&engine).iter().all(|job|job.started_at_ms.is_none()&&job.order_confirmed_at_ms.is_none()));
        ack(&engine,&job,"queue_finished",json!({"status":"paused","started":false,"pause_reason":"inventory_stale"}));
        assert_eq!(jobs(&engine)[0].status,"queued");
        assert!(jobs(&engine).iter().all(|job|job.started_at_ms.is_none()&&job.order_confirmed_at_ms.is_none()));
        observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();let reassigned=jobs(&engine)[0].clone();
        ack(&engine,&job,"queue_started",Value::Null);
        ack(&engine,&job,"queue_progress",json!({"status":"pending_payment","order_number":"W123"}));
        assert!(jobs(&engine)[0].started_at_ms.is_none()&&jobs(&engine)[0].order_confirmed_at_ms.is_none());
        ack(&engine,&reassigned,"queue_started",Value::Null);
        let before=now_ms();ack(&engine,&reassigned,"queue_finished",json!({"status":"pending_payment","order_number":"W123"}));
        assert!(jobs(&engine)[0].order_confirmed_at_ms.is_some_and(|at|at>=before&&at<=now_ms()));
        assert!(jobs(&engine)[1].started_at_ms.is_none()&&jobs(&engine)[1].order_confirmed_at_ms.is_none());
    }

    #[test]
    fn queue_confirmation_requires_pending_number_and_validates_worker_timestamp() {
        for result in [json!({"status":"submission_unknown","order_number":"W123"}),json!({"status":"payment_unknown"}),
            json!({"status":"pending_payment","order_number":" "}),json!({"status":"succeeded","order_number":"W123"})] {
            assert_eq!(queue_order_confirmation_at(&result,100,200),None);
        }
        for invalid in [json!(0),json!(99),json!(201),json!("150")] {
            assert_eq!(queue_order_confirmation_at(&json!({"status":"pending_payment","order_number":"W123","payment_confirmed_at_ms":invalid}),100,200),Some(200));
        }
        assert_eq!(queue_order_confirmation_at(&json!({"status":"queue_complete","payment_confirmed_at_ms":150,
            "pending_orders":[{"order_number":"W123","payment_confirmed_at_ms":160}],
            "attempts":[{"status":"submission_unknown"}]}),100,200),Some(150));
        assert_eq!(queue_order_confirmation_at(&json!({"queue_jobs":[{"result":{"status":"pending_payment","order_number":"W123","payment_confirmed_at_ms":160}}]}),100,200),Some(160));
    }

    #[tokio::test]
    async fn rejected_queue_assignment_and_terminal_job_cannot_change_aggregate_progress() {
        let (_root,engine,host,account)=fixture();
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);engine.queue_pump_once();
        let assigned=jobs(&engine);let task=assigned[0].task_id.as_deref().unwrap();
        let stage=|job:&QueueJob,message:&str|json!({"type":"stage","queue_job_id":job.id,"queue_assignment":job.assignment,"message":message});
        for message in ["prepare","verify","submit"] {engine.apply_worker_event(&host,&account,task,&stage(&assigned[0],message),true);}
        let before=engine.tasks.lock().unwrap()[task].clone();
        assert_eq!(serde_json::to_value(before.purchase_progress.as_ref().unwrap()).unwrap(),json!({"current":"submit","completed":["prepare","verify"]}));
        let mut stale=stage(&assigned[0],"checkout");stale["queue_assignment"]=json!(assigned[0].assignment-1);stale["order_submitted"]=json!(true);
        engine.apply_worker_event(&host,&account,task,&stale,true);
        let unchanged=engine.tasks.lock().unwrap()[task].clone();
        assert_eq!(unchanged.stage,before.stage);assert_eq!(serde_json::to_value(&unchanged).unwrap(),serde_json::to_value(&before).unwrap());
        engine.apply_worker_event(&host,&account,task,&json!({"type":"queue_finished","queue_job_id":assigned[0].id,
            "queue_assignment":assigned[0].assignment,"result":{"success":false,"status":"failed"}}),true);
        assert_eq!(jobs(&engine)[0].status,"failed");
        // A second serial allocation uses the coordinator only after the first finishes.
        ready(&engine,&assigned[0]);engine.queue_pump_once();let assigned=jobs(&engine);
        engine.apply_worker_event(&host,&account,task,&stage(&assigned[1],"checkout"),true);
        let second=engine.tasks.lock().unwrap()[task].clone();assert_eq!(second.stage,"checkout");
        engine.apply_worker_event(&host,&account,task,&stage(&assigned[0],"prepare"),true);
        engine.apply_worker_event(&host,&account,task,&json!({"type":"queue_progress","queue_job_id":assigned[0].id,
            "queue_assignment":assigned[0].assignment,"result":{"status":"pending_payment","order_number":"W123"}}),true);
        assert_eq!(serde_json::to_value(&engine.tasks.lock().unwrap()[task]).unwrap(),serde_json::to_value(&second).unwrap());
    }

    #[tokio::test]
    async fn queue_stage_updates_target_timeline_and_does_not_touch_waiting_sibling() {
        let (root,engine,host,account)=fixture();
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);engine.queue_pump_once();
        let initial=jobs(&engine);let first=&initial[0];let task=first.task_id.as_deref().unwrap();
        assert!(initial[0].dispatched_at_ms.is_some());assert!(initial[1].task_id.is_none());
        let event=json!({"type":"stage","queue_job_id":first.id,"queue_assignment":first.assignment,"message":"  正在核对取货时段  ","order_submitted":true});
        assert!(engine.queue_worker_event(&account,task,&event));let after=jobs(&engine);
        assert_eq!(after[0].message,"正在核对取货时段");assert!(after[0].order_submitted);assert!(after[0].submit_started_at_ms.is_some());
        assert_eq!(serde_json::to_value(&after[1]).unwrap(),serde_json::to_value(&initial[1]).unwrap());
        let disk:Value=serde_json::from_slice(&fs::read(root.path().join("settings/purchase_queue.json")).unwrap()).unwrap();assert!(disk["jobs"][0]["submit_started_at_ms"].is_number());
    }

    #[tokio::test]
    async fn queue_stage_rejects_stale_ownership_empty_and_inactive_updates() {
        let (_root,engine,host,account)=fixture();
        observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();
        let job=jobs(&engine)[0].clone();let task=job.task_id.as_deref().unwrap();
        let event=json!({"type":"stage","queue_job_id":job.id,"queue_assignment":job.assignment,"message":"正在确认金额"});
        let before=serde_json::to_value(jobs(&engine)).unwrap();
        engine.queue_worker_event("another-account",task,&event);
        engine.queue_worker_event(&account,"another-task",&event);
        for (key,value) in [("queue_job_id",json!("another-job")),("queue_assignment",json!(job.assignment+1)),
            ("queue_assignment",Value::Null),("message",json!(" \n\t ")),("message",Value::Null)] {
            let mut invalid=event.clone();invalid[key]=value;
            engine.queue_worker_event(&account,task,&invalid);
        }
        assert_eq!(serde_json::to_value(jobs(&engine)).unwrap(),before);
        // A late stage must preserve cancellation attention and every terminal result.
        for status in ["queued","waiting_stock","cancelling","cancelled","succeeded","failed","interrupted","submission_unknown"] {
            {
                let mut queue=engine.purchase_queue.lock().unwrap();let current=&mut queue.jobs[0];
                current.status=status.into();current.message="保留停止或处理结果".into();
                current.result=Some(json!({"status":status}));current.order_submitted=true;
            }
            let before=serde_json::to_value(jobs(&engine)).unwrap();
            engine.queue_worker_event(&account,task,&event);
            assert_eq!(serde_json::to_value(jobs(&engine)).unwrap(),before,"{status}");
        }
    }

    #[tokio::test]
    async fn unknown_submission_blocks_only_its_account_and_never_replays_job() {
        let (_root,engine,host,account)=fixture();let at=now_ms();observe(&engine,&host,vec![target(&account,"R390",at),target(&account,"R391",at)]);engine.queue_pump_once();
        let first=jobs(&engine)[0].clone();ready(&engine,&first);
        ack(&engine,&first,"queue_finished",json!({"success":false,"status":"submission_unknown","order_submitted":true}));
        assert_eq!(jobs(&engine)[0].status,"submission_unknown");engine.queue_pump_once();assert_eq!(jobs(&engine)[1].status,"queued");
        assert!(engine.queue_command(host.clone(),"resume",&json!({"account_id":account})).is_err());
        let outcomes=observe(&engine,&host,vec![target(&account,"R390",now_ms())]);assert_eq!(outcomes[0]["status"],"already_queued");
        assert_eq!(jobs(&engine).len(),2);
    }

    #[tokio::test]
    async fn configuration_changes_wait_for_active_coordinator_and_keep_waiting_jobs() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);engine.queue_pump_once();
        assert!(engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":3})).unwrap_err().contains("先暂停"));
        assert_eq!(jobs(&engine)[1].status,"queued");
        engine.queue_command(host.clone(),"configure",&json!({"mode":"sequential","max_concurrency":1})).unwrap();
        for invalid in [json!({"mode":"unknown","max_concurrency":1}),json!({"mode":"parallel","max_concurrency":0}),json!({"mode":"parallel","max_concurrency":11}),json!({"mode":"parallel","max_concurrency":"3"})] {
            assert!(engine.queue_command(host.clone(),"configure",&invalid).is_err());
        }
    }

    #[tokio::test]
    async fn queue_results_are_sanitized_and_worker_payload_keeps_exact_store_retry_contract() {
        let (root,engine,host,account)=fixture();let mut item=target(&account,"R390",now_ms());item["concurrency"]=json!(3);item["retry_count"]=json!(2);
        observe(&engine,&host,vec![item]);engine.queue_pump_once();let job=jobs(&engine)[0].clone();
        let payload=job.payload();assert_eq!(payload["concurrency"],1);assert_eq!(job.requested_concurrency,Some(3));assert_eq!(payload["retry_count"],2);assert_eq!(payload["pickup_store_id"],"R390");assert_eq!(payload["pickup_location"],"200000");
        ack(&engine,&job,"queue_finished",json!({"success":true,"status":"pending_payment","order_submitted":true,"order_number":"W111111","payment_url":"private-link","pending_orders":[{"qr_data":"private-image","order_number":"W111111"}]}));
        let disk=fs::read_to_string(root.path().join("settings/purchase_queue.json")).unwrap();assert!(!disk.contains("private-link"));assert!(!disk.contains("private-image"));assert!(disk.contains("W111111"));
    }

    #[tokio::test]
    async fn global_capacity_counts_effective_one_route_per_account() {
        let (_root,engine,host,account)=fixture();let mut accounts=vec![account];
        for index in 1..11 {let other=engine.add_account(format!("slots{index}@example.test")).unwrap();accounts.push(other.id);}
        for owner in engine.accounts.lock().unwrap().iter_mut(){owner.pending=false;}
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":10})).unwrap();
        let targets=accounts.iter().map(|owner|{let mut row=target(owner,"R001",now_ms());row["concurrency"]=json!(6);row}).collect();observe(&engine,&host,targets);engine.queue_pump_once();
        let before=jobs(&engine);assert_eq!(before.iter().filter(|job|job.inflight()).count(),10);assert!(before[10].pending());
        assert!(before.iter().all(|job|job.concurrency==1&&job.requested_concurrency==Some(6)));
        assert_eq!(engine.purchase_queue.lock().unwrap().active_child_slots(),10);
        ready(&engine,&before[0]);ack(&engine,&before[0],"queue_finished",json!({"status":"failed","success":false}));engine.queue_pump_once();
        assert!(jobs(&engine)[10].inflight());assert_eq!(engine.purchase_queue.lock().unwrap().active_child_slots(),10);
    }

    #[tokio::test]
    async fn cancelling_one_inflight_target_preserves_other_jobs_and_rejects_old_ack() {
        let (_root,engine,host,account)=fixture();engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);engine.queue_pump_once();
        let before=jobs(&engine);ready(&engine,&before[0]);ack(&engine,&before[0],"queue_started",Value::Null);
        engine.queue_command(host.clone(),"cancel",&json!({"job_id":before[0].id})).unwrap();
        assert_eq!(jobs(&engine)[0].status,"cancelling");assert_eq!(jobs(&engine)[1].status,"queued");
        ack(&engine,&before[0],"queue_finished",json!({"status":"cancelled","order_submitted":false}));
        assert_eq!(jobs(&engine)[0].status,"cancelled");assert_eq!(jobs(&engine)[1].status,"queued");
        ack(&engine,&before[0],"queue_started",Value::Null);assert_eq!(jobs(&engine)[0].status,"cancelled");
        assert_eq!(observe(&engine,&host,vec![target(&account,"R390",now_ms())])[0]["status"],"already_queued");
    }

    #[tokio::test]
    async fn prompt_closed_rejects_old_assignment_even_when_a_stale_prompt_still_matches() {
        let (_root,engine,host,account)=fixture();observe(&engine,&host,vec![target(&account,"R390",now_ms())]);engine.queue_pump_once();
        let job=jobs(&engine)[0].clone();let task_id=job.task_id.as_deref().unwrap();
        let event=json!({"type":"prompt_closed","request_id":"code","queue_job_id":job.id,"queue_assignment":job.assignment});
        {
            let mut tasks=engine.tasks.lock().unwrap();let task=tasks.get_mut(task_id).unwrap();task.status="waiting".into();
            task.prompt=Some(json!({"id":"code","kind":"code","queue_job_id":job.id,"queue_assignment":job.assignment}));
        }
        // The prompt itself can remain from an older generation; current durable
        // allocation ownership is checked before it may update the task UI.
        engine.purchase_queue.lock().unwrap().jobs[0].assignment+=1;
        engine.apply_worker_event(&host,&account,task_id,&event,true);
        assert!(engine.tasks.lock().unwrap()[task_id].prompt.is_some());
        engine.purchase_queue.lock().unwrap().jobs[0].assignment=job.assignment;
        engine.apply_worker_event(&host,&account,task_id,&event,true);
        assert!(engine.tasks.lock().unwrap()[task_id].prompt.is_none());
        assert_eq!(engine.tasks.lock().unwrap()[task_id].status,"running");
    }

    #[tokio::test]
    async fn exhausted_authentication_is_terminal_without_reassignment_or_account_gate() {
        let (_root,engine,host,account)=fixture();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);engine.queue_pump_once();
        let first=jobs(&engine)[0].clone();ready(&engine,&first);
        ack(&engine,&first,"queue_finished",json!({"status":"paused","started":false,"order_submitted":false,
            "pause_reason":"authentication","requires_serial_checkout":true,"message":"自动登录失败"}));
        let after=jobs(&engine);assert_eq!(after[0].status,"failed");assert_eq!(after[0].sequence,first.sequence);
        assert_eq!(after[0].message,"自动登录失败");
        assert!(engine.purchase_queue.lock().unwrap().blocked_accounts.is_empty());
        for _ in 0..3 {engine.queue_pump_once();}
        let continued=jobs(&engine);assert_eq!(continued[0].status,"failed");assert_eq!(continued[0].assignment,first.assignment);
        assert!(continued[1].inflight());assert_eq!(continued[1].account_id,account);
        // An old completion/start acknowledgement cannot revive the completed allocation.
        ack(&engine,&first,"queue_started",Value::Null);assert_eq!(jobs(&engine)[0].status,"failed");
        ack(&engine,&continued[1],"queue_finished",json!({"success":false,"status":"failed","order_submitted":false}));
        let second=engine.add_account("after-auth-failure@example.test".into()).unwrap();engine.accounts.lock().unwrap()[1].pending=false;
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms()),target(&second.id,"R392",now_ms())]);
        engine.queue_pump_once();let final_jobs=jobs(&engine);assert_eq!(final_jobs.len(),3);assert!(final_jobs[2].inflight());
        assert_eq!(final_jobs[0].assignment,first.assignment);
    }

    #[tokio::test]
    async fn restart_removes_legacy_authentication_gate_but_preserves_unknown_cancelled_and_new_signal_requirement() {
        let (root,engine,host,account)=fixture();
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms()),target(&account,"R392",now_ms())]);
        let unknown_account=Uuid::new_v4().to_string();
        {
            let mut queue=engine.purchase_queue.lock().unwrap();
            queue.blocked_accounts.insert(account.clone(),"authentication".into());
            queue.blocked_accounts.insert(unknown_account.clone(),"submission_unknown".into());
            queue.jobs[1].status="cancelled".into();
            queue.jobs[2].status="running".into();
            queue.jobs[2].account_id=unknown_account.clone();
            queue.save(root.path()).unwrap();
        }
        let migrated=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert!(!migrated.blocked_accounts.contains_key(&account));
        assert_eq!(migrated.blocked_accounts[&unknown_account],"submission_unknown");
        assert_eq!(migrated.jobs[0].status,"waiting_stock");assert!(!migrated.jobs[0].eligible());
        assert_eq!(migrated.jobs[1].status,"cancelled");
        assert_eq!(migrated.jobs[2].status,"interrupted");
        assert!(migrated.paused);
        let restored=Engine::new(root.path().into()).unwrap();restored.queue_stopping.store(true,Ordering::SeqCst);
        restored.accounts.lock().unwrap()[0].pending=false;
        restored.queue_command(host.clone(),"resume",&json!({})).unwrap();restored.queue_pump_once();
        assert!(restored.snapshot().tasks.is_empty());
        observe(&restored,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms())]);restored.queue_pump_once();
        let jobs=jobs(&restored);assert!(jobs[0].inflight());assert_eq!(jobs[1].status,"cancelled");assert_eq!(jobs[2].status,"interrupted");
        assert_eq!(restored.purchase_queue.lock().unwrap().blocked_accounts[&unknown_account],"submission_unknown");
    }

    #[tokio::test]
    async fn legacy_requeued_authentication_failure_is_terminal_but_unexecuted_and_unknown_evidence_survive() {
        let (root,engine,host,account)=fixture();
        observe(&engine,&host,(390..398).map(|store|target(&account,&format!("R{store}"),now_ms())).collect());
        let legacy=json!({"status":"paused","started":false,"pause_reason":"authentication",
            "order_submitted":false,"queue_assignment":1,"message":"原账号自动登录失败"});
        let original=jobs(&engine)[0].clone();
        {
            let mut queue=engine.purchase_queue.lock().unwrap();
            queue.blocked_accounts.insert(account.clone(),"authentication".into());
            for index in [0,2,3,4,5,6,7] {
                queue.jobs[index].assignment=1;queue.jobs[index].result=Some(legacy.clone());queue.jobs[index].task_id=None;
            }
            queue.jobs[2].order_submitted=true;
            queue.jobs[3].result.as_mut().unwrap()["result"]=json!({"status":"submission_unknown"});
            queue.jobs[4].assignment=0;queue.jobs[4].result.as_mut().unwrap()["queue_assignment"]=json!(0);
            queue.jobs[5].assignment=2; // Old result belongs to a previous allocation.
            queue.jobs[6].status="cancelled".into();
            queue.jobs[7].status="running".into();
            queue.save(root.path()).unwrap();
        }
        let migrated=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert_eq!(migrated.jobs[0].status,"failed");assert_eq!(migrated.jobs[0].id,original.id);
        assert_eq!(migrated.jobs[0].message,"原账号自动登录失败");assert!(migrated.jobs[0].task_id.is_none());
        assert_eq!(migrated.jobs[0].assignment,1);assert_eq!(migrated.jobs[0].result.as_ref().unwrap(),&legacy);
        for index in [1,4,5] {assert_eq!(migrated.jobs[index].status,"waiting_stock");assert!(!migrated.jobs[index].eligible());}
        for index in [2,3] {assert_eq!(migrated.jobs[index].status,"submission_unknown");}
        assert_eq!(migrated.jobs[6].status,"cancelled");assert_eq!(migrated.jobs[7].status,"interrupted");
        assert_eq!(migrated.blocked_accounts[&account],"submission_unknown");
        let again=QueueState::load(root.path(),&HashMap::new()).unwrap();
        assert_eq!(again.jobs[0].status,"failed");assert_eq!(again.jobs[6].status,"cancelled");
        assert_eq!(again.blocked_accounts[&account],"submission_unknown");
    }

    #[tokio::test]
    async fn pending_durable_submission_evidence_precedes_authentication_and_assignment_migration() {
        let (root,engine,host,account)=fixture();
        observe(&engine,&host,(390..395).map(|store|target(&account,&format!("R{store}"),now_ms())).collect());
        {
            let mut queue=engine.purchase_queue.lock().unwrap();
            queue.jobs[0].assignment=2;queue.jobs[0].order_submitted=true;
            queue.jobs[0].result=Some(json!({"status":"paused","started":false,"pause_reason":"authentication","queue_assignment":1}));
            queue.jobs[1].result=Some(json!({"status":"failed","attempts":[{"status":"submission_unknown"}]}));
            queue.jobs[2].result=Some(json!({"status":"failed","submitted":true}));
            queue.jobs[3].order_submitted=true;
            queue.jobs[3].result=Some(json!({"status":"pending_payment","order_number":"KNOWN-001"}));
            queue.jobs[4].assignment=2;queue.jobs[4].order_submitted=true;
            queue.jobs[4].result=Some(json!({"status":"pending_payment","order_number":"OLD-001","queue_assignment":1}));
            queue.save(root.path()).unwrap();
        }
        let migrated=QueueState::load(root.path(),&HashMap::new()).unwrap();
        for index in [0,1,2,4] {assert_eq!(migrated.jobs[index].status,"submission_unknown");assert!(!migrated.jobs[index].eligible());}
        assert_eq!(migrated.jobs[3].status,"succeeded");assert!(!migrated.jobs[3].eligible());
        assert_eq!(migrated.blocked_accounts[&account],"submission_unknown");
    }

    #[test]
    fn known_parallel_child_payments_are_not_misclassified_as_unknown_submission() {
        let known=json!({"status":"parallel_complete","order_submitted":true,"pending_orders":[{"order_number":"W111111"}],
            "attempts":[{"status":"pending_payment","order_submitted":true,"result":{"status":"pending_payment","order_number":"W111111"}}]});
        assert!(!contains_unknown(&known));
        let mut mixed=known.clone();mixed["attempts"].as_array_mut().unwrap().push(json!({"status":"submission_unknown","order_submitted":true}));
        assert!(contains_unknown(&mixed));
        assert!(contains_unknown(&json!({"status":"failed","order_submitted":true})));
        assert!(!contains_unknown(&json!({"status":"failed","order_submitted":false})));
        assert!(!contains_unknown(&json!({"status":"queue_complete","order_submitted":true,"queue_jobs":[known]})));
    }

    #[test]
    fn aggregate_submission_requires_order_evidence_and_never_masks_unknown_children() {
        for status in ["submission_unknown","unknown_submission","payment_unknown","verification_required"] {
            assert!(contains_unknown(&json!({"status":status,"order_number":"KNOWN-001"})));
        }
        assert!(contains_unknown(&json!({"submitted":true,"status":"failed"})));
        assert!(contains_unknown(&json!({"submitted":true,"attempts":[{"status":"failed"}]})));
        assert!(contains_unknown(&json!({"order_submitted":true,"queue_jobs":[{"status":"failed"}],"pending_orders":[{}]})));
        assert!(contains_unknown(&json!({"order_submitted":true,"pending_orders":[{"order_number":"   "}]})));
        for key in ["result","attempts","queue_jobs","jobs","pending_orders"] {
            let mut aggregate=json!({"order_submitted":true,"order_number":"KNOWN-001"});
            aggregate[key]=json!([{"submitted":true,"status":"failed"}]);
            assert!(contains_unknown(&aggregate),"unknown child hidden under {key}");
            aggregate[key]=json!({"submitted":true,"status":"failed"});
            assert!(contains_unknown(&aggregate),"unknown object hidden under {key}");
        }
        assert!(!contains_unknown(&json!({"submitted":true,"pending_orders":[{"order_number":"KNOWN-001"}]})));
        assert!(!contains_unknown(&json!({"submitted":true,"queue_jobs":[{"result":{"order_number":"KNOWN-001"}}]})));
        assert!(!contains_unknown(&json!({"success":false,"attempts":[{"status":"failed","submitted":false}]})));
        let mixed=json!({"submitted":true,"pending_orders":[{"order_number":"KNOWN-001"},
            {"status":"verification_required","order_submitted":true}]});
        assert!(contains_unknown(&mixed));
    }

    #[tokio::test]
    async fn submitted_alias_aggregate_without_number_blocks_only_the_owning_account() {
        let (_root,engine,host,account)=fixture();
        let second=engine.add_account("alias-unknown@example.test".into()).unwrap();engine.accounts.lock().unwrap()[1].pending=false;
        observe(&engine,&host,vec![target(&account,"R390",now_ms()),target(&account,"R391",now_ms()),target(&second.id,"R392",now_ms())]);
        engine.queue_pump_once();let first=jobs(&engine)[0].clone();ready(&engine,&first);
        ack(&engine,&first,"queue_finished",json!({"status":"failed","submitted":true,"attempts":[{"status":"failed"}]}));
        assert_eq!(jobs(&engine)[0].status,"submission_unknown");
        assert_eq!(engine.purchase_queue.lock().unwrap().blocked_accounts[&account],"submission_unknown");
        engine.queue_pump_once();let after=jobs(&engine);assert!(after[1].pending());assert!(after[2].inflight());
        assert_eq!(after[0].assignment,first.assignment);
    }

    #[tokio::test]
    async fn queue_target_preempts_explicit_login_preheat_but_preserves_real_login() {
        let (_root,engine,host,account)=fixture();
        let warm=engine.start(host.clone(),account.clone(),"login".into(),json!({"warm_only":true})).unwrap();
        let login=engine.start(host.clone(),account.clone(),"login".into(),json!({})).unwrap();
        observe(&engine,&host,vec![target(&account,"R390",now_ms())]);
        let controls=engine.controls.lock().unwrap();assert!(*controls[&warm].cancel.borrow());assert!(!*controls[&login].cancel.borrow());drop(controls);
        engine.queue_pump_once();assert_eq!(jobs(&engine)[0].status,"queued");
    }

    #[tokio::test]
    async fn new_queue_target_cancels_only_background_session_maintenance() {
        let (_root,engine,host,account)=fixture();
        let maintenance=engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        let login=engine.start(host.clone(),account.clone(),"login".into(),json!({})).unwrap();
        observe(&engine,&host,vec![target(&account,"R390",now_ms())]);
        let controls=engine.controls.lock().unwrap();assert!(*controls[&maintenance].cancel.borrow());assert!(!*controls[&login].cancel.borrow());
        drop(controls);engine.queue_pump_once();assert_eq!(jobs(&engine)[0].status,"queued");
    }

    fn bridge_audit(path:&Path)->Vec<Value> {
        let children=path.file_name().is_some_and(|name|name=="children.jsonl");
        let prefix=if children {"children-"} else {"protocol-"};
        let mut paths= {
            fs::read_dir(path.parent().unwrap()).into_iter().flatten().filter_map(Result::ok)
                .filter(|entry|entry.file_name().to_str().is_some_and(|name|name.starts_with(prefix)&&name.ends_with(".jsonl")))
                .map(|entry|entry.path()).collect::<Vec<_>>()
        };
        if path.exists(){paths.push(path.into());}
        let mut events:Vec<Value>=paths.iter().flat_map(|path|fs::read_to_string(path).unwrap_or_default().lines()
            .filter_map(|line|serde_json::from_str::<Value>(line).ok()).collect::<Vec<_>>()).collect();
        events.sort_by(|a,b|a["at_ns"].as_u64().cmp(&b["at_ns"].as_u64())
            .then_with(||a["type"].as_str().cmp(&b["type"].as_str()))
            .then_with(||a["task_id"].as_str().cmp(&b["task_id"].as_str())));
        events
    }

    #[test]
    fn queue_status_is_pure_read_without_starting_a_runtime_or_writing_queue_state() {
        let root=tempfile::tempdir().unwrap();let engine=Engine::new(root.path().into()).unwrap();
        let host=TaskHost {worker:root.path().join("not-a-worker"),script:None,on_update:Arc::new(|_|{})};
        let state=engine.queue_command(host,"status",&json!({})).unwrap();
        assert_eq!(state["running_count"],0);assert_eq!(state["queued_count"],0);
        let runtime=engine.queue_runtime.lock().unwrap();assert!(!runtime.pumping);assert!(runtime.host.is_none());assert!(runtime.coordinators.is_empty());
        assert!(!root.path().join("settings/purchase_queue.json").exists());
    }

    async fn bridge_wait(condition:impl Fn()->bool)->bool {
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(12);
        while !condition() {
            if std::time::Instant::now()>=deadline {return false}
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        true
    }

    struct BridgeCleanup {engine:Arc<Engine>,audit:PathBuf}
    impl Drop for BridgeCleanup {
        fn drop(&mut self) {
            for store in ["R001","R002","R003"] {let _=fs::write(self.audit.join(format!("release-{store}")),b"release synthetic fixture");}
            self.engine.queue_stopping.store(true,Ordering::SeqCst);
            for control in self.engine.controls.lock().unwrap().values() {let _=control.cancel.send(true);}
        }
    }

    #[tokio::test]
    async fn queue_claims_live_preparation_with_same_pid_and_exact_group_assignment_ack() {
        let project=Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();let python=project.join(if cfg!(windows){".venv/Scripts/python.exe"}else{".venv/bin/python"});
        let (root,engine,_,account)=fixture();save_policy(root.path(),&account,priority(&["R001"]));
        let script=root.path().join("prepared-queue-fixture.py");fs::write(&script,r#"import hashlib,json,pathlib,sys,time,os
r=json.loads(sys.stdin.readline())
def emit(kind,**data):
 print(json.dumps(dict(v=1,account_id=r['account_id'],task_id=r['task_id'],type=kind,**data)),flush=True)
assert r['action']=='session_maintain'
raw=(pathlib.Path(sys.argv[2])/'accounts'/r['account_id']/'checkout.json').read_bytes()
now=int(time.time()*1000)
emit('prompt',prompt={'id':'live-queue-lease','kind':'prepared_purchase','label':'ready','fixture_pid':os.getpid(),'prepared':{'v':1,'sku':'MLH74FE/A','quantity':1,'fulfillment':'RETAIL','config_fingerprint':'a'*64,'config_file_sha256':hashlib.sha256(raw).hexdigest(),'authorized_store_ids':['R001'],'prepared_at_ms':now,'expires_at_ms':now+300000,'preparation_stage':'review','review_verified':True,'order_ready':True}})
reply=json.loads(sys.stdin.readline());claim=reply['value']['prepared_claim'];target=reply['value']['params']
assert claim['source']=='queue' and claim['prepared_task_id']==r['task_id']
assert target['pickup_store_id']=='R001' and len(target['pickup_policy']['stores'])==1
identity={key:target[key] for key in ['queue_job_id','queue_assignment','purchase_group_id']}
emit('queue_started',**identity)
result={'success':True,'status':'pending_payment','order_number':'W12345678','order_submitted':True,'pid':os.getpid()}
emit('queue_finished',result=result,**identity)
emit('result',result=result)
"#).unwrap();
        let host=TaskHost{worker:python,script:Some(script),on_update:Arc::new(|_|{})};
        let prepared=engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        assert!(bridge_wait(||engine.tasks.lock().unwrap().get(&prepared).is_some_and(|task|task.prompt.as_ref().is_some_and(|prompt|prompt["kind"]=="prepared_purchase"))).await);
        let pid=engine.tasks.lock().unwrap()[&prepared].prompt.as_ref().unwrap()["fixture_pid"].as_u64().unwrap();
        observe(&engine,&host,vec![target(&account,"R001",now_ms())]);
        assert!(!*engine.controls.lock().unwrap()[&prepared].cancel.borrow());engine.queue_pump_once();
        let assigned=jobs(&engine)[0].clone();assert_eq!(assigned.task_id.as_deref(),Some(prepared.as_str()));assert_eq!(assigned.assignment,1);
        assert!(bridge_wait(||jobs(&engine)[0].status=="succeeded").await);
        assert!(bridge_wait(||engine.controls.lock().unwrap().is_empty()).await);
        let task=engine.tasks.lock().unwrap()[&prepared].clone();assert_eq!(task.result.as_ref().unwrap()["pid"],pid);
        assert_eq!(task.prepared_handoff.as_ref().unwrap()["queue_job_id"],assigned.id);
        assert_eq!(task.prepared_handoff.as_ref().unwrap()["purchase_group_id"],assigned.purchase_group_id.unwrap());
        let done=jobs(&engine)[0].clone();assert!(done.dispatched_at_ms.is_some());assert!(done.started_at_ms.is_some());assert!(done.submit_started_at_ms.is_some());assert!(done.order_confirmed_at_ms.is_some());
    }

    #[tokio::test]
    async fn claimed_preparation_failure_waits_for_worker_terminal_before_next_store() {
        let project=Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();let python=project.join(if cfg!(windows){".venv/Scripts/python.exe"}else{".venv/bin/python"});
        let (root,engine,_,account)=fixture();save_policy(root.path(),&account,priority(&["R001","R002"]));
        let script=root.path().join("prepared-failure-fixture.py");fs::write(&script,r#"import hashlib,json,pathlib,sys,time
r=json.loads(sys.stdin.readline());root=pathlib.Path(sys.argv[2])
def emit(kind,**data):
 print(json.dumps(dict(v=1,account_id=r['account_id'],task_id=r['task_id'],type=kind,**data)),flush=True)
if r['action']=='session_maintain':
 raw=(root/'accounts'/r['account_id']/'checkout.json').read_bytes();now=int(time.time()*1000)
 emit('prompt',prompt={'id':'delayed-failure','kind':'prepared_purchase','label':'ready','prepared':{'v':1,'sku':'MLH74FE/A','quantity':1,'fulfillment':'RETAIL','config_fingerprint':'a'*64,'config_file_sha256':hashlib.sha256(raw).hexdigest(),'authorized_store_ids':['R001','R002'],'prepared_at_ms':now,'expires_at_ms':now+300000,'preparation_stage':'review','review_verified':True,'order_ready':True}})
 reply=json.loads(sys.stdin.readline());target=reply['value']['params']
else:
 target=r['params']['queue_targets'][0];emit('queue_ready')
identity={key:target[key] for key in ['queue_job_id','queue_assignment','purchase_group_id']}
emit('queue_started',**identity);emit('queue_finished',result={'status':'failed','success':False,'order_submitted':False},**identity)
if r['action']=='session_maintain':
 (root/'failed-before-exit').write_text('failed');deadline=time.monotonic()+10
 while not (root/'allow-prepared-exit').exists():
  assert time.monotonic()<deadline
  time.sleep(.01)
emit('result',result={'success':False,'status':'failed','order_submitted':False})
"#).unwrap();
        let host=TaskHost{worker:python,script:Some(script),on_update:Arc::new(|_|{})};let prepared=engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        assert!(bridge_wait(||engine.tasks.lock().unwrap()[&prepared].prompt.is_some()).await);
        observe(&engine,&host,vec![target(&account,"R001",now_ms()),target(&account,"R002",now_ms())]);engine.queue_pump_once();
        assert!(bridge_wait(||root.path().join("failed-before-exit").exists()&&jobs(&engine)[0].status=="failed").await);
        engine.queue_pump_once();let delayed=jobs(&engine);assert_eq!(delayed[0].task_id.as_deref(),Some(prepared.as_str()));assert!(delayed[1].pending());assert!(delayed[1].task_id.is_none());
        assert!(engine.queue_runtime.lock().unwrap().coordinators[&account].closing);
        fs::write(root.path().join("allow-prepared-exit"),b"release original worker").unwrap();
        assert!(bridge_wait(||!engine.controls.lock().unwrap().contains_key(&prepared)).await);engine.queue_pump_once();
        let next=jobs(&engine);assert!(next[1].task_id.is_some());assert_ne!(next[1].task_id.as_deref(),Some(prepared.as_str()));
        assert!(bridge_wait(||jobs(&engine)[1].status=="failed"&&!engine.controls.lock().unwrap().contains_key(next[1].task_id.as_deref().unwrap())).await);
    }

    #[tokio::test]
    async fn cancelling_claimed_preparation_signals_owner_worker_and_preserves_submission_evidence() {
        for submitted in [false,true] {
            let project=Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();let python=project.join(if cfg!(windows){".venv/Scripts/python.exe"}else{".venv/bin/python"});
            let (root,engine,_,account)=fixture();save_policy(root.path(),&account,priority(&["R001"]));
            let script=root.path().join("prepared-cancel-fixture.py");fs::write(&script,r#"import hashlib,json,pathlib,sys,time
r=json.loads(sys.stdin.readline());root=pathlib.Path(sys.argv[2])
def emit(kind,**data):
 print(json.dumps(dict(v=1,account_id=r['account_id'],task_id=r['task_id'],type=kind,**data)),flush=True)
raw=(root/'accounts'/r['account_id']/'checkout.json').read_bytes();now=int(time.time()*1000)
emit('prompt',prompt={'id':'cancel-owner','kind':'prepared_purchase','label':'ready','prepared':{'v':1,'sku':'MLH74FE/A','quantity':1,'fulfillment':'RETAIL','config_fingerprint':'a'*64,'config_file_sha256':hashlib.sha256(raw).hexdigest(),'authorized_store_ids':['R001'],'prepared_at_ms':now,'expires_at_ms':now+300000,'preparation_stage':'review','review_verified':True,'order_ready':True}})
reply=json.loads(sys.stdin.readline());target=reply['value']['params'];identity={key:target[key] for key in ['queue_job_id','queue_assignment','purchase_group_id']}
emit('queue_started',**identity)
if r['params'].get('_fixture_submitted'):
 emit('stage',message='synthetic submission already sent',order_submitted=True,**identity)
(root/'claim-awaiting-cancel').write_text('ready')
command=json.loads(sys.stdin.readline());assert command['type']=='cancel'
(root/'cancel-received-without-further-submit').write_text('cancel')
emit('cancelled')
"#).unwrap();
            let host=TaskHost{worker:python,script:Some(script),on_update:Arc::new(|_|{})};let prepared=engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({"_fixture_submitted":submitted})).unwrap();
            assert!(bridge_wait(||engine.tasks.lock().unwrap()[&prepared].prompt.is_some()).await);
            observe(&engine,&host,vec![target(&account,"R001",now_ms())]);engine.queue_pump_once();
            assert!(bridge_wait(||root.path().join("claim-awaiting-cancel").exists()&&jobs(&engine)[0].status=="running"&&jobs(&engine)[0].order_submitted==submitted).await);
            let job=jobs(&engine)[0].clone();engine.queue_command(host.clone(),"cancel",&json!({"job_id":job.id})).unwrap();
            assert!(bridge_wait(||root.path().join("cancel-received-without-further-submit").exists()&&!engine.controls.lock().unwrap().contains_key(&prepared)).await);
            let finished=jobs(&engine)[0].clone();assert!(!finished.pending());assert_eq!(finished.order_submitted,submitted);
            assert_eq!(engine.tasks.lock().unwrap()[&prepared].status,"cancelled");
            if submitted {assert_eq!(engine.purchase_queue.lock().unwrap().blocked_accounts[&account],"submission_unknown");}
        }
    }

    #[tokio::test]
    async fn real_python_single_store_group_success_cancels_waiting_sibling() {
        let project=Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python=project.join(if cfg!(windows){".venv/Scripts/python.exe"}else{".venv/bin/python"});
        assert!(python.is_file(),"run worker setup before the offline priority bridge fixture");
        let (root,engine,_,account)=fixture();let audit=root.path().join("queue-bridge-audit");fs::create_dir_all(&audit).unwrap();
        let _cleanup=BridgeCleanup {engine:engine.clone(),audit:audit.clone()};
        let host=TaskHost {worker:python,script:Some(project.join("tests/fixtures/queue_bridge_worker.py")),on_update:Arc::new(|_|{})};
        let mut policy=priority(&["R002","R001","R003"]);
        for row in policy["stores"].as_array_mut().unwrap() {row["store_name"]=json!(format!("Test store {}",row["store_id"].as_str().unwrap()));}
        save_policy(root.path(),&account,policy);
        private_json(&scope_dir(root.path(),&account).join("auth.json"),&json!({"account":"queue@example.test","cookies":{}})).unwrap();
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        let mut a=target(&account,"R001",now_ms());let mut b=target(&account,"R002",now_ms());a["store_name"]=json!("Test store R001");b["store_name"]=json!("Test store R002");
        let outcomes=engine.inventory_dispatch(&host,&json!({"targets":[a.clone(),b.clone()]}));
        assert_eq!(outcomes.len(),2);assert_eq!(outcomes[0]["status"],"queued");engine.queue_pump_once();
        assert!(bridge_wait(||bridge_audit(&audit.join("children.jsonl")).iter().any(|entry|entry["type"]=="start")).await,
            "priority child missing: {}",json!(bridge_audit(&audit.join("protocol.jsonl"))));
        assert!(bridge_wait(|| {
            let current=jobs(&engine);
            bridge_audit(&audit.join("protocol.jsonl")).iter().any(|event|event["type"]=="stage"
                &&event["queue_job_id"]==current[0].id&&event["message"]==current[0].message)
        }).await,"assigned target stage was not routed from the real worker");
        let starts=bridge_audit(&audit.join("children.jsonl"));assert_eq!(starts.len(),1);assert_eq!(starts[0]["store_id"],"R002");
        assert_eq!(starts[0]["pickup_policy"]["stores"].as_array().unwrap().len(),1);
        assert_eq!(starts[0]["pickup_policy"]["stores"][0]["store_id"],"R002");
        assert_eq!(starts[0]["pickup_policy"]["time_slot_rule"],json!({"mode":"earliest","fallback":"earliest"}));
        assert_eq!(engine.inventory_dispatch(&host,&json!({"targets":[a,b]}))[0]["status"],"already_queued");
        assert_eq!(jobs(&engine).len(),2);fs::write(audit.join("release-R002"),b"synthetic release").unwrap();
        assert!(bridge_wait(||jobs(&engine)[0].status=="succeeded").await);assert_eq!(jobs(&engine)[1].status,"cancelled");
        let task_id=jobs(&engine)[0].task_id.clone().unwrap();
        assert!(bridge_wait(||engine.queue_runtime.lock().unwrap().coordinators.get(&account).is_some_and(|coordinator|coordinator.idle)).await);
        engine.queue_pump_once();assert!(bridge_wait(||engine.controls.lock().unwrap().is_empty()).await);
        let task=engine.tasks.lock().unwrap()[&task_id].clone();let result=task.result.unwrap();
        assert_eq!(result["pending_orders"].as_array().unwrap().len(),1);assert_eq!(result["pending_orders"][0]["pickup_store_id"],"R002");
        assert_eq!(result["queue_jobs"][0]["pickup_policy"],starts[0]["pickup_policy"]);
        assert_eq!(engine.queue_command(host,"status",&json!({})).unwrap()["reserved_child_slots"],0);
    }

    #[tokio::test]
    async fn real_python_accounts_run_in_parallel_and_each_account_streams_stores_serially() {
        let project=Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python=project.join(if cfg!(windows){".venv/Scripts/python.exe"}else{".venv/bin/python"});
        assert!(python.is_file(),"run worker setup before offline bridge fixture");
        let (root,engine,_,account)=fixture();let other=engine.add_account("other@example.test".into()).unwrap();engine.accounts.lock().unwrap()[1].pending=false;
        let audit=root.path().join("queue-bridge-audit");fs::create_dir_all(&audit).unwrap();let _cleanup=BridgeCleanup {engine:engine.clone(),audit:audit.clone()};
        let host=TaskHost {worker:python,script:Some(project.join("tests/fixtures/queue_bridge_worker.py")),on_update:Arc::new(|_|{})};
        for owner in [&account,&other.id] {
            let email=if *owner==account {"queue@example.test"}else {"other@example.test"};
            private_json(&scope_dir(root.path(),owner).join("auth.json"),&json!({"account":email,"cookies":{}})).unwrap();
            private_json(&scope_dir(root.path(),owner).join("checkout.json"),&json!({})).unwrap();
        }
        engine.queue_command(host.clone(),"configure",&json!({"mode":"parallel","max_concurrency":2})).unwrap();
        let mut first=target(&account,"R001",now_ms());first["store_name"]=json!("Test store R001");
        let mut second=target(&other.id,"R002",now_ms());second["store_name"]=json!("Test store R002");
        engine.inventory_dispatch(&host,&json!({"targets":[first.clone(),second.clone()]}));engine.queue_pump_once();
        assert!(bridge_wait(||bridge_audit(&audit.join("children.jsonl")).iter().filter(|entry|entry["type"]=="start").count()==2).await,
            "real account children did not start: {}",json!(bridge_audit(&audit.join("protocol.jsonl"))));
        let initial=jobs(&engine);assert!(initial.iter().all(|job|job.inflight()));assert_ne!(initial[0].task_id,initial[1].task_id);
        let mut third=target(&account,"R003",now_ms());third["store_name"]=json!("Test store R003");
        engine.inventory_dispatch(&host,&json!({"targets":[first,second,third]}));engine.queue_pump_once();assert!(jobs(&engine)[2].pending());
        fs::write(audit.join("release-R001"),b"first account first store").unwrap();
        assert!(bridge_wait(||jobs(&engine)[0].status=="succeeded").await);engine.queue_pump_once();
        assert!(bridge_wait(||bridge_audit(&audit.join("children.jsonl")).iter().any(|entry|entry["type"]=="start"&&entry["store_id"]=="R003")).await);
        let current=jobs(&engine);assert_eq!(current[1].status,"running");assert_eq!(current[2].task_id,initial[0].task_id);
        fs::write(audit.join("release-R002"),b"second account").unwrap();fs::write(audit.join("release-R003"),b"first account second store").unwrap();
        assert!(bridge_wait(||jobs(&engine).iter().all(|job|job.status=="succeeded")).await);
        assert!(bridge_wait(||engine.queue_runtime.lock().unwrap().coordinators.values().all(|coordinator|coordinator.idle)).await);engine.queue_pump_once();
        assert!(bridge_wait(||engine.controls.lock().unwrap().is_empty()).await);
        let events=bridge_audit(&audit.join("children.jsonl"));let(mut active,mut maximum)=(0_i32,0_i32);
        for event in &events {if event["type"]=="start"{active+=1;maximum=maximum.max(active);}else{active-=1;}assert!(active>=0);}
        assert_eq!((maximum,active),(2,0));assert_eq!(events.len(),6);
        let first_finish=events.iter().find(|event|event["type"]=="finish"&&event["store_id"]=="R001").unwrap()["at_ns"].as_u64().unwrap();
        let third_start=events.iter().find(|event|event["type"]=="start"&&event["store_id"]=="R003").unwrap()["at_ns"].as_u64().unwrap();assert!(third_start>=first_finish);
        let protocol=bridge_audit(&audit.join("protocol.jsonl"));assert_eq!(protocol.iter().filter(|event|event["type"]=="queue_finished").count(),3);
        let state=engine.queue_command(host,"status",&json!({})).unwrap();assert_eq!(state["running_count"],0);assert_eq!(state["queued_count"],0);
    }

}
