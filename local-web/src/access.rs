use axum::http::Uri;

#[derive(Debug)]
pub struct AllowedOrigin {
    pub origin: String,
    pub authority: String,
    pub secure: bool,
}

// An explicit origin list keeps DNS rebinding and cross-site requests blocked
// when an operator publishes the Web port beyond loopback.
pub fn allowed_origins(value: &str) -> Result<Vec<AllowedOrigin>, String> {
    let mut origins: Vec<AllowedOrigin> = Vec::new();
    for value in value.split(',').map(str::trim).filter(|value| !value.is_empty()) {
        let invalid = || "DUO_WEB_ALLOWED_ORIGINS 只接受完整的 HTTP/HTTPS 来源，不允许通配符、用户信息、路径或重复主机".to_owned();
        let uri: Uri = value.parse().map_err(|_| invalid())?;
        let scheme = uri.scheme_str().ok_or_else(invalid)?;
        let authority = uri.authority().ok_or_else(invalid)?;
        if !matches!(scheme, "http" | "https")
            || !matches!(uri.path(), "" | "/")
            || uri.query().is_some()
            || value.contains(['@', '#', '*', '?'])
            || authority.host().is_empty()
            || authority.host() == "0.0.0.0"
            || authority.port_u16() == Some(0)
            || origins.iter().any(|origin| origin.authority == authority.as_str()) {
            return Err(invalid());
        }
        origins.push(AllowedOrigin {
            origin: format!("{scheme}://{authority}"),
            authority: authority.to_string(),
            secure: scheme == "https",
        });
    }
    Ok(origins)
}

pub fn tunnel_disabled(value: &str) -> Result<bool, String> {
    match value.trim() {
        "1" | "true" => Ok(true),
        "" | "0" | "false" => Ok(false),
        _ => Err("DUO_WEB_DISABLE_TUNNEL 需为 1、0、true 或 false".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_origins_keep_their_scheme_and_authority() {
        let origins = allowed_origins(" http://192.168.0.200:17654,https://pi.example.test/ ").unwrap();
        assert_eq!(origins[0].authority, "192.168.0.200:17654");
        assert!(!origins[0].secure);
        assert_eq!(origins[1].origin, "https://pi.example.test");
        assert!(origins[1].secure);
        assert!(allowed_origins("").unwrap().is_empty());
    }

    #[test]
    fn ambiguous_or_non_origin_configuration_is_rejected() {
        for value in ["*", "http://*.example.test", "http://user@pi.test", "http://pi.test/path",
            "http://pi.test?query", "http://pi.test#fragment", "http://0.0.0.0:17654",
            "http://pi.test:0", "ftp://pi.test", "pi.test:17654",
            "http://pi.test:17654,https://pi.test:17654"] {
            assert!(allowed_origins(value).is_err(), "{value}");
        }
        assert!(tunnel_disabled("1").unwrap());
        assert!(!tunnel_disabled("").unwrap());
        assert!(tunnel_disabled("maybe").is_err());
    }
}
