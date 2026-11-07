//! Bounded native mechanisms. Python remains the owner of policy and durable state.
pub mod protocol;
pub mod fs_read;
pub mod lsp_session;
pub mod repomap;
pub mod snapshot;

use serde_json::{json, Value};

/// Original protocol-1 dispatch. Its input and result shapes are intentionally unchanged.
pub fn dispatch_legacy(value: &Value) -> Result<Value, String> {
    if value
        .get("protocol_version")
        .is_some_and(|v| v.as_u64() != Some(1))
    {
        return Err("unsupported native protocol version".into());
    }
    match value.get("operation").and_then(Value::as_str) {
        Some("snapshot") => {
            snapshot::snapshot(value.get("contract").ok_or("snapshot requires contract")?)
        }
        Some("repomap") => repomap::run(value),
        Some("info") => Ok(json!({"schema_version":1,"protocol_version":1,
            "version":env!("CARGO_PKG_VERSION"),"operations":["snapshot","repomap"]})),
        _ => Err("unsupported native operation".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_info_keeps_its_exact_shape() {
        assert_eq!(dispatch_legacy(&json!({"operation":"info"})).unwrap(),
            json!({"schema_version":1,"protocol_version":1,"version":"1.5.1",
                "operations":["snapshot","repomap"]}));
        assert_eq!(dispatch_legacy(&json!({"operation":"info","protocol_version":1})).unwrap(),
            dispatch_legacy(&json!({"operation":"info"})).unwrap());
        for version in [json!(null), json!(true), json!(1.0), json!(2), json!("1")] {
            assert!(dispatch_legacy(&json!({"operation":"info","protocol_version":version})).is_err());
        }
    }
}
