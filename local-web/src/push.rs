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
            .env("PYTHONUNBUFFERED", "1").env("PYTHONIOENCODING", "utf-8");
        #[cfg(windows)] command.creation_flags(0x08000000);
        let mut child = command.spawn().map_err(|_| "无法启动 Web Push 服务")?;
        let stdin = child.stdin.take().ok_or("没有推送输入通道")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("没有推送输出通道")?).lines();
        Ok(Arc::new(Self { client: Mutex::new(Client { child, stdin, stdout }) }))
    }
    pub async fn call(&self, operation: &str, params: Value) -> Result<Value, String> {
        let mut client = self.client.lock().await;
        if client.child.try_wait().map_err(|_| "无法读取推送服务状态")?.is_some() { return Err("推送服务已停止，请重新启动本地服务".into()); }
        let data = format!("{}\n", json!({"operation":operation,"params":params}));
        client.stdin.write_all(data.as_bytes()).await.map_err(|_| "推送输入通道已断开")?;
        let line = tokio::time::timeout(Duration::from_secs(25), client.stdout.next_line()).await
            .map_err(|_| "推送服务超时")?.map_err(|_| "推送通道读取失败")?.ok_or("推送服务已停止")?;
        let value: Value = serde_json::from_str(&line).map_err(|_| "推送服务数据无效")?;
        if value["ok"] == true { Ok(value["result"].clone()) } else { Err(value["error"].as_str().unwrap_or("推送服务暂时不可用").to_owned()) }
    }
    pub async fn stop(&self) { let _ = self.client.lock().await.child.kill().await; }
}
