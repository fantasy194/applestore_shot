//! One bounded writer for engine events; Python owns the shared SQLite schema.
use crate::engine::TaskHost;
use serde_json::{json, Value};
use std::{collections::HashMap, path::{Path, PathBuf}, process::Stdio,
    sync::{Mutex, OnceLock}, time::Duration};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{mpsc, oneshot}};

const CAPACITY: usize = 2048;
const MAX_RESPONSE: usize = 8 * 1024 * 1024;
static WRITERS: OnceLock<Mutex<HashMap<PathBuf, mpsc::Sender<Message>>>> = OnceLock::new();

enum Message {
    Append(Value),
    Query(Value, oneshot::Sender<Result<Value, String>>),
    Stop(oneshot::Sender<()>),
}

struct Client { child: Child, input: ChildStdin, output: Lines<BufReader<ChildStdout>> }

fn writers() -> &'static Mutex<HashMap<PathBuf, mpsc::Sender<Message>>> {
    WRITERS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn command(host: &TaskHost, root: &Path, operation: &str) -> Command {
    let mut command = Command::new(&host.worker);
    if let Some(script) = &host.script { command.arg(script); }
    command.args(["--activity-log", operation]).arg("--data-dir").arg(root)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
        .kill_on_drop(true).env("PYTHONUTF8", "1").env("PYTHONPATH", "")
        .env("PYTHONIOENCODING", "utf-8").env("PYTHONUNBUFFERED", "1");
    #[cfg(windows)] command.creation_flags(0x08000000);
    command
}

impl Client {
    fn spawn(host: &TaskHost, root: &Path) -> Result<Self, String> {
        let mut child = command(host, root, "serve").spawn().map_err(|_| "操作日志服务无法启动")?;
        let input = child.stdin.take().ok_or("操作日志输入不可用")?;
        let output = BufReader::new(child.stdout.take().ok_or("操作日志输出不可用")?).lines();
        Ok(Self { child, input, output })
    }

    async fn call(&mut self, operation: &str, params: Value) -> Result<Value, String> {
        if self.child.try_wait().map_err(|_| "无法读取日志服务状态")?.is_some() {
            return Err("操作日志服务已退出".into());
        }
        let line = format!("{}\n", json!({"operation":operation,"params":params}));
        tokio::time::timeout(Duration::from_secs(8), async {
            self.input.write_all(line.as_bytes()).await.map_err(|_| "日志服务输入已断开")?;
            let line = self.output.next_line().await.map_err(|_| "日志服务响应不可读")?
                .ok_or("日志服务已退出")?;
            if line.len() > MAX_RESPONSE { return Err("日志响应超出大小限制".into()); }
            let value: Value = serde_json::from_str(&line).map_err(|_| "日志服务响应格式无效")?;
            if value["ok"] == true { Ok(value["result"].clone()) }
            else { Err("操作日志暂时无法读取或保存，请稍后重试".into()) }
        }).await.map_err(|_| "操作日志服务响应超时".to_owned())?
    }
}

/// Call from an active Tokio runtime in both desktop and local Web startup.
pub fn start(host: TaskHost, root: PathBuf) {
    let (sender, mut receiver) = mpsc::channel(CAPACITY);
    {
        let mut writers = writers().lock().unwrap();
        if writers.get(&root).is_some_and(|sender| !sender.is_closed()) { return; }
        writers.insert(root.clone(), sender);
    }
    tokio::spawn(async move {
        let mut client: Option<Client> = None;
        let mut stopped = None;
        while let Some(message) = receiver.recv().await {
            if let Message::Stop(done) = message {
                if let Some(mut connection) = client.take() { let _ = connection.child.kill().await; }
                stopped = Some(done);
                break;
            }
            if client.is_none() { client = Client::spawn(&host, &root).ok(); }
            let (operation, params, reply) = match message {
                Message::Append(params) => ("append", params, None),
                Message::Query(params, reply) => ("query", params, Some(reply)),
                Message::Stop(_) => unreachable!(),
            };
            let result = if let Some(connection) = client.as_mut() { connection.call(operation, params).await }
                else { Err("操作日志服务无法启动".into()) };
            if result.is_err() {
                eprintln!("操作日志服务暂时不可用；未改变下单执行结果。");
                if let Some(mut connection) = client.take() { let _ = connection.child.kill().await; }
            }
            if let Some(reply) = reply { let _ = reply.send(result); }
        }
        writers().lock().unwrap().remove(&root);
        if let Some(done) = stopped { let _ = done.send(()); }
    });
}

/// Queue only explicit business metadata. The shared sink redacts it again.
pub fn append(root: &Path, event: &str, context: &Value) {
    let sender = writers().lock().unwrap().get(root).cloned();
    let Some(sender) = sender else { return };
    let mut details = serde_json::Map::new();
    for key in ["queue_job_id", "queue_assignment", "operation", "sequence", "enqueued_at_ms", "mode", "max_concurrency",
        "queued_count", "running_count", "source", "sku", "quantity", "store_id", "store_name",
        "concurrency", "retry_count", "status", "reason", "checked_at_ms", "order_submitted"] {
        if let Some(value) = context.get(key) { details.insert(key.into(), value.clone()); }
    }
    let category = context["category"].as_str().filter(|category|
        matches!(*category, "queue" | "authentication" | "operation")).unwrap_or("queue");
    let params = json!({"account_id":context["account_id"].as_str().unwrap_or(""),
        "task_id":context["task_id"].as_str().unwrap_or(""), "category":category,
        "action":context["action"].as_str().unwrap_or("queue"),
        "level":if context["status"] == "failed" || context["status"] == "submission_unknown" { "error" } else { "info" },
        "event":event, "message":context["message"].as_str().unwrap_or("下单队列状态已更新。"), "details":details});
    if sender.try_send(Message::Append(params)).is_err() {
        eprintln!("操作日志队列已满或正在退出；未改变下单执行结果。");
    }
}

fn validate_query(params: &Value) -> Result<(), String> {
    let fields = params.as_object().ok_or("日志筛选参数无效")?;
    if fields.contains_key("before_id") && fields.contains_key("after_id") {
        return Err("日志前后游标不能同时使用".into());
    }
    for (key, value) in fields {
        match key.as_str() {
            "limit" => if !value.as_u64().is_some_and(|limit| (1..=500).contains(&limit)) {
                return Err("日志每页数量必须为 1–500".into());
            },
            "before_id" | "after_id" | "since_ms" | "until_ms" => if !value.as_u64().is_some_and(|n| n <= i64::MAX as u64) {
                return Err("日志分页或时间参数无效".into());
            },
            "account_id" | "task_id" | "category" | "action" | "level" => if !value.as_str().is_some_and(|text|
                text.len() <= 100 && text.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))) {
                return Err("日志筛选条件无效".into());
            },
            _ => return Err("不支持的日志筛选条件".into()),
        }
    }
    Ok(())
}

pub async fn query(host: &TaskHost, root: &Path, params: Value) -> Result<Value, String> {
    validate_query(&params)?;
    let sender = writers().lock().unwrap().get(root).cloned();
    if let Some(sender) = sender {
        let (reply, result) = oneshot::channel();
        tokio::time::timeout(Duration::from_secs(2), sender.send(Message::Query(params, reply))).await
            .map_err(|_| "日志查询正在排队，请稍后重试")?.map_err(|_| "日志服务正在退出")?;
        return tokio::time::timeout(Duration::from_secs(10), result).await
            .map_err(|_| "日志查询超时")?.map_err(|_| "日志服务已退出")?;
    }
    let mut process = command(host, root, "query");
    for (key, value) in params.as_object().unwrap() {
        process.arg(format!("--{}", key.replace('_', "-")))
            .arg(value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string()));
    }
    process.stdin(Stdio::null());
    let output = tokio::time::timeout(Duration::from_secs(10), process.output()).await
        .map_err(|_| "日志查询超时")?.map_err(|_| "无法运行日志查询")?;
    if !output.status.success() || output.stdout.len() > MAX_RESPONSE { return Err("操作日志暂时无法读取".into()); }
    serde_json::from_slice(&output.stdout).map_err(|_| "日志查询结果格式无效".into())
}

pub async fn stop(root: &Path) {
    let sender = writers().lock().unwrap().get(root).cloned();
    if let Some(sender) = sender {
        let (done, wait) = oneshot::channel();
        let _ = tokio::time::timeout(Duration::from_secs(12), async {
            let _ = sender.send(Message::Stop(done)).await;
            let _ = wait.await;
        }).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_query_rejects_unbounded_or_credential_parameters() {
        assert!(validate_query(&json!({"limit":500,"category":"queue","before_id":10})).is_ok());
        assert!(validate_query(&json!({"limit":40,"after_id":0})).is_ok());
        for invalid in [json!({"limit":501}), json!({"limit":true}), json!({"password":"secret"}),
            json!({"task_id":"../private"}), json!({"since_ms":-1}),
            json!({"after_id":-1}), json!({"after_id":true}), json!({"after_id":1,"before_id":2})] {
            assert!(validate_query(&invalid).is_err());
        }
    }

    #[tokio::test]
    async fn engine_events_reach_the_real_shared_sqlite_reader_without_private_fields() {
        let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
        let project = manifest.parent().unwrap();
        let host = TaskHost { worker: project.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" }),
            script:Some(project.join("worker/main.py")),on_update:std::sync::Arc::new(|_|{}) };
        assert!(host.worker.is_file(), "prepare the worker environment with scripts/setup_worker.py before testing");
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        start(host.clone(), root.to_path_buf());
        append(root, "queue_enqueued", &json!({"queue_job_id":"fixture-job","account_id":"fixture-account",
            "sku":"MLH74FE","store_id":"R705","quantity":1,"message":"门店有货，已加入队列。",
            "queue_assignment":2,"operation":"click",
            "password":"private-password","payment_url":"https://payment.example.test/secret"}));
        let page = query(&host, root, json!({"category":"queue","limit":10})).await.unwrap();
        assert_eq!(page["entries"].as_array().unwrap().len(), 1);
        assert_eq!(page["entries"][0]["details"]["store_id"], "R705");
        assert_eq!(page["entries"][0]["details"]["queue_job_id"], "fixture-job");
        assert_eq!(page["entries"][0]["details"]["queue_assignment"], 2);
        assert_eq!(page["entries"][0]["details"]["operation"], "click");
        assert!(!page.to_string().contains("private-password"));
        assert!(!page.to_string().contains("payment.example"));
        stop(root).await;
        let restored = query(&host, root, json!({"category":"queue"})).await.unwrap();
        assert_eq!(restored["entries"].as_array().unwrap().len(), 1);
    }
}
