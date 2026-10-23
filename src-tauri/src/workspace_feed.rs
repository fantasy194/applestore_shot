//! Read-only Web transport. Persistent tasks remain complete; only the wire view is reduced.
use super::{Account, Engine, Task, LOCAL_ID};
use serde_json::{json, Value};
use std::{collections::{BTreeMap, VecDeque}, hash::Hasher, io::Write};
use uuid::Uuid;

const RETAINED_CHANGES: usize = 128;

struct Digest(std::collections::hash_map::DefaultHasher);
impl Write for Digest {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> { self.0.write(bytes); Ok(bytes.len()) }
    fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
}
fn revision(task: &Task) -> String {
    let mut digest = Digest(Default::default());
    // Hash the complete content, including bounded log replacement, rather than second-resolution timestamps.
    serde_json::to_writer(&mut digest, task).expect("task serialization");
    format!("{:016x}", digest.0.finish())
}
// Preserve only bounded order-attention evidence in the polling view.
// Never copy nested payment/contact details into the workspace transport.
pub(super) fn purchase_attention(result: &Value) -> Value {
    struct Attention {
        pending: std::collections::BTreeSet<String>,
        unknown: std::collections::BTreeSet<String>,
        pending_unidentified: bool,
        unknown_unidentified: bool,
        remaining: usize,
    }
    fn number(value: &Value) -> Option<&str> {
        value.as_str().map(str::trim).filter(|number| !number.is_empty() && number.len() <= 128
            && number.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-'))
    }
    impl Attention {
        fn visit(&mut self, node: &Value, depth: usize, pending_entry: bool) {
            if depth > 3 || self.remaining == 0 {
                self.unknown_unidentified = true;
                return;
            }
            self.remaining -= 1;
            let order_number = number(&node["order_number"]).or_else(|| number(&node["result"]["order_number"]));
            let pending = pending_entry || node["status"] == "pending_payment";
            let known = order_number.is_some()
                || node["pending_orders"].as_array().is_some_and(|rows| !rows.is_empty())
                || node["result"]["pending_orders"].as_array().is_some_and(|rows| !rows.is_empty());
            let has_children = ["queue_jobs", "attempts", "jobs"].iter()
                .any(|key| node[*key].as_array().is_some_and(|rows| !rows.is_empty()));
            let unknown = matches!(node["status"].as_str(), Some("submission_unknown" | "payment_unknown"))
                || (node["order_submitted"] == true && !known && !has_children);
            if pending {
                if let Some(number) = order_number { self.pending.insert(number.into()); }
                else { self.pending_unidentified = true; }
            }
            if unknown {
                if let Some(number) = order_number { self.unknown.insert(number.into()); }
                else { self.unknown_unidentified = true; }
            }
            for key in ["pending_orders", "queue_jobs", "attempts", "jobs"] {
                if let Some(rows) = node[key].as_array() {
                    if depth == 3 && !rows.is_empty() { self.unknown_unidentified = true; return; }
                    for row in rows {
                        if self.remaining == 0 { self.unknown_unidentified = true; return; }
                        self.visit(row, depth + 1, key == "pending_orders");
                    }
                }
            }
            if let Some(nested) = node.get("result").filter(|nested| nested.is_object()) {
                self.visit(nested, depth + 1, false);
            }
        }
    }
    let mut attention = Attention {pending:Default::default(), unknown:Default::default(),
        pending_unidentified:false, unknown_unidentified:false, remaining:100};
    attention.visit(result, 0, false);
    json!({"pending_order_numbers":attention.pending,"unknown_order_numbers":attention.unknown,
        "pending_unidentified":attention.pending_unidentified,"unknown_unidentified":attention.unknown_unidentified})
}

fn summary(task: &Task) -> Value {
    let result = task.result.as_ref().map(|result| {
        let mut reduced = serde_json::Map::new();
        for key in ["success", "status", "message", "logged_in", "scope", "name", "account_removed", "order_number", "device_trust_saved", "preparation_stage", "order_ready", "review_verified"] {
            if let Some(value) = result.get(key).filter(|value| value.is_boolean() || value.is_number() || value.is_string()) {
                let bounded = match value.as_str() {
                    Some(text) => Value::String(text.chars().take(2048).collect()),
                    None => value.clone(),
                };
                reduced.insert(key.into(), bounded);
            }
        }
        if matches!(task.action.as_str(), "purchase" | "checkout") {
            reduced.insert("purchase_attention".into(), purchase_attention(result));
        }
        // Older purchase cards need exact official terminal evidence even when
        // their historical order-query details have never been opened in this browser.
        // Keep every item status (including unknown/null) so reducing the payload
        // cannot turn a partially cancelled order into an all-cancelled order.
        if task.action == "orders" {
            if let Some(orders) = result["orders"].as_array() {
                let verified: Vec<_> = orders.iter().filter(|order| order["verified_by"] == "official_detail")
                    .filter_map(|order| {
                        let number = order["order_number"].as_str().filter(|number| number.len() <= 128)?;
                        let items = order["items"].as_array()?;
                        let statuses: Vec<_> = items.iter().map(|item| json!({"status":item["status"].as_str()})).collect();
                        Some(json!({"order_number":number,"verified_by":"official_detail","items":statuses}))
                    }).collect();
                if !verified.is_empty() { reduced.insert("orders".into(),json!(verified)); }
            }
        }
        Value::Object(reduced)
    });
    json!({"id":task.id,"account_id":task.account_id,"action":task.action,"status":task.status,
        "stage":task.stage,"purchase_progress":task.purchase_progress.as_ref().map(|progress|progress.sanitized()),
        "created_at":task.created_at,"updated_at":task.updated_at,
        "execution_started_at_ms":task.execution_started_at_ms,"prompt":task.prompt,"result":result,
        "order_submitted":task.order_submitted,"logs":[],"schedule":task.schedule,
        "queue_job_ids":task.queue_job_ids,"is_summary":true,"detail_revision":revision(task),
        "log_count":task.logs.len()})
}

#[derive(Clone)]
struct Change {
    revision: u64,
    tasks: BTreeMap<String, Value>,
    removed: Vec<String>,
    accounts_changed: bool,
    defaults_changed: bool,
}
pub(super) struct WorkspaceFeed {
    generation: String,
    revision: u64,
    tasks: BTreeMap<String, Value>,
    accounts: Value,
    defaults: Value,
    changes: VecDeque<Change>,
}
impl Default for WorkspaceFeed {
    fn default() -> Self {
        Self {generation:Uuid::new_v4().to_string(),revision:0,tasks:BTreeMap::new(),
            accounts:Value::Null,defaults:Value::Null,changes:VecDeque::new()}
    }
}

fn sorted_tasks(tasks: impl Iterator<Item = Value>) -> Vec<Value> {
    let mut tasks: Vec<_> = tasks.collect();
    tasks.sort_by(|a,b| b["created_at"].as_u64().cmp(&a["created_at"].as_u64()).then_with(|| a["id"].as_str().cmp(&b["id"].as_str())));
    tasks
}

impl Engine {
    /// A scoped explicit detail read is the only Web task API returning logs and large results.
    pub fn task_detail(&self, account_id: &str, task_id: &str) -> Result<Value, String> {
        self.task_read(account_id, task_id, true)
    }

    pub fn task_read(&self, account_id: &str, task_id: &str, include_logs: bool) -> Result<Value, String> {
        Uuid::parse_str(account_id).map_err(|_| "账号标识无效")?;
        Uuid::parse_str(task_id).map_err(|_| "任务标识无效")?;
        if account_id != LOCAL_ID && !self.accounts.lock().unwrap().iter().any(|account| account.id == account_id) {
            return Err("任务不存在或不属于此账号".into());
        }
        let tasks = self.tasks.lock().unwrap();
        let task = tasks.get(task_id).filter(|task| task.account_id == account_id)
            .ok_or("任务不存在或不属于此账号")?;
        // Serialize only the requested fields, so monitor reads do not even allocate old logs.
        let mut detail = summary(task);
        detail["result"] = task.result.clone().unwrap_or(Value::Null);
        if include_logs { detail["logs"] = json!(task.logs); }
        detail["is_summary"] = json!(false);
        detail["logs_included"] = json!(include_logs);
        Ok(detail)
    }

    pub fn workspace_snapshot(&self) -> Value {
        self.workspace_changes(None)["snapshot"].take()
    }

    /// One process-wide bounded journal serves every client; cursor expiry safely resets to a light view.
    pub fn workspace_changes(&self, cursor: Option<&str>) -> Value {
        let mut feed = self.workspace_feed.lock().unwrap();
        // Never hold account/default locks while acquiring tasks (mutations use their own established order).
        let accounts: Vec<Account> = self.accounts.lock().unwrap().clone();
        let defaults = serde_json::to_value(self.defaults.lock().unwrap().clone()).unwrap();
        let tasks: BTreeMap<_,_> = self.tasks.lock().unwrap().values()
            .filter(|task| task.account_id == LOCAL_ID || accounts.iter().any(|a| a.id == task.account_id))
            .map(|task| (task.id.clone(),summary(task))).collect();
        let accounts = serde_json::to_value(accounts).unwrap();
        let changed: BTreeMap<_,_> = tasks.iter().filter(|(id,task)| feed.tasks.get(*id) != Some(*task))
            .map(|(id,task)| (id.clone(),task.clone())).collect();
        let removed: Vec<_> = feed.tasks.keys().filter(|id| !tasks.contains_key(*id)).cloned().collect();
        let accounts_changed = accounts != feed.accounts;
        let defaults_changed = defaults != feed.defaults;
        if !changed.is_empty() || !removed.is_empty() || accounts_changed || defaults_changed {
            feed.revision += 1;
            let change = Change {revision:feed.revision,tasks:changed,removed,accounts_changed,defaults_changed};
            feed.changes.push_back(change);
            while feed.changes.len() > RETAINED_CHANGES { feed.changes.pop_front(); }
            feed.tasks = tasks; feed.accounts = accounts; feed.defaults = defaults;
        }
        let requested = cursor.and_then(|cursor| cursor.rsplit_once(':'))
            .filter(|(generation,_)| *generation == feed.generation)
            .and_then(|(_,revision)| revision.parse::<u64>().ok())
            .filter(|revision| *revision <= feed.revision && feed.changes.front().is_none_or(|oldest| revision.saturating_add(1) >= oldest.revision));
        let current_cursor = format!("{}:{}",feed.generation,feed.revision);
        let Some(requested) = requested else {
            return json!({"cursor":current_cursor,"reset":true,"snapshot":{
                "accounts":feed.accounts,"tasks":sorted_tasks(feed.tasks.values().cloned()),
                "defaults":feed.defaults,"data_dir":self.root.to_string_lossy()},"tasks":[],"removed_task_ids":[]});
        };
        let mut changed = BTreeMap::new();
        let mut removed = std::collections::BTreeSet::new();
        let mut accounts_changed = false;
        let mut defaults_changed = false;
        for change in feed.changes.iter().filter(|change| change.revision > requested) {
            for (id,task) in &change.tasks { removed.remove(id); changed.insert(id.clone(),task.clone()); }
            for id in &change.removed { changed.remove(id); removed.insert(id.clone()); }
            accounts_changed |= change.accounts_changed; defaults_changed |= change.defaults_changed;
        }
        let mut response = json!({"cursor":current_cursor,"reset":false,
            "tasks":sorted_tasks(changed.into_values()),"removed_task_ids":removed});
        if accounts_changed { response["accounts"] = feed.accounts.clone(); }
        if defaults_changed { response["defaults"] = feed.defaults.clone(); }
        response
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn task(id: &str) -> Task {
        serde_json::from_value(json!({"id":id,"account_id":LOCAL_ID,"action":"inventory_monitor",
            "status":"running","stage":"正在检查库存","created_at":100,"updated_at":100,
            "prompt":null,"result":{"targets":[{"sku":"TESTSKU","store_id":"R001","status":"available"}]},
            "order_submitted":false,"logs":["initial"]})).unwrap()
    }
    fn setup() -> (tempfile::TempDir, std::sync::Arc<Engine>, String) {
        let root = tempfile::tempdir().unwrap();
        let engine = Engine::new(root.path().into()).unwrap();
        let id = Uuid::new_v4().to_string();
        engine.tasks.lock().unwrap().insert(id.clone(),task(&id));
        (root,engine,id)
    }
    fn cursor(response: &Value) -> &str { response["cursor"].as_str().unwrap() }

    #[test]
    fn summary_is_small_and_retains_auth_order_and_schedule_metadata() {
        let (_root,engine,id) = setup();
        let mut tasks = engine.tasks.lock().unwrap();
        let task = tasks.get_mut(&id).unwrap();
        task.result = Some(json!({"success":true,"status":"pending_payment","order_number":"W123",
            "qr_code":"PRIVATE_QR","pickup_contact":{"email":"PRIVATE_CONTACT"},"message":"请付款"}));
        task.prompt = Some(json!({"id":"request","kind":"code","label":"输入验证码"}));
        task.order_submitted = true;
        task.queue_job_ids = vec!["job-1".into()];
        task.schedule = Some(serde_json::from_value(json!({"at_ms":1000,"sku":"TEST","quantity":1,"fulfillment":"RETAIL"})).unwrap());
        task.logs = vec!["PRIVATE_LOG".repeat(10000)];
        drop(tasks);
        let summary = engine.workspace_snapshot();
        let serialized = summary.to_string();
        assert!(!serialized.contains("PRIVATE_"));
        let view = &summary["tasks"][0];
        assert_eq!(view["result"]["status"],"pending_payment");
        assert_eq!(view["result"]["order_number"],"W123");
        assert_eq!(view["queue_job_ids"],json!(["job-1"]));
        assert_eq!(view["schedule"]["sku"],"TEST");
        assert_eq!(view["prompt"]["id"],"request");
        assert_eq!(view["order_submitted"],true);
        assert_eq!(view["logs"],json!([]));
        assert_eq!(view["log_count"],1);
        assert!(serialized.len() < 2000);
        assert!(engine.snapshot().tasks[0].logs[0].contains("PRIVATE_LOG"));
    }

    #[test]
    fn purchase_summary_keeps_queue_payment_and_unknown_attention_without_nested_details() {
        let mut current = task(&Uuid::new_v4().to_string());
        current.action = "purchase".into();
        current.result = Some(json!({"success":true,"status":"queue_complete","message":"队列已完成",
            "pending_orders":[{"order_number":"W123","payment_url":"PRIVATE_URL","contact":"PRIVATE_CONTACT"}],
            "queue_jobs":[{"status":"succeeded","result":{"status":"pending_payment","order_number":"W123"}},
                {"status":"submission_unknown","result":{"order_number":"W456","qr_data":"PRIVATE_QR"}}],
            "attempts":[{"status":"payment_unknown","order_number":"W789"}]}));
        let view = summary(&current);
        assert_eq!(view["result"]["status"],"queue_complete");
        assert_eq!(view["result"]["success"],true);
        assert_eq!(view["result"]["message"],"队列已完成");
        assert_eq!(view["result"]["purchase_attention"],json!({"pending_order_numbers":["W123"],
            "unknown_order_numbers":["W456","W789"],"pending_unidentified":false,"unknown_unidentified":false}));
        let serialized = view.to_string();
        for private in ["PRIVATE_","pending_orders","queue_jobs","attempts","payment_url","contact","qr_data"] {
            assert!(!serialized.contains(private),"{private}");
        }
        current.action = "checkout".into();assert_eq!(summary(&current)["result"]["purchase_attention"],view["result"]["purchase_attention"]);
        current.action = "inventory_monitor".into();assert!(summary(&current)["result"].get("purchase_attention").is_none());
    }

    #[test]
    fn purchase_summary_marks_missing_numbers_and_unconfirmed_submitted_leaves() {
        let evidence = purchase_attention(&json!({"status":"queue_complete","order_submitted":true,
            "pending_orders":[{"order_number":""}],"jobs":[{"status":"succeeded","order_submitted":true}],
            "attempts":[{"status":"submission_unknown"},{"status":"payment_unknown","order_number":"W123"}]}));
        assert_eq!(evidence,json!({"pending_order_numbers":[],"unknown_order_numbers":["W123"],
            "pending_unidentified":true,"unknown_unidentified":true}));
        assert_eq!(purchase_attention(&json!({"order_submitted":true}))["unknown_unidentified"],true);
        assert_eq!(purchase_attention(&json!({"order_submitted":true,"order_number":"W123"}))["unknown_unidentified"],false);
        assert_eq!(purchase_attention(&json!({"order_submitted":true,"jobs":[{"status":"pending_payment","order_number":"W123"}]})),
            json!({"pending_order_numbers":["W123"],"unknown_order_numbers":[],"pending_unidentified":false,"unknown_unidentified":false}));
    }

    #[test]
    fn purchase_attention_bounds_depth_entries_and_identifiers_without_hiding_uncertainty() {
        let deepest = json!({"result":{"result":{"result":{"status":"pending_payment","order_number":"W123"}}}});
        assert_eq!(purchase_attention(&deepest)["pending_order_numbers"],json!(["W123"]));
        let truncated = json!({"result":deepest});
        assert_eq!(purchase_attention(&truncated)["pending_order_numbers"],json!([]));
        assert_eq!(purchase_attention(&truncated)["unknown_unidentified"],true);
        let rows:Vec<_> = (0..150).map(|i|json!({"order_number":format!("W{i:03}"),"contact":"PRIVATE_CONTACT"})).collect();
        let bounded = purchase_attention(&json!({"pending_orders":rows}));
        assert_eq!(bounded["pending_order_numbers"].as_array().unwrap().len(),99);
        assert_eq!(bounded["unknown_unidentified"],true);
        let invalid = purchase_attention(&json!({"pending_orders":[{"order_number":"W".repeat(129)},
            {"order_number":"https://PRIVATE_PAYMENT"},{"order_number":"  W123  "},{"order_number":"W123"}],
            "jobs":[{"status":"submission_unknown","order_number":"private@example.test"}]}));
        assert_eq!(invalid,json!({"pending_order_numbers":["W123"],"unknown_order_numbers":[],
            "pending_unidentified":true,"unknown_unidentified":true}));
        assert!(!invalid.to_string().contains("PRIVATE_"));assert!(!invalid.to_string().contains("example.test"));
    }

    #[test]
    fn purchase_progress_summary_is_fixed_bounded_and_does_not_copy_private_metadata() {
        let mut current=task(&Uuid::new_v4().to_string());current.action="purchase".into();
        current.purchase_progress=Some(serde_json::from_value(json!({"current":"payment",
            "completed":["prepare","verify","checkout","submit","payment","submit","PRIVATE_CONTACT"],
            "contact":"PRIVATE_CONTACT","payment_url":"PRIVATE_PAYMENT","logs":["PRIVATE_LOG"]})).unwrap());
        let light=summary(&current);
        assert_eq!(light["purchase_progress"],json!({"current":"payment","completed":["prepare","verify","checkout","submit"]}));
        assert!(!light.to_string().contains("PRIVATE_"));
        assert!(light["purchase_progress"].to_string().len()<128);
        current.purchase_progress=Some(serde_json::from_value(json!({"current":"PRIVATE_URL".repeat(1000),
            "completed":vec!["PRIVATE_CONTACT";10000]})).unwrap());
        assert_eq!(summary(&current)["purchase_progress"],json!({"current":null,"completed":[]}));
        current.purchase_progress=None;assert!(summary(&current)["purchase_progress"].is_null());
    }

    #[test]
    fn same_second_and_same_length_log_replacements_have_distinct_revisions() {
        let (_root,engine,id) = setup();
        let first = engine.workspace_changes(None);
        engine.tasks.lock().unwrap().get_mut(&id).unwrap().logs[0] = "changed".into();
        let second = engine.workspace_changes(Some(cursor(&first)));
        assert_eq!(second["reset"],false);
        assert_eq!(second["tasks"].as_array().unwrap().len(),1);
        assert_ne!(first["snapshot"]["tasks"][0]["detail_revision"],second["tasks"][0]["detail_revision"]);
        engine.tasks.lock().unwrap().get_mut(&id).unwrap().result = Some(json!({"success":false}));
        let third = engine.workspace_changes(Some(cursor(&first)));
        assert_eq!(third["tasks"].as_array().unwrap().len(),1);
        assert_eq!(third["tasks"][0]["result"]["success"],false);
        let empty = engine.workspace_changes(Some(cursor(&third)));
        assert_eq!(empty["tasks"],json!([]));
        assert_eq!(cursor(&empty),cursor(&third));
        assert!(empty.to_string().len() < 200);
    }

    #[test]
    fn scoped_detail_rejects_wrong_account_and_supports_result_only_reads() {
        let (_root,engine,id) = setup();
        let account = engine.add_account("fixture@example.test".into()).unwrap();
        assert!(engine.task_detail(&account.id,&id).is_err());
        assert!(engine.task_detail("../../private",&id).is_err());
        assert!(engine.task_detail(LOCAL_ID,"../../private").is_err());
        assert!(engine.task_detail(LOCAL_ID,&Uuid::new_v4().to_string()).is_err());
        let full = engine.task_detail(LOCAL_ID,&id).unwrap();
        let result = engine.task_read(LOCAL_ID,&id,false).unwrap();
        assert_eq!(full["logs"],json!(["initial"]));
        assert_eq!(result["logs"],json!([]));
        assert_eq!(result["result"]["targets"][0]["sku"],"TESTSKU");
        assert_eq!(full["detail_revision"],result["detail_revision"]);
        assert_eq!(result["logs_included"],false);
        assert_eq!(result["is_summary"],false);
    }

    #[test]
    fn history_summary_preserves_exact_official_order_terminal_evidence_without_private_details() {
        let (_root,engine,id) = setup();
        {
            let mut tasks = engine.tasks.lock().unwrap();
            let query = tasks.get_mut(&id).unwrap();
            query.action = "orders".into();
            query.result = Some(json!({"success":true,"device_trust_saved":true,"orders":[
                {"order_number":"W123","verified_by":"official_detail","items":[
                    {"status":"PAYMENT_EXPIRED_STORED_ORDER","detail_url":"PRIVATE_LINK","name":"PRIVATE_NAME"}]},
                {"order_number":"W456","verified_by":"official_detail","items":[
                    {"status":"CANCELLED_STORED_ORDER"},{"status":null}]},
                {"order_number":"W789","verified_by":"list_only","items":[{"status":"CANCELLED"}]}
            ],"pickup_contact":"PRIVATE_CONTACT","qr":"PRIVATE_QR"}));
        }
        let snapshot = engine.workspace_snapshot();
        let result = &snapshot["tasks"][0]["result"];
        assert_eq!(result["device_trust_saved"],true);
        assert_eq!(result["orders"].as_array().unwrap().len(),2);
        assert_eq!(result["orders"][0],json!({"order_number":"W123","verified_by":"official_detail",
            "items":[{"status":"PAYMENT_EXPIRED_STORED_ORDER"}]}));
        assert_eq!(result["orders"][1]["items"],json!([{"status":"CANCELLED_STORED_ORDER"},{"status":null}]));
        assert!(!snapshot.to_string().contains("PRIVATE_"));
    }

    #[test]
    fn accounts_add_remove_and_defaults_are_recoverable_without_lost_tasks() {
        let (_root,engine,id) = setup();
        let first = engine.workspace_changes(None);
        let account = engine.add_account("new@example.test".into()).unwrap();
        let added = engine.workspace_changes(Some(cursor(&first)));
        assert_eq!(added["accounts"][0]["id"],account.id);
        engine.accounts.lock().unwrap().clear();
        engine.tasks.lock().unwrap().remove(&id);
        engine.defaults.lock().unwrap().preset_quantity = 3;
        let removed = engine.workspace_changes(Some(cursor(&added)));
        assert_eq!(removed["accounts"],json!([]));
        assert_eq!(removed["removed_task_ids"],json!([id]));
        assert_eq!(removed["defaults"]["preset_quantity"],3);
        let recovered = engine.workspace_changes(Some("previous-process:0"));
        assert_eq!(recovered["reset"],true);
        assert_eq!(recovered["snapshot"]["tasks"],json!([]));
    }

    #[test]
    fn expired_future_or_malformed_cursor_resets_and_journal_is_bounded() {
        let (_root,engine,id) = setup();
        let first = engine.workspace_changes(None);
        for index in 0..140 {
            engine.tasks.lock().unwrap().get_mut(&id).unwrap().stage = format!("round {index}");
            engine.workspace_changes(None);
        }
        assert_eq!(engine.workspace_feed.lock().unwrap().changes.len(),RETAINED_CHANGES);
        assert_eq!(engine.workspace_changes(Some(cursor(&first)))["reset"],true);
        assert_eq!(engine.workspace_changes(Some("invalid"))["reset"],true);
        let generation = engine.workspace_feed.lock().unwrap().generation.clone();
        assert_eq!(engine.workspace_changes(Some(&format!("{generation}:999999")))["reset"],true);
    }

    #[test]
    fn large_history_does_not_expand_monitor_delta_or_explicit_result_only_read() {
        let (_root,engine,id) = setup();
        {
            let mut tasks = engine.tasks.lock().unwrap();
            for _ in 0..340 {
                let history_id = Uuid::new_v4().to_string();
                let mut history = task(&history_id);
                history.status = "succeeded".into();
                history.action = "orders".into();
                history.result = Some(json!({"old_query":"private-data".repeat(1000)}));
                history.logs = vec!["historical-log".repeat(100);30];
                tasks.insert(history_id,history);
            }
            tasks.get_mut(&id).unwrap().logs = vec!["monitor-log".repeat(1000);100];
        }
        let full = serde_json::to_vec(&engine.snapshot()).unwrap().len();
        let first = engine.workspace_changes(None);
        let light = serde_json::to_vec(&first["snapshot"]).unwrap().len();
        engine.tasks.lock().unwrap().get_mut(&id).unwrap().result = Some(json!({"targets":[{"sku":"TESTSKU","store_id":"R001","status":"unavailable"}],"checked_at_ms":12345}));
        let delta = engine.workspace_changes(Some(cursor(&first)));
        let delta_size = delta.to_string().len();
        let monitor = engine.task_read(LOCAL_ID,&id,false).unwrap().to_string().len();
        eprintln!("history fixture bytes: full={full}, initial_light={light}, monitor_delta={delta_size}, monitor_detail_without_logs={monitor}");
        assert!(full > 10_000_000);
        assert!(light * 20 < full);
        assert!(delta_size < 10_000);
        assert!(monitor < 10_000);
        assert_eq!(engine.snapshot().tasks.len(),341);
    }
}
