//! Replay only the bounded fields needed by Web Push. Observations are
//! idempotent; no account, checkout or order action is sent through here.
use std::collections::HashMap;

use serde_json::{json, Map, Value};

use crate::{engine::Task, push::PushBridge};

// The Python JSONL daemon accepts at most four million characters per line.
// Count UTF-8 bytes instead, leaving ample room for the request envelope.
const BATCH_LIMIT: usize = 1_600_000;
const MAX_ITEMS: usize = 10_000;

fn scalars(node: &Value, fields: &[&str]) -> Map<String, Value> {
    let mut result = Map::new();
    for key in fields {
        let Some(value) = node.get(*key) else { continue };
        match value {
            Value::String(text) if text.len() <= 512 => { result.insert((*key).into(), value.clone()); }
            Value::Bool(_) | Value::Number(_) | Value::Null => { result.insert((*key).into(), value.clone()); }
            _ => {}
        }
    }
    result
}

fn order(node: &Value) -> Value {
    let mut result = scalars(node, &["order_number", "status", "success", "verified_by",
        "payment_confirmed_at_ms", "payment_deadline_ms"]);
    if let Some(items) = node.get("items").and_then(Value::as_array) {
        let statuses = if items.len() > MAX_ITEMS {
            // Too much data must not turn a partial list into proof that every
            // item is terminal. An empty status cannot confirm an order.
            vec![json!({"status":""})]
        } else {
            items.iter().map(|item| {
                // A malformed or oversized status must prevent, not create,
                // positive evidence that every item is terminal.
                match item.get("status").and_then(Value::as_str) {
                    Some(status) if status.len() <= 80 => json!({"status":status}),
                    _ => json!({"status":""}),
                }
            }).collect()
        };
        result.insert("items".into(), Value::Array(statuses));
    }
    Value::Object(result)
}

fn projected(task: &Task) -> Option<Value> {
    if !matches!(task.action.as_str(), "purchase" | "checkout" | "cancel_order" | "orders" | "qr" | "inventory_monitor" | "inventory_check") {
        return None;
    }
    let source = task.result.as_ref()?;
    let result = source.as_object()?;
    let mut reduced = order(source).as_object()?.clone();
    for field in ["pending_orders", "orders"] {
        if let Some(items) = result.get(field).and_then(Value::as_array) {
            reduced.insert(field.into(), Value::Array(items.iter().filter(|x| x.is_object()).map(order).collect()));
        }
    }
    if let Some(items) = result.get("targets").and_then(Value::as_array) {
        reduced.insert("targets".into(), Value::Array(items.iter().filter(|x| x.is_object()).map(|item| {
            Value::Object(scalars(item, &["sku", "store_id", "store_name", "status", "checked_at_ms"]))
        }).collect()));
    }
    Some(json!({"id":task.id,"account_id":task.account_id,"action":task.action,
        "created_at":task.created_at,"schedule":task.schedule.as_ref().map(|s| json!({"triggered_at_ms":s.triggered_at_ms})),
        "result":reduced}))
}

fn fragments(task: &Value) -> Vec<Value> {
    let mut base = task.clone();
    let result = base["result"].as_object_mut().expect("projected result");
    let arrays: Vec<_> = ["orders", "pending_orders", "targets"].iter().filter_map(|field| {
        result.remove(*field).and_then(|items| items.as_array().cloned().map(|v| (*field, v)))
    }).collect();
    let mut fragments = vec![base.clone()];
    base["result"] = json!({});
    for (field, items) in arrays {
        for item in items {
            let mut fragment = base.clone();
            fragment["result"][field] = json!([item]);
            fragments.push(fragment);
        }
    }
    fragments
}

fn batches(tasks: &[Value], limit: usize) -> Result<Vec<Vec<Value>>, String> {
    let mut batches = Vec::new();
    let mut current = Vec::new();
    let mut size = 512;
    for task in tasks {
        for fragment in fragments(task) {
            let bytes = serde_json::to_vec(&fragment).map_err(|_| "无法整理推送任务")?.len() + 1;
            if bytes + 512 > limit { return Err("推送任务过大，稍后重新核对".into()); }
            if size + bytes > limit && !current.is_empty() {
                batches.push(std::mem::take(&mut current));
                size = 512;
            }
            current.push(fragment);
            size += bytes;
        }
    }
    if !current.is_empty() { batches.push(current); }
    Ok(batches)
}

#[derive(Default)]
pub struct PushObserver { results: HashMap<String, (u64, Value)> }

impl PushObserver {
    /// `recovery` also opens the delivery gate after initial state is loaded.
    /// A failed call never marks a result as seen, and leaves the gate closed
    /// if a multi-batch recovery was already in progress.
    pub async fn observe(&mut self, bridge: &PushBridge, tasks: Vec<Task>, recovery: bool,
                         exclude_monitor: bool) -> Result<(), String> {
        if recovery { bridge.call("hold_delivery", json!({})).await?; }
        let mut changed = Vec::new();
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default().as_secs();
        for task in tasks {
            if exclude_monitor && task.action == "inventory_monitor" { continue; }
            let Some(value) = projected(&task) else { continue };
            if let Some((seen_at, seen)) = self.results.get_mut(&task.id) {
                // After a snapshot recovery, buffered broadcasts may carry an
                // older result. Allow same-second changes, and allow a genuine
                // clock rollback rather than permanently blocking later events.
                if matches!(task.action.as_str(), "inventory_monitor" | "inventory_check")
                    && task.updated_at < *seen_at && *seen_at <= now.saturating_add(5) { continue; }
                if seen == &value {
                    // The payload was already acknowledged. Its newer version
                    // still forms a watermark for older buffered events.
                    *seen_at = task.updated_at;
                    continue;
                }
            }
            changed.push((task.id, task.updated_at, value));
        }
        if changed.is_empty() && !recovery { return Ok(()); }
        let values: Vec<_> = changed.iter().map(|(_, _, value)| value.clone()).collect();
        let pieces = batches(&values, BATCH_LIMIT)?;
        let held = recovery || pieces.len() > 1;
        if held && !recovery { bridge.call("hold_delivery", json!({})).await?; }
        for batch in pieces {
            bridge.call("observe_batch", json!({"tasks":batch})).await?;
        }
        if held { bridge.call("resume_delivery", json!({})).await?; }
        // Update only after the worker has acknowledged the entire operation.
        if self.results.len() + changed.len() > 2000 { self.results.clear(); }
        for (id, updated_at, value) in changed { self.results.insert(id, (updated_at, value)); }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, path::Path};

    fn task(result: Value) -> Task {
        serde_json::from_value(json!({"id":"fixture","account_id":"account-a","action":"purchase",
            "status":"succeeded","stage":"done","created_at":100,"updated_at":101,"prompt":null,
            "result":result,"order_submitted":true,"logs":["PRIVATE"]})).unwrap()
    }

    #[test]
    fn projection_removes_private_data_and_fragments_large_batches() {
        let entries: Vec<_> = (0..80).map(|i| json!({"order_number":format!("W{i:09}"),
            "status":"pending_payment","success":true,"payment_url":"PRIVATE"})).collect();
        let task = task(json!({"status":"parallel_complete","pending_orders":entries,"qr_data":"PRIVATE"}));
        let reduced = projected(&task).unwrap();
        assert!(!reduced.to_string().contains("PRIVATE"));
        let batched = batches(&[reduced], 1600).unwrap();
        assert!(batched.len() > 1);
        assert_eq!(batched.iter().flatten().filter(|x| x["result"]["pending_orders"].is_array()).count(),80);
        assert!(batched.iter().all(|batch| serde_json::to_vec(&json!({"tasks":batch})).unwrap().len() < 1600));
    }

    #[test]
    fn oversized_item_list_cannot_prove_a_terminal_order() {
        let row = json!({"items":vec![json!({"status":"CANCELED"});MAX_ITEMS+1]});
        assert_eq!(order(&row)["items"],json!([{"status":""}]));
    }

    #[test]
    fn unknown_order_item_cannot_be_dropped_to_prove_every_item_cancelled() {
        let row = json!({"items":[{"status":"CANCELED"},null]});
        assert_eq!(order(&row)["items"],json!([{"status":"CANCELED"},{"status":""}]));
    }

    #[tokio::test]
    async fn failed_recovery_keeps_delivery_held_and_retries_the_unacknowledged_batch() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("push_recovery.py");
        fs::write(&script, r#"
import json,os,sys
assert os.environ.get('DUO_WEB_PUSH_DEFER_DELIVERY')=='1'
held=True
attempts=0
resumes=0
records=[]
for line in sys.stdin:
    req=json.loads(line); op=req['operation']; result={}; error=None
    if op=='hold_delivery': held=True
    elif op=='observe_batch':
        attempts+=1
        if attempts==1: error='synthetic observation failure'
        else: records.extend(x.get('action') for x in req['params']['tasks'])
    elif op=='resume_delivery':
        held=False; resumes+=1
    elif op=='status': result={'held':held,'attempts':attempts,'resumes':resumes,'records':records}
    reply={'request_id':req['request_id'],'ok':error is None}
    reply.update({'result':result} if error is None else {'error':error})
    print(json.dumps(reply),flush=True)
"#).unwrap();
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python = root.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" });
        let bridge = PushBridge::spawn(&python, Some(&script), temp.path()).unwrap();
        let pending = task(json!({"success":true,"status":"pending_payment","order_number":"W123456789"}));
        let mut cancellation = task(json!({"success":true,"status":"order_cancelled","order_number":"W123456789"}));
        cancellation.id = "cancel".into(); cancellation.action = "cancel_order".into();
        let tasks = vec![pending.clone(), cancellation];
        let mut observer = PushObserver::default();
        assert!(observer.observe(&bridge,tasks.clone(),true,true).await.is_err());
        let status = bridge.call("status",json!({})).await.unwrap();
        assert_eq!(status["held"],true);
        assert_eq!(status["resumes"],0);
        assert!(observer.results.is_empty());
        observer.observe(&bridge,tasks,true,true).await.unwrap();
        let status = bridge.call("status",json!({})).await.unwrap();
        assert_eq!(status["held"],false);
        assert_eq!(status["attempts"],2);
        assert_eq!(status["resumes"],1);
        assert_eq!(status["records"],json!(["purchase","cancel_order"]));
        assert_eq!(observer.results.len(),2);

        // An unchanged newer result updates the watermark without an RPC;
        // subsequently buffered, strictly older results must not be replayed.
        let mut stock = pending.clone(); stock.id = "inventory".into(); stock.action = "inventory_monitor".into();
        stock.result = Some(json!({"targets":[{"sku":"MLH74FE","store_id":"R123","status":"available","checked_at_ms":1000}]}));
        stock.updated_at = 109;
        observer.observe(&bridge,vec![stock.clone()],false,false).await.unwrap();
        let mut newest = stock.clone(); newest.updated_at = 110;
        observer.observe(&bridge,vec![newest.clone()],false,false).await.unwrap();
        let mut stale = newest; stale.updated_at = 109;
        stale.result = Some(json!({"targets":[{"sku":"MLH74FE","store_id":"R123","status":"unavailable","checked_at_ms":999}]}));
        observer.observe(&bridge,vec![stale],false,false).await.unwrap();
        assert_eq!(bridge.call("status",json!({})).await.unwrap()["attempts"],3);

        // A server clock rollback cannot permanently suppress later results.
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
        observer.results.get_mut("inventory").unwrap().0 = now + 100;
        let mut rollback = stock; rollback.updated_at = now;
        rollback.result = Some(json!({"targets":[{"sku":"MLH74FE","store_id":"R123","status":"unavailable","checked_at_ms":1001}]}));
        observer.observe(&bridge,vec![rollback],false,false).await.unwrap();
        assert_eq!(bridge.call("status",json!({})).await.unwrap()["attempts"],4);

        // A late authoritative order detail is still passed to the daemon;
        // only inventory snapshots use the timestamp watermark.
        let mut detail = pending; detail.id = "detail".into(); detail.action = "orders".into();
        detail.updated_at = 109;
        detail.result = Some(json!({"orders":[{"order_number":"W123456789","verified_by":"official_detail","items":[{"status":"PAID"}]}]}));
        observer.results.insert("detail".into(),(110,json!({"old":true})));
        observer.observe(&bridge,vec![detail],false,false).await.unwrap();
        assert_eq!(bridge.call("status",json!({})).await.unwrap()["attempts"],5);

        let mut oversized = task(json!({"success":true,"status":"pending_payment","order_number":"W123456789"}));
        oversized.id = "x".repeat(BATCH_LIMIT);
        assert!(observer.observe(&bridge,vec![oversized],true,false).await.is_err());
        assert_eq!(bridge.call("status",json!({})).await.unwrap()["held"],true);
        bridge.stop().await;
    }

    #[tokio::test]
    async fn real_push_daemon_consumes_recovered_cancel_and_late_pending_in_either_order() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python = root.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" });
        let worker = root.join("worker/main.py");
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis();
        for reverse in [false, true] {
            let temp = tempfile::tempdir().unwrap();
            let bridge = PushBridge::spawn(&python, Some(&worker), temp.path()).unwrap();
            let mut pending = task(json!({"success":true,"status":"pending_payment","order_number":"W123456789",
                "payment_confirmed_at_ms":now,"payment_deadline_ms":now+1_800_000}));
            pending.account_id = "217825d7-8a2a-4386-93f4-1fd6857c5f20".into();
            let mut cancellation = task(json!({"success":false,"status":"cancellation_unknown","order_number":"W123456789"}));
            cancellation.id = "cancel".into(); cancellation.action = "cancel_order".into();
            cancellation.account_id = pending.account_id.clone(); cancellation.order_submitted = false;
            let mut tasks = vec![pending, cancellation];
            if reverse { tasks.reverse(); }
            let mut observer = PushObserver::default();
            observer.observe(&bridge,tasks,true,true).await.unwrap();
            assert_eq!(observer.results.len(),2);
            let status = bridge.call("status",json!({})).await.unwrap();
            assert_eq!(status["pending_reminders"],0);
            assert_eq!(status["subscription_count"],0);
            bridge.stop().await;
        }
    }
}
