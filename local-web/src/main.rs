#[path = "../../src-tauri/src/engine.rs"]
mod engine;
#[path = "../../src-tauri/src/browser_remote.rs"]
mod browser_remote;
#[path = "../../src-tauri/src/activity.rs"]
mod activity;
#[path = "../../src-tauri/src/session_maintenance.rs"]
mod session_maintenance;
mod auth;
mod access;
mod push;

use axum::{
    extract::{DefaultBodyLimit, Path as UrlPath, Request, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{
        sse::{Event, KeepAlive},
        IntoResponse, Response, Sse,
    },
    routing::{get, post},
    Json, Router,
};
use engine::{Engine, Task, TaskHost};
use fs2::FileExt;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{convert::Infallible, fs::OpenOptions, path::PathBuf, sync::{Arc, Mutex}};
use tokio::sync::{broadcast, watch};
use tower_http::services::ServeDir;

#[derive(Clone)]
struct LocalState {
    engine: Arc<Engine>,
    host: TaskHost,
    events: broadcast::Sender<Task>,
    stopping: watch::Receiver<bool>,
    shutdown: watch::Sender<bool>,
    authority: String,
    token: String,
    dev_origin: Option<String>,
    auth: Arc<auth::Auth>,
    public_origin: Arc<Mutex<Option<String>>>,
    allowed_origins: Arc<Vec<access::AllowedOrigin>>,
    tunnel_disabled: bool,
    trust_network: bool,
    push: Arc<push::PushBridge>,
}

fn bad_request(message: String) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"error":message}))).into_response()
}

async fn auth_status(State(s): State<LocalState>, headers: HeaderMap) -> Response {
    let local_host = headers.get(header::HOST).and_then(|x| x.to_str().ok()) == Some(s.authority.as_str());
    let authenticated = if local_host || s.trust_network { s.auth.authenticated(&headers, &s.token) } else { s.auth.authenticated_session(&headers) };
    Json(json!({"authenticated":authenticated})).into_response()
}

async fn login(State(s): State<LocalState>, headers: HeaderMap, Json(body): Json<Value>) -> Response {
    let password = body["password"].as_str().unwrap_or("");
    if password.len() > 128 { return bad_request("访问密码过长".into()); }
    let hash = match s.auth.begin_login() { Ok(hash) => hash, Err(message) => return (StatusCode::TOO_MANY_REQUESTS, Json(json!({"error":message}))).into_response() };
    let password = password.to_owned();
    let verified = tokio::task::spawn_blocking(move || auth::Auth::verify(&password, &hash)).await.unwrap_or(false);
    if !verified { return (StatusCode::UNAUTHORIZED, Json(json!({"error":"访问密码不正确"}))).into_response(); }
    let host = headers.get(header::HOST).and_then(|x| x.to_str().ok());
    let secure = if s.trust_network { access::forwarded_secure(&headers) } else {
        host != Some(s.authority.as_str())
            && s.allowed_origins.iter().find(|origin| host == Some(origin.authority.as_str())).is_none_or(|origin| origin.secure)
    };
    let cookie = format!("duo_session={}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800{}", s.auth.session(), if secure { "; Secure" } else { "" });
    ([(header::SET_COOKIE, cookie)], Json(json!({"authenticated":true}))).into_response()
}

async fn logout(State(s): State<LocalState>, headers: HeaderMap) -> Response {
    s.auth.logout(&headers);
    ([(header::SET_COOKIE, "duo_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")], Json(json!({"authenticated":false}))).into_response()
}

async fn password(State(s): State<LocalState>, Json(body): Json<Value>) -> Response {
    match s.auth.change_password(&s.engine.root, body["password"].as_str().unwrap_or("")) {
        Ok(()) => ([(header::SET_COOKIE, "duo_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")], Json(json!({"success":true}))).into_response(),
        Err(message) => bad_request(message),
    }
}

fn valid_public_origin(value: &str) -> bool {
    value.strip_prefix("https://").is_some_and(|host| host.strip_suffix(".trycloudflare.com").is_some_and(|name|
        !name.is_empty() && name.len() <= 63 && name.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')))
}

async fn tunnel(State(s): State<LocalState>, headers: HeaderMap, Json(body): Json<Value>) -> Response {
    if s.tunnel_disabled {
        return (StatusCode::FORBIDDEN, Json(json!({"error":"当前部署已停用 Quick Tunnel"}))).into_response();
    }
    // Only a local launch-token holder may change the one trusted public Host.
    if (!s.trust_network && headers.get(header::HOST).and_then(|x| x.to_str().ok()) != Some(s.authority.as_str()))
        || headers.get(header::AUTHORIZATION).and_then(|x| x.to_str().ok()) != Some(s.token.as_str()) {
        return StatusCode::FORBIDDEN.into_response();
    }
    if body["origin"].is_null() {
        let mut current = s.public_origin.lock().unwrap();
        if body["expected_origin"].as_str().is_none_or(|expected| current.as_deref() == Some(expected)) {
            *current = None;
        }
    } else {
        let origin = body["origin"].as_str().unwrap_or("");
        if !valid_public_origin(origin) { return bad_request("只允许当前 Quick Tunnel 的 HTTPS 地址".into()); }
        let mut current = s.public_origin.lock().unwrap();
        if current.as_deref().is_some_and(|active| active != origin) {
            return (StatusCode::CONFLICT, Json(json!({"error":"已有 Quick Tunnel 正在运行，请先关闭原隧道"}))).into_response();
        }
        *current = Some(origin.to_owned());
    }
    Json(json!({"origin":s.public_origin.lock().unwrap().clone()})).into_response()
}

async fn request_shutdown(State(s): State<LocalState>, headers: HeaderMap) -> Response {
    if (!s.trust_network && headers.get(header::HOST).and_then(|x| x.to_str().ok()) != Some(s.authority.as_str()))
        || headers.get(header::AUTHORIZATION).and_then(|x| x.to_str().ok()) != Some(s.token.as_str()) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let _ = s.shutdown.send(true);
    Json(json!({"stopping":true})).into_response()
}

async fn push_command(State(s): State<LocalState>, UrlPath(operation): UrlPath<String>, Json(body): Json<Value>) -> Response {
    if !matches!(operation.as_str(), "status" | "subscribe" | "unsubscribe" | "test" | "acknowledge") {
        return StatusCode::NOT_FOUND.into_response();
    }
    match s.push.call(&operation, body).await { Ok(value) => Json(value).into_response(), Err(message) => bad_request(message) }
}

// Match the existing desktop bridge so all account and checkout UI can be reused.
#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Input {
    #[serde(default)]
    cursor: Option<String>,
    #[serde(default)]
    include_logs: Option<bool>,
    #[serde(default)]
    email: String,
    #[serde(default)]
    account_id: String,
    #[serde(default)]
    action: String,
    #[serde(default)]
    params: Value,
    #[serde(default)]
    task_id: String,
    #[serde(default)]
    request_id: String,
    #[serde(default)]
    value: Value,
    #[serde(default)]
    sku: String,
    #[serde(default)]
    quantity: u32,
    #[serde(default)]
    operation: String,
}

async fn command(
    State(s): State<LocalState>,
    UrlPath(command): UrlPath<String>,
    Json(p): Json<Input>,
) -> Response {
    let result: Result<Value, String> = match command.as_str() {
        "snapshot" => Ok(json!(s.engine.snapshot())),
        "workspace_snapshot" => Ok(s.engine.workspace_snapshot()),
        "workspace_changes" => if p.cursor.as_ref().is_some_and(|cursor| cursor.len() > 128) {
            Err("同步游标无效".into())
        } else { Ok(s.engine.workspace_changes(p.cursor.as_deref())) },
        "task_detail" => s.engine.task_read(&p.account_id, &p.task_id, p.include_logs.unwrap_or(true)),
        "browser_command" => browser_remote::command(&s.engine, &p.account_id, &p.task_id, &p.operation, &p.params).await,
        "queue_command" => s.engine.queue_command(s.host.clone(), &p.operation, &p.params),
        "activity_logs" => activity::query(&s.host, &s.engine.root, p.params).await,
        "maintenance_command" => session_maintenance::command(s.engine.clone(), s.host.clone(), &p.operation, &p.params).await,
        "add_account" => s.engine.add_account(p.email).map(|a| json!(a)),
        "remove_account" => s.engine.remove_account(&s.host, &p.account_id).await.map(|_| Value::Null),
        "save_preset" => s
            .engine
            .save_preset(&p.account_id, p.sku, p.quantity)
            .map(|a| json!(a)),
        "start_task" => s
            .engine
            .start(s.host, p.account_id, p.action, p.params)
            .map(|id| json!(id)),
        "reply_task" => s
            .engine
            .reply(&s.host, &p.account_id, &p.task_id, &p.request_id, p.value)
            .map(|_| Value::Null),
        "cancel_task" => s
            .engine
            .cancel(&p.account_id, &p.task_id)
            .map(|_| Value::Null),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    match result {
        Ok(value) => Json(value).into_response(),
        Err(message) => (StatusCode::BAD_REQUEST, Json(json!({"error":message}))).into_response(),
    }
}

async fn events(State(s): State<LocalState>) -> Response {
    // Subscribe before taking the snapshot; reconnects recover prompts/results missed offline.
    let mut receiver = s.events.subscribe();
    let mut stopping = s.stopping.clone();
    let stream = async_stream::stream! {
        // Flush headers immediately, including a fresh workspace with no tasks.
        yield Ok::<_, Infallible>(Event::default().comment("connected"));
        for task in s.engine.snapshot().tasks {
            yield Ok::<_, Infallible>(Event::default().json_data(task).unwrap());
        }
        loop {
            tokio::select! {
                _ = stopping.changed() => break,
                event = receiver.recv() => match event {
                    Ok(task) => yield Ok(Event::default().json_data(task).unwrap()),
                    Err(broadcast::error::RecvError::Lagged(_)) => {
                        for task in s.engine.snapshot().tasks {
                            yield Ok(Event::default().json_data(task).unwrap());
                        }
                    }
                    Err(_) => break,
                }
            }
        }
    };
    Sse::new(stream)
        .keep_alive(KeepAlive::default().interval(std::time::Duration::from_secs(5)))
        .into_response()
}

async fn guard(State(s): State<LocalState>, request: Request, next: Next) -> Response {
    let mut response = guarded_request(State(s), request, next).await;
    let headers = response.headers_mut();
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    headers.insert(header::X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    headers.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"));
    response
}

async fn guarded_request(State(s): State<LocalState>, request: Request, next: Next) -> Response {
    // Loopback binding alone is insufficient: reject DNS rebinding and foreign browser origins.
    if *s.stopping.borrow() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let h = request.headers();
    let public_origin = s.public_origin.lock().unwrap().clone();
    let host = h.get(header::HOST).and_then(|v| v.to_str().ok());
    let local_host = host == Some(s.authority.as_str());
    let public_host = public_origin.as_deref().is_some_and(|origin| host == origin.strip_prefix("https://"));
    let allowed_origin = s.allowed_origins.iter().find(|origin| host == Some(origin.authority.as_str()));
    let host_ok = local_host || public_host || allowed_origin.is_some();
    let origin_ok = h.get(header::ORIGIN).is_none_or(|v| {
        local_host && (v == format!("http://{}", s.authority).as_str()
            || s.dev_origin.as_deref().is_some_and(|origin| v == origin))
            || public_host && public_origin.as_deref().is_some_and(|origin| v == origin)
            || allowed_origin.is_some_and(|origin| v == origin.origin.as_str())
    });
    if !s.trust_network && (!host_ok || !origin_ok) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let path = request.uri().path();
    let public_auth = matches!(path, "/api/auth/status" | "/api/auth/login");
    if !s.trust_network && !local_host && (matches!(path, "/api/shutdown" | "/api/tunnel/register")
        || request.method() != axum::http::Method::GET && h.get(header::ORIGIN).is_none()) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let authenticated = if local_host || s.trust_network { s.auth.authenticated(h, &s.token) } else { s.auth.authenticated_session(h) };
    if path.starts_with("/api/") && !public_auth && !authenticated
    {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error":"请使用访问密码登录，或从本机启动链接打开。"})),
        )
            .into_response();
    }
    let mut response = next.run(request).await;
    let h = response.headers_mut();
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(header::CONTENT_SECURITY_POLICY, HeaderValue::from_static("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"));
    response
}

fn data_directory() -> Result<PathBuf, String> {
    let base = if cfg!(target_os = "macos") {
        PathBuf::from(std::env::var("HOME").map_err(|_| "无法确定用户目录")?)
            .join("Library/Application Support")
    } else if cfg!(windows) {
        PathBuf::from(std::env::var("APPDATA").map_err(|_| "无法确定用户目录")?)
    } else {
        return Err("当前本地 Web 构建支持 macOS 和 Windows".into());
    };
    Ok(base.join("com.niulai.local-web"))
}

fn open_browser(url: &str) -> std::io::Result<()> {
    #[cfg(target_os = "macos")]
    std::process::Command::new("open").arg(url).spawn()?;
    #[cfg(windows)]
    std::process::Command::new("rundll32.exe")
        .args(["url.dll,FileProtocolHandler", url])
        .spawn()?;
    Ok(())
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    }
    #[cfg(windows)]
    {
        let mut break_signal = tokio::signal::windows::ctrl_break().expect("signal handler");
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = break_signal.recv() => {} }
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut directory = None;
    let mut port: u16 = 17653;
    let mut listen_address = std::net::Ipv4Addr::LOCALHOST;
    let mut open = true;
    let mut dev_origin = None;
    let mut source_worker = false;
    let mut web_root = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--data-dir" => {
                directory = Some(PathBuf::from(args.next().ok_or("--data-dir 缺少路径")?))
            }
            "--port" => port = args.next().ok_or("--port 缺少端口")?.parse()?,
            "--listen-address" => {
                listen_address = args.next().ok_or("--listen-address 缺少 IPv4 地址")?.parse()?;
            }
            "--no-open" => open = false,
            "--source-worker" => source_worker = true,
            "--web-root" => web_root = Some(PathBuf::from(args.next().ok_or("--web-root 缺少路径")?)),
            "--dev-origin" if cfg!(debug_assertions) => {
                let origin = args.next().ok_or("--dev-origin 缺少网址")?;
                let dev_port = origin
                    .strip_prefix("http://127.0.0.1:")
                    .and_then(|port| port.parse::<u16>().ok())
                    .filter(|port| *port != 0)
                    .ok_or("开发网页必须使用 http://127.0.0.1:<端口>")?;
                dev_origin = Some(format!("http://127.0.0.1:{dev_port}"));
            }
            _ => return Err(format!("未知参数：{arg}").into()),
        }
    }
    let package = std::env::current_exe()?
        .parent()
        .ok_or("无效程序路径")?
        .to_path_buf();
    let development = dev_origin.is_some();
    let (worker, script) = if development || source_worker {
        let project = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or("无效项目路径")?;
        (
            project.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            }),
            Some(project.join("worker/main.py")),
        )
    } else {
        (
            package.join("worker").join(if cfg!(windows) {
                "duo-killer-worker.exe"
            } else {
                "duo-killer-worker"
            }),
            None,
        )
    };
    let web_root = web_root.unwrap_or_else(|| package.join("www"));
    if !worker.is_file() || (!development && !web_root.join("index.html").is_file()) {
        return Err(if development {
            "请先执行 pnpm worker:setup 准备 Python 开发环境"
        } else {
            "程序文件不完整，请运行项目中的 pnpm build:web 重新构建"
        }
        .into());
    }
    let root = match directory {
        Some(path) => path,
        None if development => data_directory()?.with_file_name("com.niulai.local-web-dev"),
        None => data_directory()?,
    };
    std::fs::create_dir_all(&root)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(root.join(".server.lock"))?;
    lock.try_lock_exclusive()
        .map_err(|_| "这个本地 Web 工作空间已在运行，请使用已打开的页面")?;
    let auth = Arc::new(auth::Auth::load(&root)?);
    let push = push::PushBridge::spawn(&worker, script.as_deref(), &root)?;
    // Check that bundled Python includes the push dependencies before exposing it.
    push.call("status", json!({})).await?;
    let engine = Engine::new(root)?;
    let listener = tokio::net::TcpListener::bind((listen_address, port))
        .await
        .map_err(|e| format!("无法启动本地服务，端口可能被占用：{e}"))?;
    // Keep the local launch authority separate from explicitly allowed origins,
    // including when Docker publishes the non-loopback listener.
    let authority = format!("127.0.0.1:{}", listener.local_addr()?.port());
    let trust_network = access::trust_network(&std::env::var("DUO_WEB_TRUST_NETWORK").unwrap_or_default())?;
    let allowed_origins = if trust_network { Vec::new() } else {
        access::allowed_origins(&std::env::var("DUO_WEB_ALLOWED_ORIGINS").unwrap_or_default())?
    };
    if allowed_origins.iter().any(|origin| origin.authority == authority) {
        return Err("DUO_WEB_ALLOWED_ORIGINS 无需重复配置本机启动地址".into());
    }
    let tunnel_disabled = access::tunnel_disabled(&std::env::var("DUO_WEB_DISABLE_TUNNEL").unwrap_or_default())?;
    for origin in &allowed_origins {
        println!("允许的访问入口：{}（使用访问密码登录）", origin.origin);
    }
    if tunnel_disabled { println!("Quick Tunnel 已停用。"); }
    if trust_network { println!("已信任上游网络：Host/Origin 检查已关闭，访问密码继续生效。"); }
    let secret = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let url = format!("http://{authority}/#access={secret}");
    let (sender, _) = broadcast::channel(256);
    let sink = sender.clone();
    let (shutdown, stopping) = watch::channel(false);
    let mut requested_shutdown = stopping.clone();
    let mut push_events = sender.subscribe();
    let push_worker = push.clone();
    let push_engine = engine.clone();
    let mut push_stop = stopping.clone();
    tokio::spawn(async move {
        let mut results = std::collections::HashMap::<String, Value>::new();
        // Recover the small gap between durable checkout result and push queueing.
        // Absolute confirmation deadlines and deduplication tombstones prevent replay.
        for task in push_engine.snapshot().tasks {
            if task.result.is_some() && task.action != "inventory_monitor" {
                let _ = push_worker.call("observe", json!({"task":task})).await;
            }
        }
        loop {
            let tasks = tokio::select! {
                _ = push_stop.changed() => break,
                event = push_events.recv() => match event {
                    Ok(task) => vec![task],
                    Err(broadcast::error::RecvError::Lagged(_)) => push_engine.snapshot().tasks,
                    Err(_) => break,
                }
            };
            for task in tasks {
                if let Some(result) = &task.result {
                    if results.get(&task.id) == Some(result) { continue; }
                    results.insert(task.id.clone(), result.clone());
                    if results.len() > 2000 { results.clear(); }
                    if push_worker.call("observe", json!({"task":task})).await.is_err() {
                        eprintln!("Web Push 事件尚未处理，请查看通知状态或重新启动服务。");
                    }
                }
            }
        }
    });
    let state = LocalState {
        engine: engine.clone(),
        host: TaskHost {
            worker,
            script,
            on_update: Arc::new(move |task| {
                let _ = sink.send(task.clone());
            }),
        },
        events: sender,
        stopping,
        shutdown: shutdown.clone(),
        authority,
        token: format!("Bearer {secret}"),
        dev_origin,
        auth,
        public_origin: Arc::new(Mutex::new(None)),
        allowed_origins: Arc::new(allowed_origins),
        tunnel_disabled,
        trust_network,
        push: push.clone(),
    };
    activity::start(state.host.clone(), engine.root.clone());
    if let Err(error) = engine.restore_monitor(state.host.clone()) { eprintln!("{error}"); }
    session_maintenance::start(engine.clone(), state.host.clone());
    let mut app = Router::new()
        .route("/api/auth/status", get(auth_status))
        .route("/api/auth/login", post(login))
        .route("/api/auth/logout", post(logout))
        .route("/api/auth/password", post(password))
        .route("/api/tunnel/register", post(tunnel))
        .route("/api/shutdown", post(request_shutdown))
        .route("/api/push/{operation}", post(push_command))
        .route("/api/events", get(events))
        .route("/api/{command}", post(command));
    if !development {
        app = app.fallback_service(ServeDir::new(web_root));
    }
    let app = app
        .layer(DefaultBodyLimit::max(1_048_576))
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state);
    println!(
        "DUO Killer · 本地 Web\n{url}\n本机链接可直接打开；其他允许的入口使用访问密码。关闭网页后服务仍运行；在此终端按 Ctrl+C 退出。"
    );
    if open && open_browser(&url).is_err() {
        eprintln!("无法自动打开默认浏览器，请打开上面的本机链接。");
    }
    let shutdown_engine = engine.clone();
    let result = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            // The dev launcher owns stdin. EOF also cleans up tasks if that parent crashes.
            let parent_closed = async {
                if development {
                    let (sender, receiver) = tokio::sync::oneshot::channel();
                    std::thread::spawn(move || {
                        let _ = std::io::stdin().read_line(&mut String::new());
                        let _ = sender.send(());
                    });
                    let _ = receiver.await;
                } else {
                    std::future::pending::<()>().await;
                }
            };
            tokio::select! { _ = shutdown_signal() => {}, _ = parent_closed => {}, _ = requested_shutdown.changed() => {} }
            let _ = shutdown.send(true);
            session_maintenance::stop(&shutdown_engine.root);
            shutdown_engine.shutdown().await;
            activity::stop(&shutdown_engine.root).await;
        })
        .await;
    if result.is_err() {
        session_maintenance::stop(&engine.root);
        engine.shutdown().await;
    }
    activity::stop(&engine.root).await;
    push.stop().await;
    drop(lock);
    result?;
    Ok(())
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("启动失败：{error}");
        std::process::exit(1);
    }
}
