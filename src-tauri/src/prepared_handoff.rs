//! Consume a live, account-owned preparation through the ordinary purchase gate.
//! Only the private prompt is a lease: disk checkpoints never resurrect workers.
use super::*;
use sha2::{Digest, Sha256};

pub(crate) fn waiting_preparation(task: &Task) -> bool {
    task.status == "waiting" && !task.order_submitted
        && task.prompt.as_ref().is_some_and(|prompt| prompt["kind"] == "prepared_purchase")
}

fn canonical_sku(value: &str) -> String {
    let value = value.trim().to_ascii_uppercase();
    value.strip_suffix("/A").unwrap_or(&value).to_owned()
}

fn store_ids(value: &Value) -> Option<HashSet<String>> {
    let stores = value.as_array()?;
    if stores.is_empty() || stores.len() > 100 { return None; }
    stores.iter().map(|store| {
        let id = store.as_str()?;
        (id.starts_with('R') && id.len() > 1 && id.len() <= 12 && id[1..].chars().all(|c| c.is_ascii_digit()))
            .then(|| id.to_owned())
    }).collect()
}

fn effective_claim_params(params: &Value) -> Option<Value> {
    if params["queue_stream"] == true {
        let targets = params["queue_targets"].as_array()?;
        if targets.len() != 1 { return None; }
        Some(targets[0].clone())
    } else { Some(params.clone()) }
}

fn matching_preparation(root: &Path, account: &str, task: &Task, params: &Value, current: u64) -> bool {
    if !waiting_preparation(task) || task.account_id != account || params["prepare_only"] == true
        || params["fulfillment"] != "RETAIL" || params["concurrency"].as_u64().unwrap_or(1) != 1 {
        return false;
    }
    let prompt = task.prompt.as_ref().unwrap();
    let prepared = &prompt["prepared"];
    if prepared["v"] != 1 || prepared["fulfillment"] != "RETAIL"
        || prepared["sku"].as_str().zip(params["sku"].as_str()).is_none_or(|(a,b)| canonical_sku(a)!=canonical_sku(b))
        || prepared["quantity"].as_u64().is_none_or(|quantity| quantity == 0 || Some(quantity) != params["quantity"].as_u64()) {
        return false;
    }
    let Some(started) = prepared["prepared_at_ms"].as_u64() else { return false; };
    let Some(expires) = prepared["expires_at_ms"].as_u64() else { return false; };
    if started > current || expires <= current || expires.saturating_sub(started) > 300_000
        || !matches!(prepared["preparation_stage"].as_str(), Some("review"|"fulfillment")) {
        return false;
    }
    let review = prepared["preparation_stage"] == "review";
    if prepared["review_verified"].as_bool() != Some(review) || prepared["order_ready"].as_bool() != Some(review) { return false; }
    let Some(authorized) = store_ids(&prepared["authorized_store_ids"]) else { return false; };
    let config_path = scope_dir(root, account).join("checkout.json");
    let Ok(raw) = fs::read(config_path) else { return false; };
    if raw.len() > 262_144 { return false; }
    let hash = format!("{:x}", Sha256::digest(&raw));
    if prepared["config_file_sha256"] != hash
        || prepared["config_fingerprint"].as_str().is_none_or(|hash| hash.len()!=64 || !hash.chars().all(|c| c.is_ascii_hexdigit())) {
        return false;
    }
    let Ok(config) = serde_json::from_slice::<Value>(&raw) else { return false; };
    if let Some(policy) = params.get("pickup_policy") {
        // Compare effective rules, not the template/override representation.
        // All legacy time rules now normalize to the same seven-day earliest slot.
        for (field, default) in [("selection_mode", "fixed"), ("contact_mode", "local"), ("id_last4", "")] {
            if policy.get(field).is_some_and(|value| value.as_str()!=Some(config["pickup"][field].as_str().unwrap_or(default))) { return false; }
        }
    }
    let requested = if let Some(id) = params["pickup_store_id"].as_str() {
        Some(HashSet::from([id.to_owned()]))
    } else {
        let policy = params.get("pickup_policy").unwrap_or(&config["pickup"]);
        if policy["selection_mode"].as_str().unwrap_or("fixed") == "priority" {
            policy["stores"].as_array().and_then(|stores|
                store_ids(&Value::Array(stores.iter().map(|store| store["store_id"].clone()).collect())))
        } else if let Some(id) = policy.get("store_id").unwrap_or(&config["pickup"]["store_id"]).as_str() {
            Some(HashSet::from([id.to_owned()]))
        } else {
            // Old fixed presets can authorize a store by name and query. The
            // worker only publishes a singleton after binding that exact scope
            // to a unique current official identity (and selection for Review).
            let pickup = &config["pickup"];
            let name = pickup["store_name"].as_str().map(str::trim).filter(|value| !value.is_empty());
            let search = pickup["search"].as_str().map(str::trim).filter(|value| !value.is_empty());
            if pickup["selection_mode"].as_str().unwrap_or("fixed") != "fixed" || authorized.len()!=1
                || name.is_none() || search.is_none() || policy.get("store_id").is_some() {
                return false;
            }
            let same = |value: Option<&Value>, expected: Option<&str>| value.is_none_or(|value|
                value.as_str().map(str::trim)==expected);
            if !same(policy.get("store_name"),name) || !same(policy.get("search"),search)
                || !same(params.get("pickup_store_name"),name) || !same(params.get("pickup_location"),search) {
                return false;
            }
            Some(authorized.clone())
        }
    };
    requested.is_some_and(|requested| !requested.is_empty() && requested.is_subset(&authorized))
}

impl Engine {
    /// Assignment persistence belongs to the caller. A returned task is the same
    /// worker/Session/Checkout, and is immediately no longer preemptible preheat.
    pub(crate) fn try_claim_prepared(&self, host: &TaskHost, account: &str, params: &Value,
        source_task: Option<&str>) -> Result<Option<String>, String> {
        let Some(params) = effective_claim_params(params) else { return Ok(None); };
        let current = now_ms();
        let mut tasks = self.tasks.lock().unwrap();
        if tasks.values().any(|task| task.account_id==account && (task_has_unknown_submission(task)
            || task.schedule.is_some() && !terminal(&task.status) && Some(task.id.as_str()) != source_task)) { return Ok(None); }
        let Some(id) = tasks.values().filter(|task| Some(task.id.as_str()) != source_task)
            .find(|task| matching_preparation(&self.root, account, task, &params, current)).map(|task| task.id.clone()) else { return Ok(None); };
        let task = tasks.get_mut(&id).unwrap();
        let prompt = task.prompt.as_ref().unwrap();
        let request = prompt["id"].as_str().ok_or("自提准备缺少等待标识")?.to_owned();
        let source = if params["queue_job_id"].is_string() { "queue" } else if source_task.is_some() { "scheduled" } else { "manual" };
        let mut claim = json!({"v":1,"account_id":account,"prepared_task_id":id,"request_id":request,
            "source":source,"requested_at_ms":current,"claimed_at_ms":current});
        if let Some(source_task) = source_task { claim["source_task_id"] = json!(source_task); }
        for field in ["queue_job_id", "queue_assignment", "purchase_group_id"] {
            if let Some(value) = params.get(field) { claim[field] = value.clone(); }
        }
        let message = json!({"type":"reply","account_id":account,"task_id":id,"request_id":request,
            "value":{"prepared_claim":claim,"params":params}});
        let accepted = self.controls.lock().unwrap().get(&id).filter(|control| !*control.cancel.borrow())
            .is_some_and(|control| control.input.try_send(message).is_ok());
        if !accepted { return Ok(None); }
        task.action = "purchase".into();
        task.warm_only = false;
        task.prompt = None;
        task.status = "running".into();
        task.stage = "已接手原自提准备，正在复核并下单".into();
        task.execution_started_at_ms = Some(current);
        task.prepared_handoff = Some(claim);
        if let Some(queue) = params["queue_job_id"].as_str() { task.queue_job_ids = vec![queue.to_owned()]; }
        drop(tasks);
        self.update(host, &id, |_| {});
        Ok(Some(id))
    }

    pub(super) async fn follow_prepared_handoff(&self, host: &TaskHost, source: &str, prepared: &str,
        cancel: &mut watch::Receiver<bool>) -> Result<(), String> {
        self.update(host, source, |task| {
            task.status = "running".into();
            task.stage = "定时已触发，原自提准备正在下单".into();
            task.prepared_handoff = Some(json!({"prepared_task_id":prepared,"source":"scheduled"}));
            if let Some(schedule) = task.schedule.as_mut() { schedule.triggered_at_ms = Some(now_ms()); }
        });
        let mut cancellation_requested = false;
        loop {
            let current = self.tasks.lock().unwrap().get(prepared).cloned().ok_or("原自提准备任务已不存在，请核对订单")?;
            if current.order_submitted && !self.tasks.lock().unwrap()[source].order_submitted {
                self.update(host, source, |task| {
                    task.order_submitted = true;
                    task.result = current.result.clone();
                });
            }
            if terminal(&current.status) {
                self.update(host, source, |task| {
                    task.status = current.status;
                    task.stage = current.stage;
                    task.order_submitted = current.order_submitted;
                    task.result = current.result;
                    task.purchase_progress = current.purchase_progress;
                });
                return Ok(());
            }
            if *cancel.borrow() && !cancellation_requested {
                cancellation_requested = true;
                let _ = self.cancel(&current.account_id, prepared);
            }
            if !self.controls.lock().unwrap().contains_key(prepared) {
                return Err("原自提准备进程意外结束，请核对订单状态".into());
            }
            tokio::select! {
                _ = cancel.changed(), if !cancellation_requested => {},
                _ = tokio::time::sleep(std::time::Duration::from_millis(25)) => {},
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (tempfile::TempDir, Arc<Engine>, TaskHost, String) {
        let root = tempfile::tempdir().unwrap();
        let engine = Engine::new(root.path().into()).unwrap();
        let account = engine.add_account("prepared@example.test".into()).unwrap();
        engine.accounts.lock().unwrap()[0].pending = false;
        let directory = scope_dir(root.path(), &account.id);
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("checkout.json"), r#"{"pickup":{"store_id":"R001"}}"#).unwrap();
        let script = root.path().join("prepared-fixture.py");
        fs::write(&script, r#"import hashlib,json,os,pathlib,sys,time
r=json.loads(sys.stdin.readline())
def emit(kind,**data):
 print(json.dumps(dict(v=1,account_id=r['account_id'],task_id=r['task_id'],type=kind,**data)),flush=True)
if r['action']=='session_maintain':
 raw=(pathlib.Path(sys.argv[2])/'accounts'/r['account_id']/'checkout.json').read_bytes()
 now=int(time.time()*1000)
 emit('progress_result',result={'success':True,'status':'prepared','pid':os.getpid()})
 emit('prompt',prompt={'id':'prepared-fixture','kind':'prepared_purchase','label':'ready','fixture_pid':os.getpid(),'prepared':{'v':1,'sku':'MK2P4CH/A','quantity':1,'fulfillment':'RETAIL','config_fingerprint':'a'*64,'config_file_sha256':hashlib.sha256(raw).hexdigest(),'authorized_store_ids':['R001'],'prepared_at_ms':now,'expires_at_ms':now+300000,'preparation_stage':'review','review_verified':True,'order_ready':True}})
 reply=json.loads(sys.stdin.readline())
 if reply.get('type')=='cancel':
  emit('cancelled');sys.exit(0)
 claim=reply['value']['prepared_claim']
 assert claim['prepared_task_id']==r['task_id'] and claim['request_id']=='prepared-fixture'
 assert reply['value']['params']['quantity']==1
 if reply['value']['params'].get('wait_for_cancel'):
  emit('stage',message='fixture-claimed-wait',order_submitted=True)
  cancel=json.loads(sys.stdin.readline())
  assert cancel['type']=='cancel'
  emit('cancelled',order_submitted=True);sys.exit(0)
else:
 assert r['action']=='purchase'
emit('result',result={'success':True,'status':'fixture_completed','pid':os.getpid(),'order_submitted':False})
"#).unwrap();
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let python = project.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" });
        (root,engine,TaskHost {worker:python,script:Some(script),on_update:Arc::new(|_|{})},account.id)
    }

    async fn wait(engine: &Engine, id: &str, ready: bool) {
        for _ in 0..1000 {
            let task = engine.tasks.lock().unwrap()[id].clone();
            if (ready && waiting_preparation(&task)) || (!ready && terminal(&task.status)) { return; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        panic!("fixture task did not reach expected state");
    }

    fn purchase() -> Value { json!({"sku":"MK2P4CH","quantity":1,"fulfillment":"RETAIL","concurrency":1,"retry_count":0}) }

    #[tokio::test]
    async fn manual_and_scheduled_purchase_claim_same_live_worker_without_cancellation() {
        for (scheduled, name_only) in [(false,false),(true,false),(false,true),(true,true)] {
            let (_root,engine,host,account) = fixture();
            if name_only {
                fs::write(scope_dir(&engine.root,&account).join("checkout.json"),r#"{"pickup":{"store_name":"Fixture Store","search":"Fixture City"}}"#).unwrap();
            }
            let prepared = engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
            wait(&engine,&prepared,true).await;
            let pid = engine.tasks.lock().unwrap()[&prepared].prompt.as_ref().unwrap()["fixture_pid"].clone();
            let mut params = purchase();
            if scheduled { params["scheduled_at"] = json!(now_ms()+1200); }
            let target = engine.start(host.clone(),account.clone(),"purchase".into(),params).unwrap();
            if !scheduled { assert_eq!(target,prepared); }
            wait(&engine,&target,false).await;
            let task = engine.tasks.lock().unwrap()[&prepared].clone();
            assert_eq!(task.status,"succeeded","{}",task.stage);
            assert_eq!(task.action,"purchase");
            assert_eq!(task.result.as_ref().unwrap()["pid"],pid);
            assert_eq!(task.prepared_handoff.as_ref().unwrap()["source"],if scheduled {"scheduled"} else {"manual"});
            if scheduled {
                let source = engine.tasks.lock().unwrap()[&target].clone();
                assert_eq!(source.result.as_ref().unwrap()["pid"],pid);
                assert_eq!(task.prepared_handoff.as_ref().unwrap()["source_task_id"],target);
                assert_eq!(source.prepared_handoff.as_ref().unwrap()["prepared_task_id"],prepared);
            }
        }
    }

    #[tokio::test]
    async fn name_only_preparation_rejects_changed_query_name_or_non_unique_authority() {
        let (_root,engine,host,account) = fixture();
        fs::write(scope_dir(&engine.root,&account).join("checkout.json"),r#"{"pickup":{"store_name":"Fixture Store","search":"Fixture City"}}"#).unwrap();
        let prepared = engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        wait(&engine,&prepared,true).await;
        let before = engine.tasks.lock().unwrap()[&prepared].clone();
        assert!(matching_preparation(&engine.root,&account,&before,&purchase(),now_ms()));
        for (field, value) in [("pickup_store_name",json!("Other Store")),("pickup_location",json!("Other City")),
            ("pickup_policy",json!({"store_name":"Other Store"})),("pickup_policy",json!({"search":"Other City"}))] {
            let mut changed = purchase(); changed[field] = value;
            assert!(!matching_preparation(&engine.root,&account,&before,&changed,now_ms()));
        }
        let mut multiple = before.clone();
        multiple.prompt.as_mut().unwrap()["prepared"]["authorized_store_ids"] = json!(["R001","R002"]);
        assert!(!matching_preparation(&engine.root,&account,&multiple,&purchase(),now_ms()));
        engine.cancel(&account,&prepared).unwrap(); wait(&engine,&prepared,false).await;
    }

    #[tokio::test]
    async fn cancelled_schedule_follows_original_purchase_unknown_submission_instead_of_hiding_it() {
        let (_root,engine,host,account) = fixture();
        let prepared = engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        wait(&engine,&prepared,true).await;
        let mut params = purchase();
        params["scheduled_at"] = json!(now_ms()+1200);
        params["wait_for_cancel"] = json!(true);
        let scheduled = engine.start(host,account.clone(),"purchase".into(),params).unwrap();
        for _ in 0..1000 {
            if engine.tasks.lock().unwrap()[&prepared].stage=="fixture-claimed-wait" { break; }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
        assert!(engine.tasks.lock().unwrap()[&prepared].order_submitted);
        engine.cancel(&account,&scheduled).unwrap();
        wait(&engine,&scheduled,false).await;
        let tasks = engine.tasks.lock().unwrap();
        assert!(terminal(&tasks[&prepared].status));
        assert_eq!(tasks[&scheduled].status,tasks[&prepared].status);
        assert!(task_has_unknown_submission(&tasks[&prepared]));
        assert!(task_has_unknown_submission(&tasks[&scheduled]));
    }

    #[tokio::test]
    async fn changed_config_or_unmatched_intent_falls_back_and_never_consumes_wrong_lease() {
        let (_root,engine,host,account) = fixture();
        let prepared = engine.start(host.clone(),account.clone(),"session_maintain".into(),json!({})).unwrap();
        wait(&engine,&prepared,true).await;
        let before = engine.tasks.lock().unwrap()[&prepared].clone();
        for params in [json!({"sku":"OTHER","quantity":1,"fulfillment":"RETAIL"}),
            json!({"sku":"MK2P4CH","quantity":2,"fulfillment":"RETAIL"}),
            json!({"sku":"MK2P4CH","quantity":1,"fulfillment":"RETAIL","pickup_store_id":"R002"}),
            json!({"sku":"MK2P4CH","quantity":1,"fulfillment":"RETAIL","concurrency":2})] {
            assert!(engine.try_claim_prepared(&host,&account,&params,None).unwrap().is_none());
        }
        assert!(validate_reply(&before,&account,"prepared-fixture",&json!(true)).is_err());
        let mut equivalent = purchase();
        equivalent["sku"] = json!("mk2p4ch/a");
        equivalent["pickup_policy"] = json!({"selection_mode":"fixed","contact_mode":"local","time_slot_rule":{"mode":"latest"},"stores":[{"store_id":"R002"}]});
        assert!(matching_preparation(&engine.root,&account,&before,&equivalent,now_ms()));
        equivalent["pickup_policy"]["contact_mode"] = json!("official");
        assert!(!matching_preparation(&engine.root,&account,&before,&equivalent,now_ms()));
        let mut unknown = before.clone();
        unknown.id = "unknown-fixture".into(); unknown.status = "interrupted".into(); unknown.order_submitted = true; unknown.result = None;
        engine.tasks.lock().unwrap().insert(unknown.id.clone(),unknown);
        assert!(engine.try_claim_prepared(&host,&account,&purchase(),None).unwrap().is_none());
        engine.tasks.lock().unwrap().remove("unknown-fixture");
        let mut expired = before.clone();
        expired.prompt.as_mut().unwrap()["prepared"]["expires_at_ms"] = json!(now_ms()-1);
        assert!(!matching_preparation(&engine.root,&account,&expired,&purchase(),now_ms()));
        fs::write(scope_dir(&engine.root,&account).join("checkout.json"),"{}").unwrap();
        assert!(engine.try_claim_prepared(&host,&account,&purchase(),None).unwrap().is_none());
        let fresh = engine.start(host,account,"purchase".into(),purchase()).unwrap();
        assert_ne!(fresh,prepared);
        wait(&engine,&fresh,false).await;
        assert_eq!(engine.tasks.lock().unwrap()[&prepared].status,"cancelled");
    }
}
