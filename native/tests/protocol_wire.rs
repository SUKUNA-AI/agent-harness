//! The actual executable, not a replacement/mock dispatch.
use serde_json::{json, Value};
use std::io::Write;
use std::process::{Command, Stdio};

fn invoke(args: &[&str], raw: &[u8]) -> std::process::Output {
    let mut child = Command::new(env!("CARGO_BIN_EXE_harness-native"))
        .args(args).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().unwrap();
    child.stdin.take().unwrap().write_all(raw).unwrap();
    child.wait_with_output().unwrap()
}
fn request() -> Value {
    json!({"protocol_version":2,"operation":"capabilities","operation_version":1,
        "request_id":"wire-1","budget":{"timeout_ms":100,"max_response_bytes":1024},"input":{}})
}

#[test]
fn executable_keeps_v1_info_and_explicitly_selects_v2() {
    let legacy = invoke(&[], br#"{"operation":"info"}"#);
    assert!(legacy.status.success()); assert!(legacy.stderr.is_empty());
    assert_eq!(serde_json::from_slice::<Value>(&legacy.stdout).unwrap(),
        json!({"schema_version":1,"protocol_version":1,"version":env!("CARGO_PKG_VERSION"),"operations":["snapshot","repomap"]}));
    let raw=serde_json::to_vec(&request()).unwrap();
    let wrong_mode=invoke(&[],&raw);
    assert!(!wrong_mode.status.success());
    assert_eq!(serde_json::from_slice::<Value>(&wrong_mode.stdout).unwrap()["error"],"unsupported native protocol version");
    let new=invoke(&["--protocol-version","2"],&raw);
    assert!(new.status.success()); assert!(new.stderr.is_empty());
    assert!(new.stdout.len() <= 1024); assert!(new.stdout.ends_with(b"\n"));
    let value:Value=serde_json::from_slice(&new.stdout).unwrap();
    assert_eq!(value["request_id"],"wire-1"); assert_eq!(value["ok"],true);
}

#[test]
fn executable_rejects_duplicate_unknown_and_oversize_without_echoing_input() {
    let mut v=request(); v["input"]=json!({"secret-marker":"not-an-operation-input"});
    for raw in [serde_json::to_vec(&v).unwrap(),
        br#"{"protocol_version":2,"protocol_version":2,"private":"secret-marker"}"#.to_vec(),
        vec![b' ';65_537]] {
        let out=invoke(&["--protocol-version","2"],&raw);
        assert!(!out.status.success()); assert!(out.stderr.is_empty());
        assert!(out.stdout.len() <= 4096);
        let value:Value=serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(value["ok"],false); assert!(value.get("output").is_none());
        assert!(!String::from_utf8(out.stdout).unwrap().contains("secret-marker"));
    }
}

#[test]
fn executable_rejects_unknown_cli_mode_before_reading_stdin() {
    let out=invoke(&["--protocol-version","3"],b"");
    assert_eq!(out.status.code(),Some(2)); assert!(out.stdout.is_empty());
}
