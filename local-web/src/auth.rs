use argon2::{password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString}, Argon2};
use axum::http::{header, HeaderMap};
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, path::Path, sync::Mutex, time::{SystemTime, UNIX_EPOCH}};

#[derive(Serialize, Deserialize)]
struct Saved { password_hash: String }
struct Inner {
    hash: String,
    sessions: HashMap<String, u64>,
    window: u64,
    failures: u32,
}
pub struct Auth { inner: Mutex<Inner> }
pub fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }
pub fn secret() -> String { format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple()) }
fn hash(password: &str) -> Result<String, String> {
    let salt = SaltString::encode_b64(uuid::Uuid::new_v4().as_bytes()).map_err(|_| "无法生成密码盐")?;
    Argon2::default().hash_password(password.as_bytes(), &salt).map(|x| x.to_string()).map_err(|_| "无法设置访问密码".into())
}
fn valid_password(password: &str) -> Result<(), String> {
    if password.len() < 8 || password.len() > 128 { return Err("访问密码需为 8 至 128 个字符".into()); }
    Ok(())
}
impl Auth {
    pub fn load(root: &Path) -> Result<Self, String> {
        let path = root.join("settings/web-auth.json");
        let override_password = std::env::var("DUO_WEB_PASSWORD").ok();
        let password_hash = if let Some(password) = override_password {
            valid_password(&password)?;
            let value = hash(&password)?;
            super::engine::private_json(&path, &Saved { password_hash: value.clone() })?;
            value
        } else if path.exists() {
            let saved: Saved = serde_json::from_slice(&std::fs::read(&path).map_err(|_| "无法读取 Web 访问设置")?).map_err(|_| "Web 访问设置损坏，未重置密码")?;
            PasswordHash::new(&saved.password_hash).map_err(|_| "Web 密码记录损坏，未重置密码")?;
            saved.password_hash
        } else {
            let password = secret()[..16].to_owned();
            let value = hash(&password)?;
            super::engine::private_json(&path, &Saved { password_hash: value.clone() })?;
            println!("首次 Web 访问密码：{password}\n请保存此密码，也可从本机页面修改。DUO_WEB_PASSWORD 可重设访问密码。");
            value
        };
        Ok(Self { inner: Mutex::new(Inner { hash: password_hash, sessions: HashMap::new(), window: now(), failures: 0 }) })
    }
    pub fn authenticated(&self, headers: &HeaderMap, local_bearer: &str) -> bool {
        if headers.get(header::AUTHORIZATION).and_then(|x| x.to_str().ok()) == Some(local_bearer) { return true; }
        let session = headers.get(header::COOKIE).and_then(|x| x.to_str().ok()).and_then(|cookies| cookies.split(';').find_map(|x| x.trim().strip_prefix("duo_session=")));
        let Some(session) = session else { return false };
        self.inner.lock().unwrap().sessions.get(session).is_some_and(|expires| *expires > now())
    }
    pub fn begin_login(&self) -> Result<String, String> {
        let mut inner = self.inner.lock().unwrap();
        if now().saturating_sub(inner.window) >= 60 { inner.failures = 0; inner.window = now(); }
        if inner.failures >= 10 { return Err("尝试次数过多，请一分钟后重试".into()); }
        // Reserve before the expensive hash verification to bound concurrent guesses.
        inner.failures += 1;
        Ok(inner.hash.clone())
    }
    pub fn verify(password: &str, encoded: &str) -> bool {
        PasswordHash::new(encoded).is_ok_and(|record| Argon2::default().verify_password(password.as_bytes(), &record).is_ok())
    }
    pub fn session(&self) -> String {
        let mut inner = self.inner.lock().unwrap();
        inner.sessions.retain(|_, expires| *expires > now());
        if inner.sessions.len() >= 100 { inner.sessions.clear(); }
        let session = secret();
        inner.sessions.insert(session.clone(), now()+604800);
        inner.failures = 0;
        session
    }
    pub fn logout(&self, headers: &HeaderMap) {
        let session = headers.get(header::COOKIE).and_then(|x| x.to_str().ok()).and_then(|cookies| cookies.split(';').find_map(|x| x.trim().strip_prefix("duo_session=")));
        if let Some(session) = session { self.inner.lock().unwrap().sessions.remove(session); }
    }
    pub fn change_password(&self, root: &Path, password: &str) -> Result<(), String> {
        valid_password(password)?;
        let value = hash(password)?;
        super::engine::private_json(&root.join("settings/web-auth.json"), &Saved { password_hash: value.clone() })?;
        let mut inner = self.inner.lock().unwrap();
        inner.hash = value;
        inner.sessions.clear();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn password_hash_sessions_and_rotation() {
        let dir = tempfile::tempdir().unwrap();
        let auth = Auth::load(dir.path()).unwrap();
        auth.change_password(dir.path(), "new-private-password").unwrap();
        let hash = auth.begin_login().unwrap();
        assert!(Auth::verify("new-private-password", &hash));
        assert!(!Auth::verify("wrong-password", &hash));
        let session = auth.session();
        let mut headers = HeaderMap::new();
        headers.insert(header::COOKIE, format!("duo_session={session}").parse().unwrap());
        assert!(auth.authenticated(&headers, "Bearer test"));
        auth.change_password(dir.path(), "changed-private-password").unwrap();
        assert!(!auth.authenticated(&headers, "Bearer test"));
        let disk = std::fs::read_to_string(dir.path().join("settings/web-auth.json")).unwrap();
        assert!(!disk.contains("changed-private-password"));
        assert!(Auth::verify("changed-private-password", &Auth::load(dir.path()).unwrap().begin_login().unwrap()));
    }
    #[test]
    fn failed_logins_are_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let auth = Auth::load(dir.path()).unwrap();
        for _ in 0..10 { assert!(auth.begin_login().is_ok()); }
        assert!(auth.begin_login().is_err());
    }
}
