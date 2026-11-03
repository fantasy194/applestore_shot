use serde_json::{json, Value};
use std::{path::Path, process::Stdio, sync::Arc, time::Duration};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines}, process::{Child, ChildStdin, ChildStdout, Command}, sync::Mutex};

struct Client { child: Child, stdin: ChildStdin, stdout: Lines<BufReader<ChildStdout>> }
pub struct PushBridge { client: Mutex<Client> }
impl PushBridge {
    pub fn spawn(worker: &Path, script: Option<&Path>, root: &Path) -> Result<Arc<Self>, String> {
        let mut command = Command::new(worker);
        if let Some(script) = script { command.arg(script); }
        command.arg("--web-push").arg("--data-dir").arg(root)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true)
            .env("PYTHONUNBUFFERED", "1").env("PYTHONIOENCODING", "utf-8")
            // Do not deliver durable outbox entries until all recovered task
            // results have been reconciled with the push worker.
            .env("DUO_WEB_PUSH_DEFER_DELIVERY", "1");
        #[cfg(windows)] command.creation_flags(0x08000000);
        let mut child = command.spawn().map_err(|_| "无法启动 Web Push 服务")?;
        let stdin = child.stdin.take().ok_or("没有推送输入通道")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("没有推送输出通道")?).lines();
        Ok(Arc::new(Self { client: Mutex::new(Client { child, stdin, stdout }) }))
    }
    pub async fn call(&self, operation: &str, params: Value) -> Result<Value, String> {
        self.call_with_timeout(operation, params, Duration::from_secs(25)).await
    }
    pub(crate) async fn call_with_timeout(&self, operation: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let mut client = self.client.lock().await;
        if client.child.try_wait().map_err(|_| "无法读取推送服务状态")?.is_some() { return Err("推送服务已停止，请重新启动本地服务".into()); }
        let request_id = uuid::Uuid::new_v4().simple().to_string();
        let data = format!("{}\n", json!({"request_id":request_id,"operation":operation,"params":params}));
        client.stdin.write_all(data.as_bytes()).await.map_err(|_| "推送输入通道已断开")?;
        let deadline = tokio::time::Instant::now() + timeout;
        loop {
            let line = tokio::time::timeout_at(deadline, client.stdout.next_line()).await
                .map_err(|_| "推送服务超时")?.map_err(|_| "推送通道读取失败")?.ok_or("推送服务已停止")?;
            let value: Value = serde_json::from_str(&line).map_err(|_| "推送服务数据无效")?;
            let Some(reply_id) = value["request_id"].as_str() else { return Err("推送服务回复缺少请求标识".into()); };
            // A timed-out request can finish after the next request is sent.
            // Discard its reply instead of treating it as the new response.
            if reply_id != request_id { continue; }
            return if value["ok"] == true { Ok(value["result"].clone()) }
                else { Err(value["error"].as_str().unwrap_or("推送服务暂时不可用").to_owned()) };
        }
    }
    pub async fn stop(&self) { let _ = self.client.lock().await.child.kill().await; }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn python() -> std::path::PathBuf {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        root.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" })
    }

    #[tokio::test]
    async fn late_reply_after_timeout_cannot_satisfy_the_next_call() {
        let temp = tempfile::tempdir().unwrap();
        let script = temp.path().join("late_push.py");
        fs::write(&script, r#"
import json,sys,time
for line in sys.stdin:
    request=json.loads(line)
    if request['operation']=='slow': time.sleep(.25)
    print(json.dumps({'request_id':request['request_id'],'ok':True,
                      'result':{'operation':request['operation']}}),flush=True)
"#).unwrap();
        let bridge = PushBridge::spawn(&python(), Some(&script), temp.path()).unwrap();
        let slow = bridge.call_with_timeout("slow", json!({}), Duration::from_millis(20)).await;
        assert!(slow.is_err());
        let fast = bridge.call_with_timeout("status", json!({}), Duration::from_secs(3)).await.unwrap();
        assert_eq!(fast["operation"], "status");
        bridge.stop().await;
    }
}
