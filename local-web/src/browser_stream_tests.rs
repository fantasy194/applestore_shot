use super::*;
use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::{TcpListener, TcpStream}};

async fn upgrade(authority: &str, cookie: &str, origin: &str, account: &str, task: &str) -> (TcpStream, String) {
    let mut stream = TcpStream::connect(authority).await.unwrap();
    stream.write_all(format!("GET /api/browser-stream?account_id={account}&task_id={task} HTTP/1.1\r\nHost: {authority}\r\nOrigin: {origin}\r\nCookie: {cookie}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n").as_bytes()).await.unwrap();
    let mut header = Vec::new();
    while !header.ends_with(b"\r\n\r\n") && header.len() < 16384 {
        header.push(stream.read_u8().await.unwrap());
    }
    (stream, String::from_utf8(header).unwrap())
}

async fn frame(stream: &mut TcpStream) -> (u8, Value) {
    let opcode = stream.read_u8().await.unwrap() & 15;
    let mut length = (stream.read_u8().await.unwrap() & 127) as usize;
    if length == 126 { length = stream.read_u16().await.unwrap() as usize; }
    if length == 127 { length = stream.read_u64().await.unwrap() as usize; }
    assert!(length < 4096);
    let mut payload = vec![0; length];
    stream.read_exact(&mut payload).await.unwrap();
    (opcode, serde_json::from_slice(&payload).unwrap_or(Value::Null))
}

#[tokio::test]
async fn authenticated_stream_cookie_origin_ownership_and_cancel() {
    let temporary = tempfile::tempdir().unwrap();
    let (engine, account, task) = browser_remote::tests::task(temporary.path()).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let authority = listener.local_addr().unwrap().to_string();
    let broker = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let broker_port = broker.local_addr().unwrap().port();
    engine::private_json(&temporary.path().join("accounts").join(&account).join("tasks").join(&task).join("browser-remote.json"),
        &json!({"version":1,"port":broker_port,"token":"a".repeat(64),"account_id":account,"task_id":task})).unwrap();
    let broker_task = tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = broker.accept().await else { break; };
            tokio::spawn(async move {
                let mut request = [0; 4096]; let _ = stream.read(&mut request).await;
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                let body = json!({"active":true,"connected":true,"frame_current":true,"frame_updated":false,"revision":1,"document_id":"a".repeat(32),"frame_age_ms":60000,"frame":null,"url_host":"apple.com"}).to_string();
                let response = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                let _ = stream.write_all(response.as_bytes()).await;
            });
        }
    });
    let project = PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
    let worker = project.join(if cfg!(windows) { ".venv/Scripts/python.exe" } else { ".venv/bin/python" });
    let push = push::PushBridge::spawn(&worker, Some(&temporary.path().join("browser-task-fixture.py")), temporary.path()).unwrap();
    let auth = Arc::new(auth::Auth::load(temporary.path()).unwrap());
    auth.change_password(temporary.path(), "fixture-access-password").unwrap();
    let (shutdown, stopping) = watch::channel(false);
    let (events, _) = broadcast::channel(8);
    let state = LocalState { engine:engine.clone(), host:TaskHost {worker, script:None,on_update:Arc::new(|_|{})}, events,
        stopping,shutdown,authority:authority.clone(),token:"Bearer fixture-local-bearer".into(),dev_origin:None,auth:auth.clone(),
        public_origin:Arc::new(Mutex::new(None)),allowed_origins:Arc::new(Vec::new()),tunnel_disabled:true,trust_network:false,push:push.clone() };
    let app = Router::new().route("/api/browser-stream",get(browser_stream)).route("/api/browser-stream-session",post(browser_stream_session))
        .route("/api/auth/login",post(login)).layer(middleware::from_fn_with_state(state.clone(),guard)).with_state(state);
    let server = tokio::spawn(async move { axum::serve(listener,app).await.unwrap(); });
    let client = reqwest::Client::new();
    let base = format!("http://{authority}");
    assert_eq!(client.post(format!("{base}/api/browser-stream-session")).send().await.unwrap().status(),StatusCode::UNAUTHORIZED);
    let response = client.post(format!("{base}/api/browser-stream-session")).header(header::AUTHORIZATION,"Bearer fixture-local-bearer").send().await.unwrap();
    assert!(response.status().is_success());
    let set_cookie = response.headers()[header::SET_COOKIE].to_str().unwrap().to_owned();
    assert!(set_cookie.contains("HttpOnly; SameSite=Strict"));
    assert!(!set_cookie.contains("Secure"));
    let cookie = set_cookie.split(';').next().unwrap();
    let (_, denied) = upgrade(&authority,cookie,"https://foreign.example",&account,&task).await;
    assert!(denied.starts_with("HTTP/1.1 403"));
    let (_, denied) = upgrade(&authority,"",&base,&account,&task).await;
    assert!(denied.starts_with("HTTP/1.1 401"));
    let (_, denied) = upgrade(&authority,cookie,&base,&uuid::Uuid::new_v4().to_string(),&task).await;
    assert!(denied.starts_with("HTTP/1.1 404"));
    let (mut stream, accepted) = upgrade(&authority,cookie,&base,&account,&task).await;
    assert!(accepted.starts_with("HTTP/1.1 101"));
    let (_, status) = frame(&mut stream).await;
    assert_eq!(status["connected"],true);
    assert_eq!(status["frame_age_ms"],60000);
    // Password login produces the same stream authentication semantics.
    let response = client.post(format!("{base}/api/auth/login")).header(header::CONTENT_TYPE,"application/json")
        .body(json!({"password":"fixture-access-password"}).to_string()).send().await.unwrap();
    assert!(response.status().is_success());
    let password_cookie = response.headers()[header::SET_COOKIE].to_str().unwrap().split(';').next().unwrap();
    let (mut password_stream, accepted) = upgrade(&authority,password_cookie,&base,&account,&task).await;
    assert!(accepted.starts_with("HTTP/1.1 101"));
    assert_eq!(frame(&mut password_stream).await.1["connected"],true);
    // Revoking one password session closes its stream without affecting another.
    let mut revoked_headers = HeaderMap::new();
    revoked_headers.insert(header::COOKIE, password_cookie.parse().unwrap());
    auth.logout(&revoked_headers);
    let closed = tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop { if frame(&mut password_stream).await.0 == 8 { break; } }
    }).await;
    assert!(closed.is_ok());
    engine.cancel(&account,&task).unwrap();
    let ended = tokio::time::timeout(std::time::Duration::from_secs(4),async {
        loop { let (opcode,status) = frame(&mut stream).await; if opcode == 8 || status["active"] == false { return status; } }
    }).await.unwrap();
    assert_eq!(ended["active"],false);
    assert_eq!(ended["frame_updated"],true);
    assert_eq!(ended["revision"],0);
    drop(stream); drop(password_stream);
    server.abort(); broker_task.abort(); push.stop().await;
}

#[test]
fn csp_explicit_socket_origins_are_bounded_and_safari_compatible() {
    for (host, secure, expected) in [("127.0.0.1:1421",false,"ws://127.0.0.1:1421"),
        ("192.168.1.10:8080",false,"ws://192.168.1.10:8080"),
        ("fixture.trycloudflare.com",true,"wss://fixture.trycloudflare.com"),
        ("[::1]:1421",false,"ws://[::1]:1421")] {
        let header = content_security_policy(host,secure);
        assert!(header.to_str().unwrap().contains(&format!("connect-src 'self' {expected};")));
    }
    for hostile in ["bad.example; connect-src *", "user@bad.example", "bad.example/path", "bad.example\r\nX-Test: secret"] {
        assert!(content_security_policy(hostile,true).to_str().unwrap().contains("connect-src 'self';"));
    }
}
