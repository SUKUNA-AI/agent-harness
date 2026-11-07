//! One bounded request/response; no daemon, persistent index, or implicit version negotiation.
use harness_native::{dispatch_legacy, protocol};
use serde_json::{json, Value};
use std::io::{self, Read, Write};

const LEGACY_MAX_REQUEST: usize = 16 * 1024 * 1024;
const LEGACY_MAX_RESPONSE: usize = 100 * 1024 * 1024;

fn legacy(raw: &[u8]) -> (Vec<u8>, i32) {
    let value = if raw.len() > LEGACY_MAX_REQUEST {
        Err("native request exceeds 16 MiB".into())
    } else {
        serde_json::from_slice::<Value>(raw)
            .map_err(|e| format!("invalid native request: {e}"))
            .and_then(|value| dispatch_legacy(&value))
    };
    let (value,status) = match value {
        Ok(value) => (value,0),
        Err(error) => (json!({"schema_version":1,"error":error}),1),
    };
    // Preserve protocol-1 payload capacity; protocol 2 additionally counts framing.
    match protocol::encode(&value,LEGACY_MAX_RESPONSE + 1) {
        Ok(bytes) => (bytes,status),
        Err(_) => (b"{\"schema_version\":1,\"error\":\"native response exceeds 100 MiB\"}\n".to_vec(),1),
    }
}
fn main() {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len()==2 && args[0]=="--lsp-session" {
        match args[1].to_str() {
            Some(session)=>harness_native::lsp_session::run_cli(session),
            None=>std::process::exit(2),
        }
    }
    let v2 = args == ["--protocol-version", "2"];
    if !args.is_empty() && !v2 {
        let _ = io::stderr().write_all(b"usage: harness-native [--protocol-version 2]\n");
        std::process::exit(2);
    }
    let cap = if v2 {protocol::MAX_REQUEST_BYTES} else {LEGACY_MAX_REQUEST};
    let mut raw = Vec::new();
    let (bytes,status) = match io::stdin().take((cap + 1) as u64).read_to_end(&mut raw) {
        Ok(_) => if v2 {protocol::dispatch(&raw)} else {legacy(&raw)},
        Err(_) => if v2 {
            // The strict parser produces a bounded error without reflecting input bytes.
            protocol::dispatch(b"")
        } else {
            (b"{\"schema_version\":1,\"error\":\"native input read failed\"}\n".to_vec(),1)
        },
    };
    let status = if io::stdout().write_all(&bytes).is_err() {1} else {status};
    std::process::exit(status);
}
