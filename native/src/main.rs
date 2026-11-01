//! One request, one bounded response. No daemon or persistent index.
mod repomap;
mod snapshot;

use serde_json::{json, Value};
use std::io::{self, Read, Write};

const MAX_REQUEST: u64 = 16 * 1024 * 1024;
const MAX_RESPONSE: usize = 100 * 1024 * 1024;

struct BoundedOutput(Vec<u8>);
impl Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_RESPONSE {
            return Err(io::Error::other("native response exceeds 100 MiB"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn request() -> Result<Value, String> {
    let mut raw = Vec::new();
    io::stdin()
        .take(MAX_REQUEST + 1)
        .read_to_end(&mut raw)
        .map_err(|e| e.to_string())?;
    if raw.len() as u64 > MAX_REQUEST {
        return Err("native request exceeds 16 MiB".into());
    }
    let value: Value =
        serde_json::from_slice(&raw).map_err(|e| format!("invalid native request: {e}"))?;
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
        Some("repomap") => repomap::run(&value),
        Some("info") => Ok(
            json!({"schema_version":1,"protocol_version":1,"version":env!("CARGO_PKG_VERSION"),"operations":["snapshot","repomap"]}),
        ),
        _ => Err("unsupported native operation".into()),
    }
}

fn main() {
    let (value, mut status) = match request() {
        Ok(value) => (value, 0),
        Err(error) => (json!({"schema_version":1,"error":error}), 1),
    };
    let mut output = BoundedOutput(Vec::new());
    if let Err(error) = serde_json::to_writer(&mut output, &value) {
        output.0 =
            serde_json::to_vec(&json!({"schema_version":1,"error":error.to_string()})).unwrap();
        status = 1;
    }
    output.0.push(b'\n');
    if io::stdout().write_all(&output.0).is_err() {
        status = 1;
    }
    std::process::exit(status);
}
