//! Durable user intent, separate from a monitor worker's ephemeral task status.
use super::*;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct SavedIntent {
    version: u8,
    enabled: bool,
}

pub(super) struct MonitorRestart {
    enabled: bool,
    startup_claimed: bool,
}

impl MonitorRestart {
    pub(super) fn load(root: &Path, legacy_enabled: bool) -> Result<Self, String> {
        let path = root.join("settings/monitor_runtime.json");
        let enabled = if path.exists() {
            let state: SavedIntent = serde_json::from_slice(
                &fs::read(&path).map_err(|_| "无法读取监控运行设置，未自动恢复监控")?,
            )
            .map_err(|_| "监控运行设置损坏，未自动恢复监控")?;
            if state.version != 1 {
                return Err("监控运行设置版本不支持，未自动恢复监控".into());
            }
            state.enabled
        } else {
            // Migrate only the latest, genuinely nonterminal anonymous monitor.
            // Persist even false so interrupted history can never re-enable it later.
            private_json(
                &path,
                &SavedIntent {
                    version: 1,
                    enabled: legacy_enabled,
                },
            )?;
            legacy_enabled
        };
        Ok(Self {
            enabled,
            startup_claimed: false,
        })
    }

    pub(super) fn set_enabled(&mut self, root: &Path, enabled: bool) -> Result<(), String> {
        private_json(
            &root.join("settings/monitor_runtime.json"),
            &SavedIntent {
                version: 1,
                enabled,
            },
        )
        .map_err(|_| "无法保存监控开关，请重试；未改变监控运行设置")?;
        self.enabled = enabled;
        Ok(())
    }

    fn claim_startup(&mut self) -> bool {
        if self.startup_claimed {
            return false;
        }
        self.startup_claimed = true;
        self.enabled
    }
}

impl Engine {
    /// Called after the host and event sink are ready, once for either Web or Tauri.
    /// Never reuse an old worker request, inventory sample, or purchase task.
    pub fn restore_monitor(self: &Arc<Self>, host: TaskHost) -> Result<Option<String>, String> {
        if !self.monitor_restart.lock().unwrap().claim_startup() {
            return Ok(None);
        }
        let id = self
            .start(
                host.clone(),
                LOCAL_ID.into(),
                "inventory_monitor".into(),
                json!({}),
            )
            .map_err(|error| {
                self.append_event("monitor_restore_failed", &json!({"message": error}));
                format!("自动恢复监控失败：{error}")
            })?;
        self.queue_restore_running_intent(host).map_err(|error| {
            self.append_event(
                "monitor_queue_restore_failed",
                &json!({"task_id": id,"message": error}),
            );
            format!("监控已恢复，但执行队列恢复失败：{error}")
        })?;
        self.append_event("monitor_restored", &json!({"task_id": id}));
        Ok(Some(id))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn intent_migrates_once_and_manual_stop_wins_over_history() {
        let root = tempfile::tempdir().unwrap();
        let mut first = MonitorRestart::load(root.path(), true).unwrap();
        assert!(first.claim_startup());
        assert!(!first.claim_startup());
        first.set_enabled(root.path(), false).unwrap();
        assert!(!MonitorRestart::load(root.path(), true)
            .unwrap()
            .claim_startup());
        let value: Value = serde_json::from_slice(
            &fs::read(root.path().join("settings/monitor_runtime.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(value, json!({"version":1,"enabled":false}));
    }

    #[test]
    fn no_previous_monitor_stays_off_and_invalid_intent_fails_closed() {
        let root = tempfile::tempdir().unwrap();
        assert!(!MonitorRestart::load(root.path(), false)
            .unwrap()
            .claim_startup());
        for value in [
            json!({"version":2,"enabled":true}),
            json!({"version":1,"enabled":"true"}),
            json!({"version":1,"enabled":true,"params":{"auto_purchase":true}}),
        ] {
            private_json(&root.path().join("settings/monitor_runtime.json"), &value).unwrap();
            assert!(MonitorRestart::load(root.path(), true).is_err());
        }
    }

    #[test]
    fn failed_persistence_does_not_change_memory_intent() {
        let root = tempfile::tempdir().unwrap();
        let mut state = MonitorRestart::load(root.path(), false).unwrap();
        let invalid_root = root.path().join("file");
        fs::write(&invalid_root, "not a directory").unwrap();
        assert!(state.set_enabled(&invalid_root, true).is_err());
        assert!(!state.claim_startup());
    }

    #[tokio::test]
    async fn failed_monitor_can_be_stopped_without_old_history_stopping_a_new_monitor() {
        let root = tempfile::tempdir().unwrap();
        let engine = Engine::new(root.path().into()).unwrap();
        let host = TaskHost {
            worker: root.path().join("missing-offline-worker"),
            script: None,
            on_update: Arc::new(|_| {}),
        };
        let old = engine
            .start(
                host.clone(),
                LOCAL_ID.into(),
                "inventory_monitor".into(),
                json!({}),
            )
            .unwrap();
        wait_until(|| terminal(&engine.tasks.lock().unwrap()[&old].status)).await;
        assert_eq!(engine.tasks.lock().unwrap()[&old].status, "failed");
        assert!(engine.cancel("wrong-account", &old).is_err());
        assert!(MonitorRestart::load(root.path(), false).unwrap().enabled);
        let current = engine
            .start(
                host.clone(),
                LOCAL_ID.into(),
                "inventory_monitor".into(),
                json!({}),
            )
            .unwrap();
        engine.cancel(LOCAL_ID, &old).unwrap();
        assert!(
            MonitorRestart::load(root.path(), false).unwrap().enabled,
            "cancelling history must not disable another active monitor"
        );
        wait_until(|| terminal(&engine.tasks.lock().unwrap()[&current].status)).await;
        engine.cancel(LOCAL_ID, &current).unwrap();
        let restored = Engine::new(root.path().into()).unwrap();
        assert!(restored.restore_monitor(host).unwrap().is_none());
        assert!(!MonitorRestart::load(root.path(), true).unwrap().enabled);
    }

    #[test]
    fn legacy_migration_requires_unambiguous_latest_live_monitor() {
        for (statuses, expected) in [
            (vec![(1, "running")], true),
            (vec![(1, "queued")], true),
            (vec![(1, "running"), (2, "failed")], false),
            (vec![(1, "running"), (2, "cancelled")], false),
            (vec![(1, "interrupted")], false),
            (vec![(1, "running"), (1, "cancelled")], false),
            (vec![(1, "cancelled"), (1, "running")], false),
        ] {
            let root = tempfile::tempdir().unwrap();
            for (index, (created_at, status)) in statuses.iter().enumerate() {
                let id = if index == 0 {
                    "ffffffff-ffff-4fff-8fff-ffffffffffff"
                } else {
                    "00000000-0000-4000-8000-000000000001"
                };
                private_json(&scope_dir(root.path(),LOCAL_ID).join("tasks").join(id).join("task.json"), &json!({
                    "id":id,"account_id":LOCAL_ID,"action":"inventory_monitor","status":status,"stage":"fixture",
                    "created_at":created_at,"updated_at":created_at,"order_submitted":false,"logs":[],
                    "result":{"monitoring":true,"targets":[{"status":"available","checked_at_ms":now_ms()}]},
                })).unwrap();
            }
            let engine = Engine::new(root.path().into()).unwrap();
            assert_eq!(
                engine.monitor_restart.lock().unwrap().enabled,
                expected,
                "{statuses:?}"
            );
            assert!(engine
                .snapshot()
                .tasks
                .iter()
                .all(|task| terminal(&task.status)));
            assert!(
                engine
                    .queue_command(fixture_host(), "status", &json!({}))
                    .unwrap()["jobs"]
                    .as_array()
                    .unwrap()
                    .is_empty(),
                "historical inventory must never enqueue a purchase"
            );
            engine
                .monitor_restart
                .lock()
                .unwrap()
                .set_enabled(root.path(), false)
                .unwrap();
            assert!(
                !Engine::new(root.path().into())
                    .unwrap()
                    .monitor_restart
                    .lock()
                    .unwrap()
                    .enabled,
                "migration happens only once and explicit false takes precedence"
            );
        }
    }

    fn fixture_host() -> TaskHost {
        let project = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        TaskHost {
            worker: project.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            }),
            script: Some(project.join("tests/fixtures/monitor_restart_worker.py")),
            on_update: Arc::new(|_| {}),
        }
    }

    fn seed_config(host: &TaskHost, root: &Path, account: &str, store: &str) {
        let output = std::process::Command::new(&host.worker)
            .arg(host.script.as_ref().unwrap())
            .args(["--seed"])
            .arg(root)
            .args([account, store])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    fn queue_state(engine: &Arc<Engine>, host: &TaskHost) -> Value {
        engine
            .queue_command(host.clone(), "status", &json!({}))
            .unwrap()
    }

    async fn wait_until(condition: impl Fn() -> bool) {
        for _ in 0..500 {
            if condition() {
                return;
            }
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        assert!(condition(), "offline restart fixture timed out");
    }

    fn purchases(root: &Path) -> usize {
        fs::read_to_string(root.join("restart-audit.jsonl"))
            .unwrap_or_default()
            .lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            .filter(|row| row["kind"] == "purchase_started")
            .count()
    }

    // The parent starts a new test executable for each phase. This is an actual
    // application-process restart plus real Python IPC, with no Apple calls.
    #[tokio::test]
    async fn restart_process_fixture() {
        let Some(root) = std::env::var_os("DUO_MONITOR_RESTART_FIXTURE_ROOT").map(PathBuf::from)
        else {
            return;
        };
        let phase = std::env::var("DUO_MONITOR_RESTART_FIXTURE_PHASE").unwrap();
        let scenario = std::env::var("DUO_MONITOR_RESTART_FIXTURE_SCENARIO").unwrap();
        let engine = Engine::new(root.clone()).unwrap();
        let host = fixture_host();
        if phase == "before" {
            engine
                .queue_stopping
                .store(true, std::sync::atomic::Ordering::SeqCst);
            let account = engine
                .add_account("restart-fixture@example.test".into())
                .unwrap();
            {
                let mut accounts = engine.accounts.lock().unwrap();
                accounts[0].pending = false;
                private_json(&root.join("accounts.json"), &*accounts).unwrap();
            }
            private_json(
                &scope_dir(&root, &account.id).join("checkout.json"),
                &json!({}),
            )
            .unwrap();
            let target = json!({"id":"fixture-R001","account_id":account.id,"sku":"MLH74FE","quantity":1,
                "store_id":"R001","store_name":"Fixture R001","location":"200000","auto_purchase":true,
                "status":"available","checked_at_ms":now_ms()});
            engine.inventory_dispatch(&host, &json!({"targets":[target]}));
            if scenario == "paused" {
                engine
                    .queue_command(host.clone(), "pause", &json!({}))
                    .unwrap();
            }
            if scenario == "cancel_all" {
                engine
                    .queue_command(host.clone(), "cancel", &json!({"all":true}))
                    .unwrap();
            } else if scenario == "cancelled" {
                let job = queue_state(&engine, &host)["jobs"][0]["id"].clone();
                engine
                    .queue_command(host.clone(), "cancel", &json!({"job_id":job}))
                    .unwrap();
            }
            seed_config(&host, &root, &account.id, "R001");
            let id = engine
                .start(
                    host.clone(),
                    LOCAL_ID.into(),
                    "inventory_monitor".into(),
                    json!({}),
                )
                .unwrap();
            wait_until(|| engine.tasks.lock().unwrap()[&id].result.is_some()).await;
            assert_eq!(purchases(&root), 0);
            if scenario == "stopped" {
                engine.cancel(LOCAL_ID, &id).unwrap();
                wait_until(|| terminal(&engine.tasks.lock().unwrap()[&id].status)).await;
            }
            if scenario == "crash" {
                std::process::exit(0);
            }
            engine.shutdown().await;
            return;
        }
        let account = engine.accounts.lock().unwrap()[0].id.clone();
        let restored = engine.restore_monitor(host.clone()).unwrap();
        assert!(
            engine.restore_monitor(host.clone()).unwrap().is_none(),
            "startup must restore at most once"
        );
        if scenario == "stopped" {
            assert!(restored.is_none());
            assert_eq!(
                engine
                    .snapshot()
                    .tasks
                    .iter()
                    .filter(|task| task.action == "inventory_monitor")
                    .count(),
                1
            );
            assert_eq!(purchases(&root), 0);
            engine.shutdown().await;
            return;
        }
        let id = restored.expect("enabled monitoring must resume in a new worker");
        wait_until(|| engine.tasks.lock().unwrap()[&id].result.is_some()).await;
        {
            let tasks = engine.tasks.lock().unwrap();
            let result = tasks[&id].result.as_ref().unwrap();
            assert_eq!(
                result["targets"][0]["store_id"], "R002",
                "read the newly saved encrypted configuration"
            );
            assert_eq!(result["targets"][0]["status"], "unknown");
        }
        let before = queue_state(&engine, &host);
        assert_eq!(
            before["paused"],
            matches!(scenario.as_str(), "paused" | "cancel_all")
        );
        assert_eq!(
            before["jobs"][0]["status"],
            match scenario.as_str() {
                "unknown" => "interrupted",
                "cancelled" | "cancel_all" => "cancelled",
                _ => "waiting_stock",
            }
        );
        if matches!(scenario.as_str(), "normal" | "crash" | "paused") {
            assert_eq!(before["jobs"][0]["checked_at_ms"], 0);
        }
        assert_eq!(
            purchases(&root),
            0,
            "old inventory must never dispatch after restart"
        );
        fs::write(
            root.join("fresh-inventory-gate"),
            "synthetic fresh inventory only",
        )
        .unwrap();
        wait_until(|| {
            engine.tasks.lock().unwrap()[&id]
                .result
                .as_ref()
                .is_some_and(|result| result["targets"][0]["status"] == "available")
        })
        .await;
        if matches!(scenario.as_str(), "paused" | "unknown" | "cancel_all") {
            tokio::time::sleep(std::time::Duration::from_millis(400)).await;
            assert_eq!(purchases(&root), 0);
            let state = queue_state(&engine, &host);
            assert_eq!(
                state["paused"],
                matches!(scenario.as_str(), "paused" | "cancel_all")
            );
            if scenario == "unknown" {
                assert!(state["blocked_accounts"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(
                        |row| row["account_id"] == account && row["reason"] == "submission_unknown"
                    ));
            }
        } else {
            wait_until(|| {
                queue_state(&engine, &host)["jobs"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|job| job["store_id"] == "R002" && job["status"] == "succeeded")
            })
            .await;
            assert_eq!(purchases(&root), 1);
            assert_eq!(
                queue_state(&engine, &host)["jobs"][0]["status"],
                if scenario == "cancelled" {
                    "cancelled"
                } else {
                    "waiting_stock"
                }
            );
        }
        engine.shutdown().await;
    }

    #[test]
    fn real_process_restart_restores_only_monitoring_and_fresh_authorized_work() {
        let host = fixture_host();
        assert!(
            host.worker.is_file(),
            "run worker setup before the offline restart fixture"
        );
        for scenario in [
            "normal",
            "crash",
            "paused",
            "unknown",
            "cancelled",
            "cancel_all",
            "stopped",
        ] {
            let root = tempfile::tempdir().unwrap();
            for phase in ["before", "after"] {
                if phase == "after" {
                    let accounts: Vec<Account> = serde_json::from_slice(
                        &fs::read(root.path().join("accounts.json")).unwrap(),
                    )
                    .unwrap();
                    seed_config(&host, root.path(), &accounts[0].id, "R002");
                    if scenario == "unknown" {
                        let path = root.path().join("settings/purchase_queue.json");
                        let mut state: Value =
                            serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
                        state["jobs"][0]["status"] = json!("dispatched");
                        state["jobs"][0]["task_id"] = json!(Uuid::new_v4().to_string());
                        state["jobs"][0]["order_submitted"] = json!(true);
                        private_json(&path, &state).unwrap();
                    }
                }
                let output = std::process::Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "engine::monitor_restart::tests::restart_process_fixture",
                        "--nocapture",
                    ])
                    .env("DUO_MONITOR_RESTART_FIXTURE_ROOT", root.path())
                    .env("DUO_MONITOR_RESTART_FIXTURE_PHASE", phase)
                    .env("DUO_MONITOR_RESTART_FIXTURE_SCENARIO", scenario)
                    .output()
                    .unwrap();
                assert!(
                    output.status.success(),
                    "scenario={scenario} phase={phase}\n{}\n{}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                );
            }
        }
    }
}
