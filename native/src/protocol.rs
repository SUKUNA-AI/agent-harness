//! Explicit protocol 2: closed envelopes and bounded, duplicate-free JSON.
//! Snapshot/digest protocol 1 is independent and is not reserialized through this API.
use serde_core::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde_json::{json, Map, Value};
use std::cell::Cell;
use std::fmt;
use std::io::{self, Write};

pub const MAX_REQUEST_BYTES: usize = 65_536;
pub const MAX_RESPONSE_BYTES: usize = 65_536;
pub const MIN_RESPONSE_BYTES: usize = 1024;
const MAX_DEPTH: usize = 32;
const MAX_NODES: usize = 8192;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ErrorCode {
    InvalidInput,
    UnsupportedCapability,
    CapacityExceeded,
}
impl ErrorCode {
    fn as_str(self) -> &'static str {
        match self {
            Self::InvalidInput => "invalid_input",
            Self::UnsupportedCapability => "unsupported_capability",
            Self::CapacityExceeded => "capacity_exceeded",
        }
    }
}

struct JsonSeed<'a> {
    depth: usize,
    nodes: &'a Cell<usize>,
    max_depth: usize,
    max_nodes: usize,
}
impl<'de> DeserializeSeed<'de> for JsonSeed<'_> {
    type Value = Value;
    fn deserialize<D: de::Deserializer<'de>>(self, deserializer: D) -> Result<Value, D::Error> {
        if self.depth > self.max_depth || self.nodes.get() >= self.max_nodes {
            return Err(de::Error::custom("JSON structural limit"));
        }
        self.nodes.set(self.nodes.get() + 1);
        deserializer.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for JsonSeed<'_> {
    type Value = Value;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { f.write_str("bounded JSON") }
    fn visit_bool<E: de::Error>(self, v: bool) -> Result<Value, E> { Ok(Value::Bool(v)) }
    fn visit_unit<E: de::Error>(self) -> Result<Value, E> { Ok(Value::Null) }
    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Value, E> { Ok(v.into()) }
    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Value, E> { Ok(v.into()) }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Value, E> {
        serde_json::Number::from_f64(v).map(Value::Number).ok_or_else(|| E::custom("non-finite number"))
    }
    fn visit_str<E: de::Error>(self, v: &str) -> Result<Value, E> { Ok(Value::String(v.to_owned())) }
    fn visit_string<E: de::Error>(self, v: String) -> Result<Value, E> { Ok(Value::String(v)) }
    fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Value, A::Error> {
        let mut values = Vec::new();
        while let Some(value) = a.next_element_seed(JsonSeed {depth:self.depth + 1, nodes:self.nodes,max_depth:self.max_depth,max_nodes:self.max_nodes})? {
            values.push(value);
        }
        Ok(Value::Array(values))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Value, A::Error> {
        let mut values = Map::new();
        while let Some(key) = a.next_key::<String>()? {
            if values.contains_key(&key) { return Err(de::Error::custom("duplicate JSON key")); }
            let value = a.next_value_seed(JsonSeed {depth:self.depth + 1, nodes:self.nodes,max_depth:self.max_depth,max_nodes:self.max_nodes})?;
            values.insert(key, value);
        }
        Ok(Value::Object(values))
    }
}

pub fn parse_strict(raw: &[u8]) -> Result<Value, ErrorCode> {
    parse_bounded(raw,MAX_REQUEST_BYTES,MAX_DEPTH,MAX_NODES)
}
pub(crate) fn parse_bounded(raw: &[u8],max_bytes:usize,max_depth:usize,max_nodes:usize) -> Result<Value, ErrorCode> {
    if raw.len() > max_bytes { return Err(ErrorCode::CapacityExceeded); }
    let nodes = Cell::new(0);
    let mut decoder = serde_json::Deserializer::from_slice(raw);
    let value = JsonSeed {depth:0, nodes:&nodes,max_depth,max_nodes}.deserialize(&mut decoder)
        .map_err(|_| ErrorCode::InvalidInput)?;
    decoder.end().map_err(|_| ErrorCode::InvalidInput)?;
    Ok(value)
}

#[derive(Debug)]
struct Request {
    operation: String,
    operation_version: u64,
    request_id: String,
    timeout_ms: u64,
    max_response_bytes: usize,
    input: Map<String, Value>,
}
fn exact<'a>(v: &'a Value, keys: &[&str]) -> Result<&'a Map<String, Value>, ErrorCode> {
    let map = v.as_object().ok_or(ErrorCode::InvalidInput)?;
    if map.len() != keys.len() || !keys.iter().all(|key| map.contains_key(*key)) {
        return Err(ErrorCode::InvalidInput);
    }
    Ok(map)
}
impl Request {
    fn decode(value: &Value) -> Result<Self, ErrorCode> {
        let map = exact(value, &["protocol_version","operation","operation_version","request_id","budget","input"])?;
        if map["protocol_version"].as_u64() != Some(2) { return Err(ErrorCode::UnsupportedCapability); }
        let operation = map["operation"].as_str().filter(|v| !v.is_empty() && v.len() <= 64)
            .ok_or(ErrorCode::InvalidInput)?.to_owned();
        let operation_version = map["operation_version"].as_u64().filter(|v| *v > 0)
            .ok_or(ErrorCode::InvalidInput)?;
        let request_id = map["request_id"].as_str().filter(|v| !v.is_empty() && v.len() <= 128
            && v.bytes().all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c)))
            .ok_or(ErrorCode::InvalidInput)?.to_owned();
        let budget = exact(&map["budget"], &["timeout_ms", "max_response_bytes"])?;
        let timeout_ms = budget["timeout_ms"].as_u64().filter(|v| (1..=120_000).contains(v))
            .ok_or(ErrorCode::InvalidInput)?;
        let max_response_bytes = budget["max_response_bytes"].as_u64()
            .filter(|v| (MIN_RESPONSE_BYTES as u64..=MAX_RESPONSE_BYTES as u64).contains(v))
            .ok_or(ErrorCode::InvalidInput)? as usize;
        let input = map["input"].as_object().ok_or(ErrorCode::InvalidInput)?.clone();
        Ok(Self {operation, operation_version, request_id, timeout_ms, max_response_bytes, input})
    }
}

fn backend() -> Value {
    json!({"name":"harness-native", "package_version":env!("CARGO_PKG_VERSION"),
        "os":std::env::consts::OS,"arch":std::env::consts::ARCH})
}
fn error(code: ErrorCode, request: Option<&Request>) -> Value {
    json!({"protocol_version":2,"operation":request.map(|r| &r.operation),
        "operation_version":request.map(|r|r.operation_version),"request_id":request.map(|r|&r.request_id),
        "backend":backend(),"ok":false,"error":{"code":code.as_str(),"message":match code {
            ErrorCode::InvalidInput=>"Invalid native request",
            ErrorCode::UnsupportedCapability=>"Unsupported native operation or version",
            ErrorCode::CapacityExceeded=>"Native byte budget exceeded",
        }}})
}

/// Returns a bounded response including its final LF, and a process exit status.
pub fn dispatch(raw: &[u8]) -> (Vec<u8>, i32) {
    let request = match parse_strict(raw).and_then(|value| Request::decode(&value)) {
        Ok(request) => request,
        Err(code) => return (encode(&error(code,None),4096).unwrap_or_else(|_| b"{}\n".to_vec()),1),
    };
    // Capabilities has no IO or user-directed work. Other operations define their own deadlines.
    let _timeout_ms = request.timeout_ms;
    let result = if request.operation != "capabilities" || request.operation_version != 1 {
        Err(ErrorCode::UnsupportedCapability)
    } else if !request.input.is_empty() {
        Err(ErrorCode::InvalidInput)
    } else {
        Ok(json!({"protocol_versions":[1,2],"operations":[
            {"operation":"snapshot","operation_version":1,"protocol_version":1},
            {"operation":"repomap","operation_version":1,"protocol_version":1},
            {"operation":"capabilities","operation_version":1,"protocol_version":2}],
            "stream_modes":if cfg!(any(target_os="linux", windows)) {vec![crate::lsp_session::capability()]} else {vec![]},
            "limits":{"max_request_bytes":MAX_REQUEST_BYTES,"max_response_bytes":MAX_RESPONSE_BYTES,
                "min_response_bytes":MIN_RESPONSE_BYTES,"max_depth":MAX_DEPTH,"max_nodes":MAX_NODES}}))
    };
    let (value,status) = match result {
        Ok(output) => (json!({"protocol_version":2,"operation":request.operation,
            "operation_version":request.operation_version,"request_id":request.request_id,
            "backend":backend(),"ok":true,"output":output}),0),
        Err(code) => (error(code,Some(&request)),1),
    };
    match encode(&value,request.max_response_bytes) {
        Ok(bytes) => (bytes,status),
        Err(_) => (encode(&error(ErrorCode::CapacityExceeded,Some(&request)),request.max_response_bytes)
            .unwrap_or_else(|_| b"{}\n".to_vec()),1),
    }
}

struct BoundedOutput { bytes: Vec<u8>, cap: usize }
impl Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.bytes.len().saturating_add(bytes.len()) > self.cap {
            return Err(io::Error::other("native response byte limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}
pub fn encode(value: &Value, cap: usize) -> io::Result<Vec<u8>> {
    let mut output = BoundedOutput {bytes:Vec::new(),cap};
    serde_json::to_writer(&mut output,value)?;
    output.write_all(b"\n")?;
    Ok(output.bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request() -> Value { json!({"protocol_version":2,"operation":"capabilities",
        "operation_version":1,"request_id":"r-1","budget":{"timeout_ms":1,"max_response_bytes":1024},"input":{}}) }
    fn response(v: &Value) -> (Value,i32) {
        let (raw,status) = dispatch(&serde_json::to_vec(v).unwrap());
        assert!(raw.ends_with(b"\n"));
        (serde_json::from_slice(&raw).unwrap(),status)
    }
    #[test]
    fn capabilities_are_explicit_and_bounded() {
        let v=request(); let (out,status)=response(&v); assert_eq!(status,0);
        for key in ["protocol_version","operation","operation_version","request_id"] { assert_eq!(out[key],v[key]); }
        assert_eq!(out["backend"],backend());
        assert!(out["output"]["operations"].as_array().unwrap().iter().all(|o| o["operation"] != "lsp-session"));
        assert!(dispatch(&serde_json::to_vec(&v).unwrap()).0.len() <= 1024);
    }
    #[test]
    fn duplicate_decoded_keys_at_every_level_are_rejected() {
        for raw in [br#"{"a":1,"a":2}"#.as_slice(), br#"{"a":{"x":1,"\u0078":2}}"#,
            br#"[{"a":0,"a":0}]"#] { assert_eq!(parse_strict(raw),Err(ErrorCode::InvalidInput)); }
    }
    #[test]
    fn malformed_nonfinite_trailing_and_structural_excess_are_rejected() {
        for raw in [b"NaN".as_slice(),b"Infinity",b"1e999",b"{} {}",b"{",b"\xff",br#""\ud800""#] {
            assert!(parse_strict(raw).is_err());
        }
        assert!(parse_strict(&vec![b' ';MAX_REQUEST_BYTES + 1]).is_err());
        let deep = format!("{}0{}","[".repeat(MAX_DEPTH+1),"]".repeat(MAX_DEPTH+1));
        assert!(parse_strict(deep.as_bytes()).is_err());
        let nodes = format!("[{}]",vec!["0";MAX_NODES].join(","));
        assert!(parse_strict(nodes.as_bytes()).is_err());
    }
    #[test]
    fn closed_envelopes_types_and_versions_are_not_coerced() {
        for key in ["protocol_version","operation","operation_version","request_id","budget","input"] {
            let mut v=request();v.as_object_mut().unwrap().remove(key);assert_eq!(response(&v).1,1);
        }
        let mut v=request();v["extra"]=json!(true);assert_eq!(response(&v).1,1);
        for bad in [json!(null),json!(true),json!(false),json!(0),json!("1"),json!(1.0)] {
            for key in ["timeout_ms","max_response_bytes"] {
                let mut v=request();v["budget"][key]=bad.clone();assert_eq!(response(&v).1,1);
            }
            let mut v=request();v["operation_version"]=bad.clone();assert_eq!(response(&v).1,1);
        }
        for path in ["input","budget"] {
            let mut v=request();v[path]["extra"]=json!(1);assert_eq!(response(&v).1,1);
        }
        for (key,bad) in [("protocol_version",json!(3)),("operation_version",json!(2)),("operation",json!("fs_read"))] {
            let mut v=request();v[key]=bad;let(out,status)=response(&v);assert_eq!(status,1);
            assert_eq!(out["error"]["code"],"unsupported_capability");
        }
    }
    #[test]
    fn serialized_budget_includes_escaping_and_final_lf() {
        let value=json!({"v":"\n雪"});let bytes=serde_json::to_vec(&value).unwrap();
        assert!(encode(&value,bytes.len()).is_err());
        assert_eq!(encode(&value,bytes.len()+1).unwrap().len(),bytes.len()+1);
    }
}
