//! A task-bound bridge to the worker's private loopback frame-event/input broker.
//! Neither the registration token nor raw broker errors leave this module.
use crate::engine::Engine;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::{fs::File, io::Read, path::Path, time::Duration};

const UNAVAILABLE: &str = "此账号的浏览器窗口未连接或任务已结束，请返回工作台查看当前任务";
const INVALID_INPUT: &str = "浏览器操作参数无效，请使用当前窗口的点击、输入、按键或滚动控件";
const INVALID_RESPONSE: &str = "浏览器窗口暂时无法读取，请稍后刷新";
const MAX_RESPONSE: usize = 3 * 1024 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Registration {
    version: u8,
    port: u16,
    token: String,
    account_id: String,
    task_id: String,
}

fn valid_id(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok_and(|id| !id.is_nil() && id.to_string() == value)
}

fn task_active(engine: &Engine, account_id: &str, task_id: &str) -> bool {
    if !valid_id(account_id) || !valid_id(task_id) {
        return false;
    }
    let snapshot = engine.snapshot();
    snapshot
        .accounts
        .iter()
        .any(|account| account.id == account_id)
        && snapshot.tasks.iter().any(|task| {
            task.id == task_id
                && task.account_id == account_id
                && matches!(task.status.as_str(), "running" | "waiting")
        })
}

fn registration(
    root: &Path,
    account_id: &str,
    task_id: &str,
) -> Result<Option<Registration>, String> {
    let canonical_root = root.canonicalize().map_err(|_| UNAVAILABLE.to_owned())?;
    let task_path = canonical_root
        .join("accounts")
        .join(account_id)
        .join("tasks")
        .join(task_id);
    let path = task_path.join("browser-remote.json");
    if !path.exists() {
        return Ok(None);
    }
    // A local registration cannot redirect the bridge through a symlink/junction.
    let canonical_task = task_path
        .canonicalize()
        .map_err(|_| UNAVAILABLE.to_owned())?;
    if canonical_task != task_path
        || path.canonicalize().map_err(|_| UNAVAILABLE.to_owned())? != path
    {
        return Err(UNAVAILABLE.into());
    }
    let mut raw = Vec::new();
    File::open(&path)
        .map_err(|_| UNAVAILABLE.to_owned())?
        .take(4097)
        .read_to_end(&mut raw)
        .map_err(|_| UNAVAILABLE.to_owned())?;
    if raw.len() > 4096 {
        return Err(UNAVAILABLE.into());
    }
    let record: Registration = serde_json::from_slice(&raw).map_err(|_| UNAVAILABLE.to_owned())?;
    if record.version != 1
        || record.port == 0
        || record.account_id != account_id
        || record.task_id != task_id
        || record.token.len() != 64
        || !record
            .token
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(UNAVAILABLE.into());
    }
    Ok(Some(record))
}

fn number(value: &Value, name: &str, min: f64, max: f64) -> Result<f64, String> {
    value[name]
        .as_f64()
        .filter(|number| number.is_finite() && *number >= min && *number <= max)
        .ok_or_else(|| INVALID_INPUT.to_owned())
}

fn input(params: &Value) -> Result<Value, String> {
    let object = params.as_object().ok_or_else(|| INVALID_INPUT.to_owned())?;
    let kind = params["type"]
        .as_str()
        .ok_or_else(|| INVALID_INPUT.to_owned())?;
    let allowed: &[&str] = match kind {
        "click" => &["type", "x", "y", "sequence", "frame_sequence", "document_id"],
        "text" => &["type", "text", "sequence", "frame_sequence", "document_id"],
        "key" => &["type", "key", "sequence", "frame_sequence", "document_id"],
        "scroll" => &["type", "delta_x", "delta_y", "sequence", "frame_sequence", "document_id"],
        "signin" => &["type", "sequence", "frame_sequence", "document_id"],
        _ => return Err(INVALID_INPUT.into()),
    };
    if object.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(INVALID_INPUT.into());
    }
    let sequence = params
        .get("sequence")
        .or_else(|| params.get("frame_sequence"))
        .and_then(Value::as_u64)
        .ok_or_else(|| INVALID_INPUT.to_owned())?;
    if params
        .get("frame_sequence")
        .is_some_and(|other| other.as_u64() != Some(sequence))
    {
        return Err(INVALID_INPUT.into());
    }
    let normalized: Result<Value, String> = match kind {
        "signin" => Ok(json!({"type":"signin"})),
        "click" => Ok(
            json!({"type":"click", "x":number(params, "x", 0., 4096.)?, "y":number(params, "y", 0., 4096.)?}),
        ),
        "text" => {
            let text = params["text"]
                .as_str()
                .filter(|text| {
                    !text.is_empty()
                        && text.len() <= 4096
                        && !text.chars().any(|character| {
                            character == '\0'
                                || character.is_control() && !matches!(character, '\n' | '\t')
                        })
                })
                .ok_or_else(|| INVALID_INPUT.to_owned())?;
            Ok(json!({"type":"text", "text":text}))
        }
        "key" => {
            let key = params["key"]
                .as_str()
                .filter(|key| {
                    matches!(
                        *key,
                        "Enter"
                            | "Tab"
                            | "Shift+Tab"
                            | "Backspace"
                            | "Delete"
                            | "Escape"
                            | "ArrowLeft"
                            | "ArrowRight"
                            | "ArrowUp"
                            | "ArrowDown"
                            | "Home"
                            | "End"
                            | "PageUp"
                            | "PageDown"
                            | "Space"
                            | "Control+A"
                            | "Meta+A"
                    )
                })
                .ok_or_else(|| INVALID_INPUT.to_owned())?;
            Ok(json!({"type":"key", "key":key}))
        }
        "scroll" => Ok(
            json!({"type":"scroll", "delta_x":number(params,"delta_x",-4096.,4096.)?,
            "delta_y":number(params,"delta_y",-4096.,4096.)?}),
        ),
        _ => Err(INVALID_INPUT.into()),
    };
    let mut normalized = normalized?;
    normalized["sequence"] = json!(sequence);
    if let Some(document) = params.get("document_id") {
        let document = document.as_str().filter(|value| value.len() == 32 && value.bytes().all(|b| b.is_ascii_hexdigit())).ok_or_else(|| INVALID_INPUT.to_owned())?;
        normalized["document_id"] = json!(document);
    }
    Ok(normalized)
}

fn safe_status(value: Value) -> Result<Value, String> {
    let active = value["active"]
        .as_bool()
        .ok_or_else(|| INVALID_RESPONSE.to_owned())?;
    if !active {
        return Ok(json!({"active":false,"frame":null,"url_host":"","connected":false,"frame_current":false,"frame_updated":true,"document_id":"","revision":0,"frame_age_ms":0}));
    }
    let mut result = Map::new();
    result.insert("active".into(), json!(true));
    let host = value["url_host"].as_str().unwrap_or("");
    if host.len() > 253
        || !host
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.'))
    {
        return Err(INVALID_RESPONSE.into());
    }
    result.insert("url_host".into(), json!(host));
    for name in ["connected", "frame_current", "frame_updated", "signin_available"] {
        if let Some(value) = value[name].as_bool() { result.insert(name.into(), json!(value)); }
    }
    for name in ["revision", "frame_age_ms"] {
        if let Some(value) = value[name].as_u64() { result.insert(name.into(), json!(value)); }
    }
    let document = value["document_id"].as_str().unwrap_or("");
    if document.len() > 64 || !document.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(INVALID_RESPONSE.into());
    }
    result.insert("document_id".into(), json!(document));
    if let Some(status) = value["signin_status"].as_str() {
        // Only finite protocol states may leave the worker, never arbitrary errors.
        if matches!(status, "" | "ready" | "running" | "working" | "unavailable" | "manual" | "submitted" | "verification" | "blocked" | "interrupted") {
            result.insert("signin_status".into(), json!(status));
        }
    }
    let frame = &value["frame"];
    if frame.is_null() {
        result.insert("frame".into(), Value::Null);
    } else {
        if !document.is_empty() && frame["document_id"].as_str() != Some(document) {
            return Err(INVALID_RESPONSE.into());
        }
        let data = frame["data_url"]
            .as_str()
            .ok_or_else(|| INVALID_RESPONSE.to_owned())?;
        let (encoded, png) = if let Some(encoded) = data.strip_prefix("data:image/jpeg;base64,") {
            (encoded, false)
        } else if let Some(encoded) = data.strip_prefix("data:image/png;base64,") {
            (encoded, true)
        } else {
            return Err(INVALID_RESPONSE.into());
        };
        if encoded.len() > 2_800_000 {
            return Err(INVALID_RESPONSE.into());
        }
        let bytes = STANDARD
            .decode(encoded)
            .map_err(|_| INVALID_RESPONSE.to_owned())?;
        if bytes.len() > 2_097_152
            || (png && !bytes.starts_with(b"\x89PNG\r\n\x1a\n"))
            || (!png && !bytes.starts_with(b"\xff\xd8\xff"))
        {
            return Err(INVALID_RESPONSE.into());
        }
        let width = frame["width"]
            .as_u64()
            .filter(|size| (1..=4096).contains(size))
            .ok_or_else(|| INVALID_RESPONSE.to_owned())?;
        let height = frame["height"]
            .as_u64()
            .filter(|size| (1..=4096).contains(size))
            .ok_or_else(|| INVALID_RESPONSE.to_owned())?;
        let sequence = frame["sequence"]
            .as_u64()
            .ok_or_else(|| INVALID_RESPONSE.to_owned())?;
        let captured_at_ms = frame["captured_at_ms"]
            .as_u64()
            .ok_or_else(|| INVALID_RESPONSE.to_owned())?;
        result.insert("frame".into(), json!({"data_url":data,"width":width,"height":height,"sequence":sequence,"captured_at_ms":captured_at_ms,"document_id":document}));
    }
    Ok(Value::Object(result))
}

/// Only an already authenticated application route may call this method.
pub async fn command(
    engine: &Engine,
    account_id: &str,
    task_id: &str,
    operation: &str,
    params: &Value,
) -> Result<Value, String> {
    if !task_active(engine, account_id, task_id) {
        return Err(UNAVAILABLE.into());
    }
    if !matches!(operation, "status" | "input" | "events") {
        return Err(INVALID_INPUT.into());
    }
    let body = if operation == "input" {
        Some(input(params)?)
    } else {
        None
    };
    let Some(record) = registration(&engine.root, account_id, task_id)? else {
        return if operation != "input" {
            Ok(json!({"active":false,"frame":null,"url_host":"","connected":false,"frame_current":false,"frame_updated":true,"document_id":"","revision":0,"frame_age_ms":0}))
        } else {
            Err(UNAVAILABLE.into())
        };
    };
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(Duration::from_secs(1))
        .timeout(Duration::from_secs(if operation == "input" { 100 } else { 4 }))
        .pool_max_idle_per_host(0)
        .build()
        .map_err(|_| UNAVAILABLE.to_owned())?;
    let endpoint = match operation {
        "events" => format!("events?after={}", params["after"].as_u64().unwrap_or(0)),
        "status" => "status".to_owned(),
        _ => "input".to_owned(),
    };
    let url = format!("http://127.0.0.1:{}/{}", record.port, endpoint);
    let request = if let Some(body) = body {
        client
            .post(url)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(serde_json::to_vec(&body).map_err(|_| INVALID_INPUT.to_owned())?)
    } else {
        client.get(url)
    };
    let mut response = request
        .bearer_auth(&record.token)
        .send()
        .await
        .map_err(|_| UNAVAILABLE.to_owned())?;
    if !response.status().is_success() {
        return Err(INVALID_RESPONSE.into());
    }
    if response
        .content_length()
        .is_some_and(|size| size > MAX_RESPONSE as u64)
    {
        return Err(INVALID_RESPONSE.into());
    }
    let mut raw = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| INVALID_RESPONSE.to_owned())?
    {
        if raw.len().saturating_add(chunk.len()) > MAX_RESPONSE {
            return Err(INVALID_RESPONSE.into());
        }
        raw.extend_from_slice(&chunk);
    }
    // A task can finish while a frame/input request is in flight. Discard late responses.
    if !task_active(engine, account_id, task_id) {
        return Err(UNAVAILABLE.into());
    }
    let value: Value = serde_json::from_slice(&raw).map_err(|_| INVALID_RESPONSE.to_owned())?;
    if operation != "input" {
        safe_status(value)
    } else if value["accepted"] == true {
        crate::activity::append(&engine.root, "browser_remote_input", &json!({
            "account_id":account_id,"task_id":task_id,"category":"authentication","action":"remote_input",
            "message":"远程登录窗口操作已发送。","operation":params["type"].as_str().unwrap_or("")
        }));
        Ok(json!({"accepted":true}))
    } else {
        Err(INVALID_RESPONSE.into())
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::engine::{private_json, TaskHost};
    use std::{path::PathBuf, sync::Arc};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
        sync::oneshot,
    };

    fn frame() -> Value {
        json!({"active":true,"url_host":"idmsa.apple.com","title":"SECRET_TITLE","token":"SECRET_TOKEN",
            "frame":{"data_url":"data:image/jpeg;base64,/9j/2Q==","width":800,"height":600,"sequence":7,"captured_at_ms":1000,"password":"SECRET_FRAME"}})
    }

    #[test]
    fn input_is_bounded_and_never_allows_navigation_or_evaluation() {
        assert_eq!(
            input(&json!({"type":"click","x":12,"y":13,"frame_sequence":7})).unwrap(),
            json!({"type":"click","x":12.0,"y":13.0,"sequence":7})
        );
        assert!(input(&json!({"type":"text","text":"fixture-password","sequence":7})).is_ok());
        assert_eq!(
            input(&json!({"type":"text","text":"   ","sequence":7})).unwrap(),
            json!({"type":"text","text":"   ","sequence":7})
        );
        assert_eq!(
            input(&json!({"type":"key","key":"Space","sequence":7})).unwrap(),
            json!({"type":"key","key":"Space","sequence":7})
        );
        for value in [
            json!({"type":"navigate","url":"https://apple.com"}),
            json!({"type":"click","x":12,"y":13,"sequence":7,"url":"https://apple.com"}),
            json!({"type":"click","x":-1,"y":13,"sequence":7}),
            json!({"type":"text","text":"x".repeat(4097),"sequence":7}),
            json!({"type":"key","key":"Control+L","sequence":7}),
            json!({"type":"text","text":"fixture-password"}),
            json!({"type":"click","x":12,"y":13,"sequence":7,"frame_sequence":8}),
        ] {
            assert_eq!(input(&value).unwrap_err(), INVALID_INPUT);
        }
    }

    #[test]
    fn frames_only_return_image_fields_without_arbitrary_broker_metadata() {
        let output = safe_status(frame()).unwrap();
        assert_eq!(output["frame"]["width"], 800);
        assert!(!output.to_string().contains("SECRET"));
        let mut invalid = frame();
        invalid["frame"]["data_url"] = json!("data:image/svg+xml;base64,PHN2Zz4=");
        assert!(safe_status(invalid).is_err());
        let inactive =
            safe_status(json!({"active":false,"error":"SECRET","token":"SECRET"})).unwrap();
        assert_eq!(inactive["active"], false);
        assert_eq!(inactive["connected"], false);
    }

    #[test]
    fn live_metadata_and_signin_are_bounded_without_exposing_arbitrary_messages() {
        let document = "a".repeat(32);
        assert_eq!(input(&json!({"type":"signin","sequence":7,"document_id":document})).unwrap(),
            json!({"type":"signin","sequence":7,"document_id":document}));
        assert!(input(&json!({"type":"signin","sequence":7,"password":"SECRET"})).is_err());
        assert!(input(&json!({"type":"signin","sequence":7,"document_id":"INVALID"})).is_err());
        let output = safe_status(json!({"active":true,"frame":null,"connected":true,"frame_current":true,
            "frame_updated":false,"revision":9,"frame_age_ms":60000,"document_id":document,
            "signin_status":"SECRET", "signin_available":true})).unwrap();
        assert_eq!(output["frame_age_ms"],60000);
        assert_eq!(output["frame_updated"],false);
        assert!(!output.to_string().contains("SECRET"));
    }

    pub(crate) async fn task(root: &Path) -> (Arc<Engine>, String, String) {
        let engine = Engine::new(root.into()).unwrap();
        let account = engine
            .add_account("browser-fixture@example.test".into())
            .unwrap();
        let project = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let python = project.join(if cfg!(windows) {
            ".venv/Scripts/python.exe"
        } else {
            ".venv/bin/python"
        });
        assert!(
            python.is_file(),
            "browser broker tests require the existing worker Python environment"
        );
        let fixture = root.join("browser-task-fixture.py");
        std::fs::write(&fixture, "import json,sys\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'v':1,'account_id':r['account_id'],'task_id':r['task_id'],'type':'stage','message':'fixture'}),flush=True)\nfor line in sys.stdin:\n m=json.loads(line)\n if m.get('type')=='cancel':\n  print(json.dumps({'v':1,'account_id':r['account_id'],'task_id':r['task_id'],'type':'cancelled'}),flush=True)\n  break\n").unwrap();
        let host = TaskHost {
            worker: python,
            script: Some(fixture),
            on_update: Arc::new(|_| {}),
        };
        let id = engine
            .start(host, account.id.clone(), "enroll".into(), json!({}))
            .unwrap();
        for _ in 0..100 {
            if task_active(&engine, &account.id, &id) {
                return (engine, account.id, id);
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("fixture task did not start");
    }

    async fn broker(body: String, status: &str, extra: &str) -> (u16, oneshot::Receiver<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let status = status.to_owned();
        let extra = extra.to_owned();
        let (sender, receiver) = oneshot::channel();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut data = Vec::new();
            let mut buffer = [0u8; 4096];
            loop {
                let count = stream.read(&mut buffer).await.unwrap();
                if count == 0 {
                    break;
                }
                data.extend_from_slice(&buffer[..count]);
                if let Some(index) = data.windows(4).position(|part| part == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&data[..index]).to_lowercase();
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.strip_prefix("content-length:")
                                .and_then(|value| value.trim().parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    if data.len() >= index + 4 + length {
                        break;
                    }
                }
            }
            let response = format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n{extra}\r\n{body}",body.len());
            let _ = stream.write_all(response.as_bytes()).await;
            let _ = sender.send(String::from_utf8(data).unwrap());
        });
        (port, receiver)
    }

    fn register(root: &Path, account: &str, task: &str, port: u16) {
        private_json(&root.join("accounts").join(account).join("tasks").join(task).join("browser-remote.json"),
            &json!({"version":1,"port":port,"token":"a".repeat(64),"account_id":account,"task_id":task})).unwrap();
    }

    #[tokio::test]
    async fn real_loopback_status_and_input_are_task_bound_and_do_not_echo_secrets() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account, task) = task(temporary.path()).await;
        let (port, request) = broker(frame().to_string(), "200 OK", "").await;
        register(temporary.path(), &account, &task, port);
        let output = command(&engine, &account, &task, "status", &json!({}))
            .await
            .unwrap();
        assert!(!output.to_string().contains("SECRET"));
        let request = request.await.unwrap();
        assert!(request.starts_with("GET /status HTTP/1.1"));
        assert!(request.contains(&format!("Bearer {}", "a".repeat(64))));
        let (port, request) = broker(
            json!({"accepted":true,"password":"SECRET_INPUT"}).to_string(),
            "200 OK",
            "",
        )
        .await;
        register(temporary.path(), &account, &task, port);
        let output = command(
            &engine,
            &account,
            &task,
            "input",
            &json!({"type":"text","text":"fixture-only-password","sequence":7}),
        )
        .await
        .unwrap();
        assert_eq!(output, json!({"accepted":true}));
        let request = request.await.unwrap();
        assert!(request.starts_with("POST /input HTTP/1.1"));
        assert!(request.contains("fixture-only-password"));
        let other = engine
            .add_account("other-fixture@example.test".into())
            .unwrap();
        assert!(command(&engine, &other.id, &task, "status", &json!({}))
            .await
            .is_err());
        engine.cancel(&account, &task).unwrap();
        for _ in 0..100 {
            if !task_active(&engine, &account, &task) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(command(&engine, &account, &task, "status", &json!({}))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn malformed_registry_redirect_and_error_bodies_are_never_exposed() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account, task) = task(temporary.path()).await;
        let path = temporary
            .path()
            .join("accounts")
            .join(&account)
            .join("tasks")
            .join(&task)
            .join("browser-remote.json");
        private_json(&path,&json!({"version":1,"port":80,"token":"SECRET_TOKEN","account_id":account,"task_id":task,"host":"evil.test"})).unwrap();
        assert_eq!(
            command(&engine, &account, &task, "status", &json!({}))
                .await
                .unwrap_err(),
            UNAVAILABLE
        );
        let (port, request) = broker(
            "SECRET_PASSWORD".into(),
            "302 Found",
            "Location: http://198.51.100.1/never-follow\r\n",
        )
        .await;
        register(temporary.path(), &account, &task, port);
        let error = command(&engine, &account, &task, "status", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(error, INVALID_RESPONSE);
        request.await.unwrap();
        let (port, request) = broker("SECRET_PASSWORD".into(), "200 OK", "").await;
        register(temporary.path(), &account, &task, port);
        let error = command(&engine, &account, &task, "status", &json!({}))
            .await
            .unwrap_err();
        assert_eq!(error, INVALID_RESPONSE);
        request.await.unwrap();
        engine.cancel(&account, &task).unwrap();
        for _ in 0..100 {
            if !task_active(&engine, &account, &task) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    #[tokio::test]
    async fn oversized_screenshot_body_is_rejected_before_forwarding() {
        let temporary = tempfile::tempdir().unwrap();
        let (engine, account, task) = task(temporary.path()).await;
        let (port, request) = broker("x".repeat(MAX_RESPONSE + 1), "200 OK", "").await;
        register(temporary.path(), &account, &task, port);
        assert_eq!(
            command(&engine, &account, &task, "status", &json!({}))
                .await
                .unwrap_err(),
            INVALID_RESPONSE
        );
        request.await.unwrap();
        engine.cancel(&account, &task).unwrap();
        for _ in 0..100 {
            if !task_active(&engine, &account, &task) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
}
